"""领域模型与规程参数。

所有日期均为 ``datetime.date``。``Evidence`` 保留来源值的状态，
从而区分“已知”“显式为零”“缺报”和“未知”四种情况。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Iterable, Mapping


class EvidenceState(str, Enum):
    PRESENT = "present"
    ZERO = "zero"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Evidence:
    """一个字段的证据状态。

    ``raw`` 保留原始输入，便于交接时解释差异。``value`` 只在 PRESENT/ZERO
    时有意义。
    """

    state: EvidenceState
    raw: str | None = None
    value: Any = None

    @property
    def known(self) -> bool:
        return self.state in (EvidenceState.PRESENT, EvidenceState.ZERO)

    @property
    def missing(self) -> bool:
        return self.state in (EvidenceState.MISSING, EvidenceState.UNKNOWN)

    def __bool__(self) -> bool:  # 让 `if evidence` 只表示“可用证据”
        return self.known


MISSING = Evidence(EvidenceState.MISSING)
UNKNOWN = Evidence(EvidenceState.UNKNOWN)


class StepState(str, Enum):
    DONE = "done"
    IN_PROGRESS = "in_progress"
    SCHEDULED = "scheduled"
    NOT_STARTED = "not_started"
    PENDING_CONFIRMATION = "pending_confirmation"
    MISSING_EVIDENCE = "missing_evidence"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    OVERDUE = "overdue"
    BLOCKED = "blocked"


class Risk(str, Enum):
    COMPLETE = "complete"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    WINDOW_CRITICAL = "window_critical"
    EVIDENCE_GAP = "evidence_gap"
    CONFIRMED_MISSED = "confirmed_missed"


RISK_PRIORITY = {
    Risk.CONFIRMED_MISSED: 0,
    Risk.WINDOW_CRITICAL: 1,
    Risk.AT_RISK: 2,
    Risk.EVIDENCE_GAP: 3,
    Risk.ON_TRACK: 4,
    Risk.COMPLETE: 5,
}


class Disposition(str, Enum):
    COMPLETE = "complete"
    ASSIGNED = "assigned"
    MITIGATION_ASSIGNED = "mitigation_assigned"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CAPACITY_SHORT = "capacity_short"
    INFEASIBLE_EVEN_WITH_EXTRA = "infeasible_even_with_extra"
    NOT_ACTIONABLE = "not_actionable"


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str
    severity: str = "warn"  # info / warn / block

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True)
class Protocol:
    """馆际护理规程的可执行参数。

    默认值与 ``docs/业务规程.md`` 一致；``protocol.csv`` 可覆盖。
    """

    quiet_min_days: int = 4          # 静置下限，从处置完成时刻起算
    recheck_max_days: int = 6        # 复检上限，从处置完成时刻起算
    recheck_urgency_days: int = 2    # 距复检上限不足该天数 -> 窗口紧迫
    deadline_urgency_days: int = 2   # 距截止不足该天数 -> 有风险
    horizon_days: int = 14           # 值班员比较的支援窗口
    batch_atomic: bool = True        # 同一批次不能拆开处置
    urgent_transfer_dual_confirmation: bool = True

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, str]]) -> "Protocol":
        values: dict[str, Any] = {}
        for row in rows:
            key = (row.get("key") or row.get("name") or "").strip()
            raw = (row.get("value") or "").strip()
            if not key or not raw:
                continue
            if key in cls.__dataclass_fields__:
                target = cls.__dataclass_fields__[key].type
                if target is bool or target == "bool":
                    values[key] = raw.lower() in {"1", "true", "yes", "y", "是"}
                elif target is int or target == "int":
                    try:
                        values[key] = int(raw)
                    except ValueError:
                        continue
                else:
                    values[key] = raw
        return cls(**values)


@dataclass(frozen=True)
class ProgressRecord:
    object_id: str
    site: str
    step: str
    event: Evidence
    deadline: Evidence
    inspection: Evidence
    elapsed: Evidence
    status: Evidence
    receipt: Evidence | None = None  # None 表示输入没有 receipt 列
    depends_on: tuple[str, ...] = ()
    material: str | None = None
    treatment_type: str | None = None
    batch: str | None = None
    owner: str | None = None
    source_row: int = 0
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    inspection: str | None
    object_id: str | None
    step: str | None
    effect: str  # shorten / add_recheck / both / reset / extend / none
    days: int | None = None
    limit_days: int | None = None
    recheck: bool = False
    confirmed: Evidence | None = None  # None 表示输入没有 confirmed 列
    finding_date: Evidence = MISSING
    note: str | None = None
    source_row: int = 0

    @property
    def adds_recheck(self) -> bool:
        return self.recheck or self.effect in {"add_recheck", "both"}

    @property
    def shortens(self) -> bool:
        return self.effect in {"shorten", "both"}

    @property
    def resets(self) -> bool:
        return self.effect in {"reset", "extend"}


@dataclass(frozen=True)
class TeamWindow:
    team: str
    site: str
    skill: str
    start: date
    end: date
    capacity: int
    source_row: int = 0

    def clamped(self, lower: date, upper: date) -> "TeamWindow | None":
        start = max(self.start, lower)
        end = min(self.end, upper)
        if start > end:
            return None
        return TeamWindow(self.team, self.site, self.skill, start, end, self.capacity, self.source_row)


@dataclass(frozen=True)
class SiteSpace:
    site: str
    available_from: date
    available_to: date
    capacity: int
    note: str | None = None
    source_row: int = 0


@dataclass(frozen=True)
class Transfer:
    object_id: str
    from_site: str | None
    to_site: str | None
    state: str  # planned / in_transit / received / returned / cancelled
    urgent: bool = False
    origin_confirmed: Evidence | None = None
    receiving_confirmed: Evidence | None = None
    returned_on: Evidence = MISSING
    note: str | None = None
    source_row: int = 0

    @property
    def in_transit(self) -> bool:
        return self.state in {"in_transit", "received"}

    @property
    def returned(self) -> bool:
        return self.state == "returned" or (self.returned_on.known and self.returned_on.value is not None)

    @property
    def dual_confirmed(self) -> bool:
        """原馆与接收馆都已明确确认才算共同确认；缺一或明确未确认都不算。"""

        return all(
            flag is not None and flag.known and flag.value is True
            for flag in (self.origin_confirmed, self.receiving_confirmed)
        )


@dataclass(frozen=True)
class DataIssue:
    code: str
    message: str
    object_id: str | None = None
    site: str | None = None
    step: str | None = None
    row: int | None = None
    severity: str = "warn"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "object_id": self.object_id,
            "site": self.site,
            "step": self.step,
            "row": self.row,
            "severity": self.severity,
        }


@dataclass
class Bundle:
    progress: list[ProgressRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    teams: list[TeamWindow] = field(default_factory=list)
    spaces: list[SiteSpace] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    issues: list[DataIssue] = field(default_factory=list)
    protocol: Protocol = field(default_factory=Protocol)
    sources: dict[str, str] = field(default_factory=dict)

    def records_for(self, object_id: str) -> list[ProgressRecord]:
        return [r for r in self.progress if r.object_id == object_id]

    def findings_for(self, object_id: str) -> list[Finding]:
        result = []
        for finding in self.findings:
            if finding.object_id == object_id:
                result.append(finding)
                continue
            if finding.object_id is None and finding.inspection:
                if any(r.inspection.known and r.inspection.value == finding.inspection
                       for r in self.records_for(object_id)):
                    result.append(finding)
        return result

    def transfer_for(self, object_id: str) -> Transfer | None:
        matches = [t for t in self.transfers if t.object_id == object_id]
        if not matches:
            return None
        # 取最新状态：returned 优先于 received/in_transit 优先于 planned
        rank = {"returned": 4, "received": 3, "in_transit": 2, "planned": 1, "cancelled": 0}
        return max(matches, key=lambda t: (rank.get(t.state, 0), t.source_row))

    @property
    def object_ids(self) -> list[str]:
        return list(dict.fromkeys(r.object_id for r in self.progress))


@dataclass(frozen=True)
class RecheckWindow:
    treatment_done: date | None
    earliest: date | None
    latest: date | None
    planned: date | None
    state: str  # not_required / pending / satisfied / too_early / missed / unknown
    satisfied: bool | None
    reasons: tuple[Reason, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "treatment_done": iso(self.treatment_done),
            "earliest": iso(self.earliest),
            "latest": iso(self.latest),
            "planned": iso(self.planned),
            "state": self.state,
            "satisfied": self.satisfied,
            "reasons": [r.to_dict() for r in self.reasons],
        }


@dataclass(frozen=True)
class StepAssessment:
    step: str
    site: str | None
    state: StepState
    raw_status: Evidence
    event: date | None
    deadline: date | None
    effective_deadline: date | None
    inspection: str | None
    elapsed: Evidence
    receipt: Evidence | None
    dependencies: tuple[str, ...]
    overdue: bool
    on_time: bool | None
    reasons: tuple[Reason, ...]
    record_sites: tuple[str, ...]
    source: str = "record"  # record / finding

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "site": self.site,
            "state": self.state.value,
            "raw_status": ev_dict(self.raw_status),
            "event": iso(self.event),
            "deadline": iso(self.deadline),
            "effective_deadline": iso(self.effective_deadline),
            "inspection": self.inspection,
            "elapsed": ev_dict(self.elapsed),
            "receipt": ev_dict(self.receipt) if self.receipt is not None else None,
            "dependencies": list(self.dependencies),
            "overdue": self.overdue,
            "on_time": self.on_time,
            "reasons": [r.to_dict() for r in self.reasons],
            "record_sites": list(self.record_sites),
            "source": self.source,
        }


@dataclass(frozen=True)
class ObjectAssessment:
    object_id: str
    sites: tuple[str, ...]
    active_site: str | None
    material: str | None
    treatment_type: str | None
    batch: str | None
    steps: tuple[StepAssessment, ...]
    treatment_complete: bool
    full_delivery: bool
    on_time_delivery: bool
    late: bool
    risk: Risk
    risk_reasons: tuple[Reason, ...]
    treatment_gap: tuple[str, ...]
    delivery_gap: tuple[str, ...]
    evidence_gaps: tuple[str, ...]
    recheck: RecheckWindow | None
    due_at: date | None
    days_to_due: int | None
    confirmed_missed: bool
    needs_confirmation: bool
    transfer_state: str | None
    urgent_transfer_needs_confirmation: bool = False

    @property
    def priority(self) -> int:
        return RISK_PRIORITY[self.risk]

    def step(self, name: str) -> StepAssessment | None:
        return next((s for s in self.steps if s.step == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "sites": list(self.sites),
            "active_site": self.active_site,
            "material": self.material,
            "treatment_type": self.treatment_type,
            "batch": self.batch,
            "steps": [s.to_dict() for s in self.steps],
            "treatment_complete": self.treatment_complete,
            "full_delivery": self.full_delivery,
            "on_time_delivery": self.on_time_delivery,
            "late": self.late,
            "risk": self.risk.value,
            "risk_reasons": [r.to_dict() for r in self.risk_reasons],
            "treatment_gap": list(self.treatment_gap),
            "delivery_gap": list(self.delivery_gap),
            "evidence_gaps": list(self.evidence_gaps),
            "recheck": self.recheck.to_dict() if self.recheck else None,
            "due_at": iso(self.due_at),
            "days_to_due": self.days_to_due,
            "confirmed_missed": self.confirmed_missed,
            "needs_confirmation": self.needs_confirmation,
            "transfer_state": self.transfer_state,
            "urgent_transfer_needs_confirmation": self.urgent_transfer_needs_confirmation,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class Aggregate:
    key: str
    objects: int
    treatment_complete: int
    full_delivery: int
    on_time_delivery: int
    late_delivery: int
    confirmed_missed: int
    evidence_gap: int
    window_critical: int
    at_risk: int
    on_track: int
    complete: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "objects": self.objects,
            "treatment_complete": self.treatment_complete,
            "full_delivery": self.full_delivery,
            "on_time_delivery": self.on_time_delivery,
            "late_delivery": self.late_delivery,
            "confirmed_missed": self.confirmed_missed,
            "evidence_gap": self.evidence_gap,
            "window_critical": self.window_critical,
            "at_risk": self.at_risk,
            "on_track": self.on_track,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class Assessment:
    as_of: date
    objects: tuple[ObjectAssessment, ...]
    issues: tuple[DataIssue, ...]
    protocol: Protocol

    def by_id(self, object_id: str) -> ObjectAssessment:
        for obj in self.objects:
            if obj.object_id == object_id:
                return obj
        raise KeyError(object_id)

    def aggregate(self, dimension: str) -> dict[str, Aggregate]:
        attr = {
            "site": "active_site",
            "material": "material",
            "treatment": "treatment_type",
            "batch": "batch",
        }.get(dimension)
        if attr is None:
            raise ValueError(f"unknown dimension: {dimension}")
        groups: dict[str, list[ObjectAssessment]] = {}
        for obj in self.objects:
            key = getattr(obj, attr) or "(未提供)"
            groups.setdefault(key, []).append(obj)
        return {key: _aggregate(key, items) for key, items in sorted(groups.items())}

    def aggregates(self) -> dict[str, dict[str, Aggregate]]:
        return {dim: self.aggregate(dim) for dim in ("site", "material", "treatment", "batch")}

    def aggregates_dict(self) -> dict[str, Any]:
        return {
            "as_of": iso(self.as_of),
            "aggregates": {
                dim: {k: v.to_dict() for k, v in values.items()}
                for dim, values in self.aggregates().items()
            },
            "overall": self.overall().to_dict(),
        }

    def overall(self) -> Aggregate:
        return _aggregate("全部", list(self.objects))

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": iso(self.as_of),
            "objects": [o.to_dict() for o in self.objects],
            "issues": [i.to_dict() for i in self.issues],
            "aggregates": {
                dim: {k: v.to_dict() for k, v in values.items()}
                for dim, values in self.aggregates().items()
            },
            "overall": self.overall().to_dict(),
        }


@dataclass(frozen=True)
class Assignment:
    object_id: str
    step: str
    team: str
    site: str
    day: date
    window: tuple[date, date]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "step": self.step,
            "team": self.team,
            "site": self.site,
            "day": iso(self.day),
            "window": [iso(self.window[0]), iso(self.window[1])],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Outcome:
    object_id: str
    disposition: Disposition
    reasons: tuple[Reason, ...]
    assigned_steps: tuple[str, ...] = ()
    delivered_by: date | None = None
    recheck_by: date | None = None
    compliance_restorable: bool = True
    mitigation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "disposition": self.disposition.value,
            "reasons": [r.to_dict() for r in self.reasons],
            "assigned_steps": list(self.assigned_steps),
            "delivered_by": iso(self.delivered_by),
            "recheck_by": iso(self.recheck_by),
            "compliance_restorable": self.compliance_restorable,
            "mitigation": self.mitigation,
        }


@dataclass(frozen=True)
class Plan:
    name: str
    teams: tuple[str, ...]
    start: date
    end: date
    assignments: tuple[Assignment, ...]
    outcomes: tuple[Outcome, ...]
    resolved: int
    mitigation: int
    needs_confirmation: int
    capacity_short: int
    infeasible: int
    not_actionable: int
    baseline: bool = False

    def outcome(self, object_id: str) -> Outcome:
        for outcome in self.outcomes:
            if outcome.object_id == object_id:
                return outcome
        raise KeyError(object_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "teams": list(self.teams),
            "start": iso(self.start),
            "end": iso(self.end),
            "baseline": self.baseline,
            "assignments": [a.to_dict() for a in self.assignments],
            "outcomes": [o.to_dict() for o in self.outcomes],
            "counts": {
                "resolved": self.resolved,
                "mitigation": self.mitigation,
                "needs_confirmation": self.needs_confirmation,
                "capacity_short": self.capacity_short,
                "infeasible": self.infeasible,
                "not_actionable": self.not_actionable,
            },
        }


@dataclass(frozen=True)
class HandoverRecord:
    generated_at: str
    as_of: str
    scope: str
    missing_objects: tuple[dict[str, Any], ...]
    per_object: tuple[dict[str, Any], ...]
    aggregates: dict[str, Any]
    overall: dict[str, Any]
    issues: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "as_of": self.as_of,
            "scope": self.scope,
            "missing_objects": list(self.missing_objects),
            "per_object": list(self.per_object),
            "aggregates": self.aggregates,
            "overall": self.overall,
            "issues": list(self.issues),
        }


def _aggregate(key: str, objects: list[ObjectAssessment]) -> Aggregate:
    def count(risk: Risk) -> int:
        return sum(1 for o in objects if o.risk is risk)

    return Aggregate(
        key=key,
        objects=len(objects),
        treatment_complete=sum(1 for o in objects if o.treatment_complete),
        full_delivery=sum(1 for o in objects if o.full_delivery),
        on_time_delivery=sum(1 for o in objects if o.on_time_delivery),
        late_delivery=sum(1 for o in objects if o.full_delivery and o.late),
        confirmed_missed=count(Risk.CONFIRMED_MISSED),
        evidence_gap=count(Risk.EVIDENCE_GAP),
        window_critical=count(Risk.WINDOW_CRITICAL),
        at_risk=count(Risk.AT_RISK),
        on_track=count(Risk.ON_TRACK),
        complete=count(Risk.COMPLETE),
    )


def iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def ev_dict(evidence: Evidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    value = evidence.value
    if isinstance(value, date):
        value = value.isoformat()
    return {"state": evidence.state.value, "raw": evidence.raw, "value": value}
