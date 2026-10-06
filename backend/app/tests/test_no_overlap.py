"""Essential test 4: no asset and no crew member ever has overlapping assignments.

This checks the property directly against every generated plan, using an interval
sweep that is independent of both the CP-SAT model and the auditor, so a bug in
either of those cannot hide a real double-booking.
"""

from __future__ import annotations

from collections import defaultdict

from app.domain.schemas import Assignment


def _overlapping_pairs(assignments, key) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """Return mission-id pairs whose intervals overlap for a shared resource key.

    Intervals are half-open ``[takeoff, return)``, so back-to-back sorties on one
    asset are legal and must not be reported here.
    """
    by_resource: dict[object, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        for resource in key(assignment):
            by_resource[resource].append(assignment)

    conflicts: list[tuple[str, str]] = []
    for _resource, group in by_resource.items():
        ordered = sorted(group, key=lambda a: (a.takeoff_minute, a.return_minute, a.mission_id))
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.takeoff_minute < earlier.return_minute:
                conflicts.append((earlier.mission_id, later.mission_id))
    return conflicts


def _asset_keys(assignment: Assignment) -> tuple[str, ...]:
    return (assignment.asset_id,)


def _crew_keys(assignment: Assignment) -> tuple[str, ...]:
    return assignment.crew_ids


def test_coverage_plan_has_no_asset_or_crew_overlap(coverage_plan) -> None:
    assert _overlapping_pairs(coverage_plan.assignments, _asset_keys) == []
    assert _overlapping_pairs(coverage_plan.assignments, _crew_keys) == []


def test_every_frontier_variant_has_no_overlap(initial_frontier) -> None:
    _state, frontier = initial_frontier
    for built in frontier.plans:
        assignments = built.plan.assignments
        assert _overlapping_pairs(assignments, _asset_keys) == [], (
            f"{built.variant.value} double-books an asset"
        )
        assert _overlapping_pairs(assignments, _crew_keys) == [], (
            f"{built.variant.value} double-books a crew member"
        )


def test_no_mission_appears_twice_in_a_plan(coverage_plan) -> None:
    mission_ids = [a.mission_id for a in coverage_plan.assignments]
    assert len(mission_ids) == len(set(mission_ids))


def test_each_crew_member_is_listed_once_per_assignment(coverage_plan) -> None:
    for assignment in coverage_plan.assignments:
        assert len(assignment.crew_ids) == len(set(assignment.crew_ids))


def test_no_overlap_after_each_scripted_disruption(initial_frontier) -> None:
    """Re-planning under every scripted event must not introduce a double-booking.

    This is the case that matters: the frozen/locked/free split means the solver only
    sees part of the problem, so a mistake there could overlap a new assignment with a
    locked or frozen one.
    """
    from app.services import disruption_monitor
    from app.services.frontier_explorer import build_frontier

    state, frontier = initial_frontier
    parent = frontier.plans[0].plan

    for key in sorted(disruption_monitor.SCRIPTED_EVENTS):
        kind, _title, payload, _description = disruption_monitor.resolve_scripted_event(key)
        application = disruption_monitor.apply_event(
            state, kind, payload, label=_title, source="test", active_plan=parent
        )
        revised = build_frontier(
            application.state,
            scenario_id=state.scenario_id,
            version=2,
            parent_plan=parent,
            affected_mission_ids=set(application.affected_mission_ids),
        )
        assert len(revised.plans) == 3, f"{key}: expected three re-plan options"
        for built in revised.plans:
            assignments = built.plan.assignments
            assert _overlapping_pairs(assignments, _asset_keys) == [], f"{key}/{built.variant.value}"
            assert _overlapping_pairs(assignments, _crew_keys) == [], f"{key}/{built.variant.value}"


def test_greedy_fallback_has_no_overlap(initial_state) -> None:
    """The fallback path must respect rule 7 too, not just the CP-SAT path."""
    from app.domain.enums import PlanVariant
    from app.services.planning_engine import PlanningRequest, solve_greedy

    request = PlanningRequest(
        state=initial_state,
        variant=PlanVariant.COVERAGE_FIRST,
        scenario_id=initial_state.scenario_id,
    )
    selected, notes = solve_greedy(request)
    assignments = [option.to_assignment() for option in selected]
    assert _overlapping_pairs(assignments, _asset_keys) == []
    assert _overlapping_pairs(assignments, _crew_keys) == []
    assert notes, "the fallback path must explain itself in notes"
