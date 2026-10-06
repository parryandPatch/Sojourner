"""SQLModel persistence tables.

Design note
-----------
The *base* scenario entities (hubs, assets, crews, stock, missions) are produced
deterministically by `app.services.scenario_studio` from a seed, so they are not
duplicated in the database. What *is* persisted is everything that mutates during
a demo session:

* the scenario record (seed + active clock),
* asset condition overrides (Maintainer demo action / scripted asset faults),
* hazard zones and weather windows created by events,
* events, plans, assignments, proposals,
* the hash-chained decision ledger,
* observations (fused feed records).

This keeps the state hub cheap to rebuild while preserving a full audit trail.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column
from sqlmodel import JSON, Field, SQLModel

from app.domain.enums import (
    ActorRole,
    AssetStatus,
    EventKind,
    EventStatus,
    LedgerAction,
    MissionStatus,
    PayloadCategory,
    PlanStatus,
    PlanVariant,
    ProposalStatus,
    RestrictionType,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class ScenarioRow(SQLModel, table=True):
    __tablename__ = "scenarios"

    id: str = Field(primary_key=True, index=True)
    name: str
    seed: int
    config_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow)
    is_active: bool = Field(default=False, index=True)
    now_minute: int = 0


class AssetConditionRow(SQLModel, table=True):
    """Mutable synthetic condition overlay for a seeded asset."""

    __tablename__ = "asset_conditions"
    __table_args__ = {"sqlite_autoincrement": True}

    id: int | None = Field(default=None, primary_key=True)
    scenario_id: str = Field(index=True)
    asset_id: str = Field(index=True)
    status: AssetStatus = AssetStatus.AVAILABLE
    maintenance_hours_since: float = 0.0
    recent_fault_count: int = 0
    available_from_minute: int | None = None
    note: str = ""
    actor_role: ActorRole = ActorRole.MAINTAINER
    updated_at: datetime = Field(default_factory=utcnow)


class HazardZoneRow(SQLModel, table=True):
    """Hazard zone, stored as a list of grid cells in the synthetic map space."""

    __tablename__ = "hazard_zones"

    id: str = Field(primary_key=True, index=True)
    scenario_id: str = Field(index=True)
    name: str
    cells: list = Field(default_factory=list, sa_column=Column(JSON))
    severity: float = 0.5
    blocking: bool = False
    active_start_minute: int = 0
    active_end_minute: int = 720
    source_event_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class WeatherWindowRow(SQLModel, table=True):
    __tablename__ = "weather_windows"

    id: str = Field(primary_key=True, index=True)
    scenario_id: str = Field(index=True)
    name: str
    hub_id: str | None = None
    region_id: str | None = None
    start_minute: int = 0
    end_minute: int = 720
    severity: float = 0.5
    restriction_type: RestrictionType = RestrictionType.CLOSURE
    blocking: bool = True
    source_event_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ObservationRow(SQLModel, table=True):
    """Fused synthetic feed record (one entity attribute observation)."""

    __tablename__ = "observations"

    id: int | None = Field(default=None, primary_key=True)
    scenario_id: str = Field(index=True)
    entity_type: str
    entity_id: str
    field: str
    value_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    source_name: str
    observed_at: datetime = Field(default_factory=utcnow)
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class EventRow(SQLModel, table=True):
    __tablename__ = "events"

    id: str = Field(primary_key=True, index=True)
    scenario_id: str = Field(index=True)
    kind: EventKind
    occurred_at: datetime = Field(default_factory=utcnow)
    occurred_minute: int = 0
    label: str = ""
    payload_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: EventStatus = EventStatus.OPEN
    source: str = "scripted"
    actor_role: ActorRole = ActorRole.PLANNER


class PlanRow(SQLModel, table=True):
    __tablename__ = "plans"

    id: str = Field(primary_key=True, index=True)
    scenario_id: str = Field(index=True)
    version: int = 1
    parent_plan_id: str | None = None
    status: PlanStatus = PlanStatus.DRAFT
    variant: PlanVariant = PlanVariant.COVERAGE_FIRST
    is_fallback: bool = False
    solver_status: str = "UNKNOWN"
    solve_time_ms: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    approved_at: datetime | None = None
    approved_by: ActorRole | None = None
    metrics_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    note: str = ""


class AssignmentRow(SQLModel, table=True):
    __tablename__ = "assignments"

    id: str = Field(primary_key=True, index=True)
    plan_id: str = Field(index=True)
    mission_id: str = Field(index=True)
    asset_id: str = Field(index=True)
    crew_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    payload_category: PayloadCategory | None = None
    payload_units: int = 0
    takeoff_minute: int = 0
    landing_minute: int = 0
    return_minute: int = 0
    transit_minutes: int = 0
    risk: float = 0.0
    risk_detail_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: MissionStatus = MissionStatus.PLANNED
    is_frozen: bool = False
    reason_codes: list = Field(default_factory=list, sa_column=Column(JSON))


class ProposalRow(SQLModel, table=True):
    __tablename__ = "proposals"

    id: str = Field(primary_key=True, index=True)
    scenario_id: str = Field(index=True)
    parent_plan_id: str | None = None
    trigger_event_id: str | None = None
    plan_id: str = Field(index=True)
    rank: int = 1
    label: PlanVariant = PlanVariant.COVERAGE_FIRST
    is_fallback: bool = False
    diff_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    explanation_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    metrics_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: ProposalStatus = ProposalStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    decided_by: ActorRole | None = None
    decision_note: str = ""


class LedgerRow(SQLModel, table=True):
    """Hash-chained decision ledger."""

    __tablename__ = "ledger"

    id: int | None = Field(default=None, primary_key=True)
    scenario_id: str = Field(index=True)
    sequence: int = Field(index=True)
    occurred_at: datetime = Field(default_factory=utcnow)
    actor_role: ActorRole
    action: LedgerAction
    entity_ref: str = ""
    payload_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    previous_hash: str = ""
    entry_hash: str = ""
    description: str = ""