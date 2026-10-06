"""Stability guard.

Re-planning rules from the spec:

* Assignments with ``takeoff_minute <= now`` are frozen and cannot change.
* Only missions invalidated or materially affected by the event are reconsidered.
* Never automatically activate a re-plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.schemas import Assignment, OperationalState, Plan


def frozen_assignments(plan: Plan | None, now_minute: int) -> tuple[Assignment, ...]:
    """Assignments that have already departed (takeoff <= now)."""
    if plan is None:
        return ()
    return tuple(a for a in plan.assignments if a.takeoff_minute <= now_minute)


def future_assignments(plan: Plan | None, now_minute: int) -> tuple[Assignment, ...]:
    if plan is None:
        return ()
    return tuple(a for a in plan.assignments if a.takeoff_minute > now_minute)


def frozen_mission_ids(plan: Plan | None, now_minute: int) -> set[str]:
    return {a.mission_id for a in frozen_assignments(plan, now_minute)}


def change_count(parent: Plan | None, candidate: tuple[Assignment, ...] | list[Assignment]) -> int:
    from app.services.planning_engine import count_changed_assignments

    return count_changed_assignments(parent, candidate)


@dataclass(slots=True)
class StabilityCheck:
    holds: bool
    violations: list[str]
    frozen_count: int
    frozen_mission_ids: list[str]

    @property
    def summary(self) -> str:
        if self.holds:
            return f"All {self.frozen_count} frozen assignment(s) are unchanged."
        return "; ".join(self.violations)


def verify_frozen_held(
    parent: Plan | None,
    candidate: tuple[Assignment, ...] | list[Assignment],
    now_minute: int,
) -> StabilityCheck:
    """Assert that no frozen assignment was altered, dropped or re-timed."""
    frozen = frozen_assignments(parent, now_minute)
    candidate_by_mission = {a.mission_id: a for a in candidate}
    violations: list[str] = []

    for assignment in frozen:
        replacement = candidate_by_mission.get(assignment.mission_id)
        if replacement is None:
            violations.append(
                f"Frozen {assignment.mission_id} on {assignment.asset_id} was dropped from the revised plan."
            )
            continue
        if replacement.signature() != assignment.signature():
            violations.append(
                f"Frozen {assignment.mission_id} changed from {assignment.asset_id}@{assignment.takeoff_minute} "
                f"to {replacement.asset_id}@{replacement.takeoff_minute}."
            )
        if replacement.takeoff_minute > assignment.takeoff_minute:
            violations.append(
                f"Frozen {assignment.mission_id} was delayed from minute {assignment.takeoff_minute} "
                f"to {replacement.takeoff_minute}."
            )

    return StabilityCheck(
        holds=not violations,
        violations=violations,
        frozen_count=len(frozen),
        frozen_mission_ids=[a.mission_id for a in frozen],
    )


def still_feasible_in_state(
    state: OperationalState,
    assignment: Assignment,
) -> bool:
    """Re-check a single assignment against the *current* state.

    Used to decide whether a previously-planned (not yet departed) mission belongs to
    the reconsider set: if its assignment no longer validates, the mission is
    materially affected by the disruption and must be replanned.
    """
    from app.services.plan_auditor import audit_plan

    report = audit_plan(state, [assignment], strict_metrics=False)
    return report.valid


def split_locked_and_free(
    state: OperationalState,
    parent: Plan | None,
    affected_mission_ids: set[str],
    now_minute: int,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return ``(locked_missions, free_missions, frozen_missions)``.

    * frozen missions keep their assignment and are never offered to the solver;
    * affected missions are free to be re-planned;
    * every other mission keeps its parent assignment (locked), provided that
      assignment is still valid in the current state.
    """
    frozen_ids = frozen_mission_ids(parent, now_minute)
    locked: set[str] = set()
    free: set[str] = set()

    for mission_id in state.missions:
        if mission_id in frozen_ids:
            continue
        if mission_id in affected_mission_ids:
            free.add(mission_id)
            continue
        previous = parent.assignment_by_mission.get(mission_id) if parent else None
        if previous is not None and still_feasible_in_state(state, previous):
            locked.add(mission_id)
        elif previous is not None:
            # Materially invalidated by the event even though it was not flagged.
            free.add(mission_id)
        else:
            # The parent left this mission unassigned, so there is nothing to keep. Locking
            # it would forbid the re-plan from ever picking it up, which is precisely the
            # case a revision exists to improve: a mission that was infeasible last time may
            # be flyable now that the clock, the weather, or a repair has moved on.
            free.add(mission_id)

    return frozenset(locked), frozenset(free), frozenset(frozen_ids)