"""Essential test 5: frozen / already-started assignments never change after a disruption.

An assignment with ``takeoff_minute <= now`` has departed. Re-planning must keep it
byte-identical: same asset, same crew, same times. This is the property that makes it
safe to re-plan at all while sorties are in the air.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.enums import PlanVariant
from app.services import disruption_monitor
from app.services.frontier_explorer import build_frontier
from app.services.plan_auditor import audit_plan
from app.services.planning_engine import PlanningRequest, run_planning
from app.services.stability_guard import (
    frozen_assignments,
    frozen_mission_ids,
    split_locked_and_free,
    verify_frozen_held,
)

ALL_VARIANTS = (
    PlanVariant.COVERAGE_FIRST,
    PlanVariant.SAFETY_FIRST,
    PlanVariant.STABILITY_FIRST,
)


def _replan(state, parent, affected, *, now, variant):
    """Run one variant against a clone of ``state`` whose clock is at ``now``.

    A clone is used because ``initial_frontier`` is session-scoped: advancing the clock
    on the shared object would leak into every later test and make results order-dependent.
    """
    state = state.clone()
    state.now_minute = now
    frozen = frozen_assignments(parent, now)
    locked, free, frozen_ids = split_locked_and_free(state, parent, affected, now)
    request = PlanningRequest(
        state=state,
        variant=variant,
        scenario_id=state.scenario_id,
        version=2,
        parent_plan=parent,
        frozen=frozen,
        locked_missions=locked,
        free_mission_ids=free,
    )
    result = run_planning(request, coverage_reference=parent.metrics.weighted_coverage)
    # Return the state that was actually solved against, so callers auditing the result
    # use the same clock position rather than the caller's un-advanced original.
    return result, state


def test_frozen_assignments_are_carried_into_every_revised_plan(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    assert parent.assignments, "the coverage plan must contain assignments to freeze"

    now = 120
    frozen = frozen_assignments(parent, now)
    assert frozen, f"no assignment departs at or before minute {now}; the test would be vacuous"

    for key in sorted(disruption_monitor.SCRIPTED_EVENTS):
        kind, title, payload, _ = disruption_monitor.resolve_scripted_event(key)
        application = disruption_monitor.apply_event(
            state, kind, payload, label=title, source="test", active_plan=parent
        )
        for variant in ALL_VARIANTS:
            result, _solved_state = _replan(
                application.state,
                parent,
                set(application.affected_mission_ids),
                now=now,
                variant=variant,
            )
            check = verify_frozen_held(parent, result.assignments, now)
            assert check.holds, f"{key}/{variant}: {check.violations}"
            assert check.frozen_count == len(frozen)


def test_frozen_assignment_keeps_its_exact_signature(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    now = 120
    frozen = {a.mission_id: a for a in frozen_assignments(parent, now)}
    assert frozen

    for key in sorted(disruption_monitor.SCRIPTED_EVENTS):
        kind, title, payload, _ = disruption_monitor.resolve_scripted_event(key)
        application = disruption_monitor.apply_event(
            state, kind, payload, label=title, source="test", active_plan=parent
        )
        # build_frontier reads now_minute off the state, so the clock has to be advanced
        # before it runs; otherwise nothing is considered frozen and the test is vacuous.
        # Work on a clone: `state` is session-scoped and shared with other tests.
        application.state.now_minute = now
        revised = build_frontier(
            application.state,
            scenario_id=state.scenario_id,
            version=2,
            parent_plan=parent,
            affected_mission_ids=set(application.affected_mission_ids),
        )
        for built in revised.plans:
            by_mission = {a.mission_id: a for a in built.plan.assignments}
            for mission_id, original in frozen.items():
                assert mission_id in by_mission, (
                    f"{key}/{built.variant.value}: frozen {mission_id} disappeared"
                )
                assert by_mission[mission_id].signature() == original.signature(), (
                    f"{key}/{built.variant.value}: frozen {mission_id} was modified"
                )
                assert by_mission[mission_id].takeoff_minute == original.takeoff_minute
                assert by_mission[mission_id].asset_id == original.asset_id
                assert set(by_mission[mission_id].crew_ids) == set(original.crew_ids)


def test_frozen_assignment_is_marked_frozen_in_the_output(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    now = 120
    frozen_ids = frozen_mission_ids(parent, now)
    assert frozen_ids

    kind, title, payload, _ = disruption_monitor.resolve_scripted_event("EVENT_A")
    application = disruption_monitor.apply_event(
        state, kind, payload, label=title, source="test", active_plan=parent
    )
    result, _solved_state = _replan(
        application.state, parent, set(application.affected_mission_ids),
        now=now, variant=PlanVariant.COVERAGE_FIRST,
    )
    for assignment in result.assignments:
        if assignment.mission_id in frozen_ids:
            assert assignment.is_frozen is True
            assert assignment.takeoff_minute <= now


def test_verify_frozen_held_detects_a_dropped_assignment(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    now = 120
    frozen = frozen_assignments(parent, now)
    assert frozen

    survivors = [a for a in parent.assignments if a.mission_id != frozen[0].mission_id]
    check = verify_frozen_held(parent, survivors, now)
    assert not check.holds
    assert any(frozen[0].mission_id in violation for violation in check.violations)


def test_verify_frozen_held_detects_a_retimed_assignment(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    now = 120
    frozen = frozen_assignments(parent, now)
    target = frozen[0]

    mutated = [
        dataclasses.replace(a, takeoff_minute=a.takeoff_minute + 45)
        if a.mission_id == target.mission_id
        else a
        for a in parent.assignments
    ]
    check = verify_frozen_held(parent, mutated, now)
    assert not check.holds
    assert any("delayed" in v or "changed" in v for v in check.violations)


def test_a_frozen_asset_fault_does_not_silently_retime_the_sortie(initial_frontier) -> None:
    """An asset faulted mid-sortie finishes its frozen sortie, then goes to repair.

    EVENT_A grounds an asset. If the sortie had already departed, the re-plan must not
    move it earlier or cancel it.
    """
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan

    # Move the clock to the latest takeoff so as many sorties as possible have departed.
    now = max(a.takeoff_minute for a in parent.assignments)
    frozen = frozen_assignments(parent, now)
    assert len(frozen) == len(parent.assignments), "all sorties should have departed by now"

    kind, title, payload, _ = disruption_monitor.resolve_scripted_event("EVENT_A")
    application = disruption_monitor.apply_event(
        state, kind, payload, label=title, source="test", active_plan=parent
    )
    result, solved_state = _replan(
        application.state, parent, set(application.affected_mission_ids),
        now=now, variant=PlanVariant.COVERAGE_FIRST,
    )
    by_mission = {a.mission_id: a for a in result.assignments}
    for assignment in frozen:
        replacement = by_mission.get(assignment.mission_id)
        assert replacement is not None, f"{assignment.mission_id} was dropped mid-sortie"
        assert replacement.takeoff_minute == assignment.takeoff_minute
        assert replacement.return_minute == assignment.return_minute
        assert replacement.asset_id == assignment.asset_id

    # Audit against the state that was actually solved against, at the same clock
    # position. `application.state` still sits at minute 0, where these sorties have not
    # yet departed and the grounded-asset fault would (correctly) be an error.
    report = audit_plan(solved_state, result.assignments)
    assert report.valid, report.messages()


@pytest.mark.parametrize("clock", [0, 60, 120, 180, 240, 300])
def test_frozen_holds_at_every_clock_position(initial_frontier, clock) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan

    kind, title, payload, _ = disruption_monitor.resolve_scripted_event("EVENT_C")
    application = disruption_monitor.apply_event(
        state, kind, payload, label=title, source="test", active_plan=parent
    )
    result, _solved_state = _replan(
        application.state, parent, set(application.affected_mission_ids),
        now=clock, variant=PlanVariant.COVERAGE_FIRST,
    )
    check = verify_frozen_held(parent, result.assignments, clock)
    assert check.holds, f"clock={clock}: {check.violations}"


def test_frozen_hold_is_vacuous_but_not_wrong_when_nothing_has_departed(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    check = verify_frozen_held(parent, parent.assignments, 0)
    assert check.holds
    assert check.frozen_count == 0
    assert "0 frozen" in check.summary
