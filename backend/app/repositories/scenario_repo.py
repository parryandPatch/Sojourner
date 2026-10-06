"""Repository layer: all database reads and writes live here.

Services never touch the session directly, which keeps the engines testable against
an in-memory state without a database.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

from sqlmodel import Session, delete, select

from app.domain.enums import (
    ActorRole,
    AssetStatus,
    EventKind,
    EventStatus,
    LedgerAction,
    PlanStatus,
    PlanVariant,
    ProposalStatus,
    RestrictionType,
)
from app.domain.models import (
    AssetConditionRow,
    AssignmentRow,
    EventRow,
    HazardZoneRow,
    LedgerRow,
    ObservationRow,
    PlanRow,
    ProposalRow,
    ScenarioRow,
    WeatherWindowRow,
)
from app.domain.schemas import (
    Assignment,
    HazardZone,
    OperationalState,
    Plan,
    PlanMetrics,
    Proposal,
    ProposalDiffOut,
    RiskBreakdown,
    SyntheticEvent,
    WeatherWindow,
)
from app.domain.schemas import (
    PlanVariant as PlanVariantEnum,
)
from app.domain.schemas import (
    ProposalStatus as ProposalStatusEnum,
)
from app.services.decision_ledger import GENESIS_HASH, build_entry, verify_chain
from app.services.readiness_estimator import estimate_readiness
from app.services.scenario_studio import ScenarioConfig, build_scenario, default_config

# --------------------------------------------------------------------------------------
# Scenario
# --------------------------------------------------------------------------------------


def active_scenario(session: Session) -> ScenarioRow | None:
    return session.exec(select(ScenarioRow).where(ScenarioRow.is_active == True)).first()  # noqa: E712


def get_scenario(session: Session, scenario_id: str) -> ScenarioRow | None:
    return session.get(ScenarioRow, scenario_id)


def create_scenario(session: Session, config: ScenarioConfig) -> ScenarioRow:
    """Persist a fresh scenario, clearing every scenario-scoped table."""
    clear_scenario_data(session)

    row = ScenarioRow(
        id=config.scenario_id,
        name=config.name,
        seed=config.seed,
        config_json={
            "mission_count": config.mission_count,
            "asset_count": config.asset_count,
            "crew_count": config.crew_count,
            "horizon_hours": config.horizon_hours,
        },
        created_at=datetime.now(UTC),
        is_active=True,
        now_minute=0,
    )
    session.add(row)

    state = build_scenario(config)
    for observation in state.observations:
        session.add(
            ObservationRow(
                scenario_id=config.scenario_id,
                entity_type=observation.entity_type,
                entity_id=observation.entity_id,
                field=observation.field,
                value_json=observation.value_json,
                source_name=observation.source_name,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
            )
        )
    session.commit()
    session.refresh(row)
    return row


def clear_scenario_data(session: Session) -> None:
    """Wipe every scenario-scoped table (used by reset)."""
    for model in (
        AssignmentRow,
        ProposalRow,
        PlanRow,
        EventRow,
        HazardZoneRow,
        WeatherWindowRow,
        AssetConditionRow,
        ObservationRow,
        LedgerRow,
        ScenarioRow,
    ):
        session.exec(delete(model))
    session.commit()


def reset_scenario(session: Session, *, seed: int, name: str, mission_count: int, asset_count: int,
                   crew_count: int, horizon_hours: int) -> ScenarioRow:
    config = default_config(
        seed,
        name=name,
        mission_count=mission_count,
        asset_count=asset_count,
        crew_count=crew_count,
        horizon_hours=horizon_hours,
    )
    return create_scenario(session, config)


def set_now_minute(session: Session, scenario_id: str, now_minute: int) -> None:
    row = get_scenario(session, scenario_id)
    if row is not None:
        row.now_minute = now_minute
        session.add(row)
        session.commit()


# --------------------------------------------------------------------------------------
# State reconstruction
# --------------------------------------------------------------------------------------


def load_state(session: Session, scenario_id: str) -> OperationalState:
    """Rebuild the operational state: seeded base + persisted event overlay."""
    row = get_scenario(session, scenario_id)
    if row is None:
        raise LookupError(f"unknown scenario: {scenario_id}")

    config = default_config(
        row.seed,
        name=row.name,
        mission_count=int(row.config_json.get("mission_count", 13)),
        asset_count=int(row.config_json.get("asset_count", 22)),
        crew_count=int(row.config_json.get("crew_count", 36)),
        horizon_hours=int(row.config_json.get("horizon_hours", 12)),
    )
    config = dataclasses.replace(config, scenario_id=row.id)
    state = build_scenario(config, now_minute=row.now_minute)

    assets = dict(state.assets)
    for condition in session.exec(
        select(AssetConditionRow).where(AssetConditionRow.scenario_id == scenario_id)
    ).all():
        base = assets.get(condition.asset_id)
        if base is None:
            continue
        hours = condition.maintenance_hours_since
        faults = condition.recent_fault_count
        readiness = estimate_readiness(hours, faults, condition.status)
        assets[condition.asset_id] = dataclasses.replace(
            base,
            status=condition.status,
            available_from_minute=(
                condition.available_from_minute
                if condition.available_from_minute is not None
                else base.available_from_minute
            ),
            maintenance_hours_since=hours,
            recent_fault_count=faults,
            readiness_probability=readiness.probability,
            readiness_low=readiness.low,
            readiness_high=readiness.high,
        )
    state.assets = assets

    hazards = list(state.hazards)
    for zone in session.exec(select(HazardZoneRow).where(HazardZoneRow.scenario_id == scenario_id)).all():
        hazards.append(
            HazardZone(
                id=zone.id,
                name=zone.name,
                cells=frozenset((int(c[0]), int(c[1])) for c in zone.cells),
                severity=zone.severity,
                active_start=zone.active_start_minute,
                active_end=zone.active_end_minute,
                blocking=zone.blocking,
                source_event_id=zone.source_event_id,
            )
        )
    state.hazards = hazards

    weather = list(state.weather)
    for window in session.exec(
        select(WeatherWindowRow).where(WeatherWindowRow.scenario_id == scenario_id)
    ).all():
        weather.append(
            WeatherWindow(
                id=window.id,
                name=window.name,
                start=window.start_minute,
                end=window.end_minute,
                severity=window.severity,
                restriction_type=RestrictionType(window.restriction_type),
                hub_id=window.hub_id,
                region_id=window.region_id,
                blocking=window.blocking,
                source_event_id=window.source_event_id,
            )
        )
    state.weather = weather

    events = [
        SyntheticEvent(
            id=event.id,
            kind=EventKind(event.kind),
            occurred_at=event.occurred_at,
            occurred_minute=event.occurred_minute,
            label=event.label,
            payload=event.payload_json,
            status=EventStatus(event.status),
            source=event.source,
            actor_role=ActorRole(event.actor_role),
        )
        for event in session.exec(
            select(EventRow).where(EventRow.scenario_id == scenario_id).order_by(EventRow.occurred_minute)
        ).all()
    ]
    state.events = events

    observations = [
        _observation_from_row(row)
        for row in session.exec(
            select(ObservationRow).where(ObservationRow.scenario_id == scenario_id)
        ).all()
    ]
    state.observations = observations or state.observations
    return state


def _observation_from_row(row: ObservationRow):  # type: ignore[no-untyped-def]
    from app.domain.schemas import Observation

    return Observation(
        id=f"OBS-{row.id}",
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        field=row.field,
        value_json=row.value_json,
        source_name=row.source_name,
        observed_at=row.observed_at,
        confidence=row.confidence,
    )


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


def persist_event(session: Session, scenario_id: str, event: SyntheticEvent, working: OperationalState) -> None:
    """Store the event plus any hazard zone / weather window it introduced."""
    session.add(
        EventRow(
            id=event.id,
            scenario_id=scenario_id,
            kind=event.kind,
            occurred_at=event.occurred_at,
            occurred_minute=event.occurred_minute,
            label=event.label,
            payload_json=dict(event.payload),
            status=EventStatus(event.status),
            source=event.source,
            actor_role=event.actor_role,
        )
    )
    for zone in working.hazards:
        if zone.source_event_id == event.id or (
            zone.id in str(event.payload.get("zone_id", "")) and zone.id not in _base_zone_ids()
        ):
            session.add(
                HazardZoneRow(
                    id=zone.id,
                    scenario_id=scenario_id,
                    name=zone.name,
                    cells=[list(c) for c in sorted(zone.cells)],
                    severity=zone.severity,
                    blocking=zone.blocking,
                    active_start_minute=zone.active_start,
                    active_end_minute=zone.active_end,
                    source_event_id=event.id,
                )
            )
    for window in working.weather:
        if window.source_event_id == event.id or window.id == str(event.payload.get("window_id", "")):
            session.add(
                WeatherWindowRow(
                    id=f"{window.id}-{event.id}",
                    scenario_id=scenario_id,
                    name=window.name,
                    hub_id=window.hub_id,
                    region_id=window.region_id,
                    start_minute=window.start,
                    end_minute=window.end,
                    severity=window.severity,
                    restriction_type=RestrictionType(window.restriction_type),
                    blocking=window.blocking,
                    source_event_id=event.id,
                )
            )
    session.commit()


def _base_zone_ids() -> set[str]:
    from app.services.scenario_studio import build_scenario as _build
    from app.services.scenario_studio import default_config as _default

    return {z.id for z in _build(_default(1)).hazards}


def mark_event_processed(session: Session, event_id: str, status: EventStatus = EventStatus.PROCESSED) -> None:
    row = session.get(EventRow, event_id)
    if row is not None:
        row.status = status
        session.add(row)
        session.commit()


# --------------------------------------------------------------------------------------
# Asset conditions
# --------------------------------------------------------------------------------------


def upsert_asset_condition(
    session: Session,
    scenario_id: str,
    *,
    asset_id: str,
    status: AssetStatus,
    maintenance_hours_since: float,
    recent_fault_count: int,
    available_from_minute: int | None,
    note: str,
    actor_role: ActorRole,
) -> None:
    existing = session.exec(
        select(AssetConditionRow)
        .where(AssetConditionRow.scenario_id == scenario_id)
        .where(AssetConditionRow.asset_id == asset_id)
    ).first()
    if existing is None:
        existing = AssetConditionRow(scenario_id=scenario_id, asset_id=asset_id)
    existing.status = status
    existing.maintenance_hours_since = maintenance_hours_since
    existing.recent_fault_count = recent_fault_count
    existing.available_from_minute = available_from_minute
    existing.note = note
    existing.actor_role = actor_role
    existing.updated_at = datetime.now(UTC)
    session.add(existing)
    session.commit()


# --------------------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------------------


def next_plan_version(session: Session, scenario_id: str) -> int:
    plans = session.exec(select(PlanRow).where(PlanRow.scenario_id == scenario_id)).all()
    return max((p.version for p in plans), default=0) + 1


def persist_plan(session: Session, scenario_id: str, plan: Plan, *, version: int, status: PlanStatus,
                 parent_plan_id: str | None, note: str = "") -> Plan:
    row = PlanRow(
        id=plan.id,
        scenario_id=scenario_id,
        version=version,
        parent_plan_id=parent_plan_id,
        status=status,
        variant=PlanVariant(plan.variant.value),
        is_fallback=plan.is_fallback,
        solver_status=plan.solver_status,
        solve_time_ms=plan.solve_time_ms,
        created_at=plan.created_at,
        metrics_json=plan.metrics.as_dict(),
        note=note,
    )
    session.add(row)
    for assignment in plan.assignments:
        session.add(_assignment_row(plan.id, assignment, frozen=assignment.is_frozen))
    session.commit()
    return plan


def _assignment_row(plan_id: str, assignment: Assignment, *, frozen: bool = False) -> AssignmentRow:
    return AssignmentRow(
        id=f"{plan_id}:{assignment.mission_id}",
        plan_id=plan_id,
        mission_id=assignment.mission_id,
        asset_id=assignment.asset_id,
        crew_ids=list(assignment.crew_ids),
        payload_category=assignment.payload_category,
        payload_units=assignment.payload_units,
        takeoff_minute=assignment.takeoff_minute,
        landing_minute=assignment.landing_minute,
        return_minute=assignment.return_minute,
        transit_minutes=assignment.transit_minutes,
        risk=assignment.risk,
        risk_detail_json=assignment.risk_breakdown.as_dict(),
        status=assignment.status,
        is_frozen=frozen,
        reason_codes=[c.value for c in assignment.reason_codes],
    )


def _assignment_from_row(row: AssignmentRow) -> Assignment:
    from app.domain.enums import MissionStatus, PayloadCategory, ReasonCode

    detail = row.risk_detail_json or {}
    return Assignment(
        id=row.id,
        mission_id=row.mission_id,
        asset_id=row.asset_id,
        crew_ids=tuple(row.crew_ids or ()),
        payload_category=PayloadCategory(row.payload_category) if row.payload_category else None,
        payload_units=row.payload_units,
        takeoff_minute=row.takeoff_minute,
        landing_minute=row.landing_minute,
        return_minute=row.return_minute,
        transit_minutes=row.transit_minutes,
        risk=row.risk,
        risk_breakdown=RiskBreakdown(
            hazard_risk=float(detail.get("hazard_risk", 0.0)),
            weather_risk=float(detail.get("weather_risk", 0.0)),
            readiness_risk=float(detail.get("readiness_risk", 0.0)),
            combined=float(detail.get("combined", row.risk)),
        ),
        status=MissionStatus(row.status),
        is_frozen=row.is_frozen,
        reason_codes=tuple(ReasonCode(c) for c in (row.reason_codes or [])),
    )


def get_plan(session: Session, plan_id: str) -> Plan | None:
    row = session.get(PlanRow, plan_id)
    if row is None:
        return None
    assignments = tuple(
        _assignment_from_row(a)
        for a in session.exec(
            select(AssignmentRow).where(AssignmentRow.plan_id == plan_id).order_by(AssignmentRow.mission_id)
        ).all()
    )
    metrics = PlanMetrics(**_metrics_kwargs(row.metrics_json or {}))
    return Plan(
        id=row.id,
        version=row.version,
        status=PlanStatus(row.status),
        variant=PlanVariantEnum(row.variant),
        assignments=assignments,
        metrics=metrics,
        created_at=row.created_at,
        parent_plan_id=row.parent_plan_id,
        approved_at=row.approved_at,
        approved_by=ActorRole(row.approved_by) if row.approved_by else None,
        is_fallback=row.is_fallback,
        solver_status=row.solver_status,
        solve_time_ms=row.solve_time_ms,
        note=row.note,
    )


def _metrics_kwargs(payload: dict) -> dict:
    allowed = {
        "missions_total",
        "missions_covered",
        "p1_covered",
        "p1_total",
        "weighted_coverage",
        "coverage_ratio",
        "mean_risk",
        "max_risk",
        "changed_assignments",
        "frozen_assignments",
        "risk_savings_vs_coverage_first",
    }
    filtered = {k: v for k, v in payload.items() if k in allowed}
    filtered.setdefault("missions_total", 0)
    filtered.setdefault("missions_covered", 0)
    filtered.setdefault("p1_covered", 0)
    filtered.setdefault("p1_total", 0)
    filtered.setdefault("weighted_coverage", 0.0)
    filtered.setdefault("coverage_ratio", 0.0)
    filtered.setdefault("mean_risk", 0.0)
    filtered.setdefault("max_risk", 0.0)
    filtered.setdefault("changed_assignments", 0)
    filtered.setdefault("frozen_assignments", 0)
    filtered.setdefault("risk_savings_vs_coverage_first", 0.0)
    return filtered


def active_plan(session: Session, scenario_id: str) -> Plan | None:
    row = session.exec(
        select(PlanRow)
        .where(PlanRow.scenario_id == scenario_id)
        .where(PlanRow.status == PlanStatus.ACTIVE)
        .order_by(PlanRow.version.desc())
    ).first()
    if row is None:
        return None
    return get_plan(session, row.id)


def latest_plan(session: Session, scenario_id: str) -> Plan | None:
    """Newest plan of any status for a scenario, regardless of activation.

    Used as the re-plan parent for the very first disruption: the initial frontier is
    DRAFT (nothing is active until a Commander approves), so an event that required an
    ACTIVE plan first would deadlock the demo flow.
    """
    row = session.exec(
        select(PlanRow)
        .where(PlanRow.scenario_id == scenario_id)
        .order_by(PlanRow.version.desc(), PlanRow.id.desc())
    ).first()
    if row is None:
        return None
    return get_plan(session, row.id)


def activate_plan(session: Session, plan_id: str, approved_by: ActorRole) -> Plan | None:
    """Make ``plan_id`` the active plan and supersede whatever was active before."""
    target = session.get(PlanRow, plan_id)
    if target is None:
        return None
    for row in plan_rows(session, target.scenario_id):
        if row.id != plan_id and row.status == PlanStatus.ACTIVE:
            row.status = PlanStatus.SUPERSEDED
            session.add(row)
    target.status = PlanStatus.ACTIVE
    target.approved_at = datetime.now(UTC)
    target.approved_by = approved_by
    session.add(target)
    session.commit()
    return get_plan(session, plan_id)


def plan_rows(session: Session, scenario_id: str) -> list[PlanRow]:
    return list(
        session.exec(
            select(PlanRow).where(PlanRow.scenario_id == scenario_id).order_by(PlanRow.version)
        ).all()
    )


def scenario_plan_ids(session: Session, scenario_id: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [row.id for row in plan_rows(session, scenario_id)]


def mark_plan_rejected(session: Session, plan_id: str) -> None:
    row = session.get(PlanRow, plan_id)
    if row is not None:
        row.status = PlanStatus.REJECTED
        session.add(row)
        session.commit()


def set_plan_status(session: Session, plan_id: str, status: PlanStatus) -> None:
    row = session.get(PlanRow, plan_id)
    if row is not None:
        row.status = status
        session.add(row)
        session.commit()


# --------------------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------------------


def persist_proposal(
    session: Session,
    scenario_id: str,
    *,
    proposal_id: str,
    parent_plan_id: str | None,
    trigger_event_id: str | None,
    plan_id: str,
    rank: int,
    label: PlanVariant,
    is_fallback: bool,
    diff: ProposalDiffOut,
    explanations: list[str],
    key_reasons: list[str],
    metrics: dict,
) -> None:
    session.add(
        ProposalRow(
            id=proposal_id,
            scenario_id=scenario_id,
            parent_plan_id=parent_plan_id,
            trigger_event_id=trigger_event_id,
            plan_id=plan_id,
            rank=rank,
            # The column is named `label`, not `variant`. Passing `variant=` here would
            # silently fall back to the column default, making every stored proposal read
            # back as COVERAGE FIRST regardless of which trade-off it actually was.
            label=label,
            is_fallback=is_fallback,
            diff_json=diff.model_dump(),
            explanation_json={"explanations": explanations, "key_reasons": key_reasons},
            metrics_json=metrics,
            status=ProposalStatus.PENDING,
        )
    )
    session.commit()


def pending_proposals(session: Session, scenario_id: str) -> list[ProposalRow]:
    return list(
        session.exec(
            select(ProposalRow)
            .where(ProposalRow.scenario_id == scenario_id)
            .where(ProposalRow.status == ProposalStatus.PENDING)
            .order_by(ProposalRow.rank)
        ).all()
    )


def all_proposals(session: Session, scenario_id: str) -> list[ProposalRow]:
    return list(
        session.exec(
            select(ProposalRow)
            .where(ProposalRow.scenario_id == scenario_id)
            .order_by(ProposalRow.created_at.desc(), ProposalRow.rank)
        ).all()
    )


def get_proposal(session: Session, proposal_id: str) -> ProposalRow | None:
    return session.get(ProposalRow, proposal_id)


def supersede_proposals(session: Session, scenario_id: str) -> None:
    for row in pending_proposals(session, scenario_id):
        row.status = ProposalStatus.SUPERSEDED
        session.add(row)
    session.commit()


def decide_proposal(
    session: Session,
    proposal_id: str,
    *,
    status: ProposalStatus,
    actor: ActorRole,
    note: str,
) -> ProposalRow | None:
    row = session.get(ProposalRow, proposal_id)
    if row is None:
        return None
    row.status = status
    row.decided_at = datetime.now(UTC)
    row.decided_by = actor
    row.decision_note = note
    session.add(row)
    session.commit()
    return row


def proposal_to_schema(row: ProposalRow) -> Proposal:
    diff_payload = row.diff_json or {}
    key_reasons = (row.explanation_json or {}).get("key_reasons", [])
    explanations = (row.explanation_json or {}).get("explanations", [])
    return Proposal(
        id=row.id,
        parent_plan_id=row.parent_plan_id,
        trigger_event_id=row.trigger_event_id,
        plan_id=row.plan_id,
        rank=row.rank,
        label=PlanVariantEnum(row.label),
        is_fallback=row.is_fallback,
        diff=ProposalDiffOut(**diff_payload),
        explanations=list(explanations),
        key_reasons=list(key_reasons),
        status=ProposalStatusEnum(row.status),
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=ActorRole(row.decided_by) if row.decided_by else None,
        decision_note=row.decision_note,
        metrics=row.metrics_json or {},
    )


# --------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------


def append_ledger(
    session: Session,
    *,
    scenario_id: str,
    actor_role: ActorRole,
    action: LedgerAction,
    entity_ref: str,
    payload: dict,
    description: str = "",
) -> LedgerRow:
    rows = list(
        session.exec(
            select(LedgerRow).where(LedgerRow.scenario_id == scenario_id).order_by(LedgerRow.sequence)
        ).all()
    )
    entry = build_entry(
        rows,
        scenario_id=scenario_id,
        actor_role=actor_role,
        action=action,
        entity_ref=entity_ref,
        payload=payload,
        description=description,
        genesis=GENESIS_HASH,
    )
    session.add(entry)
    session.commit()
    session.refresh(entry)
    return entry


def ledger_rows(session: Session, scenario_id: str) -> list[LedgerRow]:
    return list(
        session.exec(
            select(LedgerRow).where(LedgerRow.scenario_id == scenario_id).order_by(LedgerRow.sequence)
        ).all()
    )


def verify_ledger(session: Session, scenario_id: str):  # type: ignore[no-untyped-def]
    return verify_chain(ledger_rows(session, scenario_id), GENESIS_HASH)