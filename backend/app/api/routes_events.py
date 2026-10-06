"""Event injection routes.

Injecting an event updates the operational state and generates a fresh set of re-plan
proposals. It never activates a plan: the Commander still has to approve one.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.api.deps import ScenarioDep, SessionDep, WriterDep
from app.api.websocket import LiveMessage, hub
from app.config import get_settings
from app.domain.enums import EventKind, EventStatus, LedgerAction, PlanStatus, ProposalStatus
from app.domain.schemas import Proposal, ProposalDiffOut
from app.repositories import scenario_repo
from app.services import disruption_monitor, frontier_explorer, state_hub
from app.services.frontier_explorer import build_frontier, default_note
from app.services.stability_guard import frozen_assignments

router = APIRouter(tags=["events"])


@router.get("/events/scripted")
def scripted_events() -> dict:
    """The four scripted demo events from the spec, with their payloads."""
    return {
        "events": [
            {
                "key": key,
                "title": entry["title"],
                "kind": EventKind(entry["kind"]),
                "description": entry["description"],
                "payload_preview": entry["payload"],
            }
            for key, entry in disruption_monitor.SCRIPTED_EVENTS.items()
        ],
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
    }


@router.post("/events/inject")
def inject_event(
    session: SessionDep,
    scenario: ScenarioDep,
    role: WriterDep,
    body: dict,
) -> dict:
    """Inject a scripted or manual synthetic event and produce re-plan proposals."""
    scripted_key = body.get("scripted_event")
    kind = body.get("kind")
    payload = dict(body.get("payload") or {})

    if scripted_key:
        try:
            kind_value, title, scripted_payload, _description = (
                disruption_monitor.resolve_scripted_event(scripted_key)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        payload = {**scripted_payload, **payload}
    elif kind:
        try:
            kind_value = EventKind(str(kind).upper())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event kind: {kind}. Use one of {[k.value for k in EventKind]}.",
            ) from exc
        title = f"Manual synthetic {kind_value.value}"
    else:
        raise HTTPException(
            status_code=400,
            detail="Provide either scripted_event (EVENT_A..EVENT_D) or kind.",
        )

    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    # The re-plan parent is the ACTIVE plan when there is one. For the very first
    # disruption there is no active plan yet (the initial frontier is DRAFT, because
    # nothing may be activated before a Commander approves), so the newest draft becomes
    # the parent instead of rejecting the event. This keeps the demo vertical slice
    # usable: plan -> disruption -> proposal -> approval.
    parent = active or scenario_repo.latest_plan(session, scenario.id)
    if parent is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "No plan to revise yet. Generate a plan set first: events revise an existing "
                "plan, they do not create one."
            ),
        )

    application = disruption_monitor.apply_event(
        state,
        kind_value,
        payload,
        label=title,
        source="scripted" if scripted_key else "manual",
        actor_role=role,
        active_plan=parent,
    )

    # Persist the event and its durable effects (asset condition, zone, weather window).
    scenario_repo.persist_event(session, scenario.id, application.event, application.state)
    _persist_asset_effects(session, scenario.id, application, role)
    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.EVENT_INJECTED,
        entity_ref=application.event.id,
        payload={
            "event_id": application.event.id,
            "kind": kind_value.value,
            "payload": payload,
            "affected_mission_ids": application.affected_mission_ids,
            "frozen_mission_ids": application.frozen_mission_ids,
        },
        description=application.summary,
    )

    # Re-plan on the updated state, keeping started assignments frozen.
    working_state = scenario_repo.load_state(session, scenario.id)
    settings = get_settings()
    version = scenario_repo.next_plan_version(session, scenario.id)
    frontier = build_frontier(
        working_state,
        scenario_id=scenario.id,
        version=version,
        parent_plan=parent,
        affected_mission_ids=set(application.affected_mission_ids),
        time_limit_seconds=settings.solver_time_limit_seconds,
    )

    scenario_repo.supersede_proposals(session, scenario.id)
    trigger_label = frontier_explorer.event_label(application.event)
    explanations = frontier_explorer.frontier_explanations(
        working_state, frontier, parent=parent, trigger_label=trigger_label
    )
    reports = frontier_explorer.feasibility_reports(working_state, parent=parent)

    proposals: list[Proposal] = []
    proposal_id_counter = 1
    for built in frontier.plans:
        label = frontier_explorer.persistable_label(built)
        scenario_repo.persist_plan(
            session,
            scenario.id,
            built.plan,
            version=version,
            status=PlanStatus.PROPOSED,
            parent_plan_id=parent.id,
            note=default_note(label),
        )
        diff = frontier_explorer.diff_plans(
            parent,
            built.plan,
            state=working_state,
            frozen_signatures={
                a.signature()
                for a in frozen_assignments(parent, working_state.now_minute)
            },
        )
        reasons = explanations.get(built.label, [])
        proposal = Proposal(
            id=f"PRP-{version}-{proposal_id_counter:02d}",
            plan_id=built.plan.id,
            rank=frontier_explorer.proposal_rank(built.variant),
            label=label,
            is_fallback=built.plan.is_fallback,
            diff=diff,
            status=ProposalStatus.PENDING,
            parent_plan_id=parent.id,
            trigger_event_id=application.event.id,
            explanations=_mission_lines(working_state, built.plan, reports),
            key_reasons=reasons,
            metrics=built.plan.metrics.as_dict(),
        )
        scenario_repo.persist_proposal(
            session,
            scenario.id,
            proposal_id=proposal.id,
            parent_plan_id=proposal.parent_plan_id,
            trigger_event_id=proposal.trigger_event_id,
            plan_id=proposal.plan_id,
            rank=proposal.rank,
            label=proposal.label,
            is_fallback=proposal.is_fallback,
            diff=proposal.diff,
            explanations=proposal.explanations,
            key_reasons=proposal.key_reasons,
            metrics=proposal.metrics,
        )
        proposals.append(proposal)
        proposal_id_counter += 1

    scenario_repo.mark_event_processed(session, application.event.id, EventStatus.PROCESSED)
    scenario_repo.append_ledger(
        session,
        scenario_id=scenario.id,
        actor_role=role,
        action=LedgerAction.PROPOSALS_GENERATED,
        entity_ref=",".join(p.id for p in proposals),
        payload={
            "proposal_ids": [p.id for p in proposals],
            "trigger_event_id": application.event.id,
            "affected_mission_ids": application.affected_mission_ids,
            "distinct_variants": frontier.distinct_count,
        },
        description=(
            f"Generated {len(proposals)} proposal(s) after {trigger_label}. "
            "None activated; commander approval required."
        ),
    )

    hub.broadcast_nowait(
        LiveMessage(
            kind="event_injected",
            payload={
                "event_id": application.event.id,
                "kind": kind_value.value,
                "summary": application.summary,
                "affected_mission_ids": application.affected_mission_ids,
            },
        )
    )
    hub.broadcast_nowait(
        LiveMessage(
            kind="proposals_generated",
            payload={
                "proposal_ids": [p.id for p in proposals],
                "trigger_event_id": application.event.id,
            },
        )
    )

    return {
        "event": {
            "id": application.event.id,
            "kind": application.event.kind,
            "occurred_at": application.event.occurred_at,
            "occurred_minute": application.event.occurred_minute,
            "label": application.event.label,
            "payload": application.event.payload,
            "status": application.event.status,
            "source": application.event.source,
            "affected_mission_ids": application.affected_mission_ids,
        },
        "affected_mission_ids": application.affected_mission_ids,
        "frozen_mission_ids": application.frozen_mission_ids,
        "parent_plan_id": parent.id,
        "proposals": [state_hub.proposal_out(working_state, p) for p in proposals],
        "proposals_generated": bool(proposals),
        "requires_commander_approval": True,
        "message": (
            application.summary
            + " Affected missions: "
            + (", ".join(application.affected_mission_ids) or "none")
            + ". Proposals are pending; nothing has been activated."
        ),
        "affected_explanations": disruption_monitor.describe_affected(application),
        "distinct_variants": frontier.distinct_count,
        # Reported, not hidden: two options can legitimately collapse onto each other when a
        # disruption leaves only one or two decisions open.
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
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


@router.get("/events")
def list_events(session: SessionDep, scenario: ScenarioDep) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    return {
        "events": [
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
            for event in reversed(state.events)
        ]
    }


def _persist_asset_effects(session, scenario_id: str, application, role) -> None:  # type: ignore[no-untyped-def]
    """Persist an asset-fault effect so the condition survives a state rebuild."""
    if application.event.kind != EventKind.ASSET_FAULT:
        return
    asset_id = str(application.event.payload.get("asset_id", ""))
    asset = application.state.assets.get(asset_id)
    if asset is None:
        return
    scenario_repo.upsert_asset_condition(
        session,
        scenario_id,
        asset_id=asset_id,
        status=asset.status,
        maintenance_hours_since=asset.maintenance_hours_since,
        recent_fault_count=asset.recent_fault_count,
        available_from_minute=asset.available_from_minute,
        note=f"synthetic fault {application.event.payload.get('fault_code', 'SYNTH')}",
        actor_role=role,
    )


def _mission_lines(state, plan, reports) -> list[str]:  # type: ignore[no-untyped-def]
    from app.services.explanation_service import render_plan_explanations

    return render_plan_explanations(state, plan, reports)


__all__ = ["ProposalDiffOut", "router"]