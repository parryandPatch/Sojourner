"""Fused operational state routes for the command deck."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import ActorRoleDep, ScenarioDep, SessionDep
from app.domain.enums import AssetStatus
from app.repositories import scenario_repo
from app.services import state_hub

router = APIRouter(tags=["state"])


@router.get("/state/overview")
def overview(session: SessionDep, scenario: ScenarioDep, role: ActorRoleDep) -> dict:
    """Everything the command deck's KPI strip and alert list need, in one payload."""
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    pending = scenario_repo.pending_proposals(session, scenario.id)
    verification = scenario_repo.verify_ledger(session, scenario.id)

    recent_events = [
        {
            "id": event.id,
            "kind": event.kind,
            "occurred_at": event.occurred_at,
            "occurred_minute": event.occurred_minute,
            "label": event.label,
            "payload": event.payload,
            "status": event.status,
            "source": event.source,
            "affected_mission_ids": [],
        }
        for event in list(reversed(state.events))[:6]
    ]

    return {
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
        "advisory_notice": state_hub.ADVISORY_NOTICE,
        "now_minute": state.now_minute,
        "now_iso": state.now_iso,
        "horizon_minutes": state.horizon_minutes,
        "active_plan": state_hub.plan_out(state, active) if active else None,
        "pending_proposals": len(pending),
        "kpis": state_hub.kpi_tiles(
            state,
            active,
            pending_proposals=len(pending),
            chain_status=verification.status,
        ),
        "counts": state_hub.counts(state),
        "recent_events": recent_events,
        "readiness_alerts": state_hub.readiness_alerts(state),
        "affected_mission_ids": [],
        "chain_status": verification.status,
        "timeline": state_hub.timeline_rows(state, active),
        "role_matrix": state_hub.role_matrix(),
        "acting_role": role.value,
    }


@router.get("/state/map")
def map_view(session: SessionDep, scenario: ScenarioDep) -> dict:
    """Neutral synthetic grid: hubs, assets, mission areas, weather, hazard zones."""
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    payload = state_hub.map_payload(state, active)
    payload["synthetic_data_notice"] = state_hub.SYNTHETIC_NOTICE
    return payload


@router.get("/state/readiness")
def readiness(session: SessionDep, scenario: ScenarioDep) -> dict:
    """Readiness rows for the asset-readiness screen (synthetic heuristic)."""
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    assigned = {a.asset_id: a.mission_id for a in (active.assignments if active else ())}

    rows: list[dict] = []
    for asset_id in sorted(state.assets):
        asset = state.assets[asset_id]
        row = state_hub.asset_out(asset, assigned.get(asset_id))
        if asset.status == AssetStatus.UNAVAILABLE:
            row["tone"] = "critical"
        elif asset.status == AssetStatus.LIMITED:
            row["tone"] = "warning"
        else:
            row["tone"] = "neutral"
        rows.append(row)
    return {
        "assets": rows,
        "readiness_method": state_hub.READINESS_METHOD,
        "readiness_note": (
            "Synthetic heuristic: base 0.98 minus 0.003 per maintenance hour, minus 0.04 per recent "
            "fault, minus 0.10 when LIMITED, clipped to [0.05, 0.99]. Not predictive ML and not "
            "validated against real-world data."
        ),
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
    }


@router.get("/state/crew")
def crew_list(session: SessionDep, scenario: ScenarioDep, hub_id: str | None = Query(default=None)) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    committed: dict[str, list[str]] = {}
    for assignment in (active.assignments if active else ()):
        for crew_id in assignment.crew_ids:
            committed.setdefault(crew_id, []).append(assignment.mission_id)

    rows = [
        {**state_hub.crew_out(crew), "assigned_mission_ids": sorted(committed.get(crew_id, []))}
        for crew_id, crew in sorted(state.crews.items())
        if hub_id is None or crew.home_hub_id == hub_id
    ]
    return {"crew": rows, "counts": state_hub.counts(state)}


@router.get("/state/stock")
def stock(session: SessionDep, scenario: ScenarioDep) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    return {"stock": [state_hub.stock_out(item) for item in state.stock]}


@router.get("/state/reason-codes")
def reason_codes() -> dict:
    """The structured reason-code dictionary every explanation sentence is rendered from."""
    return {
        "reason_codes": state_hub.reason_code_dictionary(),
        "risk_formula": state_hub.RISK_FORMULA,
        "explanation_policy": (
            "Every sentence is rendered from stored reason codes by a fixed template. "
            "No language model is involved in producing explanations."
        ),
    }


@router.post("/state/clock")
def advance_clock(
    session: SessionDep,
    scenario: ScenarioDep,
    body: dict,
) -> dict:
    """Move the operational clock. Assignments with takeoff <= now become frozen."""
    target = int(body.get("advance_to_minute", 0))
    state = scenario_repo.load_state(session, scenario.id)
    if not 0 <= target <= state.horizon_minutes:
        raise HTTPException(
            status_code=400,
            detail=f"advance_to_minute must be between 0 and the {state.horizon_minutes}-minute horizon.",
        )
    scenario_repo.set_now_minute(session, scenario.id, target)
    reloaded = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)

    frozen_now = [
        a.mission_id for a in (active.assignments if active else ()) if a.takeoff_minute <= target
    ]
    return {
        "now_minute": reloaded.now_minute,
        "now_iso": reloaded.now_iso,
        "frozen_mission_ids": sorted(frozen_now),
        "note": (
            f"{len(frozen_now)} assignment(s) are now frozen and cannot be changed by re-planning."
        ),
    }


__all__ = ["router"]