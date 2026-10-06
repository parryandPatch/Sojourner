"""Frontier explorer: produces the three trade-off plans and the proposal diffs.

Order of work:

1. Solve COVERAGE FIRST to obtain the weighted-coverage reference.
2. Solve SAFETY FIRST with coverage constrained to >= 85% of that reference.
3. Solve STABILITY FIRST with the same coverage floor and a dominant change penalty.

If CP-SAT cannot produce a feasible solution inside the time budget, the
deterministic greedy fallback is used and the plan is labelled ``FALLBACK``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.config import get_settings
from app.domain.enums import ActorRole, EventKind, PlanStatus, PlanVariant
from app.domain.schemas import (
    Assignment,
    OperationalState,
    Plan,
    ProposalDiffOut,
)
from app.services import disruption_monitor, planning_engine, stability_guard
from app.services.explanation_service import (
    explain_plan_variant,
    explain_revision,
    render_plan_explanations,
)
from app.services.feasibility_engine import FeasibilityContext, enumerate_options
from app.services.plan_auditor import audit_plan
from app.services.planning_engine import (
    COVERAGE_RETENTION_FLOOR,
    PlanningRequest,
    SolveResult,
    run_planning,
)

VARIANT_ORDER: tuple[PlanVariant, ...] = (
    PlanVariant.COVERAGE_FIRST,
    PlanVariant.SAFETY_FIRST,
    PlanVariant.STABILITY_FIRST,
)


@dataclass(slots=True)
class BuiltPlan:
    variant: PlanVariant
    plan: Plan
    result: SolveResult
    audit_findings: list[str]
    audit_valid: bool

    @property
    def label(self) -> str:
        return (PlanVariant.FALLBACK if self.plan.is_fallback else self.variant).value


@dataclass(slots=True)
class Frontier:
    """The full trade-off set for one decision point."""

    plans: list[BuiltPlan]
    coverage_reference: float
    total_solve_ms: int
    notes: list[str] = field(default_factory=list)
    frozen_count: int = 0
    stability_summary: str = ""
    # How many missions the solve was actually free to re-decide. Recorded because it is the
    # thing that determines whether three genuinely different options can exist.
    free_mission_count: int = 0

    @property
    def distinct_signatures(self) -> list[tuple]:
        return [tuple(sorted(a.signature() for a in p.plan.assignments)) for p in self.plans]

    @property
    def distinct_count(self) -> int:
        return len(set(self.distinct_signatures))

    @property
    def duplicate_groups(self) -> tuple[tuple[str, ...], ...]:
        """Variant labels that produced byte-identical assignment sets.

        The spec asks for "exactly three distinct options". That is achievable whenever the
        state leaves enough decisions open, but it is not achievable on every state, and
        when it is not, the honest answer is to say so rather than perturb a plan until it
        merely looks different. A disruption typically leaves only the affected missions
        free to be re-decided (the spec's re-planning rule), so a single asset fault can
        leave a single decision -- at which point all three objectives genuinely agree and
        only one answer exists.

        Each entry is one group of two or more labels that collapsed onto each other.
        """
        groups: dict[tuple, list[str]] = {}
        for built, signature in zip(self.plans, self.distinct_signatures, strict=True):
            groups.setdefault(signature, []).append(built.label)
        return tuple(tuple(labels) for labels in groups.values() if len(labels) > 1)

    @property
    def duplicates_by_label(self) -> dict[str, tuple[str, ...]]:
        """Label -> the other labels it is identical to."""
        out: dict[str, tuple[str, ...]] = {}
        for labels in self.duplicate_groups:
            for label in labels:
                out[label] = tuple(other for other in labels if other != label)
        return out


def build_frontier(
    state: OperationalState,
    *,
    scenario_id: str,
    version: int,
    parent_plan: Plan | None = None,
    affected_mission_ids: set[str] | None = None,
    time_limit_seconds: float | None = None,
    trigger_label: str = "",
    plan_id_factory=None,
) -> Frontier:
    """Solve all three variants against ``state``."""
    settings = get_settings()
    limit = time_limit_seconds or settings.solver_time_limit_seconds
    now = state.now_minute

    frozen = stability_guard.frozen_assignments(parent_plan, now)
    if parent_plan is None:
        locked: frozenset[str] = frozenset()
        free: frozenset[str] | None = None
    else:
        locked, free, _frozen_ids = stability_guard.split_locked_and_free(
            state, parent_plan, affected_mission_ids or set(), now
        )

    notes: list[str] = []
    built: list[BuiltPlan] = []
    counter = {"n": 0}

    def next_plan_id(variant: PlanVariant) -> str:
        if plan_id_factory is not None:
            return plan_id_factory(variant)
        counter["n"] += 1
        suffix = "A" if parent_plan is None else "R"
        return f"PLN-{suffix}{version}-{variant.value.split()[0][:3]}{counter['n']}"

    # 1. Coverage first establishes the reference coverage.
    coverage_request = PlanningRequest(
        state=state,
        variant=PlanVariant.COVERAGE_FIRST,
        scenario_id=scenario_id,
        version=version,
        parent_plan=parent_plan,
        frozen=frozen,
        locked_missions=locked,
        free_mission_ids=free,
        time_limit_seconds=limit,
    )
    coverage_result = run_planning(coverage_request, allow_fallback=settings.greedy_fallback)
    coverage_plan_id = next_plan_id(PlanVariant.COVERAGE_FIRST)
    coverage_plan = coverage_result.to_plan(coverage_plan_id, PlanVariant.COVERAGE_FIRST)
    coverage_report = audit_plan(state, coverage_plan.assignments, expected_frozen=frozen)
    built.append(
        BuiltPlan(
            variant=PlanVariant.COVERAGE_FIRST,
            plan=coverage_plan,
            result=coverage_result,
            audit_findings=coverage_report.messages(),
            audit_valid=coverage_report.valid,
        )
    )
    coverage_reference = coverage_plan.metrics.weighted_coverage
    notes.extend(coverage_result.notes)

    stability = stability_guard.verify_frozen_held(parent_plan, coverage_plan.assignments, now)

    # With no parent plan there is nothing to stay close to, so the deterministic greedy
    # baseline becomes the stability reference. This keeps STABILITY FIRST a genuinely
    # different option on the initial plan instead of a duplicate of COVERAGE FIRST.
    stability_reference: dict = {}
    if parent_plan is None:
        baseline_options, _baseline_notes = planning_engine.solve_greedy(coverage_request)
        stability_reference = {
            option.mission_id: option.to_assignment() for option in baseline_options
        }
        notes.append(
            f"Stability reference for the initial plan set: the deterministic greedy baseline "
            f"({len(stability_reference)} assignment(s)), because there is no active plan to preserve."
        )

    # 2 & 3. Safety first and stability first, both floored at 85% of the reference.
    for variant in (PlanVariant.SAFETY_FIRST, PlanVariant.STABILITY_FIRST):
        request = PlanningRequest(
            state=state,
            variant=variant,
            scenario_id=scenario_id,
            version=version,
            parent_plan=parent_plan,
            frozen=frozen,
            locked_missions=locked,
            free_mission_ids=free,
            coverage_floor=COVERAGE_RETENTION_FLOOR,
            time_limit_seconds=limit,
            stability_reference=stability_reference or None,
        )
        result = run_planning(
            request,
            allow_fallback=settings.greedy_fallback,
            coverage_reference=coverage_reference,
        )
        if result.metrics.weighted_coverage < coverage_reference * COVERAGE_RETENTION_FLOOR:
            notes.append(
                f"{variant.value} fell below the {int(COVERAGE_RETENTION_FLOOR * 100)}% coverage floor "
                f"({result.metrics.weighted_coverage:.0f} < "
                f"{coverage_reference * COVERAGE_RETENTION_FLOOR:.0f}); the floor could not be met."
            )
        plan_id = next_plan_id(variant)
        plan = result.to_plan(plan_id, variant)
        report = audit_plan(state, plan.assignments, expected_frozen=frozen)
        built.append(
            BuiltPlan(
                variant=variant,
                plan=plan,
                result=result,
                audit_findings=report.messages(),
                audit_valid=report.valid,
            )
        )
        notes.extend(result.notes)

    frontier = Frontier(
        plans=built,
        coverage_reference=coverage_reference,
        total_solve_ms=sum(p.result.solve_time_ms for p in built),
        notes=notes,
        frozen_count=len(frozen),
        stability_summary=stability.summary,
        free_mission_count=len(free) if free is not None else len(state.missions),
    )

    # Report, rather than hide, any pair of options the solver made identical.
    for labels in frontier.duplicate_groups:
        joined = ", ".join(labels)
        scope = (
            f"the disruption left only {frontier.free_mission_count} mission(s) free to be "
            "re-decided"
            if parent_plan is not None
            else "the state left little room for the objectives to diverge"
        )
        frontier.notes.append(
            f"{joined} produced identical assignments: {scope}, so all the objectives select "
            f"the same option. Only {frontier.distinct_count} distinct option(s) exist for this "
            "state. This is reported rather than perturbed into a cosmetic difference."
        )

    return frontier


# --------------------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------------------


def diff_plans(
    parent: Plan | None,
    candidate: Plan,
    *,
    state: OperationalState,
    frozen_signatures: set[tuple] | None = None,
) -> ProposalDiffOut:
    """Structured added / removed / changed lists between a parent and candidate plan."""
    parent_by_mission = parent.assignment_by_mission if parent else {}
    candidate_by_mission = candidate.assignment_by_mission
    frozen = frozen_signatures or set()

    added: list[dict] = []
    removed: list[dict] = []
    changed: list[dict] = []
    unchanged = 0
    frozen_held = 0

    for mission_id, assignment in sorted(candidate_by_mission.items()):
        previous = parent_by_mission.get(mission_id)
        row = _row(state, mission_id, assignment)
        if previous is None:
            added.append({**row, "change": "newly assigned"})
            continue
        if previous.signature() == assignment.signature():
            unchanged += 1
            if assignment.signature() in frozen:
                frozen_held += 1
            continue
        changed.append(
            {
                **row,
                "change": "reassigned",
                "previous_asset_id": previous.asset_id,
                "previous_takeoff_minute": previous.takeoff_minute,
                "previous_crew_ids": list(previous.crew_ids),
                "previous_risk": round(previous.risk, 4),
                "risk_delta": round(assignment.risk - previous.risk, 4),
                "takeoff_delta_minutes": assignment.takeoff_minute - previous.takeoff_minute,
            }
        )

    for mission_id, assignment in sorted(parent_by_mission.items()):
        if mission_id in candidate_by_mission:
            continue
        removed.append(
            {
                **_row(state, mission_id, assignment),
                "change": "no longer assigned",
                "priority": state.mission(mission_id).priority,
            }
        )

    return ProposalDiffOut(
        added=added,
        removed=removed,
        changed=changed,
        unchanged_count=unchanged,
        frozen_held_count=frozen_held,
    )


def _row(state: OperationalState, mission_id: str, assignment: Assignment) -> dict:
    mission = state.mission(mission_id)
    asset = state.assets.get(assignment.asset_id)
    return {
        "mission_id": mission_id,
        "mission_title": mission.title,
        "priority": mission.priority,
        "asset_id": assignment.asset_id,
        "asset_class": asset.class_.value if asset else "",
        "crew_ids": list(assignment.crew_ids),
        "takeoff_minute": assignment.takeoff_minute,
        "takeoff_iso": state.minute_to_iso(assignment.takeoff_minute),
        "return_minute": assignment.return_minute,
        "return_iso": state.minute_to_iso(assignment.return_minute),
        "risk": round(assignment.risk, 4),
        "risk_breakdown": assignment.risk_breakdown.as_dict(),
    }


# --------------------------------------------------------------------------------------
# Explanations for a frontier
# --------------------------------------------------------------------------------------


def frontier_explanations(
    state: OperationalState,
    frontier: Frontier,
    *,
    parent: Plan | None,
    trigger_label: str = "",
) -> dict[str, list[str]]:
    """Key-reason lines per variant label."""
    by_label: dict[str, list[str]] = {}
    reference = next((p for p in frontier.plans if p.variant == PlanVariant.COVERAGE_FIRST), None)
    frozen = stability_guard.frozen_assignments(parent, state.now_minute)

    # The per-assignment sentences need the feasibility reports for the plan being
    # described; they are computed once here and reused for the coverage-first option.
    _by_mission, reports = enumerate_options(
        state, FeasibilityContext(now_minute=state.now_minute, committed=frozen)
    )

    for built in frontier.plans:
        reasons = explain_plan_variant(
            built.label,
            built.plan.metrics,
            reference_metrics=reference.plan.metrics if reference else None,
            parent=parent,
            stability_summary=frontier.stability_summary if built.variant == PlanVariant.COVERAGE_FIRST else "",
            fallback=built.plan.is_fallback,
        )
        if parent is not None and trigger_label:
            reasons.append(
                explain_revision(
                    state,
                    parent,
                    built.plan,
                    trigger_label=trigger_label,
                    stability_summary=frontier.stability_summary,
                    frozen_count=frontier.frozen_count,
                )
            )
        if not built.audit_valid:
            reasons.append(
                f"WARNING: independent auditor reported {len(built.audit_findings)} finding(s) for this option."
            )
        by_label[built.label] = reasons

    # Per-assignment sentences, attached to the coverage-first option.
    if reference is not None:
        by_label[reference.label] = by_label.get(reference.label, []) + render_plan_explanations(
            state, reference.plan, reports
        )
    return by_label


def feasibility_reports(state: OperationalState, parent: Plan | None = None):  # type: ignore[no-untyped-def]
    frozen = stability_guard.frozen_assignments(parent, state.now_minute)
    _by_mission, reports = enumerate_options(state, FeasibilityContext(now_minute=state.now_minute, committed=frozen))
    return reports


def event_label(event) -> str:  # type: ignore[no-untyped-def]
    if event is None:
        return "An update to the operational picture"
    if event.kind == EventKind.ASSET_FAULT:
        return f"Asset fault on {event.payload.get('asset_id', 'an asset')}"
    if event.kind == EventKind.WEATHER_RESTRICTION:
        target = event.payload.get("hub_id") or event.payload.get("region_id")
        return f"Weather restriction affecting {target}"
    if event.kind == EventKind.HAZARD_ZONE:
        return f"Hazard zone {event.payload.get('zone_id', 'update')} expanded"
    if event.kind == EventKind.MISSION_PRIORITY_CHANGE:
        return f"Mission {event.payload.get('mission_id')} priority changed"
    return event.kind.value


def proposal_rank(variant: PlanVariant) -> int:
    return VARIANT_ORDER.index(variant) + 1


def persistable_label(built: BuiltPlan) -> PlanVariant:
    return PlanVariant.FALLBACK if built.plan.is_fallback else built.variant


def default_note(variant: PlanVariant) -> str:  # type: ignore[no-untyped-def]
    if variant == PlanVariant.COVERAGE_FIRST:
        return "Maximises priority-weighted mission coverage."
    if variant == PlanVariant.SAFETY_FIRST:
        return f"Minimises combined risk while retaining at least {int(COVERAGE_RETENTION_FLOOR * 100)}% of coverage-first."
    if variant == PlanVariant.STABILITY_FIRST:
        return f"Minimises changes to the current plan while retaining at least {int(COVERAGE_RETENTION_FLOOR * 100)}% of coverage-first."
    return "Deterministic greedy fallback."


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


__all__ = [
    "BuiltPlan",
    "Frontier",
    "build_frontier",
    "default_note",
    "diff_plans",
    "event_label",
    "feasibility_reports",
    "frontier_explanations",
    "persistable_label",
    "proposal_rank",
    "utcnow",
    "ActorRole",
    "PlanStatus",
    "disruption_monitor",
]