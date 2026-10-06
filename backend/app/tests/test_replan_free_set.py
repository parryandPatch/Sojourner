"""Which missions a re-plan is allowed to touch.

The spec says re-planning reconsiders "only missions invalidated or materially affected by
the event". That restriction is what keeps a re-plan cheap and what keeps an untouched
sortie from being shuffled for no reason — but it has an edge case that is easy to get
wrong, and getting it wrong is invisible until you read the assignment list.

The edge case is a mission the parent plan did **not** assign. It has no parent assignment
to keep, so there is nothing to protect by holding it still. Locking it would forbid the
re-plan from ever picking it up, and a mission that was infeasible when the parent was
built may be flyable now that the clock has moved, a repair has finished, or a weather
window has opened. That is exactly the improvement a revision exists to find, and locking
the mission would make it permanently unreachable.

These tests pin the rule: hold what the parent did, re-open what it did not.
"""

from __future__ import annotations

import pytest

from app.domain.enums import PlanVariant
from app.services import disruption_monitor
from app.services.frontier_explorer import build_frontier
from app.services.stability_guard import split_locked_and_free

from .conftest import build_state

ALL_VARIANTS = (
    PlanVariant.COVERAGE_FIRST,
    PlanVariant.SAFETY_FIRST,
    PlanVariant.STABILITY_FIRST,
)


def _split(parent, state, affected=(), now=0):
    return split_locked_and_free(state, parent, set(affected), now)


def test_a_mission_the_parent_skipped_is_free_not_locked() -> None:
    """The core rule. Locking an unassigned mission would orphan it forever."""
    state = build_state(now_minute=0)
    frontier = build_frontier(state, scenario_id="SCN-TEST", version=1)
    parent = frontier.plans[-1].plan  # the option that leaves the most missions uncovered

    skipped = [m for m in state.missions if m not in {a.mission_id for a in parent.assignments}]
    assert skipped, "this option must skip at least one mission for the test to mean anything"

    locked, free, _frozen = _split(parent, state)
    for mission_id in skipped:
        assert mission_id not in locked, (
            f"{mission_id} has no parent assignment, so locking it forbids a re-plan "
            f"from ever assigning it"
        )
        assert mission_id in free


def test_a_mission_the_parent_did_assign_is_locked_when_it_is_still_valid() -> None:
    state = build_state(now_minute=0)
    parent = build_frontier(state, scenario_id="SCN-TEST", version=1).plans[0].plan
    locked, _free, _frozen = _split(parent, state)
    for assignment in parent.assignments:
        assert assignment.mission_id in locked, (
            f"{assignment.mission_id} was assigned and still valid; a re-plan must keep it "
            f"rather than treat it as a free decision"
        )


def test_locked_and_free_partition_every_mission_exactly_once() -> None:
    state = build_state(now_minute=0)
    parent = build_frontier(state, scenario_id="SCN-TEST", version=1).plans[0].plan
    locked, free, frozen = _split(parent, state)

    assert locked.isdisjoint(free)
    assert frozen.isdisjoint(locked)
    assert frozen.isdisjoint(free)
    # Anything frozen is neither locked nor free: it is carried across untouched.
    assert locked | free == set(state.missions) - frozen


def test_affected_missions_are_free_even_when_the_parent_assigned_them() -> None:
    state = build_state(now_minute=0)
    parent = build_frontier(state, scenario_id="SCN-TEST", version=1).plans[0].plan
    assigned = {a.mission_id for a in parent.assignments}
    target = sorted(assigned)[0]

    locked, free, _frozen = _split(parent, state, affected=[target])
    assert target in free
    assert target not in locked


def test_a_revision_never_covers_fewer_missions_than_its_parent(initial_frontier) -> None:
    """The contract the unlocked-mission rule exists to protect.

    Across every scripted event, every parent option, and every clock position, a revision
    must not lose ground. Before the fix it could not gain ground either — a mission the
    parent skipped was locked, so the revision was structurally incapable of improving —
    which is what this asserts now that it no longer is.
    """
    state, frontier = initial_frontier
    gained = 0
    for key in sorted(disruption_monitor.SCRIPTED_EVENTS):
        for parent in [built.plan for built in frontier.plans]:
            for now in (60, 120, 180, 240, 300):
                kind, title, payload, _ = disruption_monitor.resolve_scripted_event(key)
                application = disruption_monitor.apply_event(
                    state, kind, payload, label=title, source="test", active_plan=parent
                )
                disrupted = build_frontier(
                    application.state,
                    scenario_id=state.scenario_id,
                    version=2,
                    parent_plan=parent,
                    affected_mission_ids=set(application.affected_mission_ids),
                ).plans[0].plan

                later = application.state.clone()
                later.now_minute = now
                revision = build_frontier(
                    later,
                    scenario_id=state.scenario_id,
                    version=3,
                    parent_plan=disrupted,
                    affected_mission_ids=set(),
                ).plans[0].plan

                assert revision.metrics.missions_covered >= disrupted.metrics.missions_covered, (
                    f"{key}/{parent.variant.value}@{now}: the revision covered "
                    f"{revision.metrics.missions_covered} missions against the disrupted "
                    f"plan's {disrupted.metrics.missions_covered}"
                )
                gained += revision.metrics.missions_covered > disrupted.metrics.missions_covered

    # Not every combination can improve — most unassigned missions are infeasible for hard
    # reasons — but at least one must, or the unlocked-mission rule is inert.
    assert gained, (
        "no revision anywhere improved on its parent, so the fix that lets a re-plan reach "
        "an unassigned mission is having no effect at all"
    )


def test_a_revision_still_never_moves_a_mission_it_was_not_told_to_move() -> None:
    """Opening up the unassigned missions must not become a licence to reshuffle."""
    state = build_state(now_minute=0)
    parent = build_frontier(state, scenario_id="SCN-TEST", version=1).plans[0].plan
    kind, title, payload, _ = disruption_monitor.resolve_scripted_event("EVENT_A")
    application = disruption_monitor.apply_event(
        state, kind, payload, label=title, source="test", active_plan=parent
    )
    later = application.state.clone()
    later.now_minute = 200

    revision = build_frontier(
        later,
        scenario_id="SCN-TEST",
        version=3,
        parent_plan=parent,
        affected_mission_ids=set(application.affected_mission_ids),
    )

    affected = set(application.affected_mission_ids)
    for built in revision.plans:
        by_mission = {a.mission_id: a for a in built.plan.assignments}
        for assignment in parent.assignments:
            if assignment.mission_id in affected:
                continue
            if assignment.mission_id not in by_mission:
                # Dropping an unaffected mission is allowed only if it became infeasible.
                continue
            assert by_mission[assignment.mission_id].signature() == assignment.signature(), (
                f"{built.variant.value} moved {assignment.mission_id}, which the event did "
                f"not affect"
            )


def test_revisions_never_drop_a_mission_the_parent_had_assigned(initial_frontier) -> None:
    """Whatever is added, nothing that was flying is quietly dropped."""
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    revision = build_frontier(
        state,
        scenario_id=state.scenario_id,
        version=2,
        parent_plan=parent,
        affected_mission_ids=set(),
    )
    for built in revision.plans:
        assigned = {a.mission_id for a in built.plan.assignments}
        for assignment in parent.assignments:
            assert assignment.mission_id in assigned, (
                f"{built.variant.value} dropped {assignment.mission_id}"
            )


@pytest.mark.parametrize("now", [0, 60, 120, 200, 300])
def test_the_free_set_is_exactly_the_unassigned_missions_when_nothing_is_affected(
    initial_frontier, now
) -> None:
    """With no event attached, the free set is precisely what the parent left open.

    That is the whole point of the rule: hold everything the parent did, re-open exactly
    what it did not do. When the parent covered every mission the free set is empty and a
    revision can only reproduce its parent, which is correct — there is nothing to decide.
    """
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    _locked, free, frozen = split_locked_and_free(state.clone(), parent, set(), now)

    expected = set(state.missions) - {a.mission_id for a in parent.assignments} - frozen
    assert free == frozenset(expected), (
        f"at minute {now} the free set was {sorted(free)}, expected {sorted(expected)}"
    )
    if not expected:
        # Nothing was open, so the three options can only agree. Assert the reporting says
        # so rather than leaving the reviewer to wonder why the cards look identical.
        revision = build_frontier(
            state.clone(), scenario_id=state.scenario_id, version=2, parent_plan=parent,
        )
        assert revision.distinct_count == 1
        assert any("identical" in note for note in revision.notes)
        assert revision.free_mission_count == 0
