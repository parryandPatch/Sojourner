"""Proposal routes: list, inspect, approve, reject.

Approval is Commander-only and is the single path that activates a plan version.
Every non-Commander role gets HTTP 403 from the dependency guard, not from here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import CommanderDep, ScenarioDep, SessionDep
from app.api.websocket import LiveMessage, hub
from app.domain.enums import LedgerAction, PlanStatus, ProposalStatus
from app.repositories import scenario_repo
from app.services import state_hub
from app.services.plan_auditor import audit_plan

router = APIRouter(tags=["proposals"])


@router.get("/proposals")
def list_proposals(
    session: SessionDep,
    scenario: ScenarioDep,
    status_filter: str | None = None,
) -> dict:
    rows = (
        scenario_repo.pending_proposals(session, scenario.id)
        if status_filter == "pending"
        else scenario_repo.all_proposals(session, scenario.id)
    )
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    return {
        "proposals": [
            {
                **state_hub.proposal_out(state, scenario_repo.proposal_to_schema(row)),
                "status_label": state_hub.proposal_status_label(ProposalStatus(row.status)),
            }
            for row in rows
        ],
        "active_plan_id": active.id if active else None,
        "approval_note": state_hub.APPROVAL_WARNING,
    }


@router.get("/proposals/{proposal_id}")
def get_proposal(session: SessionDep, scenario: ScenarioDep, proposal_id: str) -> dict:
    row = scenario_repo.get_proposal(session, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    state = scenario_repo.load_state(session, scenario.id)
    plan = scenario_repo.get_plan(session, row.plan_id)
    report = audit_plan(state, plan.assignments) if plan else None

    return {
        **state_hub.proposal_out(state, scenario_repo.proposal_to_schema(row)),
        "status_label": state_hub.proposal_status_label(ProposalStatus(row.status)),
        "plan": state_hub.plan_out(state, plan, audit=False) if plan else None,
        "auditor_valid": report.valid if report else False,
        "auditor_findings": report.messages() if report else ["Plan not found."],
    }


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    session: SessionDep,
    scenario: ScenarioDep,
    proposal_id: str,
    role: CommanderDep,
    body: dict | None = None,
) -> dict:
    """Commander-only activation. Non-Commander roles are rejected with HTTP 403."""
    payload = body or {}
    note = str(payload.get("note", ""))

    row = scenario_repo.get_proposal(session, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    if ProposalStatus(row.status) != ProposalStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal {proposal_id} is already {ProposalStatus(row.status).value}.",
        )

    state = scenario_repo.load_state(session, scenario.id)
    plan = scenario_repo.get_plan(session, row.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown plan for proposal: {proposal_id}")

    # Re-audit immediately before activation. A plan that no longer validates is not
    # activated, whatever its stored status says.
    report = audit_plan(state, plan.assignments)
    if not report.valid:
        raise HTTPException(
            status_code=409,
            detail=(
                "Refusing to activate: the independent auditor found "
                f"{len(report.errors)} error(s). " + " | ".join(report.messages())
            ),
        )

    scenario_repo.decide_proposal(
        session,
        proposal_id,
        status=ProposalStatus.APPROVED,
        actor=role,
        note=note or "Approved by Commander.",
    )
    activated = scenario_repo.activate_plan(session, row.plan_id, role)
    scenario_repo.supersede_proposals(session, scenario.id)
    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.PROPOSAL_APPROVED,
        entity_ref=proposal_id,
        payload={
            "proposal_id": proposal_id,
            "plan_id": row.plan_id,
            "note": note,
            "metrics": plan.metrics.as_dict(),
            "auditor_valid": True,
        },
        description=f"Commander approved proposal {proposal_id}; plan version {row.plan_id} is now active.",
    )

    hub.broadcast_nowait(
        LiveMessage(
            kind="proposal_approved",
            payload={
                "proposal_id": proposal_id,
                "plan_id": row.plan_id,
                "approved_by": role.value,
                "note": note,
            },
        )
    )

    return {
        "proposal_id": proposal_id,
        "plan_id": row.plan_id,
        "status": ProposalStatus.APPROVED.value,
        "approved_by": role.value,
        "approved_at": activated.approved_at if activated else None,
        "note": note,
        "active_plan": state_hub.plan_out(state, activated) if activated else None,
        "message": (
            f"Proposal {proposal_id} approved by {role.value}. Synthetic plan version "
            f"{row.plan_id} is now the ACTIVE plan."
        ),
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    session: SessionDep,
    scenario: ScenarioDep,
    proposal_id: str,
    role: CommanderDep,
    body: dict | None = None,
) -> dict:
    """Commander-only rejection. The active plan is untouched."""
    payload = body or {}
    note = str(payload.get("note", ""))

    row = scenario_repo.get_proposal(session, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown proposal: {proposal_id}")
    if ProposalStatus(row.status) != ProposalStatus.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Proposal {proposal_id} is already {ProposalStatus(row.status).value}.",
        )

    scenario_repo.decide_proposal(
        session,
        proposal_id,
        status=ProposalStatus.REJECTED,
        actor=role,
        note=note or "Rejected by Commander.",
    )
    scenario_repo.set_plan_status(session, row.plan_id, PlanStatus.REJECTED)
    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.PROPOSAL_REJECTED,
        entity_ref=proposal_id,
        payload={"proposal_id": proposal_id, "plan_id": row.plan_id, "note": note},
        description=f"Commander rejected proposal {proposal_id}; the active plan is unchanged.",
    )

    hub.broadcast_nowait(
        LiveMessage(
            kind="proposal_rejected",
            payload={"proposal_id": proposal_id, "plan_id": row.plan_id, "note": note},
        )
    )

    return {
        "proposal_id": proposal_id,
        "plan_id": row.plan_id,
        "status": ProposalStatus.REJECTED.value,
        "decided_by": role.value,
        "note": note,
        "message": (
            f"Proposal {proposal_id} rejected. The previously approved plan remains active; "
            "nothing was changed."
        ),
    }


__all__ = ["router"]