"""逐件评估引擎。

判定顺序（每一步都保留可解释的原因）：

1. 按 ``object`` 合并护理链；同一藏品跨馆交接只产生一个 ``ObjectAssessment``。
2. 同一 ``inspection`` 的发现同时作用于共享该检查的工序；发现可以缩短时限，
   也可以只要求附加复检。
3. 只有“实际完成处置”才重置复检阶段时限（静置下限从处置完成时刻起算）。
4. 证据缺失（缺报/未知/缺回执）与已确认错过安全窗口分开判断。
5. 处置完成度与完整交付度分别计算。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from .model import (
    DataIssue,
    Evidence,
    EvidenceState,
    Finding,
    ObjectAssessment,
    ProgressRecord,
    Protocol,
    RecheckWindow,
    Reason,
    Risk,
    StepAssessment,
    StepState,
    Transfer,
    iso,
)

STEP_ORDER = {"inspect": 0, "treat": 1, "recheck": 2, "return": 3}
STEP_LABEL = {"inspect": "检查", "treat": "处置", "recheck": "复检", "return": "回库"}

_STEP_ALIASES = {
    "inspect": "inspect",
    "inspection": "inspect",
    "check": "inspect",
    "检查": "inspect",
    "巡检": "inspect",
    "treat": "treat",
    "treatment": "treat",
    "process": "treat",
    "处置": "treat",
    "处理": "treat",
    "微环境处置": "treat",
    "recheck": "recheck",
    "re-check": "recheck",
    "review": "recheck",
    "复检": "recheck",
    "复查": "recheck",
    "return": "return",
    "return_to_store": "return",
    "restock": "return",
    "回库": "return",
    "归还": "return",
}

_STATUS_ALIASES = {
    "done": StepState.DONE,
    "complete": StepState.DONE,
    "completed": StepState.DONE,
    "finished": StepState.DONE,
    "已完成": StepState.DONE,
    "完成": StepState.DONE,
    "已处置": StepState.DONE,
    "已回库": StepState.DONE,
    "in_progress": StepState.IN_PROGRESS,
    "in progress": StepState.IN_PROGRESS,
    "doing": StepState.IN_PROGRESS,
    "processing": StepState.IN_PROGRESS,
    "处置中": StepState.IN_PROGRESS,
    "进行中": StepState.IN_PROGRESS,
    "处理中": StepState.IN_PROGRESS,
    "scheduled": StepState.SCHEDULED,
    "planned": StepState.SCHEDULED,
    "已排期": StepState.SCHEDULED,
    "已预约": StepState.SCHEDULED,
    "计划": StepState.SCHEDULED,
    "not_started": StepState.NOT_STARTED,
    "pending": StepState.NOT_STARTED,
    "未开始": StepState.NOT_STARTED,
    "待开始": StepState.NOT_STARTED,
    "待处理": StepState.NOT_STARTED,
    "blocked": StepState.BLOCKED,
    "受阻": StepState.BLOCKED,
    "暂停": StepState.BLOCKED,
    "pending_confirmation": StepState.PENDING_CONFIRMATION,
    "待确认": StepState.PENDING_CONFIRMATION,
}

_DONE = StepState.DONE


def canonical_step(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    return _STEP_ALIASES.get(text, text)


def _status_state(evidence: Evidence) -> StepState:
    if evidence.state is EvidenceState.MISSING:
        return StepState.MISSING_EVIDENCE
    if evidence.state is EvidenceState.UNKNOWN:
        return StepState.UNKNOWN_EVIDENCE
    if evidence.state is EvidenceState.ZERO:
        return StepState.NOT_STARTED
    text = (evidence.raw or "").strip().lower()
    return _STATUS_ALIASES.get(text, StepState.UNKNOWN_EVIDENCE)


def _evidence_gap_state(state: StepState) -> bool:
    return state in (
        StepState.MISSING_EVIDENCE,
        StepState.UNKNOWN_EVIDENCE,
        StepState.PENDING_CONFIRMATION,
    )


def _as_date(evidence: Evidence | None) -> date | None:
    if evidence is None or not evidence.known:
        return None
    return evidence.value if isinstance(evidence.value, date) else None


def _completed_on(record: ProgressRecord) -> date | None:
    for key in ("completed_on", "done_on", "completed", "completed_at", "finished_on"):
        raw = record.extra.get(key)
        if raw:
            from .io import parse_date

            parsed = parse_date(raw)
            if parsed.known and isinstance(parsed.value, date):
                return parsed.value
    return _as_date(record.event)


_RANK = {
    StepState.DONE: 5,
    StepState.PENDING_CONFIRMATION: 4,
    StepState.IN_PROGRESS: 3,
    StepState.SCHEDULED: 2,
    StepState.NOT_STARTED: 1,
    StepState.BLOCKED: 0,
    StepState.MISSING_EVIDENCE: -1,
    StepState.UNKNOWN_EVIDENCE: -2,
}


def _merge_records(records: list[ProgressRecord]) -> tuple[ProgressRecord, tuple[Reason, ...]]:
    """同一 (object, step) 在交接两侧各有一条记录时合并为一条。

    合并而不是新增：换馆交接不产生第二件待交付对象。
    """

    def key(record: ProgressRecord) -> tuple[int, int, int]:
        event = _as_date(record.event)
        return (
            _RANK.get(_status_state(record.status), -3),
            event.toordinal() if event else 0,
            record.source_row,
        )

    best = max(records, key=key)
    sites = tuple(dict.fromkeys(r.site for r in records if r.site))
    reasons: tuple[Reason, ...] = ()
    if len(records) > 1:
        reasons = (
            Reason(
                "merged_handover",
                f"{best.step} 在 {', '.join(sites)} 各有记录，已按交接合并为同一藏品的一条工序",
                "info",
            ),
        )
    return best, reasons


def _finding_targets(finding: Finding, step: str, inspection: str | None, object_id: str) -> bool:
    if finding.object_id and finding.object_id != object_id:
        return False
    if finding.step:
        return canonical_step(finding.step) == step
    if finding.inspection and inspection:
        return finding.inspection == inspection
    return False


def _finding_applies(finding: Finding) -> tuple[bool, Reason | None]:
    """按业务记录判断发现是否已确认。

    明确未确认的发现不改变时限；确认状态缺报/未知时仍按发现执行，但会
    留下待核实提示，避免把“没有审批记录”当成“已批准”。
    """

    confirmed = finding.confirmed
    if confirmed is not None and confirmed.known and confirmed.value is False:
        return False, Reason(
            "finding_not_confirmed",
            f"{finding.inspection or '检查'} 的发现尚未确认，暂不改变时限",
            "warn",
        )
    if confirmed is None or not confirmed.known:
        return True, Reason(
            "finding_confirmation_unknown",
            f"{finding.inspection or '检查'} 的发现缺少确认记录，按发现执行但需核实",
            "warn",
        )
    return True, None


def _marker_date(finding: Finding, fallback: date | None) -> date | None:
    if finding.finding_date.known and isinstance(finding.finding_date.value, date):
        return finding.finding_date.value
    return fallback


def _reachable_findings(
    findings: Iterable[Finding], object_id: str, inspections: set[str]
) -> list[Finding]:
    """按藏品归属收窄发现：显式 object 优先，其次共享检查号。"""

    result: list[Finding] = []
    for finding in findings:
        if finding.object_id is not None:
            if finding.object_id == object_id:
                result.append(finding)
        elif finding.inspection and finding.inspection in inspections:
            result.append(finding)
    return result


def _effective_deadline(
    *,
    record_deadline: date | None,
    step: str,
    inspection: str | None,
    state: StepState,
    event: date | None,
    findings: list[Finding],
    object_id: str,
) -> tuple[date | None, tuple[Reason, ...]]:
    value = record_deadline
    reasons: list[Reason] = []
    for finding in findings:
        if not _finding_targets(finding, step, inspection, object_id):
            continue
        if finding.shortens:
            reference = _marker_date(finding, event)
            if finding.limit_days is not None and reference is not None:
                candidate = reference + timedelta(days=finding.limit_days)
                if value is None or candidate < value:
                    value = candidate
                    reasons.append(
                        Reason(
                            "deadline_shortened_by_finding",
                            f"{finding.inspection or '检查'} 将 {step} 时限缩短至 {candidate.isoformat()}",
                        )
                    )
            elif finding.days is not None and value is not None:
                candidate = value - timedelta(days=finding.days)
                value = candidate
                reasons.append(
                    Reason(
                        "deadline_shortened_by_finding",
                        f"{finding.inspection or '检查'} 将 {step} 时限提前 {finding.days} 天",
                    )
                )
        if finding.resets:
            # 规程：实际发生处置才重置相应阶段时限。
            if state is not _DONE or event is None:
                reasons.append(
                    Reason(
                        "reset_requires_actual_step",
                        f"{finding.inspection or '检查'} 要求重置 {step} 时限，但该工序尚未实际完成，暂不重置",
                        "info",
                    )
                )
            elif finding.days is not None:
                candidate = event + timedelta(days=finding.days)
                value = candidate
                reasons.append(
                    Reason(
                        "deadline_reset_after_actual_step",
                        f"{step} 实际完成后时限重置为 {candidate.isoformat()}",
                        "info",
                    )
                )
    return value, tuple(reasons)


def _recheck_limit(
    *,
    findings: list[Finding],
    object_id: str,
    treatment_done: date | None,
    protocol: Protocol,
    record_deadline: date | None,
) -> tuple[date | None, date | None, tuple[Reason, ...]]:
    """复检窗口 (earliest, latest)。

    静置下限与复检上限都从实际处置完成时刻起算。工序行上的复检截止只有在
    与静置下限不冲突时才作为附加上限；若它早于静置下限，说明它是旧计划，
    处置实际完成后阶段时限已重置，不再采用。
    """

    if treatment_done is None:
        return None, None, ()
    reasons: list[Reason] = []
    earliest = treatment_done + timedelta(days=protocol.quiet_min_days)
    latest = treatment_done + timedelta(days=protocol.recheck_max_days)
    for finding in findings:
        if finding.object_id and finding.object_id != object_id:
            continue
        if not (finding.adds_recheck or _finding_targets(finding, "recheck", None, object_id)):
            continue
        reference = _marker_date(finding, treatment_done)
        if finding.limit_days is not None and reference is not None:
            candidate = reference + timedelta(days=finding.limit_days)
            if candidate < latest:
                latest = candidate
                reasons.append(
                    Reason("recheck_limit_from_finding", f"复检上限受检查限制为 {candidate.isoformat()}")
                )
        elif finding.days is not None and finding.shortens:
            latest = latest - timedelta(days=finding.days)
            reasons.append(
                Reason("recheck_limit_from_finding", f"复检上限因检查提前 {finding.days} 天")
            )
    if record_deadline is not None:
        if record_deadline < earliest:
            reasons.append(
                Reason(
                    "stale_recheck_deadline_ignored",
                    f"工序行复检截止 {record_deadline.isoformat()} 早于静置下限 "
                    f"{earliest.isoformat()}，按实际处置完成时间重置，不采用旧截止",
                    "info",
                )
            )
        elif record_deadline < latest:
            latest = record_deadline
            reasons.append(
                Reason(
                    "recheck_limit_from_deadline",
                    f"复检上限受工序截止 {record_deadline.isoformat()} 约束",
                )
            )
    return earliest, latest, tuple(reasons)


def _recheck_window(
    *,
    required: bool,
    treatment_done: date | None,
    recheck_state: StepState | None,
    planned: date | None,
    findings: list[Finding],
    protocol: Protocol,
    as_of: date,
    object_id: str,
    record_deadline: date | None,
) -> RecheckWindow | None:
    if not required:
        return RecheckWindow(treatment_done, None, None, planned, "not_required", None, ())

    earliest, latest, limit_reasons = _recheck_limit(
        findings=findings,
        object_id=object_id,
        treatment_done=treatment_done,
        protocol=protocol,
        record_deadline=record_deadline,
    )
    reasons: list[Reason] = list(limit_reasons)

    if treatment_done is None:
        state = "unknown" if recheck_state in (StepState.MISSING_EVIDENCE, StepState.UNKNOWN_EVIDENCE) else "pending"
        reasons.append(Reason("treatment_not_completed", "处置尚未完成，静置与复检窗口尚未开始", "info"))
        return RecheckWindow(None, None, None, planned, state, None, tuple(reasons))

    assert earliest is not None and latest is not None
    if earliest > latest:
        reasons.append(
            Reason(
                "no_valid_recheck_window",
                f"静置下限 {earliest.isoformat()} 晚于复检上限 {latest.isoformat()}，不存在合规复检窗口",
                "block",
            )
        )
        return RecheckWindow(treatment_done, earliest, latest, planned, "impossible", False, tuple(reasons))

    if recheck_state is _DONE:
        if planned is None:
            reasons.append(Reason("recheck_date_missing", "复检已完成但缺少完成日期", "block"))
            return RecheckWindow(treatment_done, earliest, latest, planned, "unknown", None, tuple(reasons))
        if planned < earliest:
            reasons.append(
                Reason("recheck_too_early", f"复检 {planned.isoformat()} 早于静置下限 {earliest.isoformat()}", "block")
            )
            return RecheckWindow(treatment_done, earliest, latest, planned, "too_early", False, tuple(reasons))
        if planned > latest:
            reasons.append(
                Reason("recheck_after_limit", f"复检 {planned.isoformat()} 晚于上限 {latest.isoformat()}", "block")
            )
            return RecheckWindow(treatment_done, earliest, latest, planned, "missed", False, tuple(reasons))
        reasons.append(Reason("recheck_satisfied", f"复检 {planned.isoformat()} 落在合规窗口内", "info"))
        return RecheckWindow(treatment_done, earliest, latest, planned, "satisfied", True, tuple(reasons))

    if planned is not None:
        if planned > latest:
            state = "missed" if as_of > latest else "invalid_plan"
            reasons.append(
                Reason(
                    "recheck_plan_after_limit",
                    f"计划复检 {planned.isoformat()} 晚于上限 {latest.isoformat()}",
                    "block" if state == "missed" else "warn",
                )
            )
            return RecheckWindow(treatment_done, earliest, latest, planned, state, False, tuple(reasons))
        if planned < earliest:
            reasons.append(
                Reason(
                    "recheck_plan_too_early",
                    f"计划复检 {planned.isoformat()} 早于静置下限 {earliest.isoformat()}，需顺延",
                    "warn",
                )
            )
            return RecheckWindow(treatment_done, earliest, latest, planned, "invalid_plan", None, tuple(reasons))
        reasons.append(Reason("recheck_planned", f"复检已排在窗口内 {planned.isoformat()}", "info"))
        return RecheckWindow(treatment_done, earliest, latest, planned, "planned", None, tuple(reasons))

    if as_of > latest:
        reasons.append(
            Reason(
                "recheck_window_expired",
                f"截至 {as_of.isoformat()} 已超过复检上限 {latest.isoformat()} 且未完成复检",
                "block",
            )
        )
        return RecheckWindow(treatment_done, earliest, latest, planned, "missed", False, tuple(reasons))
    reasons.append(Reason("recheck_unscheduled", "尚未安排复检日期", "warn"))
    return RecheckWindow(treatment_done, earliest, latest, planned, "pending", None, tuple(reasons))


def assess(
    records: Iterable[ProgressRecord],
    findings: Iterable[Finding],
    protocol: Protocol,
    as_of: date,
    transfers: Iterable[Transfer] = (),
):
    """评估所有藏品，返回 ``Assessment``。"""

    from .model import Assessment

    records = list(records)
    finding_list = list(findings)
    transfer_list = list(transfers)
    issues: list[DataIssue] = []
    if protocol.quiet_min_days > protocol.recheck_max_days:
        issues.append(
            DataIssue(
                "invalid_protocol",
                f"静置下限 {protocol.quiet_min_days} 天大于复检上限 {protocol.recheck_max_days} 天",
                severity="block",
            )
        )

    by_object: dict[str, list[ProgressRecord]] = {}
    for record in records:
        by_object.setdefault(record.object_id, []).append(record)

    transfer_by_object: dict[str, list[Transfer]] = {}
    for transfer in transfer_list:
        transfer_by_object.setdefault(transfer.object_id, []).append(transfer)
    rank = {"returned": 4, "received": 3, "in_transit": 2, "planned": 1, "cancelled": 0}

    objects: list[ObjectAssessment] = []
    for object_id in sorted(by_object):
        transfers = transfer_by_object.get(object_id, [])
        active_transfer = (
            max(transfers, key=lambda t: (rank.get(t.state, 0), t.source_row)) if transfers else None
        )
        objects.append(
            _assess_object(
                object_id, by_object[object_id], finding_list, protocol, as_of, issues, active_transfer
            )
        )
    objects.sort(key=lambda o: (o.priority, o.due_at or date.max, o.object_id))
    return Assessment(as_of=as_of, objects=tuple(objects), issues=tuple(issues), protocol=protocol)


def _assess_object(
    object_id: str,
    records: list[ProgressRecord],
    findings: list[Finding],
    protocol: Protocol,
    as_of: date,
    issues: list[DataIssue],
    transfer: Transfer | None = None,
) -> ObjectAssessment:
    inspections = {r.inspection.value for r in records if r.inspection.known and r.inspection.value}
    object_findings = _reachable_findings(findings, object_id, inspections)

    # 1. 归一化并合并交接两侧的同一步骤。
    grouped: dict[str, list[ProgressRecord]] = {}
    for record in records:
        step = canonical_step(record.step)
        if not step:
            continue
        if step != record.step:
            record = ProgressRecord(**{**record.__dict__, "step": step})
        grouped.setdefault(step, []).append(record)

    merged: dict[str, ProgressRecord] = {}
    merge_reasons: dict[str, tuple[Reason, ...]] = {}
    for step, group in grouped.items():
        best, reasons = _merge_records(group)
        merged[step] = best
        merge_reasons[step] = reasons
        if len(group) > 1:
            issues.append(
                DataIssue(
                    "merged_step",
                    f"{object_id} 的 {step} 在 {', '.join(sorted({r.site for r in group}))} 重复登记，已按交接合并",
                    object_id=object_id,
                    step=step,
                )
            )

    # 2. 先后约束：显式 depends_on 优先，否则按规范顺序取前一步。
    ordered = sorted(merged, key=lambda s: STEP_ORDER.get(s, 10))
    deps: dict[str, tuple[str, ...]] = {}
    for index, step in enumerate(ordered):
        explicit = merged[step].depends_on
        if explicit:
            deps[step] = tuple(canonical_step(d) for d in explicit)
        elif index > 0:
            deps[step] = (ordered[index - 1],)
        else:
            deps[step] = ()

    # 3. 基础状态、回执与证据。
    base: dict[str, tuple[StepState, date | None, date | None, list[Reason]]] = {}
    treatment_done: date | None = None
    for step in ordered:
        record = merged[step]
        state = _status_state(record.status)
        reasons: list[Reason] = list(merge_reasons.get(step, ()))
        if state in (StepState.MISSING_EVIDENCE, StepState.UNKNOWN_EVIDENCE):
            reasons.append(
                Reason(
                    "status_missing" if state is StepState.MISSING_EVIDENCE else "status_unknown",
                    f"{step} 的进度{'缺报' if state is StepState.MISSING_EVIDENCE else '为未知'}，"
                    "不能视为完成或逾期",
                    "block",
                )
            )
        if state is _DONE and record.receipt is not None:
            if not (record.receipt.known and record.receipt.value is True):
                state = StepState.PENDING_CONFIRMATION
                reasons.append(Reason("missing_receipt", f"{step} 缺少现场回执，阶段保持待确认", "block"))
        if state is StepState.PENDING_CONFIRMATION and not any(r.code == "missing_receipt" for r in reasons):
            reasons.append(Reason("pending_confirmation", f"{step} 等待馆方确认", "block"))
        if state is _DONE and record.receipt is not None and record.receipt.value is True:
            reasons.append(Reason("receipt_confirmed", f"{step} 现场回执已确认", "info"))

        event = _as_date(record.event)
        completed = _completed_on(record) if state is _DONE else None
        if state is _DONE and completed is not None:
            event = completed
            if step == "treat":
                treatment_done = completed
        deadline = _as_date(record.deadline)
        base[step] = (state, event, deadline, reasons)

    # 2. 检查发现：按确认状态筛选后，缩短时限 / 要求附加复检。
    applicable: list[Finding] = []
    confirmation_notes: list[tuple[Finding, Reason]] = []
    for finding in object_findings:
        applies, note = _finding_applies(finding)
        if note is not None:
            confirmation_notes.append((finding, note))
        if applies:
            applicable.append(finding)
    object_findings = applicable

    recheck_required = "recheck" in merged
    for finding in object_findings:
        if finding.adds_recheck:
            recheck_required = True

    for step in ordered:
        state, event, deadline, reasons = base[step]
        inspection = merged[step].inspection.value if merged[step].inspection.known else None
        effective, deadline_reasons = _effective_deadline(
            record_deadline=deadline,
            step=step,
            inspection=inspection,
            state=state,
            event=event,
            findings=object_findings,
            object_id=object_id,
        )
        reasons.extend(deadline_reasons)
        base[step] = (state, event, effective, reasons, deadline, inspection)  # type: ignore[assignment]

    # 确认记录缺失的提示只挂到该发现实际作用的工序上，避免刷屏。
    for step in ordered:
        state, event, effective, reasons, raw_deadline, inspection = base[step]  # type: ignore[misc]
        for finding, note in confirmation_notes:
            if _finding_targets(finding, step, inspection, object_id):
                reasons.append(note)
        base[step] = (state, event, effective, reasons, raw_deadline, inspection)  # type: ignore[assignment]

    # 5. 复检工序缺失但检查要求附加复检 -> 合成一个待完成工序。
    synthetic_recheck = recheck_required and "recheck" not in merged
    if synthetic_recheck:
        inspection = next((f.inspection for f in object_findings if f.adds_recheck and f.inspection), None)
        recheck_reasons = tuple(
            Reason(
                "recheck_added_by_finding",
                f"{f.inspection or '检查'} 要求附加复检，完整交付需等待复检",
            )
            for f in object_findings
            if f.adds_recheck
        )
        ordered = ordered + ["recheck"]
        deps["recheck"] = ("treat",) if "treat" in merged else ()
        base["recheck"] = (
            StepState.NOT_STARTED,
            None,
            None,
            list(recheck_reasons),
            None,
            inspection,
        )

    # 6. 静置/复检窗口。
    recheck_state = base.get("recheck", (None,))[0] if "recheck" in base else None
    recheck_planned = base["recheck"][1] if "recheck" in base else None
    recheck_record_deadline = base["recheck"][4] if "recheck" in base else None
    recheck = _recheck_window(
        required=recheck_required,
        treatment_done=treatment_done,
        recheck_state=recheck_state,
        planned=recheck_planned,
        findings=object_findings,
        protocol=protocol,
        as_of=as_of,
        object_id=object_id,
        record_deadline=recheck_record_deadline,
    )

    # 7. 组装步骤，并处理先后约束。
    steps: list[StepAssessment] = []
    for step in ordered:
        state, event, deadline, reasons, raw_deadline, inspection = base[step]  # type: ignore[misc]
        if step == "recheck" and recheck is not None and recheck.latest is not None:
            # 复检工序的有效截止以静置规程算出的上限为准；工序行上的旧截止
            # 若早于静置下限，已在 _recheck_limit 中判定为过期计划。
            if deadline != recheck.latest:
                deadline = recheck.latest
                reasons = list(reasons) + [
                    Reason(
                        "recheck_limit_from_protocol",
                        f"复检上限按静置规程为 {recheck.latest.isoformat()}",
                        "info",
                    )
                ]
        steps.append(
            StepAssessment(
                step=step,
                site=(merged[step].site or None) if step in merged else None,
                state=state,
                raw_status=(merged[step].status if step in merged else Evidence(EvidenceState.UNKNOWN, "finding")),
                event=event,
                deadline=raw_deadline,
                effective_deadline=deadline,
                inspection=inspection,
                elapsed=(merged[step].elapsed if step in merged else Evidence(EvidenceState.MISSING)),
                receipt=(merged[step].receipt if step in merged else None),
                dependencies=deps[step],
                overdue=False,
                on_time=None,
                reasons=tuple(reasons),
                record_sites=tuple(dict.fromkeys(r.site for r in grouped.get(step, []) if r.site)),
                source="record" if step in merged else "finding",
            )
        )

    step_map = {s.step: s for s in steps}
    fixed: list[StepAssessment] = []
    for step in steps:
        reasons = list(step.reasons)
        state = step.state
        for dep in step.dependencies:
            dep_step = step_map.get(dep)
            if dep_step is None or dep_step.state is _DONE:
                continue
            if state is _DONE:
                reasons.append(
                    Reason(
                        "precedence_violation",
                        f"{step.step} 登记为完成，但前置 {dep} 尚未完成",
                        "block",
                    )
                )
                state = StepState.BLOCKED
            elif state in (StepState.SCHEDULED, StepState.IN_PROGRESS):
                reasons.append(Reason("predecessor_incomplete", f"{dep} 尚未完成，{step.step} 需等待", "info"))
        fixed.append(_replace_step(step, state=state, reasons=tuple(reasons)))

    # 8. 截止判断与证据缺口。
    final_steps: list[StepAssessment] = []
    evidence_gaps: list[str] = []
    late_reasons: list[Reason] = []
    for step in fixed:
        reasons = list(step.reasons)
        evidence_gap = _evidence_gap_state(step.state)
        if evidence_gap:
            evidence_gaps.append(step.step)
        on_time: bool | None = None
        overdue = False
        deadline = step.effective_deadline
        if step.state is _DONE and deadline is not None and step.event is not None:
            on_time = step.event <= deadline
            if not on_time:
                overdue = True
                reasons.append(
                    Reason(
                        "done_after_deadline",
                        f"{step.step} 于 {step.event.isoformat()} 完成，晚于有效截止 {deadline.isoformat()}",
                        "block",
                    )
                )
                late_reasons.append(
                    Reason("step_done_late", f"{step.step} 已确认晚于截止 {deadline.isoformat()}")
                )
        elif step.state is not _DONE and deadline is not None and not evidence_gap:
            if as_of > deadline:
                overdue = True
                on_time = False
                reasons.append(
                    Reason(
                        "deadline_passed",
                        f"{step.step} 截至 {as_of.isoformat()} 已超过有效截止 {deadline.isoformat()} 仍未完成",
                        "warn",
                    )
                )
                late_reasons.append(
                    Reason("step_deadline_passed", f"{step.step} 的截止 {deadline.isoformat()} 已过且未完成")
                )
            else:
                reasons.append(
                    Reason(
                        "deadline_approaching",
                        f"{step.step} 距有效截止 {(deadline - as_of).days} 天",
                        "info",
                    )
                )
        final_steps.append(_replace_step(step, overdue=overdue, on_time=on_time, reasons=tuple(reasons)))

    step_map = {s.step: s for s in final_steps}

    # 9. 已确认错过安全窗口（只针对复检窗口）。
    #    若复检进度本身缺报/未知，就不能断言“错过”——那属于证据缺口，
    #    需要馆方先确认；两者处置不同。
    recheck_evidence_gap = recheck_state in (
        StepState.MISSING_EVIDENCE,
        StepState.UNKNOWN_EVIDENCE,
        StepState.PENDING_CONFIRMATION,
    )
    window_gone = (
        recheck is not None
        and recheck.state in {"missed", "impossible"}
        and not recheck_evidence_gap
    )
    confirmed_missed_reasons: list[Reason] = []
    if window_gone:
        confirmed_missed_reasons.append(
            Reason(
                "recheck_window_missed" if recheck.state == "missed" else "recheck_window_impossible",
                f"复检安全窗口 {iso(recheck.earliest)} 至 {iso(recheck.latest)} "
                f"{'已错过' if recheck.state == 'missed' else '不可能满足'}",
                "block",
            )
        )

    # 10. 处置完成 vs 完整交付。
    treat_step = step_map.get("treat")
    treatment_complete = treat_step is None or treat_step.state is _DONE
    # 处置完成度只看处置前的工序；复检与回库属于完整交付的缺口。
    treatment_gap = tuple(
        s.step
        for s in final_steps
        if s.step in {"inspect", "treat"} and s.state is not _DONE
    )
    delivery_gap = tuple(s.step for s in final_steps if s.state is not _DONE)
    if recheck is not None and recheck.satisfied is False and "recheck" not in delivery_gap:
        delivery_gap = delivery_gap + ("recheck",)
    full_delivery = bool(final_steps) and not delivery_gap and not evidence_gaps and not confirmed_missed_reasons
    # 完成状态与截止条件分开：已完成但晚于有效截止仍算“逾期完成”，
    # 只有全部步骤都在截止前完成才算准时交付。
    late = any(s.overdue for s in final_steps)
    on_time_delivery = full_delivery and not late

    # 11. 风险。
    risk_reasons: list[Reason] = []
    due_candidates = [
        s.effective_deadline
        for s in final_steps
        if s.state is not _DONE and s.effective_deadline is not None
    ]
    if recheck is not None and recheck.latest is not None and recheck.satisfied is not True:
        due_candidates.append(recheck.latest)
    due_at = min(due_candidates) if due_candidates else None
    days_to_due = (due_at - as_of).days if due_at else None

    window_days_left = (
        (recheck.latest - as_of).days
        if recheck is not None and recheck.latest is not None and recheck.satisfied is not True
        else None
    )
    window_tight = window_days_left is not None and window_days_left <= protocol.recheck_urgency_days
    deadline_tight = days_to_due is not None and days_to_due <= protocol.deadline_urgency_days

    if full_delivery:
        risk = Risk.COMPLETE if on_time_delivery else Risk.AT_RISK
        if not on_time_delivery:
            risk_reasons.append(
                Reason("delivered_late", "护理链已完成，但有步骤晚于有效截止", "warn")
            )
    elif confirmed_missed_reasons:
        risk = Risk.CONFIRMED_MISSED
    elif evidence_gaps:
        risk = Risk.EVIDENCE_GAP
        for step in evidence_gaps:
            risk_reasons.append(
                Reason("evidence_gap", f"{step} 的进度证据不足，需馆方确认后才能排支援", "warn")
            )
    elif recheck is not None and recheck.state in {"too_early", "invalid_plan"}:
        risk = Risk.WINDOW_CRITICAL
        risk_reasons.append(
            Reason(
                "recheck_window_needs_adjustment",
                "复检安排与静置窗口冲突，需要调整到合规窗口内",
                "warn",
            )
        )
    elif window_tight:
        risk = Risk.WINDOW_CRITICAL
        risk_reasons.append(
            Reason("recheck_window_tight", f"距复检上限仅 {window_days_left} 天", "warn")
        )
    elif deadline_tight or late_reasons:
        risk = Risk.AT_RISK
        if deadline_tight:
            risk_reasons.append(
                Reason("deadline_tight", f"距最近有效截止 {days_to_due} 天 ({due_at.isoformat()})", "warn")
            )
    else:
        risk = Risk.ON_TRACK

    # 12. 交接状态：在途护理链保持锁定，正式交还才可重新占用。
    all_sites = tuple(dict.fromkeys(r.site for r in records if r.site))
    active_site = _active_site(records, all_sites)
    transfer_state = transfer.state if transfer is not None else None
    if transfer is not None and transfer.state == "in_transit" and transfer.to_site:
        active_site = transfer.to_site

    needs_confirmation = bool(evidence_gaps)
    if recheck is not None and recheck.state in {"pending", "unknown"}:
        needs_confirmation = True
    urgent_unconfirmed = False
    if transfer is not None:
        urgent_unconfirmed = (
            transfer.urgent
            and transfer.state in {"planned", "in_transit"}
            and not transfer.dual_confirmed
        )
        if transfer.state in {"planned", "in_transit"} and not transfer.dual_confirmed:
            needs_confirmation = True

    risk_reasons = list(confirmed_missed_reasons) + late_reasons + risk_reasons
    if transfer is not None:
        if transfer.state in {"in_transit", "received"}:
            risk_reasons.append(
                Reason(
                    "in_flight_chain_locked",
                    "护理链在途，正式交还前不得为优化完成率而切断或重新占用",
                    "block",
                )
            )
        if transfer.state == "returned":
            risk_reasons.append(
                Reason("returned_and_reassignable", "交接已正式交还，可按新窗口重新安排", "info")
            )

    batch = next((r.batch for r in records if r.batch), None)
    material = next((r.material for r in records if r.material), None)
    treatment_type = next((r.treatment_type for r in records if r.treatment_type), None)

    return ObjectAssessment(
        object_id=object_id,
        sites=all_sites,
        active_site=active_site,
        material=material,
        treatment_type=treatment_type,
        batch=batch,
        steps=tuple(final_steps),
        treatment_complete=treatment_complete,
        full_delivery=full_delivery,
        on_time_delivery=on_time_delivery,
        late=late,
        risk=risk,
        risk_reasons=tuple(_dedupe_reasons(risk_reasons)),
        treatment_gap=treatment_gap,
        delivery_gap=delivery_gap,
        evidence_gaps=tuple(evidence_gaps),
        recheck=recheck,
        due_at=due_at,
        days_to_due=days_to_due,
        confirmed_missed=window_gone,
        needs_confirmation=needs_confirmation,
        transfer_state=transfer_state,
        urgent_transfer_needs_confirmation=urgent_unconfirmed,
    )


def _replace_step(step: StepAssessment, **changes) -> StepAssessment:
    data = step.__dict__.copy()
    data.update(changes)
    return StepAssessment(**data)


def _dedupe_reasons(reasons: Iterable[Reason]) -> list[Reason]:
    seen = set()
    result = []
    for reason in reasons:
        key = (reason.code, reason.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(reason)
    return result


def _active_site(records: list[ProgressRecord], sites: tuple[str, ...]) -> str | None:
    dated = [
        (r.event.value, r.source_row, r.site)
        for r in records
        if r.event.known and isinstance(r.event.value, date) and r.site
    ]
    if dated:
        dated.sort(key=lambda item: (item[0], item[1]))
        return dated[-1][2]
    return sites[0] if sites else None
