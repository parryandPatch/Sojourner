"""Planning routes: generate the three-option frontier, read plans back.

Generating a plan set never activates anything. Activation happens only through
`routes_proposals` and only for a Commander.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, HTTPException

from app.api.deps import ScenarioDep, SessionDep, WriterDep
from app.api.websocket import LiveMessage, hub
from app.config import get_settings
from app.domain.enums import LedgerAction, PlanStatus
from app.repositories import scenario_repo
from app.services import explanation_service, frontier_explorer, state_hub
from app.services.frontier_explorer import build_frontier, default_note
from app.services.plan_auditor import audit_plan

router = APIRouter(tags=["plans"])


@router.post("/plans/generate")
def generate_plans(
    session: SessionDep,
    scenario: ScenarioDep,
    role: WriterDep,
    body: dict | None = None,
) -> dict:
    """Build exactly three trade-off options against the current state.

    The first call produces the initial plan set. Later calls with ``revise=true`` re-plan
    against the active plan so started assignments stay frozen.
    """
    settings = get_settings()
    payload = body or {}
    revise = bool(payload.get("revise", False))

    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    parent = active if revise else None

    version = scenario_repo.next_plan_version(session, scenario.id)
    parent_plan_id = parent.id if parent else None

    frontier = build_frontier(
        state,
        scenario_id=scenario.id,
        version=version,
        parent_plan=parent,
        time_limit_seconds=settings.solver_time_limit_seconds,
    )

    reference_metrics = frontier.plans[0].plan.metrics if frontier.plans else None
    explanations = frontier_explorer.frontier_explanations(
        state, frontier, parent=parent, trigger_label=payload.get("trigger_label", "")
    )

    stored: list[str] = []
    rendered: list[dict] = []
    for built in frontier.plans:
        label = frontier_explorer.persistable_label(built)
        # `Plan` is a frozen dataclass, so the coverage-first risk reference is applied
        # by rebuilding it rather than by mutating `built.plan` in place.
        plan = built.plan
        if reference_metrics is not None:
            plan = dataclasses.replace(
                plan, metrics=_with_risk_delta(plan.metrics, reference_metrics.mean_risk)
            )

        scenario_repo.persist_plan(
            session,
            scenario.id,
            plan,
            version=version,
            status=PlanStatus.PROPOSED if parent else PlanStatus.DRAFT,
            parent_plan_id=parent_plan_id,
            note=payload.get("note") or default_note(label),
        )
        stored.append(plan.id)

        assignment_explanations = _assignment_explanations(state, plan)
        rendered.append(
            {
                **state_hub.plan_out(
                    state,
                    plan,
                    explanations=assignment_explanations,
                    notes=[*frontier.notes, *built.result.notes],
                    audit=False,
                ),
                "key_reasons": explanations.get(built.label, []),
                "rank": frontier_explorer.proposal_rank(built.variant),
            }
        )

    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.PLAN_GENERATED,
        entity_ref=",".join(stored),
        payload={
            "plan_ids": stored,
            "version": version,
            "parent_plan_id": parent_plan_id,
            "distinct_variants": frontier.distinct_count,
            "coverage_reference": frontier.coverage_reference,
            "solve_time_ms_total": frontier.total_solve_ms,
        },
        description=(
            f"Generated {len(stored)} synthetic plan option(s) "
            f"({'revision' if parent else 'initial'}); none activated."
        ),
    )

    hub.broadcast_nowait(
        LiveMessage(
            kind="plans_generated",
            payload={
                "plan_ids": stored,
                "version": version,
                "parent_plan_id": parent_plan_id,
                "distinct_variants": frontier.distinct_count,
            },
        )
    )

    return {
        "parent_plan_id": parent_plan_id,
        "trigger_event_id": payload.get("trigger_event_id"),
        "coverage_reference": frontier.coverage_reference,
        "plans": rendered,
        "solve_time_ms_total": frontier.total_solve_ms,
        "distinct_variants": frontier.distinct_count,
        # Which options the solver made identical, and why. The UI shows this instead of
        # presenting three cards that differ only in their name.
        "duplicate_options": [list(group) for group in frontier.duplicate_groups],
        "free_mission_count": frontier.free_mission_count,
        "distinct_options_note": (
            "Three genuinely different options."
            if frontier.distinct_count >= len(frontier.plans)
            else (
                f"Only {frontier.distinct_count} distinct option(s) exist for this state; "
                f"{frontier.free_mission_count} mission(s) were free to be re-decided. "
                "Reported rather than perturbed into a cosmetic difference."
            )
        ),
        "notes": frontier.notes,
        "activation_note": (
            "No plan is active. A Commander must approve a proposal before any of these "
            "versions becomes the active plan."
        ),
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


@router.get("/plans")
def list_plans(session: SessionDep, scenario: ScenarioDep) -> dict:
    rows = scenario_repo.plan_rows(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    return {
        "plans": [
            {
                "id": row.id,
                "version": row.version,
                "status": row.status,
                "status_label": state_hub.plan_status_label(PlanStatus(row.status)),
                "variant": row.variant,
                "is_fallback": row.is_fallback,
                "solver_status": row.solver_status,
                "solve_time_ms": row.solve_time_ms,
                "created_at": row.created_at,
                "parent_plan_id": row.parent_plan_id,
                "approved_at": row.approved_at,
                "approved_by": row.approved_by,
                "metrics": row.metrics_json,
                "note": row.note,
            }
            for row in reversed(rows)
        ],
        "active_plan_id": active.id if active else None,
    }


@router.get("/plans/{plan_id}")
def get_plan(session: SessionDep, scenario: ScenarioDep, plan_id: str) -> dict:
    plan = scenario_repo.get_plan(session, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan: {plan_id}")
    state = scenario_repo.load_state(session, scenario.id)
    report = audit_plan(state, plan.assignments)
    return {
        **state_hub.plan_out(
            state,
            plan,
            explanations=_assignment_explanations(state, plan),
            notes=[plan.note] if plan.note else [],
            audit=False,
        ),
        "auditor_valid": report.valid,
        "auditor_findings": report.messages(),
        "status_label": state_hub.plan_status_label(plan.status),
        "approved_by_role": plan.approved_by,
    }


@router.get("/plans/{plan_id}/explanations")
def plan_explanations(session: SessionDep, scenario: ScenarioDep, plan_id: str) -> dict:
    """Per-assignment and per-unassigned-mission sentences, rendered from reason codes."""
    plan = scenario_repo.get_plan(session, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan: {plan_id}")
    state = scenario_repo.load_state(session, scenario.id)
    reports = frontier_explorer.feasibility_reports(state, parent=plan)
    return {
        "plan_id": plan_id,
        "variant": plan.variant,
        "explanations": explanation_service.render_plan_explanations(state, plan, reports),
        "reason_codes": state_hub.reason_code_dictionary(),
    }


@router.get("/feasibility/{mission_id}")
def feasibility(session: SessionDep, scenario: ScenarioDep, mission_id: str) -> dict:
    """Valid / invalid option explanation for one mission."""
    state = scenario_repo.load_state(session, scenario.id)
    if mission_id not in state.missions:
        raise HTTPException(status_code=404, detail=f"Unknown mission: {mission_id}")

    active = scenario_repo.active_plan(session, scenario.id)
    reports = frontier_explorer.feasibility_reports(state, parent=active)
    report = reports.get(mission_id)
    if report is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=404, detail=f"No feasibility report for {mission_id}.")

    mission = state.mission(mission_id)
    best = report.best()
    explanations: list[str] = []
    if best is None:
        explanations.append(explanation_service.explain_unassigned(state, mission, report))
    else:
        chosen = best.to_assignment()
        explanations.append(
            explanation_service.explain_assignment(
                state, mission, chosen, alternatives=list(report.options)
            ).text
        )
        runner_up = next((o for o in report.options if o.asset_id != chosen.asset_id), None)
        if runner_up is not None:
            explanations.append(
                f"{report.option_count} valid option(s) were evaluated; the next best alternative "
                f"was {runner_up.asset_id} departing at minute {runner_up.takeoff_minute} with "
                f"combined risk {runner_up.risk:.2f}."
            )
        explanations.append(
            f"All {report.option_count} candidate option(s) satisfy every hard constraint."
        )

    return {
        "mission_id": mission_id,
        "feasible": report.feasible,
        "option_count": report.option_count,
        "best_option": (
            state_hub.assignment_out(state, best.to_assignment()) if best is not None else None
        ),
        "blocking_reason_counts": {code.value: count for code, count in report.blocking_counts.items()},
        "candidates_examined": report.candidates_examined,
        "explanations": explanations,
        "notes": report.notes,
        "reason_codes": state_hub.reason_code_dictionary(),
    }


def _assignment_explanations(state, plan) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Map mission_id -> the rendered 'why this assignment' sentence for that mission."""
    by_mission = plan.assignment_by_mission
    alternatives = list(by_mission.values())
    lines: dict[str, str] = {}
    for mission in state.missions_in_priority_order():
        assignment = by_mission.get(mission.id)
        if assignment is None:
            continue
        lines[mission.id] = explanation_service.explain_assignment(
            state, mission, assignment, alternatives=alternatives
        ).text
    return lines


def _with_risk_delta(metrics, reference_mean_risk: float):  # type: ignore[no-untyped-def]
    return dataclasses.replace(
        metrics,
        risk_savings_vs_coverage_first=round(reference_mean_risk - metrics.mean_risk, 4),
    )


__all__ = ["router"]