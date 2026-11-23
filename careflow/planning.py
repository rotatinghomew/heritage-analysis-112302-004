"""两周支援方案比较。

支援班组的排程必须同时满足：

* 技能与所在馆匹配，且在班组差旅窗口与当日容量内；
* 处置完成后的静置下限与复检上限（处置实际完成才重置阶段时限）；
* 同一批次不拆给不同班组；
* 在途护理链保持锁定，正式交还后才可重新占用；
* 支援后仍留有合规复检窗口；
* 若馆方已腾出场地，场地容量也占用一个名额。

``compare_plans`` 额外跑一个“无限产能”压力方案，用来把积压分成
“兼容班组可消掉”“需馆方确认”“即使增加一个班组也来不及”。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Mapping

from .model import (
    Assessment,
    Assignment,
    Bundle,
    Disposition,
    ObjectAssessment,
    Outcome,
    Plan,
    Protocol,
    Reason,
    Risk,
    SiteSpace,
    StepAssessment,
    TeamWindow,
)

_SUPPORT_STEPS = ("treat", "recheck")


def _clamp_teams(teams: Iterable[TeamWindow], start: date, end: date) -> list[TeamWindow]:
    result = []
    for team in teams:
        clamped = team.clamped(start, end)
        if clamped is not None:
            result.append(clamped)
    return result


def _stress_teams(assessment: Assessment, start: date, end: date) -> list[TeamWindow]:
    """压力方案：每个馆每个技能都有一个不限产能的班组。"""

    teams: list[TeamWindow] = []
    sites = sorted({obj.active_site for obj in assessment.objects if obj.active_site})
    for site in sites:
        for skill in _SUPPORT_STEPS:
            teams.append(TeamWindow("EXTRA", site, skill, start, end, 10**6))
    return teams


def _site_space_ok(spaces: list[SiteSpace], site: str | None, day: date, used: dict[tuple[str, date], int]) -> bool:
    if not site or not spaces:
        return True
    site_spaces = [s for s in spaces if s.site == site]
    if not site_spaces:
        return True
    return any(
        space.available_from <= day <= space.available_to
        and used.get((site, day), 0) < space.capacity
        for space in site_spaces
    )


def _team_slots(
    teams: list[TeamWindow],
    used: dict[tuple[str, date], int],
    site: str | None,
    skill: str,
    low: date,
    high: date,
) -> list[tuple[TeamWindow, date]]:
    slots: list[tuple[TeamWindow, date]] = []
    for team in teams:
        if team.skill != skill:
            continue
        if site and team.site != site:
            continue
        day = max(team.start, low)
        last = min(team.end, high)
        while day <= last:
            if used.get((team.team, day), 0) < team.capacity:
                slots.append((team, day))
            day += timedelta(days=1)
    slots.sort(key=lambda item: (item[1], item[0].team))
    return slots


def _first_slot(
    teams: list[TeamWindow],
    used: dict[tuple[str, date], int],
    site: str | None,
    skill: str,
    low: date,
    high: date,
    spaces: list[SiteSpace],
    space_used: dict[tuple[str, date], int],
    *,
    team_name: str | None = None,
) -> tuple[TeamWindow, date] | None:
    for team, day in _team_slots(teams, used, site, skill, low, high):
        if team_name and team.team != team_name:
            continue
        if not _site_space_ok(spaces, site, day, space_used):
            continue
        return team, day
    return None


def _recheck_bounds(
    obj: ObjectAssessment,
    treat_day: date,
    protocol: Protocol,
    *,
    mitigation: bool = False,
    end: date | None = None,
) -> tuple[date, date] | None:
    """给定处置完成日，返回可行的复检窗口。

    减损排程（``mitigation``）只保留静置下限，不再声称满足原安全上限：
    对象已被确认错过窗口，目标是尽快处置与复检，而不是恢复合规。
    """

    earliest = treat_day + timedelta(days=protocol.quiet_min_days)
    if mitigation:
        latest = end or treat_day + timedelta(days=protocol.recheck_max_days)
        return (earliest, latest) if earliest <= latest else None
    latest = treat_day + timedelta(days=protocol.recheck_max_days)
    if obj.recheck is not None:
        # 由检查限制过的上限仍然有效；旧计划截止若早于静置下限，说明阶段时限
        # 已随实际处置完成而重置，不再采用。
        if obj.recheck.latest is not None and obj.recheck.treatment_done is not None:
            latest = min(latest, obj.recheck.latest)
        elif obj.recheck.latest is not None and obj.recheck.planned is None:
            latest = min(latest, obj.recheck.latest)
    if earliest > latest:
        return None
    return earliest, latest


def _unit_needs(obj: ObjectAssessment) -> list[StepAssessment]:
    needed: list[StepAssessment] = []
    for step in obj.steps:
        if step.step not in _SUPPORT_STEPS:
            continue
        if step.state.value in {"done", "pending_confirmation", "missing_evidence", "unknown_evidence"}:
            continue
        if step.step == "recheck" and step.state.value == "scheduled":
            # 馆方已把复检排在合规窗口内时，支援不必重复占用名额；
            # 只有排期本身不合规（过早/过晚）才需要改派。
            if obj.recheck is not None and obj.recheck.state == "planned":
                continue
        needed.append(step)
    return needed


def _units(objects: Iterable[ObjectAssessment]) -> list[list[ObjectAssessment]]:
    """按批次合并为不可拆分的单元。"""

    units: dict[str, list[ObjectAssessment]] = {}
    singles: list[list[ObjectAssessment]] = []
    for obj in objects:
        if obj.batch:
            units.setdefault(obj.batch, []).append(obj)
        else:
            singles.append([obj])
    return singles + list(units.values())


def _preblock(obj: ObjectAssessment) -> tuple[Disposition, Reason] | None:
    if obj.risk is Risk.COMPLETE:
        return Disposition.COMPLETE, Reason("already_complete", "处置与交付均已完成", "info")
    if obj.evidence_gaps:
        # 缺证不等于逾期：进度能否算完成都还不确定，只能等馆方确认。
        return Disposition.NEEDS_CONFIRMATION, Reason(
            "evidence_gap",
            f"缺证据步骤: {', '.join(obj.evidence_gaps)}；需馆方确认后才能排支援，"
            "在确认前不占用通过件数名额",
            "warn",
        )
    if obj.transfer_state in {"in_transit", "received"}:
        return Disposition.NOT_ACTIONABLE, Reason(
            "in_flight_locked",
            "工序已被接收馆接手且在途，正式交还前不得重新占用这条护理链",
            "block",
        )
    if obj.urgent_transfer_needs_confirmation:
        return Disposition.NEEDS_CONFIRMATION, Reason(
            "urgent_transfer_dual_confirmation",
            "紧急转馆尚未取得原馆与接收馆共同确认，不能排入支援",
            "block",
        )
    if obj.confirmed_missed:
        # 规程：经确认已超过安全上限的对象优先占用合格班组。这里不拦截排程，
        # 结果会记为“减损支援”，不进入通过件数。
        return None
    return None


def _schedule(
    assessment: Assessment,
    bundle: Bundle,
    start: date,
    end: date,
    teams: list[TeamWindow],
    *,
    unlimited: bool = False,
) -> tuple[list[Assignment], list[Outcome]]:
    protocol = bundle.protocol
    spaces = [] if unlimited else list(bundle.spaces)
    used: dict[tuple[str, date], int] = {}
    space_used: dict[tuple[str, date], int] = {}

    preblocked: dict[str, tuple[Disposition, Reason]] = {}
    needs: dict[str, list[StepAssessment]] = {}
    for obj in assessment.objects:
        blocked = _preblock(obj)
        if blocked is not None:
            preblocked[obj.object_id] = blocked
        else:
            needs[obj.object_id] = _unit_needs(obj)

    # 批次完整性：任一批次成员被前置阻塞，整批不得拆分。已因自身原因被阻塞的
    # 成员保留原原因（例如在途锁定），其余成员转入待确认。
    batch_members: dict[str, list[str]] = {}
    for obj in assessment.objects:
        if obj.batch:
            batch_members.setdefault(obj.batch, []).append(obj.object_id)
    for batch, members in batch_members.items():
        blocked_members = [
            m for m in members if m in preblocked and preblocked[m][0] is not Disposition.COMPLETE
        ]
        if not blocked_members:
            continue
        for member in members:
            # 已因自身原因被阻塞的成员保留原原因，其余成员才需要因批次完整性转入待确认。
            if member in preblocked:
                continue
            reason = Reason(
                "batch_not_splittable",
                f"批次 {batch} 中 {', '.join(blocked_members)} 未就绪；同一批次不能拆开处置",
                "warn",
            )
            preblocked[member] = (Disposition.NEEDS_CONFIRMATION, reason)
            needs.pop(member, None)

    actionable = [obj for obj in assessment.objects if obj.object_id in needs and needs[obj.object_id]]
    actionable.sort(key=lambda o: (o.priority, o.due_at or date.max, o.object_id))

    assignments: list[Assignment] = []
    treat_days: dict[str, date] = {}
    recheck_days: dict[str, date] = {}
    assigned_steps: dict[str, list[str]] = {}
    failed: dict[str, tuple[Disposition, Reason]] = {}

    for unit in _units(actionable):
        result = _schedule_unit(
            unit, needs, teams, used, space_used, spaces, start, end, protocol,
            mitigation=all(obj.confirmed_missed for obj in unit),
        )
        if result[0] is None:
            _, disposition, reason = result  # type: ignore[misc]
            for obj in unit:
                failed[obj.object_id] = (disposition, reason)
            continue
        unit_assignments, unit_treat, unit_recheck = result
        assignments.extend(unit_assignments)
        treat_days.update(unit_treat)
        recheck_days.update(unit_recheck)
        for obj in unit:
            assigned_steps[obj.object_id] = [s.step for s in needs[obj.object_id]]

    outcomes: list[Outcome] = []
    for obj in assessment.objects:
        object_id = obj.object_id
        if object_id in preblocked:
            disposition, reason = preblocked[object_id]
            outcomes.append(
                Outcome(
                    object_id=object_id,
                    disposition=disposition,
                    reasons=(reason,),
                    delivered_by=obj.due_at if disposition is Disposition.COMPLETE else None,
                )
            )
            continue
        if object_id in failed:
            disposition, reason = failed[object_id]
            outcomes.append(Outcome(object_id=object_id, disposition=disposition, reasons=(reason,)))
            continue

        treat_day = treat_days.get(object_id)
        if treat_day is None and obj.recheck is not None:
            treat_day = obj.recheck.treatment_done
        recheck_day = recheck_days.get(object_id)
        if recheck_day is None and obj.recheck is not None and obj.recheck.satisfied is True:
            recheck_day = obj.recheck.planned

        reasons: list[Reason] = []
        if obj.recheck is not None and obj.recheck.satisfied is True:
            reasons.append(Reason("recheck_already_satisfied", "复检已满足窗口", "info"))

        steps_left = needs.get(object_id, [])
        if obj.batch:
            reasons.append(Reason("batch_kept_together", f"批次 {obj.batch} 保持同一班组处置", "info"))
        scheduled = bool(assigned_steps.get(object_id))

        if obj.confirmed_missed:
            if scheduled:
                reasons.append(
                    Reason(
                        "confirmed_missed_mitigation_only",
                        "已确认错过安全复检窗口；此次为减损支援，不计入通过件数",
                        "block",
                    )
                )
                outcomes.append(
                    Outcome(
                        object_id=object_id,
                        disposition=Disposition.MITIGATION_ASSIGNED,
                        reasons=tuple(reasons),
                        assigned_steps=tuple(assigned_steps[object_id]),
                        recheck_by=recheck_day,
                        compliance_restorable=False,
                        mitigation=True,
                    )
                )
            else:
                outcomes.append(
                    Outcome(
                        object_id=object_id,
                        disposition=Disposition.INFEASIBLE_EVEN_WITH_EXTRA,
                        reasons=(
                            Reason(
                                "confirmed_missed_no_capacity",
                                "已确认错过安全窗口，且两周内没有可派班组减损",
                                "block",
                            ),
                        ),
                        compliance_restorable=False,
                    )
                )
            continue

        if treat_day is not None and recheck_day is None:
            bounds = _recheck_bounds(obj, treat_day, protocol, end=end)
            if bounds is None:
                outcomes.append(
                    Outcome(
                        object_id=object_id,
                        disposition=Disposition.INFEASIBLE_EVEN_WITH_EXTRA,
                        reasons=(
                            Reason(
                                "no_recheck_after_support",
                                "支援后无法同时满足静置下限与复检上限",
                                "block",
                            ),
                        ),
                        compliance_restorable=False,
                    )
                )
                continue

        if recheck_day is not None and recheck_day <= end:
            disposition = Disposition.ASSIGNED
            delivered_by = recheck_day
            reasons.append(Reason("scheduled", f"最迟 {recheck_day.isoformat()} 可完成复检", "info"))
        elif recheck_day is not None:
            disposition = Disposition.INFEASIBLE_EVEN_WITH_EXTRA
            delivered_by = None
            reasons.append(
                Reason(
                    "recheck_outside_horizon",
                    f"复检 {recheck_day.isoformat()} 落在两周窗口之外",
                    "block",
                )
            )
        elif scheduled:
            disposition = Disposition.ASSIGNED
            delivered_by = treat_day
            reasons.append(Reason("scheduled_partial", "已排入支援，但完整交付仍需后续工序", "info"))
        elif steps_left:
            disposition = Disposition.CAPACITY_SHORT
            delivered_by = None
            reasons.append(Reason("capacity_short", "两周窗口内没有可用产能", "warn"))
        else:
            disposition = Disposition.NOT_ACTIONABLE
            delivered_by = None
            reasons.append(Reason("no_support_step", "剩余工序不由支援班组承担", "info"))

        outcomes.append(
            Outcome(
                object_id=object_id,
                disposition=disposition,
                reasons=tuple(reasons),
                assigned_steps=tuple(assigned_steps.get(object_id, [])),
                delivered_by=delivered_by,
                recheck_by=recheck_day,
            )
        )

    return assignments, outcomes


def _release(
    assignment: Assignment,
    used: dict[tuple[str, date], int],
    space_used: dict[tuple[str, date], int],
) -> None:
    key = (assignment.team, assignment.day)
    used[key] = max(0, used.get(key, 0) - 1)
    space_key = (assignment.site, assignment.day)
    space_used[space_key] = max(0, space_used.get(space_key, 0) - 1)


def _schedule_unit(
    unit: list[ObjectAssessment],
    needs: dict[str, list[StepAssessment]],
    teams: list[TeamWindow],
    used: dict[tuple[str, date], int],
    space_used: dict[tuple[str, date], int],
    spaces: list[SiteSpace],
    start: date,
    end: date,
    protocol: Protocol,
    *,
    mitigation: bool = False,
) -> tuple[list[Assignment], dict[str, date], dict[str, date]] | tuple[None, Disposition, Reason]:
    """为一个不可拆分单元找联合可行解：先定处置班组/日期，再在窗口内排复检。

    单元只含复检需求时，直接按复检班组与静置窗口排程。
    """

    needs_treat = any(any(s.step == "treat" for s in needs[o.object_id]) for o in unit)
    treat_site = next(
        (s.site for o in unit for s in needs[o.object_id] if s.step == "treat" and s.site),
        unit[0].active_site,
    )
    if not needs_treat:
        return _schedule_recheck_only(
            unit, needs, teams, used, space_used, spaces, start, end, protocol,
            mitigation=mitigation,
        )

    candidate_teams = sorted(
        {t.team for t in teams if t.skill == "treat" and (not treat_site or t.site == treat_site)}
    )
    # 已过的普通截止不构成硬阻断：晚做仍可完成，只是不再算准时；真正的硬约束
    # 是处置后的静置/复检安全窗口。这里只按“能否在窗口内完成交付”约束。
    high = end
    low = start

    reasons: list[Reason] = []
    for team_name in candidate_teams:
        cursor = low
        placements: list[Assignment] = []
        ok = True
        for obj in unit:
            step = next((s for s in needs[obj.object_id] if s.step == "treat"), None)
            if step is None:
                continue
            own_deadline = step.effective_deadline
            slot_high = high
            if own_deadline is not None and own_deadline >= start:
                # 准时优先：若截止仍在窗口内，尽量在截止前完成；否则允许晚做。
                slot_high = min(high, own_deadline)
            slot = _first_slot(
                teams, used, treat_site, "treat", max(cursor, low), slot_high,
                spaces, space_used, team_name=team_name,
            )
            if slot is None:
                ok = False
                break
            team, day = slot
            used[(team.team, day)] = used.get((team.team, day), 0) + 1
            space_used[(treat_site, day)] = space_used.get((treat_site, day), 0) + 1
            placements.append(
                Assignment(
                    object_id=obj.object_id,
                    step="treat",
                    team=team.team,
                    site=treat_site or team.site,
                    day=day,
                    window=(max(cursor, low), min(high, own_deadline)),
                    reason="在班组差旅窗口内，且保留批次完整与后续静置窗口",
                )
            )
            cursor = day
        if not ok:
            for assignment in placements:
                _release(assignment, used, space_used)
            continue

        # 处置日期确定后，为每个成员的复检找窗口。
        treat_map = {a.object_id: a.day for a in placements}
        recheck_placements: list[Assignment] = []
        ok = True
        for obj in unit:
            recheck_step = next((s for s in needs[obj.object_id] if s.step == "recheck"), None)
            if recheck_step is None:
                continue
            treat_day = treat_map.get(obj.object_id)
            if treat_day is None and obj.recheck is not None:
                treat_day = obj.recheck.treatment_done
            if treat_day is None:
                ok = False
                reasons.append(
                    Reason("treatment_date_missing", f"{obj.object_id} 的处置完成日期缺失", "block")
                )
                break
            bounds = _recheck_bounds(
                obj, treat_day, protocol, mitigation=mitigation or obj.confirmed_missed, end=end
            )
            if bounds is None:
                ok = False
                reasons.append(
                    Reason("no_recheck_window", f"{obj.object_id} 处置后无合规复检窗口", "block")
                )
                break
            slot = _first_slot(
                teams, used, obj.active_site, "recheck", bounds[0], min(bounds[1], end),
                spaces, space_used,
            )
            if slot is None:
                ok = False
                reasons.append(
                    Reason(
                        "no_recheck_slot",
                        f"{obj.object_id} 的复检窗口 {bounds[0].isoformat()} 至 "
                        f"{min(bounds[1], end).isoformat()} 没有兼容班组",
                        "warn",
                    )
                )
                break
            team, day = slot
            used[(team.team, day)] = used.get((team.team, day), 0) + 1
            space_used[(obj.active_site, day)] = space_used.get((obj.active_site, day), 0) + 1
            recheck_placements.append(
                Assignment(
                    object_id=obj.object_id,
                    step="recheck",
                    team=team.team,
                    site=obj.active_site or team.site,
                    day=day,
                    window=(bounds[0], min(bounds[1], end)),
                    reason="复检不早于静置下限、不晚于上限，且留有两周内的交付窗口",
                )
            )
        if ok:
            return (
                placements + recheck_placements,
                {a.object_id: a.day for a in placements},
                {a.object_id: a.day for a in recheck_placements},
            )
        for assignment in placements + recheck_placements:
            _release(assignment, used, space_used)

    # 无法安排：区分“没有兼容班组”与“有班组但档期/场地不足”。
    compatible = any(t.skill == "treat" and (not treat_site or t.site == treat_site) for t in teams)
    if not compatible:
        return None, Disposition.NEEDS_CONFIRMATION, Reason(
            "no_compatible_team", f"{treat_site or '该馆'} 没有可承担处置的班组", "warn"
        )
    detail = "；".join(r.detail for r in reasons) if reasons else "班组档期或场地容量不足"
    return None, Disposition.CAPACITY_SHORT, Reason("capacity_short", detail, "warn")


def _schedule_recheck_only(
    unit: list[ObjectAssessment],
    needs: dict[str, list[StepAssessment]],
    teams: list[TeamWindow],
    used: dict[tuple[str, date], int],
    space_used: dict[tuple[str, date], int],
    spaces: list[SiteSpace],
    start: date,
    end: date,
    protocol: Protocol,
    *,
    mitigation: bool = False,
) -> tuple[list[Assignment], dict[str, date], dict[str, date]] | tuple[None, Disposition, Reason]:
    placements: list[Assignment] = []
    reasons: list[Reason] = []
    for obj in unit:
        step = next((s for s in needs[obj.object_id] if s.step == "recheck"), None)
        if step is None:
            continue
        treat_day = obj.recheck.treatment_done if obj.recheck is not None else None
        if treat_day is None:
            reasons.append(Reason("treatment_date_missing", f"{obj.object_id} 的处置完成日期缺失", "block"))
            break
        bounds = _recheck_bounds(
            obj, treat_day, protocol, mitigation=mitigation or obj.confirmed_missed, end=end
        )
        if bounds is None:
            reasons.append(Reason("no_recheck_window", f"{obj.object_id} 处置后无合规复检窗口", "block"))
            break
        slot = _first_slot(
            teams, used, obj.active_site, "recheck", max(bounds[0], start), min(bounds[1], end),
            spaces, space_used,
        )
        if slot is None:
            reasons.append(
                Reason(
                    "no_recheck_slot",
                    f"{obj.object_id} 的复检窗口没有兼容班组或场地",
                    "warn",
                )
            )
            break
        team, day = slot
        used[(team.team, day)] = used.get((team.team, day), 0) + 1
        space_used[(obj.active_site, day)] = space_used.get((obj.active_site, day), 0) + 1
        placements.append(
            Assignment(
                object_id=obj.object_id,
                step="recheck",
                team=team.team,
                site=obj.active_site or team.site,
                day=day,
                window=(max(bounds[0], start), min(bounds[1], end)),
                reason="复检不早于静置下限、不晚于上限",
            )
        )
    if len(placements) == len([o for o in unit if any(s.step == "recheck" for s in needs[o.object_id])]):
        return placements, {}, {a.object_id: a.day for a in placements}
    for assignment in placements:
        _release(assignment, used, space_used)
    if reasons and all(r.code == "no_recheck_window" for r in reasons):
        return None, Disposition.INFEASIBLE_EVEN_WITH_EXTRA, reasons[0]
    if reasons:
        return None, Disposition.CAPACITY_SHORT, reasons[0]
    return None, Disposition.CAPACITY_SHORT, Reason("capacity_short", "复检班组档期不足", "warn")


def _plan_from(
    name: str,
    teams: list[TeamWindow],
    start: date,
    end: date,
    assignments: list[Assignment],
    outcomes: list[Outcome],
    *,
    baseline: bool = False,
) -> Plan:
    counts = {d: 0 for d in Disposition}
    for outcome in outcomes:
        counts[outcome.disposition] += 1
    resolved = counts[Disposition.ASSIGNED] + counts[Disposition.COMPLETE]
    return Plan(
        name=name,
        teams=tuple(sorted({t.team for t in teams})),
        start=start,
        end=end,
        assignments=tuple(assignments),
        outcomes=tuple(outcomes),
        resolved=resolved,
        mitigation=counts[Disposition.MITIGATION_ASSIGNED],
        needs_confirmation=counts[Disposition.NEEDS_CONFIRMATION],
        capacity_short=counts[Disposition.CAPACITY_SHORT],
        infeasible=counts[Disposition.INFEASIBLE_EVEN_WITH_EXTRA],
        not_actionable=counts[Disposition.NOT_ACTIONABLE],
        baseline=baseline,
    )


def plan_support(
    assessment: Assessment,
    bundle: Bundle,
    *,
    start: date | None = None,
    teams: Iterable[TeamWindow] | None = None,
    name: str = "支援方案",
    unlimited: bool = False,
) -> Plan:
    protocol = bundle.protocol
    start = start or assessment.as_of
    end = start + timedelta(days=max(protocol.horizon_days, 1) - 1)
    if unlimited:
        team_list = _stress_teams(assessment, start, end)
    elif teams is None:
        team_list = _clamp_teams(bundle.teams, start, end)
    else:
        team_list = _clamp_teams(teams, start, end)
    assignments, outcomes = _schedule(assessment, bundle, start, end, team_list, unlimited=unlimited)
    return _plan_from(name, team_list, start, end, assignments, outcomes)


def baseline_plan(assessment: Assessment, bundle: Bundle, *, start: date | None = None) -> Plan:
    """不投入支援时的交付缺口快照。"""

    protocol = bundle.protocol
    start = start or assessment.as_of
    end = start + timedelta(days=max(protocol.horizon_days, 1) - 1)
    outcomes: list[Outcome] = []
    for obj in assessment.objects:
        if obj.full_delivery:
            outcomes.append(
                Outcome(obj.object_id, Disposition.COMPLETE, (Reason("already_complete", "已完成", "info"),))
            )
        elif obj.evidence_gaps:
            outcomes.append(
                Outcome(
                    obj.object_id,
                    Disposition.NEEDS_CONFIRMATION,
                    (Reason("evidence_gap", "缺证据", "warn"),),
                )
            )
        elif obj.transfer_state in {"in_transit", "received"}:
            outcomes.append(
                Outcome(
                    obj.object_id,
                    Disposition.NOT_ACTIONABLE,
                    (Reason("in_flight_locked", "在途护理链，需正式交还", "block"),),
                )
            )
        elif obj.recheck is not None and obj.recheck.satisfied is True and obj.treatment_complete:
            outcomes.append(
                Outcome(
                    obj.object_id,
                    Disposition.NOT_ACTIONABLE,
                    (Reason("recheck_done", "仅剩馆方内部工序", "info"),),
                )
            )
        elif obj.recheck is not None and obj.recheck.state in {"missed", "impossible"}:
            outcomes.append(
                Outcome(
                    obj.object_id,
                    Disposition.INFEASIBLE_EVEN_WITH_EXTRA,
                    (Reason("no_recheck_window", "无合规复检窗口", "block"),),
                )
            )
        else:
            outcomes.append(
                Outcome(
                    obj.object_id,
                    Disposition.CAPACITY_SHORT,
                    (Reason("no_support", "当前没有投入支援班组", "warn"),),
                )
            )
    return _plan_from("不投入支援", [], start, end, [], outcomes, baseline=True)


def compare_plans(
    assessment: Assessment,
    bundle: Bundle,
    *,
    start: date | None = None,
    candidate_teams: Mapping[str, Iterable[TeamWindow]] | None = None,
) -> dict[str, object]:
    """比较两周内可行的支援方案，并给出推荐与逐类积压说明。"""

    start = start or assessment.as_of
    protocol = bundle.protocol
    end = start + timedelta(days=max(protocol.horizon_days, 1) - 1)

    plans: list[Plan] = [baseline_plan(assessment, bundle, start=start)]

    if candidate_teams is None:
        clamped = _clamp_teams(bundle.teams, start, end)
        by_team: dict[str, list[TeamWindow]] = {}
        for team in clamped:
            by_team.setdefault(team.team, []).append(team)
        candidates = dict(by_team)
        if len(candidates) > 1:
            candidates["全部班组"] = clamped
    else:
        candidates = {name: list(teams) for name, teams in candidate_teams.items()}

    for name, teams in candidates.items():
        plans.append(plan_support(assessment, bundle, start=start, teams=teams, name=f"投入 {name}"))

    stress = plan_support(assessment, bundle, start=start, name="无限产能压力测试", unlimited=True)

    plans.sort(key=lambda p: (p.baseline, -p.resolved, p.capacity_short, p.infeasible, p.name))
    best = next((p for p in plans if not p.baseline), None)

    categories: dict[str, list[dict[str, object]]] = {
        "resolved_by_compatible_team": [],
        "mitigation_only": [],
        "needs_confirmation": [],
        "capacity_short": [],
        "infeasible_even_with_extra": [],
        "not_actionable": [],
    }
    baseline = baseline_plan(assessment, bundle, start=start)
    for obj in assessment.objects:
        outcome = best.outcome(obj.object_id) if best else baseline.outcome(obj.object_id)
        stress_outcome = stress.outcome(obj.object_id)
        if outcome.disposition in (Disposition.ASSIGNED, Disposition.COMPLETE):
            bucket = "resolved_by_compatible_team"
        elif outcome.disposition is Disposition.NEEDS_CONFIRMATION:
            bucket = "needs_confirmation"
        elif obj.confirmed_missed or outcome.disposition is Disposition.MITIGATION_ASSIGNED:
            # 合规安全窗口已经失去；即便增派班组也只能减损。若连减损都排不进，
            # 归入“增派也来不及”。
            if stress_outcome.disposition is Disposition.MITIGATION_ASSIGNED:
                bucket = "mitigation_only"
            else:
                bucket = "infeasible_even_with_extra"
        elif stress_outcome.disposition is Disposition.INFEASIBLE_EVEN_WITH_EXTRA:
            bucket = "infeasible_even_with_extra"
        elif outcome.disposition is Disposition.NOT_ACTIONABLE:
            bucket = "not_actionable"
        else:
            bucket = "capacity_short"
        categories[bucket].append(
            {
                "object_id": obj.object_id,
                "risk": obj.risk.value,
                "batch": obj.batch,
                "active_site": obj.active_site,
                "disposition": outcome.disposition.value,
                "reasons": [r.to_dict() for r in outcome.reasons],
                "stress_disposition": stress_outcome.disposition.value,
                "stress_reasons": [r.to_dict() for r in stress_outcome.reasons],
                "baseline_disposition": baseline.outcome(obj.object_id).disposition.value,
            }
        )

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": protocol.horizon_days},
        "plans": [plan.to_dict() for plan in plans],
        "stress": stress.to_dict(),
        "recommended": best.name if best else None,
        "recommendation": _recommend(plans, best),
        "categories": categories,
    }


def _recommend(plans: list[Plan], best: Plan | None) -> str:
    if best is None:
        return "两周窗口内没有可投入的支援班组。"

    def kind(plan: Plan, disposition: Disposition) -> list[str]:
        return [o.object_id for o in plan.outcomes if o.disposition is disposition]

    baseline = next((p for p in plans if p.baseline), None)
    delta = best.resolved - (baseline.resolved if baseline else 0)
    parts = [f"推荐 {best.name}: 可解决 {best.resolved} 件，较不投入支援多 {delta} 件。"]
    if best.capacity_short:
        parts.append(f"仍有 {best.capacity_short} 件受产能限制: {', '.join(kind(best, Disposition.CAPACITY_SHORT))}。")
    if best.needs_confirmation:
        parts.append(
            f"{best.needs_confirmation} 件需馆方先确认: {', '.join(kind(best, Disposition.NEEDS_CONFIRMATION))}。"
        )
    if best.infeasible:
        parts.append(
            f"{best.infeasible} 件即使增加班组也来不及: {', '.join(kind(best, Disposition.INFEASIBLE_EVEN_WITH_EXTRA))}。"
        )
    return " ".join(parts)
