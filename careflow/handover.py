"""交接快照与专项支援前后对比。

目标：

1. 复原原来各馆看到的交付缺口（按馆/材料/处理类型聚合，并保留逐件记录）；
2. 校验快照在重新读取后仍能复现相同结果；
3. 对比专项支援改变了哪些逐件结果和截止原因。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import Assessment, HandoverRecord, ObjectAssessment

_GAP_KEYS = (
    "risk",
    "treatment_complete",
    "full_delivery",
    "on_time_delivery",
    "late",
    "treatment_gap",
    "delivery_gap",
    "due_at",
)


def _scope_objects(assessment: Assessment, scope: str | None) -> list[ObjectAssessment]:
    """某馆当前应当看到的藏品。

    判定标准是该馆仍是藏品的当前保管馆，或还有未完成工序登记在该馆。
    已经完成全部在该馆工序、并交接出去的藏品不会再出现在原馆的交付缺口里；
    但同一藏品的护理链在评估结果中仍是同一件。
    """

    if not scope or scope in {"all", "全部"}:
        return list(assessment.objects)
    result = []
    for obj in assessment.objects:
        if obj.active_site == scope:
            result.append(obj)
            continue
        if any(
            step.site == scope and step.state.value != "done"
            for step in obj.steps
        ):
            result.append(obj)
    return result


def _gap_view(obj: ObjectAssessment) -> dict[str, Any]:
    return {
        "object_id": obj.object_id,
        "sites": list(obj.sites),
        "active_site": obj.active_site,
        "material": obj.material,
        "treatment_type": obj.treatment_type,
        "batch": obj.batch,
        "risk": obj.risk.value,
        "treatment_complete": obj.treatment_complete,
        "full_delivery": obj.full_delivery,
        "on_time_delivery": obj.on_time_delivery,
        "late": obj.late,
        "treatment_gap": list(obj.treatment_gap),
        "delivery_gap": list(obj.delivery_gap),
        "evidence_gaps": list(obj.evidence_gaps),
        "due_at": obj.due_at.isoformat() if obj.due_at else None,
        "days_to_due": obj.days_to_due,
        "reasons": [r.to_dict() for r in obj.risk_reasons],
    }


def _subset_assessment(assessment: Assessment, objects: Iterable[ObjectAssessment]) -> Assessment:
    return Assessment(
        as_of=assessment.as_of,
        objects=tuple(objects),
        issues=assessment.issues,
        protocol=assessment.protocol,
    )


def build_snapshot(
    assessment: Assessment,
    *,
    scope: str | None = None,
    generated_at: date | str | None = None,
) -> HandoverRecord:
    """生成可直接写盘、再读回校验的交接快照。"""

    objects = _scope_objects(assessment, scope)
    subset = _subset_assessment(assessment, objects)
    aggregates = {
        dim: {k: v.to_dict() for k, v in values.items()}
        for dim, values in subset.aggregates().items()
    }
    missing = tuple(
        {
            "object_id": obj.object_id,
            "treatment_gap": list(obj.treatment_gap),
            "delivery_gap": list(obj.delivery_gap),
            "evidence_gaps": list(obj.evidence_gaps),
        }
        for obj in objects
        if obj.delivery_gap or obj.evidence_gaps or obj.confirmed_missed
    )
    if isinstance(generated_at, date):
        generated = generated_at.isoformat()
    else:
        generated = generated_at or assessment.as_of.isoformat()
    return HandoverRecord(
        generated_at=generated,
        as_of=assessment.as_of.isoformat(),
        scope=scope or "all",
        missing_objects=missing,
        per_object=tuple(_gap_view(obj) for obj in objects),
        aggregates=aggregates,
        overall=subset.overall().to_dict(),
        issues=tuple(i.to_dict() for i in assessment.issues),
    )


def save_snapshot(record: HandoverRecord, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def load_snapshot(path: str | Path) -> HandoverRecord:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return HandoverRecord(
        generated_at=data["generated_at"],
        as_of=data["as_of"],
        scope=data["scope"],
        missing_objects=tuple(data["missing_objects"]),
        per_object=tuple(data["per_object"]),
        aggregates=data["aggregates"],
        overall=data["overall"],
        issues=tuple(data["issues"]),
    )


def verify_snapshot(assessment: Assessment, record: HandoverRecord, *, tol: int = 0) -> list[str]:
    """把快照与现场重算结果比对，返回差异描述（空列表表示一致）。

    ``tol`` 只作用于计数聚合中允许的舍入差，默认零容忍。
    """

    problems: list[str] = []
    if record.as_of != assessment.as_of.isoformat():
        problems.append(f"as_of 不一致: 快照 {record.as_of} != 现场 {assessment.as_of.isoformat()}")
    fresh = build_snapshot(assessment, scope=None if record.scope == "all" else record.scope)
    if len(record.per_object) != len(fresh.per_object):
        problems.append(f"逐件记录数量不一致: 快照 {len(record.per_object)} != 现场 {len(fresh.per_object)}")
    fresh_by_id = {item["object_id"]: item for item in fresh.per_object}
    for item in record.per_object:
        current = fresh_by_id.get(item["object_id"])
        if current is None:
            problems.append(f"{item['object_id']} 在快照中存在, 现场缺失")
            continue
        for key in _GAP_KEYS:
            if item.get(key) != current.get(key):
                problems.append(
                    f"{item['object_id']}.{key}: 快照 {item.get(key)!r} != 现场 {current.get(key)!r}"
                )
        old_codes = [r["code"] for r in item.get("reasons", [])]
        new_codes = [r["code"] for r in current.get("reasons", [])]
        if old_codes != new_codes:
            problems.append(f"{item['object_id']} 的截止原因不一致: {old_codes} != {new_codes}")
    for dim, values in record.aggregates.items():
        current = fresh.aggregates.get(dim, {})
        if set(values) != set(current):
            problems.append(f"聚合维度 {dim} 的分组不一致: {sorted(values)} != {sorted(current)}")
            continue
        for key, value in values.items():
            for field, number in value.items():
                if field == "key":
                    continue
                if abs(int(number) - int(current[key].get(field, 0))) > tol:
                    problems.append(
                        f"聚合 {dim}/{key}/{field}: 快照 {number} != 现场 {current[key].get(field)}"
                    )
    for field, number in record.overall.items():
        if field == "key":
            continue
        if abs(int(number) - int(fresh.overall.get(field, 0))) > tol:
            problems.append(f"总体 {field}: 快照 {number} != 现场 {fresh.overall.get(field)}")
    return problems


# ---------------------------------------------------------------------------
# 逐件差异：既可比较两个 Assessment，也可比较两个交接快照。
# ---------------------------------------------------------------------------


def _views(before_items: Iterable[Mapping[str, Any]], after_items: Iterable[Mapping[str, Any]]):
    old = {item["object_id"]: item for item in before_items}
    new = {item["object_id"]: item for item in after_items}
    return old, new


def diff_views(
    before_items: Iterable[Mapping[str, Any]],
    after_items: Iterable[Mapping[str, Any]],
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """比较两份逐件记录的视图（快照或 ``ObjectAssessment`` 的序列化结果）。"""

    old, new = _views(before_items, after_items)
    plan_outcomes = {o["object_id"]: o for o in (plan or {}).get("outcomes", [])}

    changed_outcomes: list[dict[str, Any]] = []
    changed_reasons: list[dict[str, Any]] = []
    unchanged = 0
    for object_id in sorted(set(old) | set(new)):
        left, right = old.get(object_id), new.get(object_id)
        if left is None or right is None:
            changed_outcomes.append(
                {
                    "object_id": object_id,
                    "before": left,
                    "after": right,
                    "change": "appeared" if left is None else "disappeared",
                }
            )
            continue
        left_view = {key: left.get(key) for key in _GAP_KEYS}
        right_view = {key: right.get(key) for key in _GAP_KEYS}
        touched = False
        if left_view != right_view:
            touched = True
            changed_outcomes.append(
                {
                    "object_id": object_id,
                    "before": left_view,
                    "after": right_view,
                    "plan_disposition": plan_outcomes.get(object_id, {}).get("disposition"),
                }
            )
        old_codes = {(r["code"], r["detail"]) for r in left.get("reasons", [])}
        new_codes = {(r["code"], r["detail"]) for r in right.get("reasons", [])}
        if old_codes != new_codes:
            touched = True
            old_by_code = {code: detail for code, detail in old_codes}
            new_by_code = {code: detail for code, detail in new_codes}
            added = [{"code": c, "detail": d} for c, d in sorted(new_codes - old_codes) if c not in old_by_code]
            removed = [{"code": c, "detail": d} for c, d in sorted(old_codes - new_codes) if c not in new_by_code]
            updated = [
                {"code": code, "before": old_by_code[code], "after": new_by_code[code]}
                for code in sorted(set(old_by_code) & set(new_by_code))
                if old_by_code[code] != new_by_code[code]
            ]
            changed_reasons.append(
                {
                    "object_id": object_id,
                    "added": added,
                    "removed": removed,
                    "updated": updated,
                }
            )
        if not touched:
            unchanged += 1
    return {
        "changed_outcomes": changed_outcomes,
        "changed_reasons": changed_reasons,
        "unchanged_objects": unchanged,
    }


def diff_assessments(
    before: Assessment,
    after: Assessment,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """对比专项支援前后的逐件结果与截止原因。"""

    result = diff_views(
        (_gap_view(obj) for obj in before.objects),
        (_gap_view(obj) for obj in after.objects),
        plan=plan,
    )
    return {
        "before_as_of": before.as_of.isoformat(),
        "after_as_of": after.as_of.isoformat(),
        **result,
    }


def diff_snapshots(
    before: HandoverRecord,
    after: HandoverRecord,
    *,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = diff_views(before.per_object, after.per_object, plan=plan)
    return {
        "before_as_of": before.as_of,
        "after_as_of": after.as_of,
        **result,
    }
