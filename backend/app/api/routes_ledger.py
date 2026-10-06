"""Decision ledger routes: read the chain, verify it, replay a plan's history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ScenarioDep, SessionDep
from app.repositories import scenario_repo
from app.services.decision_ledger import chain_badge

router = APIRouter(tags=["ledger"])


@router.get("/ledger")
def read_ledger(session: SessionDep, scenario: ScenarioDep, limit: int | None = None) -> dict:
    """Read decision history with hash links and a live verification status."""
    rows = scenario_repo.ledger_rows(session, scenario.id)
    verification = scenario_repo.verify_ledger(session, scenario.id)
    entries = [
        {
            "id": row.id,
            "sequence": row.sequence,
            "occurred_at": row.occurred_at,
            "actor_role": row.actor_role,
            "action": row.action,
            "entity_ref": row.entity_ref,
            "description": row.description,
            "payload": row.payload_json,
            "previous_hash": row.previous_hash,
            "entry_hash": row.entry_hash,
        }
        for row in rows
    ]
    if limit is not None:
        entries = entries[-limit:]

    return {
        "scenario_id": scenario.id,
        "entries": entries,
        "chain_status": verification.status,
        "chain_badge": chain_badge(verification),
        "chain_length": verification.length,
        "head_hash": verification.head_hash,
        "verification_detail": verification.detail,
        "integrity_note": (
            "Demonstration tamper-evidence only: entry_hash = SHA-256(previous_hash + "
            "canonical_JSON(payload) + occurred_at + action + actor_role). This is not a "
            "replacement for an enterprise security or audit system."
        ),
    }


@router.get("/ledger/verify")
def verify(session: SessionDep, scenario: ScenarioDep) -> dict:
    """Recompute every hash and every link in the chain."""
    verification = scenario_repo.verify_ledger(session, scenario.id)
    return {
        "chain_status": verification.status,
        "chain_badge": chain_badge(verification),
        "chain_length": verification.length,
        "checked": verification.checked,
        "broken_at": verification.broken_at,
        "head_hash": verification.head_hash,
        "verification_detail": verification.detail,
    }


@router.get("/replay/{plan_id}")
def replay(session: SessionDep, scenario: ScenarioDep, plan_id: str) -> dict:
    """Event/plan sequence leading up to ``plan_id``, for the replay control."""
    plan = scenario_repo.get_plan(session, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan: {plan_id}")

    rows = scenario_repo.ledger_rows(session, scenario.id)
    verification = scenario_repo.verify_ledger(session, scenario.id)
    chain = plan.id
    steps = [
        {
            "sequence": row.sequence,
            "occurred_at": row.occurred_at,
            "action": row.action,
            "actor_role": row.actor_role,
            "entity_ref": row.entity_ref,
            "description": row.description,
            "payload": row.payload_json,
            "entry_hash": row.entry_hash,
            "is_on_plan_path": plan.id in row.entity_ref or plan.id in str(row.payload_json),
        }
        for row in rows
    ]

    return {
        "plan_id": plan.id,
        "scenario_id": scenario.id,
        "plan_version": plan.version,
        "plan_status": plan.status,
        "plan_variant": plan.variant,
        "steps": steps,
        "chain_status": verification.status,
        "head_hash": verification.head_hash,
        "plan_path_chain": chain,
        "note": (
            "Replays the recorded decision sequence for this plan version. No state is changed "
            "by reading the replay."
        ),
    }


__all__ = ["router"]