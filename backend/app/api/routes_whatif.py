"""What-if routes.

This screen is informational only: there is no approve route here and none is reachable
from this module. Every experiment runs on a clone and returns a digest pair proving the
live state was not mutated.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ScenarioDep, SessionDep, WriterDep
from app.api.websocket import LiveMessage, hub
from app.config import get_settings
from app.domain.enums import EventKind, LedgerAction, PlanVariant
from app.repositories import scenario_repo
from app.services import state_hub, whatif_lab

router = APIRouter(tags=["what-if"])


@router.post("/what-if/run")
def run_what_if(
    session: SessionDep,
    scenario: ScenarioDep,
    role: WriterDep,
    body: dict | None = None,
) -> dict:
    """Run an isolated scenario fork. Never mutates active state."""
    settings = get_settings()
    payload = body or {}
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)

    scripted = [(str(key), {}) for key in payload.get("scripted_events", []) or []]
    if payload.get("scripted_event"):
        scripted.append((str(payload["scripted_event"]), {}))

    events: list[tuple[EventKind, dict]] = []
    for spec in payload.get("events", []) or []:
        raw_kind = spec.get("kind")
        try:
            events.append((EventKind(str(raw_kind).upper()), dict(spec.get("payload") or {})))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event kind in what-if spec: {raw_kind}.",
            ) from exc

    overrides = dict(payload.get("overrides") or {})
    if not scripted and not events and not overrides:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one scripted_event, events entry, or overrides entry.",
        )

    variant = _variant(payload.get("variant"))
    result = whatif_lab.run_what_if(
        state,
        label=str(payload.get("label", "what-if experiment")),
        scripted_events=scripted,
        events=events,
        overrides=overrides,
        active_plan=active,
        scenario_id=scenario.id,
        variant=variant,
        time_limit_seconds=settings.solver_time_limit_seconds,
    )

    # Re-read the live state and confirm it is byte-identical to what it was before.
    after = scenario_repo.load_state(session, scenario.id)
    live_digest = after.snapshot_digest()
    isolated = live_digest == result.state_digest_before

    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.WHAT_IF_RUN,
        entity_ref=result.experiment_plan.id,
        payload={
            "label": result.label,
            "variant": variant.value,
            "scripted_events": [key for key, _ in scripted],
            "override_keys": sorted(overrides),
            "state_digest_before": result.state_digest_before,
            "state_digest_after": result.state_digest_after,
            "isolation_verified": isolated,
            "coverage_delta": result.coverage_delta,
            "mean_risk_delta": result.mean_risk_delta,
        },
        description=(
            f"What-if experiment '{result.label}' ran on a cloned state; "
            f"live state isolation {'verified' if isolated else 'FAILED'}."
        ),
    )

    hub.broadcast_nowait(
        LiveMessage(
            kind="what_if_run",
            payload={
                "label": result.label,
                "isolation_verified": isolated,
                "changed_missions": result.changed_missions,
            },
        )
    )

    return {
        "label": result.label,
        "variant": variant.value,
        "state_digest_before": result.state_digest_before,
        "state_digest_after": result.state_digest_after,
        "isolation_verified": isolated,
        "active_plan_id": result.active_plan_id,
        "experiment_plan": state_hub.plan_out(
            state, result.experiment_plan, audit=False
        ),
        "experiment_auditor_valid": result.audit_valid,
        "experiment_auditor_findings": result.audit_findings,
        "comparison": result.comparison,
        "coverage_delta": round(result.coverage_delta, 2),
        "mean_risk_delta": round(result.mean_risk_delta, 4),
        "changed_missions": result.changed_missions,
        "notes": result.notes,
        "solve_time_ms": result.solve_time_ms,
        "approval_notice": (
            "What-if results are information only. This screen cannot approve or activate a plan."
        ),
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


@router.get("/what-if/templates")
def templates(session: SessionDep, scenario: ScenarioDep) -> dict:
    """Ready-made experiment templates for the what-if lab."""
    state = scenario_repo.load_state(session, scenario.id)
    return {
        "variants": [variant.value for variant in
                     (PlanVariant.COVERAGE_FIRST, PlanVariant.SAFETY_FIRST, PlanVariant.STABILITY_FIRST)],
        "scripted_events": sorted(_scripted_keys()),
        "hubs": [
            {"id": hub.id, "name": hub.name, "status": state_hub.hub_status(state, hub)}
            for hub in sorted(state.hubs.values(), key=lambda h: h.id)
        ],
        "assets": [
            {"id": a.id, "label": a.label, "class": a.class_.value, "status": a.status.value}
            for a in sorted(state.assets.values(), key=lambda a: a.id)
        ],
        "override_help": {
            "mission.<id>": "replace mission fields, e.g. {'mission.MIS-001': {'priority': 1}}",
            "asset.<id>": "replace asset fields, e.g. {'asset.AST-006': {'status': 'UNAVAILABLE'}}",
            "weather_window": "list of synthetic weather windows to append",
            "hazard_zone": "list of synthetic hazard zones to append",
        },
    }


def _scripted_keys() -> set[str]:  # type: ignore[no-untyped-def]
    from app.services.scenario_studio import SCRIPTED_EVENTS

    return set(SCRIPTED_EVENTS)


def _variant(value) -> PlanVariant:  # type: ignore[no-untyped-def]
    if value is None:
        return PlanVariant.COVERAGE_FIRST
    # Enum members are spaced ("STABILITY FIRST"); accept the underscored and lower-case
    # spellings too so a client does not have to know the exact display form.
    text = str(value).replace("_", " ").strip().upper()
    try:
        candidate = PlanVariant(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown variant: {value}. Use COVERAGE FIRST, SAFETY FIRST or STABILITY FIRST.",
        ) from exc
    if candidate == PlanVariant.FALLBACK:
        raise HTTPException(status_code=400, detail="FALLBACK is a solver label, not a requestable variant.")
    return candidate


__all__ = ["router"]