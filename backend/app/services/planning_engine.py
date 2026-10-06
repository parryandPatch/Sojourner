"""CP-SAT planning engine plus the deterministic greedy fallback.

Objectives (integer scaled)
---------------------------
    priority_weighted_coverage
  - risk_penalty
  - resource_cost_penalty
  - plan_change_penalty      (only when revising an active plan)

Priority weights: P1 = 100, P2 = 60, P3 = 35, P4 = 20, P5 = 10.
Fractional terms are multiplied by ``SCALE`` so every CP-SAT coefficient is an integer.

Hard modelling notes
--------------------
* One boolean per candidate option; at most one option per mission.
* Per-asset and per-crew optional fixed-size intervals under ``AddNoOverlap``.
* Per-hub, per-slot movement booleans bounded by runway capacity (reduced by any
  frozen assignment that already loads the slot).
* Frozen assignments (``takeoff_minute <= now``) are pinned: they keep their exact
  asset, crew and takeoff, and no other option may be chosen for that mission.
* Locked missions (unaffected by the disruption) may only keep their parent option.
"""

from __future__ import annotations

import dataclasses
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ortools.sat.python import cp_model

from app.domain.enums import ActorRole, PlanStatus, PlanVariant, ReasonCode
from app.domain.schemas import (
    Assignment,
    Mission,
    MissionStatus,
    OperationalState,
    Plan,
    PlanMetrics,
)
from app.services.feasibility_engine import (
    CandidateOption,
    FeasibilityContext,
    check_hub_capacity,
    enumerate_options,
)

SCALE = 1000
DEFAULT_RISK_WEIGHT = 6
DEFAULT_COST_WEIGHT = 3
DEFAULT_CHANGE_WEIGHT = 8
SAFETY_RISK_WEIGHT = 900
SAFETY_COST_WEIGHT = 0
STABILITY_CHANGE_WEIGHT = 40
STABILITY_RISK_WEIGHT = 6
STABILITY_COST_WEIGHT = 1
COVERAGE_RETENTION_FLOOR = 0.85

WEIGHTS: dict[PlanVariant, tuple[int, int, int]] = {
    # variant -> (risk_weight, cost_weight, change_weight)
    PlanVariant.COVERAGE_FIRST: (DEFAULT_RISK_WEIGHT, DEFAULT_COST_WEIGHT, DEFAULT_CHANGE_WEIGHT),
    PlanVariant.SAFETY_FIRST: (SAFETY_RISK_WEIGHT, SAFETY_COST_WEIGHT, 0),
    PlanVariant.STABILITY_FIRST: (STABILITY_RISK_WEIGHT, STABILITY_COST_WEIGHT, STABILITY_CHANGE_WEIGHT),
}


@dataclass(slots=True)
class PlanningRequest:
    state: OperationalState
    variant: PlanVariant
    scenario_id: str
    version: int = 1
    parent_plan: Plan | None = None
    frozen: tuple[Assignment, ...] = ()
    locked_missions: frozenset[str] = frozenset()
    free_mission_ids: frozenset[str] | None = None
    coverage_floor: float | None = None
    time_limit_seconds: float = 8.0
    plan_id: str | None = None
    # Assignment set the plan-change penalty is measured against. Defaults to the parent
    # plan; for an initial plan (no parent) the frontier supplies the deterministic greedy
    # baseline so STABILITY FIRST still has something concrete to stay close to.
    stability_reference: dict[str, Assignment] | None = None

    def is_free(self, mission: Mission) -> bool:
        if mission.id in self.locked_missions:
            return False
        if self.free_mission_ids is not None:
            return mission.id in self.free_mission_ids
        return True


@dataclass(slots=True)
class SolveResult:
    assignments: tuple[Assignment, ...]
    metrics: PlanMetrics
    solver_status: str
    solve_time_ms: int
    is_fallback: bool
    notes: list[str] = field(default_factory=list)
    selected_option_ids: list[str] = field(default_factory=list)

    def to_plan(
        self,
        plan_id: str,
        variant: PlanVariant,
        status: PlanStatus = PlanStatus.DRAFT,
        created_at: datetime | None = None,
        parent_plan_id: str | None = None,
        approved_by: ActorRole | None = None,
        note: str = "",
    ) -> Plan:
        label = PlanVariant.FALLBACK if self.is_fallback else variant
        return Plan(
            id=plan_id,
            version=1,
            status=status,
            variant=label,
            assignments=self.assignments,
            metrics=self.metrics,
            created_at=created_at or datetime.now(UTC),
            parent_plan_id=parent_plan_id,
            approved_by=approved_by,
            is_fallback=self.is_fallback,
            solver_status=self.solver_status,
            solve_time_ms=self.solve_time_ms,
            note=note,
        )


# --------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------


def compute_metrics(
    state: OperationalState,
    assignments: tuple[Assignment, ...] | list[Assignment],
    *,
    parent: Plan | None = None,
    frozen: tuple[Assignment, ...] = (),
) -> PlanMetrics:
    missions = [m for m in state.missions.values() if m.status != MissionStatus.CANCELLED]
    covered_ids = {a.mission_id for a in assignments}
    weighted = sum(m.priority_weight for m in missions if m.id in covered_ids)
    total_weight = sum(m.priority_weight for m in missions)
    p1_total = sum(1 for m in missions if m.priority == 1)
    p1_covered = sum(1 for m in missions if m.priority == 1 and m.id in covered_ids)
    risks = [a.risk for a in assignments]
    mean_risk = sum(risks) / len(risks) if risks else 0.0
    max_risk = max(risks) if risks else 0.0
    changed = count_changed_assignments(parent, assignments) if parent else 0
    frozen_signatures = {a.signature() for a in frozen}
    return PlanMetrics(
        missions_total=len(missions),
        missions_covered=len(covered_ids),
        p1_covered=p1_covered,
        p1_total=p1_total,
        weighted_coverage=float(weighted),
        coverage_ratio=(weighted / total_weight) if total_weight else 0.0,
        mean_risk=mean_risk,
        max_risk=max_risk,
        changed_assignments=changed,
        frozen_assignments=sum(1 for a in assignments if a.signature() in frozen_signatures),
    )


def count_changed_assignments(parent: Plan | None, assignments: tuple[Assignment, ...] | list[Assignment]) -> int:
    """Number of assignment signatures that differ between two plans."""
    if parent is None:
        return 0
    parent_by_mission = parent.assignment_by_mission
    changed = 0
    seen: set[str] = set()
    for assignment in assignments:
        seen.add(assignment.mission_id)
        previous = parent_by_mission.get(assignment.mission_id)
        if previous is None or previous.signature() != assignment.signature():
            changed += 1
    for mission_id in parent_by_mission:
        if mission_id not in seen:
            changed += 1
    return changed


# --------------------------------------------------------------------------------------
# Option pool
# --------------------------------------------------------------------------------------


def build_option_pool(request: PlanningRequest) -> tuple[list[CandidateOption], dict[str, FeasibilityContext]]:
    """Enumerate valid options across all missions that the request allows to move."""
    state = request.state
    frozen = tuple(request.frozen)
    ctx = FeasibilityContext(now_minute=state.now_minute, committed=frozen)

    free = [m for m in state.missions_in_priority_order() if request.is_free(m)]
    locked = [m for m in state.missions_in_priority_order() if not request.is_free(m)]

    options_by_mission, _reports = enumerate_options(state, ctx, missions=free)

    pool: list[CandidateOption] = []
    for mission in free:
        pool.extend(options_by_mission.get(mission.id, []))
    for mission in locked:
        pool.extend(_locked_options(state, mission, request))

    # Deterministic global ordering; also used as the CP-SAT tie-break term.
    pool.sort(
        key=lambda o: (
            state.mission(o.mission_id).priority,
            o.mission_id,
            o.takeoff_minute,
            round(o.risk, 6),
            o.asset_id,
            o.crew_ids,
        )
    )
    _ = locked
    return pool, options_by_mission


def _locked_options(state: OperationalState, mission: Mission, request: PlanningRequest) -> list[CandidateOption]:
    """Rebuild the exact parent option for a locked mission, or nothing if there was none."""
    if request.parent_plan is None:
        return []
    previous = request.parent_plan.assignment_by_mission.get(mission.id)
    if previous is None:
        return []
    asset = state.assets.get(previous.asset_id)
    if asset is None:
        return []
    from app.services.feasibility_engine import cost_fraction as _cost

    crew = tuple(state.crews[c] for c in previous.crew_ids if c in state.crews)
    return [
        CandidateOption(
            mission_id=mission.id,
            asset_id=previous.asset_id,
            asset_class=asset.class_,
            crew_ids=tuple(sorted(previous.crew_ids)),
            payload_category=previous.payload_category,
            payload_units=previous.payload_units,
            takeoff_minute=previous.takeoff_minute,
            landing_minute=previous.landing_minute,
            return_minute=previous.return_minute,
            transit_minutes=previous.transit_minutes,
            risk=previous.risk,
            risk_breakdown=previous.risk_breakdown,
            cost_fraction=_cost(asset, crew, previous.sortie_minutes),
            reason_codes=previous.reason_codes or (ReasonCode.NO_TIME_OVERLAP,),
        )
    ]


# --------------------------------------------------------------------------------------
# CP-SAT model
# --------------------------------------------------------------------------------------


def _frozen_load(state: OperationalState, frozen: tuple[Assignment, ...]) -> dict[tuple[str, int], int]:
    load: dict[tuple[str, int], int] = defaultdict(int)
    for assignment in frozen:
        hub = state.hub(state.mission(assignment.mission_id).origin_hub_id)
        for minute in (assignment.takeoff_minute, assignment.return_minute):
            load[(hub.id, minute // hub.slot_minutes)] += 1
    return dict(load)


def _optional_interval(
    model: cp_model.CpModel, presence: cp_model.IntVar, option: CandidateOption, name: str
) -> cp_model.IntervalVar:
    """Optional interval with a fixed start/size, guarded by ``presence``.

    Built from explicitly fixed integer variables because OR-Tools requires
    IntVar bounds rather than plain literals for optional intervals.
    """
    start = model.NewIntVar(option.takeoff_minute, option.takeoff_minute, f"{name}.start")
    size = model.NewIntVar(option.sortie_minutes, option.sortie_minutes, f"{name}.size")
    end = model.NewIntVar(option.return_minute, option.return_minute, f"{name}.end")
    return model.NewOptionalIntervalVar(start, size, end, presence, name)


def solve_with_cpsat(
    request: PlanningRequest,
    pool: list[CandidateOption],
    *,
    coverage_reference: float | None = None,
) -> tuple[list[CandidateOption], str, int, list[str]]:
    state = request.state
    risk_weight, cost_weight, change_weight = WEIGHTS[request.variant]
    parent_by_mission = request.parent_plan.assignment_by_mission if request.parent_plan else {}
    # The change penalty is measured against the stability reference, which for an
    # initial plan is the deterministic greedy baseline rather than a parent plan.
    change_reference = (
        request.stability_reference
        if request.stability_reference is not None
        else parent_by_mission
    )

    model = cp_model.CpModel()
    variables: dict[int, cp_model.IntVar] = {}
    coefficients: list[int] = []

    for index, option in enumerate(pool):
        variable = model.NewBoolVar(f"x{index}")
        variables[index] = variable
        coverage = state.mission(option.mission_id).priority_weight * SCALE
        risk_points = round(option.risk * SCALE) * risk_weight
        cost_points = round(option.cost_fraction * SCALE) * cost_weight
        previous = change_reference.get(option.mission_id)
        changed = 1 if (previous is not None and previous.signature() != option.to_assignment().signature()) else 0
        change_points = change_weight * SCALE * changed
        # `- index` is a deterministic tie-break so repeated runs return identical plans.
        coefficients.append(coverage - risk_points - cost_points - change_points - index)

    # At most one option per mission.
    options_by_mission: dict[str, list[int]] = defaultdict(list)
    for index, option in enumerate(pool):
        options_by_mission[option.mission_id].append(index)
    for indices in options_by_mission.values():
        # NOTE: the deprecated CamelCase wrappers do NOT flatten a list argument; passing
        # one silently builds an empty constraint. Always use the snake_case API here.
        model.add_at_most_one(*[variables[i] for i in indices])

    # Locked missions may only repeat their parent option (or stay unassigned).
    for mission_id in request.locked_missions:
        indices = options_by_mission.get(mission_id, [])
        if not indices:
            continue
        if len(indices) == 1:
            model.Add(variables[indices[0]] == 1)
        else:
            # More than one rebuild means the parent assignment could not be reproduced.
            for index in indices[1:]:
                model.Add(variables[index] == 0)

    # No-overlap per asset and, critically, per individual crew member (not per crew set:
    # two different teams sharing a pilot would otherwise double-book that pilot).
    for resource, owners_of in (("asset", lambda o: (o.asset_id,)), ("crew", lambda o: o.crew_ids)):
        intervals: dict[str, list[cp_model.IntervalVar]] = defaultdict(list)
        for index, option in enumerate(pool):
            interval = _optional_interval(model, variables[index], option, f"{resource}:{index}")
            for owner in owners_of(option):
                intervals[owner].append(interval)
        for items in intervals.values():
            if len(items) > 1:
                model.add_no_overlap(items)

    # Hub runway capacity per slot, reduced by any frozen load already in the slot.
    # Frozen missions are pinned below and still appear in the pool, so their movements are
    # already counted by `_frozen_load`; counting them again here would double-charge the
    # slot and could drive the model INFEASIBLE.
    frozen_slots = _frozen_load(state, request.frozen)
    frozen_mission_ids = {a.mission_id for a in request.frozen}
    slot_members: dict[tuple[str, int], list[int]] = defaultdict(list)
    for index, option in enumerate(pool):
        if option.mission_id in frozen_mission_ids:
            continue
        hub = state.hub(state.mission(option.mission_id).origin_hub_id)
        for minute in option.hub_movements():
            slot_members[(hub.id, minute // hub.slot_minutes)].append(index)
    for slot, indices in slot_members.items():
        hub_id, _slot_index = slot
        hub = state.hub(hub_id)
        effective = hub.runway_capacity_per_slot - frozen_slots.get(slot, 0)
        if effective <= 0:
            for index in indices:
                model.Add(variables[index] == 0)
            continue
        if len(indices) <= effective:
            continue
        movement = []
        for index in indices:
            flag = model.NewBoolVar(f"mv{slot[0]}:{slot[1]}:{index}")
            # Equality, not just an upper bound: a one-sided implication would let the
            # solver zero the flag for a selected option and void the capacity bound.
            model.Add(flag == variables[index])
            movement.append(flag)
        model.Add(sum(movement) <= effective)

    # Frozen assignments: pin the exact option, or forbid the mission entirely.
    pinned_notes: list[str] = []
    for assignment in request.frozen:
        match = None
        for index in options_by_mission.get(assignment.mission_id, []):
            option = pool[index]
            if option.asset_id == assignment.asset_id and option.takeoff_minute == assignment.takeoff_minute:
                match = index
                break
        if match is None:
            for index in options_by_mission.get(assignment.mission_id, []):
                model.Add(variables[index] == 0)
            pinned_notes.append(
                f"Frozen assignment for {assignment.mission_id} could not be reproduced; mission held out."
            )
        else:
            for index in options_by_mission[assignment.mission_id]:
                model.Add(variables[index] == (1 if index == match else 0))

    # Coverage floor for the safety/stability variants.
    floor_notes: list[str] = []
    if request.coverage_floor and coverage_reference is not None:
        # NOTE: the constraint sums priority_weight * SCALE, so the floor must be scaled
        # to match. Leaving it unscaled would make the floor trivially satisfiable and the
        # safety/stability variants would silently drop almost every mission.
        required = int(round(coverage_reference * request.coverage_floor)) * SCALE
        model.Add(
            sum(
                state.mission(pool[i].mission_id).priority_weight * SCALE * variables[i]
                for i in range(len(pool))
            )
            >= required
        )
        floor_notes.append(
            f"Coverage constrained to >= {required // SCALE} weighted points "
            f"({int(request.coverage_floor * 100)}% of coverage-first {coverage_reference:.0f})."
        )

    model.Maximize(sum(coefficients[i] * variables[i] for i in range(len(pool))))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.5, request.time_limit_seconds)
    solver.parameters.num_search_workers = 1  # determinism over raw speed
    solver.parameters.random_seed = 0
    solver.parameters.log_search_progress = False

    started = time.perf_counter()
    status = solver.Solve(model)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    status_name = solver.StatusName(status)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [], status_name, elapsed_ms, pinned_notes + floor_notes

    selected = [pool[i] for i in range(len(pool)) if solver.Value(variables[i]) == 1]
    selected.sort(key=lambda o: (o.mission_id, o.takeoff_minute, o.asset_id))
    return selected, status_name, elapsed_ms, pinned_notes + floor_notes


# --------------------------------------------------------------------------------------
# Deterministic greedy fallback
# --------------------------------------------------------------------------------------


def solve_greedy(request: PlanningRequest) -> tuple[list[CandidateOption], list[str]]:
    """Deterministic greedy plan. Used when CP-SAT cannot produce a feasible solution."""
    state = request.state
    notes = ["Deterministic greedy fallback used (labelled FALLBACK); the solver did not return a solution."]

    committed: list[Assignment] = list(request.frozen)

    selected: list[CandidateOption] = []
    parent_by_mission = request.parent_plan.assignment_by_mission if request.parent_plan else {}

    def load_of(hub_id: str, minute: int) -> int:
        hub = state.hub(hub_id)
        key = (hub.id, minute // hub.slot_minutes)
        return sum(
            1
            for a in committed
            for m in (a.takeoff_minute, a.return_minute)
            if state.mission(a.mission_id).origin_hub_id == hub_id
            and m // hub.slot_minutes == key[1]
        )

    ordered_missions = [
        m
        for m in state.missions_in_priority_order()
        if request.is_free(m) and m.id not in {a.mission_id for a in request.frozen}
    ]
    # Preserve locked missions from the parent plan.
    for mission_id in sorted(request.locked_missions):
        previous = parent_by_mission.get(mission_id)
        if previous is not None:
            committed.append(previous)
            selected.append(
                CandidateOption(
                    mission_id=previous.mission_id,
                    asset_id=previous.asset_id,
                    asset_class=state.asset(previous.asset_id).class_,
                    crew_ids=tuple(sorted(previous.crew_ids)),
                    payload_category=previous.payload_category,
                    payload_units=previous.payload_units,
                    takeoff_minute=previous.takeoff_minute,
                    landing_minute=previous.landing_minute,
                    return_minute=previous.return_minute,
                    transit_minutes=previous.transit_minutes,
                    risk=previous.risk,
                    risk_breakdown=previous.risk_breakdown,
                    cost_fraction=0.0,
                    reason_codes=previous.reason_codes or (),
                )
            )

    for mission in ordered_missions:
        # The committed set must be rebuilt for every mission. A context captured once
        # before the loop would not see the options already chosen here, so rule 7
        # (no asset/crew overlap) would be silently violated by the fallback path.
        ctx = FeasibilityContext(now_minute=state.now_minute, committed=tuple(committed))
        options, _reports = enumerate_options(state, ctx, missions=[mission])
        candidates = sorted(
            options.get(mission.id, []),
            key=lambda o: (round(o.risk, 6), o.takeoff_minute, o.asset_id, o.crew_ids),
        )
        chosen: CandidateOption | None = None
        for option in candidates:
            hub = state.hub(mission.origin_hub_id)
            if not check_hub_capacity(
                mission.origin_hub_id,
                option.hub_movements(),
                hub.slot_minutes,
                hub.runway_capacity_per_slot,
                {
                    (mission.origin_hub_id, minute // hub.slot_minutes): load_of(mission.origin_hub_id, minute)
                    for minute in option.hub_movements()
                },
            ):
                continue
            chosen = option
            break
        if chosen is None:
            continue
        selected.append(chosen)
        committed.append(chosen.to_assignment())

    selected.sort(key=lambda o: (o.mission_id, o.takeoff_minute, o.asset_id))
    return selected, notes


# --------------------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------------------


def run_planning(
    request: PlanningRequest,
    *,
    allow_fallback: bool = True,
    coverage_reference: float | None = None,
) -> SolveResult:
    """Solve one trade-off variant and return the resulting plan payload.

    ``coverage_reference`` is the coverage-first weighted coverage the safety/stability
    floors are measured against. It must be passed for those variants, otherwise the
    floor constraint cannot be built.
    """
    state = request.state
    pool, _by_mission = build_option_pool(request)

    selected, solver_status, elapsed_ms, notes = solve_with_cpsat(
        request, pool, coverage_reference=coverage_reference
    )
    is_fallback = False

    if not selected and allow_fallback:
        selected, fallback_notes = solve_greedy(request)
        notes = notes + fallback_notes
        is_fallback = True
        solver_status = f"{solver_status}->GREEDY"
    elif not selected:
        notes.append("No options available and the greedy fallback is disabled.")

    assignments = tuple(
        sorted(
            (option.to_assignment() for option in selected),
            key=lambda a: (a.mission_id, a.takeoff_minute, a.asset_id),
        )
    )

    # Frozen assignments must appear in the final plan even when the solver dropped them,
    # and every frozen assignment must carry `is_frozen=True`. The flag has to be forced
    # here rather than trusted from the option: a frozen sortie can also be re-emitted
    # through the locked/free mechanism, in which case the option's own flag is False and
    # the UI would wrongly present an airborne assignment as still negotiable.
    frozen_by_mission = {a.mission_id: a for a in request.frozen}
    present = {a.mission_id for a in assignments}
    normalised: list[Assignment] = []
    for assignment in assignments:
        original = frozen_by_mission.get(assignment.mission_id)
        if original is not None:
            # Use the parent's own values, not the solver's, so nothing can drift.
            normalised.append(
                dataclasses.replace(original, is_frozen=True)
            )
        else:
            normalised.append(assignment)
    for frozen_assignment in request.frozen:
        if frozen_assignment.mission_id not in present:
            normalised.append(dataclasses.replace(frozen_assignment, is_frozen=True))

    assignments = tuple(sorted(normalised, key=lambda a: (a.mission_id, a.takeoff_minute, a.asset_id)))
    metrics = compute_metrics(state, assignments, parent=request.parent_plan, frozen=request.frozen)
    return SolveResult(
        assignments=assignments,
        metrics=metrics,
        solver_status=solver_status,
        solve_time_ms=elapsed_ms,
        is_fallback=is_fallback,
        notes=notes,
        selected_option_ids=[f"{o.mission_id}:{o.asset_id}:{o.takeoff_minute}" for o in selected],
    )


def coverage_floor_for(coverage_reference: float, floor: float = COVERAGE_RETENTION_FLOOR) -> float:
    """Weighted-coverage floor used by the safety and stability variants."""
    return coverage_reference * floor