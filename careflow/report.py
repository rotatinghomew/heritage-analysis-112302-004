"""面向值班员与馆方的文字化视图。

图形式输出留给 JSON；这里把相同的评估结果排成可直接贴进值班记录的
摘要，避免各馆只报“自己这一步是否准时”。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Mapping

from .model import Assessment, Disposition, ObjectAssessment, Risk

_RISK_LABEL = {
    Risk.CONFIRMED_MISSED: "已确认错过安全窗口",
    Risk.WINDOW_CRITICAL: "复检窗口紧迫/冲突",
    Risk.AT_RISK: "截止临近",
    Risk.EVIDENCE_GAP: "证据不足待确认",
    Risk.ON_TRACK: "正常",
    Risk.COMPLETE: "已完成",
}

_DISPOSITION_LABEL = {
    Disposition.COMPLETE: "已完成",
    Disposition.ASSIGNED: "可支援",
    Disposition.MITIGATION_ASSIGNED: "减损支援",
    Disposition.NEEDS_CONFIRMATION: "需馆方确认",
    Disposition.CAPACITY_SHORT: "产能不足",
    Disposition.INFEASIBLE_EVEN_WITH_EXTRA: "增派也来不及",
    Disposition.NOT_ACTIONABLE: "不可改派",
}

_CATEGORY_LABEL = {
    "resolved_by_compatible_team": "兼容班组可消掉",
    "mitigation_only": "只能减损，不计通过",
    "needs_confirmation": "需馆方确认",
    "capacity_short": "产能不足",
    "infeasible_even_with_extra": "增派也来不及",
    "not_actionable": "在途/不可改派",
}


def _fmt_date(value: date | str | None) -> str:
    if value is None:
        return "-"
    return value.isoformat() if isinstance(value, date) else str(value)


def _gap_text(gaps: Iterable[str]) -> str:
    gaps = list(gaps)
    return "、".join(gaps) if gaps else "无"


def format_object(obj: ObjectAssessment) -> str:
    head = f"{obj.object_id}｜{obj.active_site or '-'}｜{_RISK_LABEL[obj.risk]}"
    if len(obj.sites) > 1:
        head += f"｜跨馆 {', '.join(obj.sites)}"
    lines = [
        head,
        f"  处置完成: {'是' if obj.treatment_complete else '否'}"
        f"（缺口 {_gap_text(obj.treatment_gap)}）；"
        f"完整交付: {'是' if obj.full_delivery else '否'}（缺口 {_gap_text(obj.delivery_gap)}）",
    ]
    if obj.recheck is not None and obj.recheck.state != "not_required":
        window = (
            f"{_fmt_date(obj.recheck.earliest)}~{_fmt_date(obj.recheck.latest)}"
            if obj.recheck.earliest
            else "未开始"
        )
        lines.append(f"  复检窗口: {window}（{obj.recheck.state}）")
    if obj.evidence_gaps:
        lines.append(f"  证据缺口: {_gap_text(obj.evidence_gaps)}")
    if obj.due_at is not None:
        suffix = f"，剩 {obj.days_to_due} 天" if obj.days_to_due is not None else ""
        lines.append(f"  最近有效截止: {obj.due_at.isoformat()}{suffix}")
    for reason in obj.risk_reasons:
        if reason.severity == "info":
            continue
        lines.append(f"  - {reason.detail}")
    return "\n".join(lines)


def format_assessment(assessment: Assessment, *, limit: int | None = None) -> str:
    overall = assessment.overall()
    lines = [
        f"评估基准日 {assessment.as_of.isoformat()}：{overall.objects} 件藏品",
        f"  处置完成 {overall.treatment_complete}｜完整交付 {overall.full_delivery}"
        f"｜准时交付 {overall.on_time_delivery}｜逾期完成 {overall.late_delivery}",
        f"  已确认错过 {overall.confirmed_missed}｜窗口紧迫 {overall.window_critical}"
        f"｜截止临近 {overall.at_risk}｜证据缺口 {overall.evidence_gap}"
        f"｜正常 {overall.on_track}｜完成 {overall.complete}",
    ]
    objects = list(assessment.objects)
    if limit is not None:
        objects = objects[:limit]
    lines.append("")
    lines.append("逐件：")
    for obj in objects:
        lines.append(format_object(obj))
        lines.append("")
    if assessment.issues:
        lines.append("数据提示：")
        for issue in assessment.issues:
            lines.append(f"  - [{issue.code}] {issue.message}")
    return "\n".join(lines).rstrip() + "\n"


def format_aggregates(assessment: Assessment, dimension: str) -> str:
    groups = assessment.aggregate(dimension)
    header = "| 分组 | 件数 | 处置完成 | 完整交付 | 准时交付 | 逾期完成 | 已确认错过 | 证据缺口 | 窗口紧迫 | 截止临近 |"
    lines = [header, "|" + "---|" * 10]
    for key, agg in groups.items():
        lines.append(
            f"| {key} | {agg.objects} | {agg.treatment_complete} | {agg.full_delivery} | "
            f"{agg.on_time_delivery} | {agg.late_delivery} | {agg.confirmed_missed} | "
            f"{agg.evidence_gap} | {agg.window_critical} | {agg.at_risk} |"
        )
    return "\n".join(lines) + "\n"


def format_plan(result: Mapping[str, Any], *, show_assignments: bool = False) -> str:
    window = result["window"]
    lines = [
        f"支援窗口 {window['start']} ~ {window['end']}（{window['days']} 天）",
        f"推荐：{result.get('recommended') or '无'}",
        f"说明：{result['recommendation']}",
        "",
        "| 方案 | 可解决 | 减损 | 需确认 | 产能不足 | 增派也来不及 | 不可改派 |",
        "|---|---|---|---|---|---|---|",
    ]
    for plan in result["plans"]:
        counts = plan["counts"]
        lines.append(
            f"| {plan['name']} | {counts['resolved']} | {counts['mitigation']} | "
            f"{counts['needs_confirmation']} | {counts['capacity_short']} | "
            f"{counts['infeasible']} | {counts['not_actionable']} |"
        )
    lines.append("")
    lines.append("积压归类：")
    for bucket, items in result["categories"].items():
        if not items:
            continue
        lines.append(f"  {_CATEGORY_LABEL.get(bucket, bucket)}：")
        for item in items:
            if bucket in {"capacity_short", "infeasible_even_with_extra", "mitigation_only"}:
                # 这些类别的结论来自压力方案：有限班组排不下、增派也来不及。
                reasons = item.get("stress_reasons") or item["reasons"]
            else:
                reasons = item["reasons"]
            reason = "；".join(r["detail"] for r in reasons)
            lines.append(f"    - {item['object_id']}：{reason}")
    if show_assignments:
        best = next((p for p in result["plans"] if p["name"] == result.get("recommended")), None)
        if best and best["assignments"]:
            lines.append("")
            lines.append(f"{best['name']} 的排程：")
            for assignment in best["assignments"]:
                lines.append(
                    f"  - {assignment['object_id']} {assignment['step']}："
                    f"{assignment['team']}@{assignment['site']} {assignment['day']}"
                )
    return "\n".join(lines) + "\n"


def format_handover(record) -> str:
    lines = [
        f"交接快照（{record.scope}）基准日 {record.as_of}，生成于 {record.generated_at}",
        f"  总体：{record.overall['objects']} 件，完整交付 {record.overall['full_delivery']}，"
        f"处置完成 {record.overall['treatment_complete']}",
    ]
    if record.missing_objects:
        lines.append("  交付缺口：")
        for item in record.missing_objects:
            lines.append(
                f"    - {item['object_id']}：处置缺口 {_gap_text(item['treatment_gap'])}，"
                f"交付缺口 {_gap_text(item['delivery_gap'])}"
            )
    return "\n".join(lines) + "\n"


def format_diff(result: Mapping[str, Any]) -> str:
    lines = [f"对比 {result['before_as_of']} → {result['after_as_of']}"]
    if not result["changed_outcomes"] and not result["changed_reasons"]:
        lines.append("  逐件结果与截止原因均无变化。")
        return "\n".join(lines) + "\n"
    for item in result["changed_outcomes"]:
        left = item.get("before") or {}
        right = item.get("after") or {}
        lines.append(
            f"  {item['object_id']}：{left.get('risk', '-')} → {right.get('risk', '-')}；"
            f"交付缺口 {_gap_text(left.get('delivery_gap', []))} → "
            f"{_gap_text(right.get('delivery_gap', []))}"
        )
    for item in result["changed_reasons"]:
        added = "；".join(r["code"] for r in item.get("added", [])) or "无"
        removed = "；".join(r["code"] for r in item.get("removed", [])) or "无"
        lines.append(f"  {item['object_id']} 截止原因：新增 {added}；消除 {removed}")
        for change in item.get("updated", []):
            lines.append(
                f"    · {change['code']}：{change['before']} → {change['after']}"
            )
    return "\n".join(lines) + "\n"
