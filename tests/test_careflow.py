import csv
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from careflow import (
    Assessment,
    Bundle,
    Disposition,
    Evidence,
    EvidenceState,
    Finding,
    ProgressRecord,
    Protocol,
    Risk,
    StepState,
    TeamWindow,
    assess,
    build_snapshot,
    compare_plans,
    diff_assessments,
    load_bundle,
    load_snapshot,
    save_snapshot,
    verify_snapshot,
)
from careflow.handover import diff_snapshots
from careflow.planning import plan_support

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
EXAMPLES = ROOT / "examples" / "weekly"

AS_OF = date(2026, 5, 8)


def ev_date(text):
    return Evidence(EvidenceState.PRESENT, text, date.fromisoformat(text))


def ev_text(text):
    return Evidence(EvidenceState.PRESENT, text, text)


def record(object_id, step, event, deadline, status, *, site="A", inspection="I1",
           elapsed=0, receipt=None, material=None, treatment_type=None, batch=None,
           depends_on=(), **extra):
    return ProgressRecord(
        object_id=object_id,
        site=site,
        step=step,
        event=ev_date(event) if event else Evidence(EvidenceState.MISSING),
        deadline=ev_date(deadline) if deadline else Evidence(EvidenceState.MISSING),
        inspection=ev_text(inspection) if inspection else Evidence(EvidenceState.MISSING),
        elapsed=Evidence(EvidenceState.ZERO, "0", 0) if elapsed == 0 else Evidence(EvidenceState.PRESENT, str(elapsed), elapsed),
        status=ev_text(status),
        receipt=receipt,
        material=material,
        treatment_type=treatment_type,
        batch=batch,
        depends_on=tuple(depends_on),
        extra=extra,
    )


def run(records, findings=(), transfers=(), protocol=None):
    return assess(records, findings, protocol or Protocol(), AS_OF, transfers)


class CareChainTests(unittest.TestCase):
    def test_handover_does_not_duplicate_object(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done", site="A"),
            record("O", "treat", "2026-05-03", "2026-05-08", "in_progress", site="A"),
            record("O", "treat", "2026-05-06", "2026-05-08", "in_progress", site="B"),
        ]
        assessment = run(records)
        self.assertEqual(len(assessment.objects), 1)
        obj = assessment.by_id("O")
        self.assertEqual(obj.sites, ("A", "B"))
        self.assertEqual([s.step for s in obj.steps].count("treat"), 1)
        merged = obj.step("treat")
        self.assertIn("merged_handover", [r.code for r in merged.reasons])
        # 合并提示是解释性的，不应被当成两件待交付对象。
        self.assertEqual(assessment.overall().objects, 1)

    def test_precedence_violation_is_flagged(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "in_progress"),
            record("O", "treat", "2026-05-03", "2026-05-08", "done"),
        ]
        obj = run(records).by_id("O")
        self.assertTrue(any(r.code == "precedence_violation" for r in obj.step("treat").reasons))
        self.assertFalse(obj.treatment_complete is False and obj.step("treat").state is StepState.DONE)

    def test_site_order_uses_care_chain_not_row_order(self):
        records = [
            record("O", "recheck", "2026-05-10", "2026-05-12", "scheduled"),
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            record("O", "treat", "2026-05-04", "2026-05-08", "done"),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.step("recheck").dependencies, ("treat",))


class FindingTests(unittest.TestCase):
    def test_unconfirmed_finding_does_not_change_deadline(self):
        records = [
            record("O", "treat", "2026-05-03", "2026-05-12", "in_progress"),
        ]
        finding = Finding(
            inspection="I1", object_id="O", step="treat", effect="shorten",
            limit_days=4, confirmed=Evidence(EvidenceState.PRESENT, "no", False),
            finding_date=ev_date("2026-05-03"),
        )
        step = run(records, [finding]).by_id("O").step("treat")
        self.assertEqual(step.effective_deadline, date(2026, 5, 12))
        self.assertIn("finding_not_confirmed", [r.code for r in step.reasons])

    def test_finding_without_confirmation_record_is_flagged(self):
        records = [record("O", "treat", "2026-05-03", "2026-05-12", "in_progress")]
        finding = Finding(
            inspection="I1", object_id="O", step="treat", effect="shorten",
            limit_days=4, confirmed=None, finding_date=ev_date("2026-05-03"),
        )
        step = run(records, [finding]).by_id("O").step("treat")
        self.assertEqual(step.effective_deadline, date(2026, 5, 7))
        self.assertIn("finding_confirmation_unknown", [r.code for r in step.reasons])

    def test_finding_shortens_deadline_on_shared_inspection(self):
        records = [
            record("A1", "inspect", "2026-05-01", "2026-05-06", "done", inspection="I9"),
            record("A1", "treat", "2026-05-02", "2026-05-12", "in_progress", inspection="I9"),
            record("B1", "inspect", "2026-05-01", "2026-05-06", "done", inspection="I9"),
            record("B1", "treat", "2026-05-02", "2026-05-12", "in_progress", inspection="I9"),
        ]
        finding = Finding(
            inspection="I9", object_id=None, step="treat", effect="shorten",
            limit_days=4, finding_date=ev_date("2026-05-03"),
        )
        assessment = run(records, [finding])
        for object_id in ("A1", "B1"):
            step = assessment.by_id(object_id).step("treat")
            self.assertEqual(step.effective_deadline, date(2026, 5, 7))
            self.assertIn("deadline_shortened_by_finding", [r.code for r in step.reasons])

    def test_finding_adds_recheck_without_recheck_step(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-05", "done"),
            record("O", "treat", "2026-05-03", "2026-05-07", "done"),
        ]
        finding = Finding(
            inspection="I1", object_id="O", step="treat",
            effect="add_recheck", recheck=True, finding_date=ev_date("2026-05-03"),
        )
        obj = run(records, [finding]).by_id("O")
        synthetic = obj.step("recheck")
        self.assertIsNotNone(synthetic)
        self.assertEqual(synthetic.source, "finding")
        self.assertEqual(synthetic.state, StepState.NOT_STARTED)
        self.assertTrue(obj.treatment_complete)
        self.assertFalse(obj.full_delivery)
        self.assertIn("recheck", obj.delivery_gap)

    def test_reset_requires_actual_treatment_completion(self):
        not_done = [
            record("O", "inspect", "2026-05-01", "2026-05-05", "done"),
            record("O", "treat", "2026-05-03", "2026-05-07", "in_progress"),
        ]
        finding = Finding(
            inspection="I1", object_id="O", step="treat", effect="reset", days=4,
            finding_date=ev_date("2026-05-03"),
        )
        step = run(not_done, [finding]).by_id("O").step("treat")
        self.assertEqual(step.effective_deadline, date(2026, 5, 7))
        self.assertIn("reset_requires_actual_step", [r.code for r in step.reasons])

        done = [
            record("O", "inspect", "2026-05-01", "2026-05-05", "done"),
            record("O", "treat", "2026-05-06", "2026-05-07", "done"),
        ]
        step = run(done, [finding]).by_id("O").step("treat")
        self.assertEqual(step.effective_deadline, date(2026, 5, 10))
        self.assertIn("deadline_reset_after_actual_step", [r.code for r in step.reasons])


class WindowTests(unittest.TestCase):
    def test_quiet_period_and_upper_limit(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            record("O", "treat", "2026-05-04", "2026-05-08", "done"),
            record("O", "recheck", "2026-05-09", "2026-05-10", "done"),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.recheck.earliest, date(2026, 5, 8))
        self.assertEqual(obj.recheck.latest, date(2026, 5, 10))
        self.assertEqual(obj.recheck.state, "satisfied")
        self.assertTrue(obj.full_delivery)

    def test_recheck_before_quiet_period_is_not_compliant(self):
        records = [
            record("O", "treat", "2026-05-04", "2026-05-08", "done"),
            record("O", "recheck", "2026-05-06", "2026-05-10", "done"),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.recheck.state, "too_early")
        self.assertFalse(obj.recheck.satisfied)
        self.assertIn("recheck", obj.delivery_gap)

    def test_recheck_after_upper_limit_is_confirmed_missed(self):
        records = [
            record("O", "treat", "2026-05-01", "2026-05-03", "done"),
            record("O", "recheck", None, "2026-05-09", "not_started"),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.recheck.state, "missed")
        self.assertTrue(obj.confirmed_missed)
        self.assertIs(obj.risk, Risk.CONFIRMED_MISSED)

    def test_missing_recheck_status_is_evidence_gap_not_missed(self):
        # 窗口虽然已过，但复检进度缺报：无法断言“已确定错过”，只能要求馆方确认。
        records = [
            record("O", "treat", "2026-05-01", "2026-05-03", "done"),
            ProgressRecord(
                object_id="O", site="A", step="recheck",
                event=Evidence(EvidenceState.MISSING), deadline=ev_date("2026-05-05"),
                inspection=ev_text("I1"), elapsed=Evidence(EvidenceState.MISSING),
                status=Evidence(EvidenceState.MISSING),
            ),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.recheck.state, "missed")
        self.assertIs(obj.risk, Risk.EVIDENCE_GAP)
        self.assertFalse(obj.confirmed_missed)
        self.assertIn("recheck", obj.evidence_gaps)

    def test_treatment_completion_and_full_delivery_are_separate(self):
        records = [
            record("O", "treat", "2026-05-04", "2026-05-08", "done"),
            record("O", "recheck", "2026-05-15", "2026-05-16", "scheduled"),
        ]
        obj = run(records).by_id("O")
        self.assertTrue(obj.treatment_complete)
        self.assertFalse(obj.full_delivery)
        self.assertEqual(obj.delivery_gap, ("recheck",))

    def test_stale_recheck_deadline_is_not_treated_as_compliance(self):
        # 工序行上的复检截止早于静置下限，说明阶段时限应随实际处置完成重置。
        records = [
            record("O", "treat", "2026-05-06", "2026-05-10", "done"),
            record("O", "recheck", "2026-05-12", "2026-05-09", "scheduled"),
        ]
        step = run(records).by_id("O").step("recheck")
        self.assertEqual(step.effective_deadline, date(2026, 5, 12))
        self.assertIn("stale_recheck_deadline_ignored", [r.code for r in
                                                         run(records).by_id("O").recheck.reasons])


class EvidenceTests(unittest.TestCase):
    def test_missing_status_is_evidence_gap_not_overdue(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            ProgressRecord(
                object_id="O", site="A", step="treat",
                event=ev_date("2026-05-04"), deadline=ev_date("2026-05-06"),
                inspection=ev_text("I1"), elapsed=Evidence(EvidenceState.MISSING),
                status=Evidence(EvidenceState.MISSING),
            ),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.step("treat").state, StepState.MISSING_EVIDENCE)
        self.assertIs(obj.risk, Risk.EVIDENCE_GAP)
        self.assertFalse(obj.confirmed_missed)
        self.assertFalse(obj.step("treat").overdue)

    def test_unknown_and_zero_are_distinct(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            ProgressRecord(
                object_id="O", site="A", step="treat",
                event=ev_date("2026-05-04"), deadline=ev_date("2026-05-06"),
                inspection=ev_text("I1"), elapsed=Evidence(EvidenceState.ZERO, "0", 0),
                status=Evidence(EvidenceState.UNKNOWN, "unknown"),
            ),
        ]
        obj = run(records).by_id("O")
        self.assertEqual(obj.step("treat").state, StepState.UNKNOWN_EVIDENCE)
        self.assertIn("status_unknown", [r.code for r in obj.step("treat").reasons])

    def test_missing_receipt_keeps_step_pending(self):
        records = [record("O", "treat", "2026-05-04", "2026-05-08", "done", receipt=Evidence(EvidenceState.PRESENT, "no", False))]
        obj = run(records).by_id("O")
        self.assertEqual(obj.step("treat").state, StepState.PENDING_CONFIRMATION)
        self.assertIn("missing_receipt", [r.code for r in obj.step("treat").reasons])
        self.assertTrue(obj.needs_confirmation)


class AggregationTests(unittest.TestCase):
    def test_aggregates_by_site_material_treatment(self):
        records = [
            record("A1", "treat", "2026-05-04", "2026-05-08", "in_progress", site="A",
                   material="bronze", treatment_type="cleaning"),
            record("B1", "treat", "2026-05-04", "2026-05-08", "done", site="B",
                   material="paper", treatment_type="deacidification"),
        ]
        assessment = run(records)
        site = assessment.aggregate("site")
        self.assertEqual(set(site), {"A", "B"})
        self.assertEqual(site["A"].objects, 1)
        self.assertEqual(site["A"].treatment_complete, 0)
        self.assertEqual(site["B"].treatment_complete, 1)
        self.assertEqual(set(assessment.aggregate("material")), {"bronze", "paper"})
        self.assertEqual(set(assessment.aggregate("treatment")), {"cleaning", "deacidification"})

    def test_treatment_complete_is_not_proxied_by_any_step_done(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            record("O", "treat", "2026-05-03", "2026-05-08", "in_progress"),
        ]
        obj = run(records).by_id("O")
        self.assertFalse(obj.treatment_complete)
        self.assertEqual(obj.treatment_gap, ("treat",))


class DeadlineTests(unittest.TestCase):
    def test_completed_on_time_is_not_late(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            record("O", "treat", "2026-05-04", "2026-05-08", "done"),
            record("O", "recheck", "2026-05-09", "2026-05-10", "done"),
        ]
        obj = run(records).by_id("O")
        self.assertTrue(obj.full_delivery)
        self.assertTrue(obj.on_time_delivery)
        self.assertFalse(obj.late)
        self.assertIs(obj.risk, Risk.COMPLETE)
        self.assertIs(run(records).overall().complete, 1)

    def test_completed_late_is_visible_not_complete(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            record("O", "treat", "2026-05-09", "2026-05-08", "done"),
            record("O", "recheck", "2026-05-13", "2026-05-14", "done"),
        ]
        obj = run(records).by_id("O")
        self.assertTrue(obj.full_delivery)
        self.assertTrue(obj.late)
        self.assertFalse(obj.on_time_delivery)
        self.assertIsNot(obj.risk, Risk.COMPLETE)
        overall = run(records).overall()
        self.assertEqual(overall.full_delivery, 1)
        self.assertEqual(overall.on_time_delivery, 0)
        self.assertEqual(overall.late_delivery, 1)


class FixtureTests(unittest.TestCase):
    def test_repository_fixtures_load(self):
        bundle = load_bundle(FIXTURES)
        self.assertEqual(len(bundle.progress), 5)
        self.assertEqual(len(bundle.teams), 3)
        assessment = assess(bundle.progress, bundle.findings, bundle.protocol, AS_OF, bundle.transfers)
        self.assertEqual(len(assessment.objects), 2)
        o1 = assessment.by_id("O1")
        o2 = assessment.by_id("O2")
        self.assertTrue(o1.treatment_complete is False)
        self.assertIs(o2.risk, Risk.EVIDENCE_GAP)

    def test_example_dataset_end_to_end(self):
        bundle = load_bundle(EXAMPLES)
        self.assertEqual(len(bundle.spaces), 2)
        assessment = assess(bundle.progress, bundle.findings, bundle.protocol, AS_OF, bundle.transfers)
        self.assertEqual(assessment.by_id("P1").sites, ("A", "B"))
        self.assertTrue(assessment.by_id("P4").confirmed_missed)
        self.assertTrue(assessment.by_id("P3").needs_confirmation)


class PlanningTests(unittest.TestCase):
    def test_compatible_team_resolves_and_recheck_kept_in_window(self):
        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done"),
            record("O", "treat", "2026-05-05", "2026-05-11", "in_progress", site="A"),
            record("O", "recheck", "2026-05-15", "2026-05-18", "scheduled", site="A"),
        ]
        assessment = run(records)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 8), date(2026, 5, 20), 1),
                   TeamWindow("T", "A", "recheck", date(2026, 5, 9), date(2026, 5, 20), 1)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        outcome = plan.outcome("O")
        self.assertIs(outcome.disposition, Disposition.ASSIGNED)
        self.assertGreaterEqual(outcome.recheck_by, date(2026, 5, 12))
        self.assertLessEqual(outcome.recheck_by, date(2026, 5, 14))
        steps = {a.step: a for a in plan.assignments}
        self.assertLess(steps["treat"].day, steps["recheck"].day)
        self.assertEqual(plan.resolved, 1)

    def test_batch_must_not_split_when_one_member_blocked(self):
        records = [
            record("A", "treat", "2026-05-05", "2026-05-11", "in_progress", site="A", batch="B1"),
            ProgressRecord(
                object_id="B", site="A", step="treat", event=ev_date("2026-05-05"),
                deadline=ev_date("2026-05-11"), inspection=ev_text("I1"),
                elapsed=Evidence(EvidenceState.MISSING), status=Evidence(EvidenceState.MISSING),
                batch="B1",
            ),
        ]
        assessment = run(records)
        self.assertIs(assessment.by_id("B").risk, Risk.EVIDENCE_GAP)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 8), date(2026, 5, 20), 2)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("A").disposition, Disposition.NEEDS_CONFIRMATION)
        self.assertIn("batch_not_splittable", [r.code for r in plan.outcome("A").reasons])
        self.assertEqual(plan.assignments, ())

    def test_in_flight_chain_is_locked_until_returned(self):
        from careflow.model import Transfer

        records = [
            record("O", "treat", "2026-05-05", "2026-05-11", "in_progress", site="A"),
        ]
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 8), date(2026, 5, 20), 1)],
            transfers=[Transfer("O", "A", "B", "in_transit")],
        )
        assessment = assess(records, [], Protocol(), AS_OF, bundle.transfers)
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("O").disposition, Disposition.NOT_ACTIONABLE)
        self.assertIn("in_flight_locked", [r.code for r in plan.outcome("O").reasons])

    def test_returned_chain_can_be_reassigned(self):
        from careflow.model import Transfer

        records = [
            record("O", "treat", "2026-05-05", "2026-05-11", "in_progress", site="A"),
        ]
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 8), date(2026, 5, 20), 1)],
            transfers=[Transfer("O", "A", "B", "returned", returned_on=ev_date("2026-05-07"))],
        )
        assessment = assess(records, [], Protocol(), AS_OF, bundle.transfers)
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("O").disposition, Disposition.ASSIGNED)

    def test_confirmed_missed_is_mitigation_only_not_resolved(self):
        records = [
            record("O", "treat", "2026-05-01", "2026-05-03", "done"),
            record("O", "recheck", None, "2026-05-09", "not_started"),
        ]
        assessment = run(records)
        self.assertTrue(assessment.by_id("O").confirmed_missed)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "recheck", date(2026, 5, 8), date(2026, 5, 20), 1)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        outcome = plan.outcome("O")
        self.assertIs(outcome.disposition, Disposition.MITIGATION_ASSIGNED)
        self.assertTrue(outcome.mitigation)
        self.assertFalse(outcome.compliance_restorable)
        self.assertEqual(plan.resolved, 0)
        self.assertEqual(plan.mitigation, 1)

    def test_evidence_gap_needs_confirmation_and_does_not_consume_slot(self):
        records = [
            record("O", "treat", "2026-05-05", "2026-05-11", "in_progress"),
            ProgressRecord(
                object_id="Q", site="A", step="treat", event=ev_date("2026-05-05"),
                deadline=ev_date("2026-05-11"), inspection=ev_text("I1"),
                elapsed=Evidence(EvidenceState.MISSING), status=Evidence(EvidenceState.MISSING),
            ),
        ]
        assessment = run(records)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 8), date(2026, 5, 20), 1)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("Q").disposition, Disposition.NEEDS_CONFIRMATION)
        self.assertIn("evidence_gap", [r.code for r in plan.outcome("Q").reasons])
        # 一个名额被确定的对象占用，缺证据对象不强占。
        self.assertEqual(len(plan.assignments), 1)
        self.assertEqual(plan.assignments[0].object_id, "O")

    def test_space_freed_by_handover_is_respected(self):
        from careflow.model import SiteSpace

        records = [
            record("O", "treat", "2026-05-05", "2026-05-20", "in_progress", site="B"),
        ]
        assessment = run(records)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "B", "treat", date(2026, 5, 8), date(2026, 5, 20), 1)],
            spaces=[SiteSpace("B", date(2026, 5, 12), date(2026, 5, 20), 1)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("O").disposition, Disposition.ASSIGNED)
        self.assertGreaterEqual(plan.assignments[0].day, date(2026, 5, 12))

    def test_no_compatible_team_means_confirmation_not_capacity(self):
        records = [record("O", "treat", "2026-05-05", "2026-05-20", "in_progress", site="A")]
        assessment = run(records)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "B", "treat", date(2026, 5, 8), date(2026, 5, 20), 1)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("O").disposition, Disposition.NEEDS_CONFIRMATION)

    def test_compare_plans_categorises_three_backlogs(self):
        from careflow.model import SiteSpace, Transfer

        records = [
            # 可被兼容班组消掉
            record("R", "treat", "2026-05-05", "2026-05-20", "in_progress", site="A"),
            # 需要馆方确认
            ProgressRecord(
                object_id="Q", site="A", step="treat", event=ev_date("2026-05-05"),
                deadline=ev_date("2026-05-20"), inspection=ev_text("I1"),
                elapsed=Evidence(EvidenceState.MISSING), status=Evidence(EvidenceState.MISSING),
            ),
            # 即使增加班组也来不及
            record("X", "treat", "2026-05-01", "2026-05-03", "done", site="C"),
            record("X", "recheck", None, "2026-05-06", "not_started", site="C"),
        ]
        assessment = run(records)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 8), date(2026, 5, 20), 1)],
        )
        result = compare_plans(assessment, bundle, start=AS_OF)
        categories = result["categories"]
        self.assertEqual([x["object_id"] for x in categories["resolved_by_compatible_team"]], ["R"])
        self.assertIn("Q", [x["object_id"] for x in categories["needs_confirmation"]])
        self.assertIn("X", [x["object_id"] for x in categories["mitigation_only"]])
        self.assertIsNotNone(result["recommended"])

    def test_two_week_window_is_enforced(self):
        records = [record("O", "treat", "2026-05-05", "2026-06-30", "in_progress", site="A")]
        assessment = run(records)
        bundle = Bundle(
            progress=records,
            teams=[TeamWindow("T", "A", "treat", date(2026, 5, 25), date(2026, 6, 30), 1)],
        )
        plan = plan_support(assessment, bundle, start=AS_OF)
        self.assertIs(plan.outcome("O").disposition, Disposition.NEEDS_CONFIRMATION)
        self.assertNotIn("O", [a.object_id for a in plan.assignments])


class HandoverTests(unittest.TestCase):
    def test_site_scope_excludes_handed_off_finished_chain(self):
        from careflow.model import Transfer

        records = [
            record("O", "inspect", "2026-05-01", "2026-05-04", "done", site="A"),
            record("O", "treat", "2026-05-04", "2026-05-08", "done", site="A"),
            record("O", "recheck", "2026-05-12", "2026-05-14", "scheduled", site="B"),
        ]
        assessment = run(records, transfers=[Transfer("O", "A", "B", "received")])
        self.assertEqual(len(assessment.objects), 1)
        self.assertEqual(build_snapshot(assessment, scope="B").overall["objects"], 1)
        # A 馆已把整条链交出去，不应再在 A 馆的待交付缺口里重复出现。
        self.assertEqual(build_snapshot(assessment, scope="A").overall["objects"], 0)

    def _assessment(self):
        records = [
            record("O1", "treat", "2026-05-05", "2026-05-08", "in_progress", site="A",
                   material="bronze", treatment_type="cleaning"),
            record("O2", "treat", "2026-05-01", "2026-05-03", "done", site="B",
                   material="paper", treatment_type="deacidification"),
            record("O2", "recheck", None, "2026-05-06", "not_started", site="B",
                   material="paper", treatment_type="deacidification"),
        ]
        return run(records)

    def test_snapshot_restores_each_site_gap(self):
        assessment = self._assessment()
        snapshot = build_snapshot(assessment, scope="A")
        self.assertEqual([item["object_id"] for item in snapshot.per_object], ["O1"])
        self.assertEqual(snapshot.overall["objects"], 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = save_snapshot(snapshot, Path(tmp) / "snap.json")
            restored = load_snapshot(path)
        self.assertEqual(restored.to_dict(), snapshot.to_dict())
        self.assertEqual(verify_snapshot(assessment, restored), [])

    def test_verify_detects_drift(self):
        assessment = self._assessment()
        snapshot = build_snapshot(assessment)
        tampered = snapshot.to_dict()
        tampered["per_object"][0]["risk"] = "complete"
        from careflow.model import HandoverRecord

        problems = verify_snapshot(assessment, HandoverRecord(**tampered))
        self.assertTrue(any("risk" in p for p in problems))

    def test_diff_reports_changed_outcomes_and_reasons(self):
        before = self._assessment()
        # 支援后：O1 的处置与复检都完成
        records = [
            record("O1", "treat", "2026-05-08", "2026-05-08", "done", site="A",
                   material="bronze", treatment_type="cleaning"),
            record("O1", "recheck", "2026-05-12", "2026-05-14", "done", site="A",
                   material="bronze", treatment_type="cleaning"),
            record("O2", "treat", "2026-05-01", "2026-05-03", "done", site="B",
                   material="paper", treatment_type="deacidification"),
            record("O2", "recheck", None, "2026-05-06", "not_started", site="B",
                   material="paper", treatment_type="deacidification"),
        ]
        after = run(records)
        result = diff_assessments(before, after)
        changed = {item["object_id"] for item in result["changed_outcomes"]}
        self.assertEqual(changed, {"O1"})
        self.assertTrue(after.by_id("O1").full_delivery)
        self.assertTrue(result["changed_reasons"])

    def test_diff_between_snapshots_keeps_scope(self):
        before = build_snapshot(self._assessment(), scope="A")
        records = [
            record("O1", "treat", "2026-05-08", "2026-05-08", "done", site="A",
                   material="bronze", treatment_type="cleaning"),
            record("O2", "treat", "2026-05-01", "2026-05-03", "done", site="B",
                   material="paper", treatment_type="deacidification"),
        ]
        after = build_snapshot(run(records), scope="A")
        result = diff_snapshots(before, after)
        self.assertEqual([item["object_id"] for item in result["changed_outcomes"]], ["O1"])


class LoadingTests(unittest.TestCase):
    def test_protocol_file_overrides_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "protocol.csv").write_text("key,value\nquiet_min_days,5\nhorizon_days,10\n", encoding="utf-8")
            bundle = load_bundle(root)
        self.assertEqual(bundle.protocol.quiet_min_days, 5)
        self.assertEqual(bundle.protocol.horizon_days, 10)

    def test_unknown_inspection_finding_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "primary.csv").write_text(
                "object,site,step,event,deadline,inspection,elapsed,status\n"
                "O,A,treat,2026-05-01,2026-05-05,I1,0,done\n",
                encoding="utf-8",
            )
            (root / "findings.csv").write_text(
                "inspection,object,step,effect,days,limit_days,recheck\n"
                "ZZ,,,shorten,1,3,no\n",
                encoding="utf-8",
            )
            bundle = load_bundle(root)
        self.assertIn("orphan_finding", [issue.code for issue in bundle.issues])

    def test_duplicate_step_records_are_merged_with_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "primary.csv").write_text(
                "object,site,step,event,deadline,inspection,elapsed,status\n"
                "O,A,treat,2026-05-01,2026-05-05,I1,0,done\n"
                "O,B,treat,2026-05-02,2026-05-05,I1,0,done\n",
                encoding="utf-8",
            )
            bundle = load_bundle(root)
        self.assertIn("duplicate_step", [issue.code for issue in bundle.issues])
        assessment = assess(bundle.progress, bundle.findings, bundle.protocol, AS_OF, bundle.transfers)
        self.assertEqual(len(assessment.objects), 1)

    def test_urgent_transfer_requires_dual_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "primary.csv").write_text(
                "object,site,step,event,deadline,inspection,elapsed,status\n"
                "O,A,treat,2026-05-01,2026-05-09,in_progress,I1,0,unknown\n",
                encoding="utf-8",
            )
            (root / "transfers.csv").write_text(
                "object,from_site,to_site,state,urgent,origin_confirmed,receiving_confirmed\n"
                "O,A,B,planned,true,yes,no\n",
                encoding="utf-8",
            )
            bundle = load_bundle(root)
        self.assertIn("urgent_transfer_unconfirmed", [issue.code for issue in bundle.issues])
        assessment = assess(bundle.progress, bundle.findings, bundle.protocol, AS_OF, bundle.transfers)
        self.assertTrue(assessment.by_id("O").urgent_transfer_needs_confirmation)


if __name__ == "__main__":
    unittest.main()
