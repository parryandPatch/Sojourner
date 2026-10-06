"""What-if sandbox.

Runs an experiment on a *clone* of the operational state. The live state, the active
plan, the proposals and the ledger are never touched: the service returns a comparison
and an isolation proof (``snapshot_digest`` before and after) so a test can assert that
nothing changed.

There is deliberately no approve path here. A what-if run produces information only;
activating a plan is a separate, Commander-only action.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.domain.enums import EventKind, PlanVariant
from app.domain.schemas import OperationalState, Plan
from app.services import disruption_monitor
from app.services.plan_auditor import audit_plan
from app.services.planning_engine import PlanningRequest, run_planning
from app.services.stability_guard import frozen_assignments


@dataclass(slots=True)
class WhatIfResult:
    """Outcome of one sandboxed experiment."""

    label: str
    state_digest_before: str
    state_digest_after: str
    isolated: bool
    active_plan_id: str | None
    experiment_plan: Plan
    audit_valid: bool
    audit_findings: list[str]
    coverage_delta: float
    mean_risk_delta: float
    changed_missions: list[str]
    comparison: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    solve_time_ms: int = 0


def run_what_if(
    state: OperationalState,
    *,
    label: str = "what-if experiment",
    scripted_events: list[tuple[str, dict]] | None = None,
    events: list[tuple[EventKind, dict]] | None = None,
    overrides: dict | None = None,
    active_plan: Plan | None = None,
    scenario_id: str = "WHAT-IF",
    variant: PlanVariant = PlanVariant.COVERAGE_FIRST,
    time_limit_seconds: float | None = None,
) -> WhatIfResult:
    """Fork ``state``, apply the experiment, solve, and prove the live state is unchanged."""
    digest_before = state.snapshot_digest()

    experiment_state = state.clone()
    notes: list[str] = []

    for key, payload in scripted_events or []:
        kind, title, scripted_payload, _description = disruption_monitor.resolve_scripted_event(key)
        merged = {**scripted_payload, **(payload or {})}
        application = disruption_monitor.apply_event(
            experiment_state,
            kind,
            merged,
            label=title,
            source="what-if",
        )
        experiment_state = application.state
        notes.append(f"Applied {title} ({kind.value}): {application.summary}")

    for kind, payload in events or []:
        application = disruption_monitor.apply_event(
            experiment_state, kind, dict(payload or {}), source="what-if"
        )
        experiment_state = application.state
        notes.append(f"Applied synthetic {kind.value}: {application.summary}")

    if overrides:
        experiment_state = disruption_monitor.whatif_overrides(experiment_state, dict(overrides))
        notes.append(f"Applied {len(overrides)} manual field override(s) on the cloned state.")

    frozen = frozen_assignments(active_plan, experiment_state.now_minute)
    limit = time_limit_seconds or 8.0

    def request_for(target: PlanVariant) -> PlanningRequest:
        return PlanningRequest(
            state=experiment_state,
            variant=target,
            scenario_id=scenario_id,
            frozen=frozen,
            time_limit_seconds=limit,
        )

    # SAFETY FIRST needs a coverage reference. It is derived inside the sandbox from the
    # same cloned state, so the reference never leaks into or from the live scenario.
    reference: float | None = None
    if variant == PlanVariant.SAFETY_FIRST:
        baseline = run_planning(request_for(PlanVariant.COVERAGE_FIRST))
        reference = baseline.metrics.weighted_coverage

    started = time.perf_counter()
    result = run_planning(request_for(variant), coverage_reference=reference)
    solve_time_ms = int((time.perf_counter() - started) * 1000)

    experiment_plan = result.to_plan(
        f"WHATIF-{label[:24].upper().replace(' ', '-')}",
        PlanVariant.FALLBACK if result.is_fallback else variant,
        note=f"what-if experiment (isolated, never activated): {label}",
    )

    report = audit_plan(experiment_state, experiment_plan.assignments, expected_frozen=frozen)

    digest_after = state.snapshot_digest()
    isolated = digest_before == digest_after

    active_metrics = active_plan.metrics if active_plan else None
    coverage_delta = experiment_plan.metrics.weighted_coverage - (
        active_metrics.weighted_coverage if active_metrics else 0.0
    )
    mean_risk_delta = experiment_plan.metrics.mean_risk - (
        active_metrics.mean_risk if active_metrics else 0.0
    )

    comparison, changed = compare(active_plan, experiment_plan)

    if not isolated:  # pragma: no cover - defensive; the isolation test asserts this
        notes.append(
            "ISOLATION FAILURE: the live state digest changed during the experiment. "
            "This is a defect, not an expected result."
        )

    return WhatIfResult(
        label=label,
        state_digest_before=digest_before,
        state_digest_after=digest_after,
        isolated=isolated,
        active_plan_id=active_plan.id if active_plan else None,
        experiment_plan=experiment_plan,
        audit_valid=report.valid,
        audit_findings=report.messages(),
        coverage_delta=coverage_delta,
        mean_risk_delta=mean_risk_delta,
        changed_missions=changed,
        comparison=comparison,
        notes=notes,
        solve_time_ms=solve_time_ms,
    )


def compare(active_plan: Plan | None, experiment_plan: Plan) -> tuple[list[dict], list[str]]:
    """Side-by-side rows: what the active plan does versus what the experiment does."""
    active_by_mission = active_plan.assignment_by_mission if active_plan else {}
    experiment_by_mission = experiment_plan.assignment_by_mission

    rows: list[dict] = []
    changed: list[str] = []
    for mission_id in sorted(set(active_by_mission) | set(experiment_by_mission)):
        active = active_by_mission.get(mission_id)
        experiment = experiment_by_mission.get(mission_id)
        differs = (active is None) != (experiment is None) or (
            active is not None
            and experiment is not None
            and active.signature() != experiment.signature()
        )
        rows.append(
            {
                "mission_id": mission_id,
                "active_asset_id": active.asset_id if active else None,
                "experiment_asset_id": experiment.asset_id if experiment else None,
                "active_takeoff": active.takeoff_minute if active else None,
                "experiment_takeoff": experiment.takeoff_minute if experiment else None,
                "changed": differs,
            }
        )
        if differs:
            changed.append(mission_id)
    return rows, changed


__all__ = ["WhatIfResult", "compare", "run_what_if"]