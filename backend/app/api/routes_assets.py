"""Asset routes: list/filter, plus the Maintainer-only condition update."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import ScenarioDep, SessionDep, WriterDep
from app.api.websocket import LiveMessage, hub
from app.domain.enums import AssetStatus, LedgerAction
from app.repositories import scenario_repo
from app.services import state_hub

router = APIRouter(tags=["assets"])


@router.get("/assets")
def list_assets(
    session: SessionDep,
    scenario: ScenarioDep,
    asset_class: str | None = Query(default=None),
    status: str | None = Query(default=None),
    home_hub_id: str | None = Query(default=None),
) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    assigned = {a.asset_id: a.mission_id for a in (active.assignments if active else ())}

    rows: list[dict] = []
    for asset in sorted(state.assets.values(), key=lambda a: a.id):
        if asset_class and asset.class_.value != asset_class.upper():
            continue
        if status and asset.status.value != status.upper():
            continue
        if home_hub_id and asset.home_hub_id != home_hub_id.upper():
            continue
        rows.append(state_hub.asset_out(asset, assigned.get(asset.id)))

    return {
        "assets": rows,
        "total": len(state.assets),
        "matched": len(rows),
        "readiness_method": state_hub.READINESS_METHOD,
        "readiness_note": (
            "Synthetic heuristic, not predictive ML. low/high is the simple uncertainty band "
            "min(0.25, 0.04 + 0.002 * maintenance_hours + 0.03 * recent_faults)."
        ),
    }


@router.get("/assets/{asset_id}")
def get_asset(session: SessionDep, scenario: ScenarioDep, asset_id: str) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    asset = state.assets.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Unknown asset: {asset_id}")
    active = scenario_repo.active_plan(session, scenario.id)
    sortie = next((a for a in (active.assignments if active else ()) if a.asset_id == asset_id), None)
    return {
        **state_hub.asset_out(asset, sortie.mission_id if sortie else None),
        "assignment": state_hub.assignment_out(state, sortie) if sortie else None,
        "readiness_method": state_hub.READINESS_METHOD,
    }


@router.post("/assets/condition")
def update_asset_condition(
    session: SessionDep,
    scenario: ScenarioDep,
    role: WriterDep,
    body: dict,
) -> dict:
    """Controlled demo action: update a synthetic asset's condition.

    This is a Maintainer-style demo control, not a real maintenance system. It is recorded
    in the ledger, and the UI labels it as a synthetic condition update.
    """
    asset_id = str(body.get("asset_id", ""))
    state = scenario_repo.load_state(session, scenario.id)
    asset = state.assets.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"Unknown asset: {asset_id}")

    try:
        new_status = AssetStatus(str(body.get("status", asset.status.value)).upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown asset status: {body.get('status')}. Use {[s.value for s in AssetStatus]}.",
        ) from exc

    hours = float(body.get("maintenance_hours_since", asset.maintenance_hours_since))
    faults = int(body.get("recent_fault_count", asset.recent_fault_count))
    released = body.get("available_from_minute")
    if released is not None:
        released = int(released)
    note = str(body.get("note", "synthetic condition update (demo)"))

    scenario_repo.upsert_asset_condition(
        session,
        scenario.id,
        asset_id=asset_id,
        status=new_status,
        maintenance_hours_since=hours,
        recent_fault_count=faults,
        available_from_minute=released,
        note=note,
        actor_role=role,
    )
    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.ASSET_CONDITION_UPDATED,
        entity_ref=asset_id,
        payload={
            "asset_id": asset_id,
            "status": new_status.value,
            "maintenance_hours_since": hours,
            "recent_fault_count": faults,
            "available_from_minute": released,
            "note": note,
        },
        description=f"Synthetic asset condition updated for {asset_id} to {new_status.value}.",
    )

    updated_state = scenario_repo.load_state(session, scenario.id)
    hub.broadcast_nowait(
        LiveMessage(
            kind="asset_condition_updated",
            payload={"asset_id": asset_id, "status": new_status.value, "note": note},
        )
    )
    return {
        **state_hub.asset_out(updated_state.assets[asset_id]),
        "note": note,
        "message": (
            f"Synthetic condition overlay updated for {asset_id}. "
            "No plan was re-generated or activated."
        ),
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
    }


__all__ = ["router"]