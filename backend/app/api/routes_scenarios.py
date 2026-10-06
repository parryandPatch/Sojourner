"""Scenario lifecycle routes: reset and generate."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ActorRoleDep, ScenarioDep, SessionDep, WriterDep
from app.api.websocket import LiveMessage, hub
from app.config import get_settings
from app.domain.enums import LedgerAction
from app.repositories import scenario_repo
from app.services import state_hub
from app.services.scenario_studio import default_config

router = APIRouter(tags=["scenarios"])


@router.post("/scenarios/reset")
def reset_scenario(
    session: SessionDep,
    role: WriterDep,
    body: dict | None = None,
) -> dict:
    """Reset to the seeded default scenario. Wipes every scenario-scoped table."""
    settings = get_settings()
    payload = body or {}
    seed = int(payload.get("seed", settings.default_seed))
    name = str(payload.get("name", settings.app_name + " synthetic demonstration scenario"))
    row = scenario_repo.reset_scenario(
        session,
        seed=seed,
        name=name,
        mission_count=int(payload.get("mission_count", settings.mission_count)),
        asset_count=int(payload.get("asset_count", settings.asset_count)),
        crew_count=int(payload.get("crew_count", settings.crew_count)),
        horizon_hours=int(payload.get("horizon_hours", settings.horizon_hours)),
    )
    scenario_repo.append_ledger(
        session,
        scenario_id=row.id,
        actor_role=role,
        action=LedgerAction.SCENARIO_RESET,
        entity_ref=row.id,
        payload={"seed": row.seed, "counts": state_hub.counts(scenario_repo.load_state(session, row.id))},
        description=f"Seeded scenario reset with seed {row.seed}.",
    )
    state = scenario_repo.load_state(session, row.id)
    hub.broadcast_nowait(
        LiveMessage(kind="scenario_reset", payload={"scenario_id": row.id, "seed": row.seed})
    )
    return _summary(state, row)


@router.post("/scenarios/generate")
def generate_scenario(
    session: SessionDep,
    role: WriterDep,
    body: dict | None = None,
) -> dict:
    """Create a scenario from an explicit seed/config (defaults come from settings)."""
    settings = get_settings()
    payload = body or {}
    seed = int(payload.get("seed") or settings.default_seed)
    config = default_config(
        seed,
        name=str(payload.get("name", "Synthetic demonstration scenario")),
        mission_count=int(payload.get("mission_count") or settings.mission_count),
        asset_count=int(payload.get("asset_count") or settings.asset_count),
        crew_count=int(payload.get("crew_count") or settings.crew_count),
        horizon_hours=int(payload.get("horizon_hours") or settings.horizon_hours),
    )
    row = scenario_repo.create_scenario(session, config)
    state = scenario_repo.load_state(session, row.id)
    scenario_repo.append_ledger(
        session,
        scenario_id=row.id,
        actor_role=role,
        action=LedgerAction.SCENARIO_GENERATED,
        entity_ref=row.id,
        payload={"seed": row.seed, "config": row.config_json},
        description=f"Scenario generated from seed {row.seed}.",
    )
    hub.broadcast_nowait(
        LiveMessage(kind="scenario_generated", payload={"scenario_id": row.id, "seed": row.seed})
    )
    return _summary(state, row)


@router.get("/scenarios/active")
def active_scenario(session: SessionDep, scenario: ScenarioDep) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    return _summary(state, scenario)


@router.get("/scenarios/roles")
def role_matrix(role: ActorRoleDep) -> dict:
    return {
        "acting_role": role.value,
        "permissions": state_hub.role_permissions(),
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


def _summary(state, row) -> dict:  # type: ignore[no-untyped-def]
    return {
        "scenario_id": row.id,
        "name": row.name,
        "seed": row.seed,
        "created_at": row.created_at,
        "now_minute": row.now_minute,
        "now_iso": state.now_iso,
        "horizon_minutes": state.horizon_minutes,
        "counts": state_hub.counts(state),
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


__all__ = ["router"]