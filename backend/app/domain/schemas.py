"""In-memory operational state plus the Pydantic v2 API schemas.

`OperationalState` is the single fused view every engine reads. It is built
deterministically from a scenario seed and then overlaid with persisted event
effects (see `app.services.state_hub`).

Time
----
Externally: UTC ISO-8601 timestamps.
Internally: integer minutes since ``scenario_start``. All planning arithmetic is
integer minutes so a given seed always produces the same plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import (
    ActorRole,
    AssetClass,
    AssetStatus,
    CrewRole,
    EventKind,
    EventStatus,
    HubStatus,
    LedgerAction,
    MissionKind,
    MissionStatus,
    PayloadCategory,
    PlanStatus,
    PlanVariant,
    ProposalStatus,
    ReasonCode,
    RestrictionType,
    ZoneSeverityBand,
)

# --------------------------------------------------------------------------------------
# Map space
# --------------------------------------------------------------------------------------

MAP_WIDTH_UNITS = 100.0
MAP_HEIGHT_UNITS = 70.0
GRID_CELL = 5  # grid cell size in map units


def distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x2 - x1, y2 - y1)


def cell_of(x: float, y: float) -> tuple[int, int]:
    return (int(math.floor(x / GRID_CELL)), int(math.floor(y / GRID_CELL)))


# --------------------------------------------------------------------------------------
# Core state entities
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Hub:
    id: str
    name: str
    x: float
    y: float
    runway_capacity_per_slot: int = 1
    status: HubStatus = HubStatus.OPEN
    slot_minutes: int = 30

    def slot_of(self, minute: int) -> int:
        return minute // self.slot_minutes


@dataclass(frozen=True, slots=True)
class Asset:
    id: str
    class_: AssetClass
    home_hub_id: str
    status: AssetStatus = AssetStatus.AVAILABLE
    available_from_minute: int = 0
    cruise_speed: float = 0.4  # map units per minute
    endurance_minutes: int = 300
    range_units: float = 90.0
    maintenance_hours_since: float = 0.0
    recent_fault_count: int = 0
    readiness_probability: float = 0.98
    readiness_low: float = 0.90
    readiness_high: float = 0.99
    label: str = ""

    @property
    def is_flyable(self) -> bool:
        return self.status not in (AssetStatus.UNAVAILABLE,)


@dataclass(frozen=True, slots=True)
class CrewMember:
    id: str
    roles: tuple[CrewRole, ...]
    home_hub_id: str
    qualified_classes: tuple[AssetClass, ...]
    available_from_minute: int = 0
    duty_minutes_used: int = 0
    duty_limit_minutes: int = 600
    last_duty_end_minute: int = -10_000
    min_rest_minutes: int = 45
    label: str = ""


@dataclass(frozen=True, slots=True)
class StockItem:
    hub_id: str
    category: PayloadCategory
    quantity: int


@dataclass(frozen=True, slots=True)
class Mission:
    id: str
    title: str
    mission_kind: MissionKind
    priority: int
    required_class: AssetClass
    required_payload_category: PayloadCategory | None
    required_asset_count: int
    origin_hub_id: str
    objective_x: float
    objective_y: float
    earliest_start: int
    latest_end: int
    station_duration_minutes: int
    maximum_risk: float
    required_crew_roles: tuple[CrewRole, ...]
    status: MissionStatus = MissionStatus.REQUESTED
    description: str = ""
    region_id: str | None = None

    @property
    def priority_weight(self) -> int:
        from app.domain.enums import priority_weight

        return priority_weight(self.priority)


@dataclass(frozen=True, slots=True)
class HazardZone:
    id: str
    name: str
    cells: frozenset[tuple[int, int]]
    severity: float
    active_start: int
    active_end: int
    blocking: bool = False
    source_event_id: str | None = None

    def is_active(self, minute: int) -> bool:
        return self.active_start <= minute <= self.active_end

    @property
    def band(self) -> ZoneSeverityBand:
        if self.severity >= 0.75:
            return ZoneSeverityBand.RESTRICTED
        if self.severity >= 0.45:
            return ZoneSeverityBand.ELEVATED
        return ZoneSeverityBand.ADVISORY


@dataclass(frozen=True, slots=True)
class WeatherWindow:
    id: str
    name: str
    start: int
    end: int
    severity: float
    restriction_type: RestrictionType
    hub_id: str | None = None
    region_id: str | None = None
    blocking: bool = True
    source_event_id: str | None = None

    def covers(self, minute: int) -> bool:
        return self.start <= minute <= self.end

    def overlaps(self, start_minute: int, end_minute: int) -> bool:
        return self.start <= end_minute and start_minute <= self.end


@dataclass(frozen=True, slots=True)
class Observation:
    id: str
    entity_type: str
    entity_id: str
    field: str
    value_json: dict
    source_name: str
    observed_at: datetime
    confidence: float


@dataclass(frozen=True, slots=True)
class SyntheticEvent:
    id: str
    kind: EventKind
    occurred_at: datetime
    occurred_minute: int
    label: str
    payload: dict
    status: EventStatus = EventStatus.OPEN
    source: str = "scripted"
    actor_role: ActorRole = ActorRole.PLANNER


# --------------------------------------------------------------------------------------
# Plan entities
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskBreakdown:
    """Transparent synthetic risk components. No black-box claims."""

    hazard_risk: float
    weather_risk: float
    readiness_risk: float
    combined: float

    def as_dict(self) -> dict:
        return {
            "hazard_risk": round(self.hazard_risk, 4),
            "weather_risk": round(self.weather_risk, 4),
            "readiness_risk": round(self.readiness_risk, 4),
            "combined": round(self.combined, 4),
            "formula": "1 - (1-hazard)*(1-weather)*(1-readiness)",
        }


@dataclass(frozen=True, slots=True)
class Assignment:
    id: str
    mission_id: str
    asset_id: str
    crew_ids: tuple[str, ...]
    payload_category: PayloadCategory | None
    payload_units: int
    takeoff_minute: int
    landing_minute: int
    return_minute: int
    transit_minutes: int
    risk: float
    risk_breakdown: RiskBreakdown
    status: MissionStatus = MissionStatus.PLANNED
    is_frozen: bool = False
    reason_codes: tuple[ReasonCode, ...] = ()

    @property
    def sortie_minutes(self) -> int:
        return self.return_minute - self.takeoff_minute

    def signature(self) -> tuple:
        """Identity of an assignment, used for plan diffing and stability checks."""
        return (self.mission_id, self.asset_id, tuple(sorted(self.crew_ids)), self.takeoff_minute)


@dataclass(frozen=True, slots=True)
class PlanMetrics:
    missions_total: int
    missions_covered: int
    p1_covered: int
    p1_total: int
    weighted_coverage: float
    coverage_ratio: float
    mean_risk: float
    max_risk: float
    changed_assignments: int
    frozen_assignments: int
    risk_savings_vs_coverage_first: float = 0.0

    def as_dict(self) -> dict:
        return {
            "missions_total": self.missions_total,
            "missions_covered": self.missions_covered,
            "p1_covered": self.p1_covered,
            "p1_total": self.p1_total,
            "weighted_coverage": round(self.weighted_coverage, 2),
            "coverage_ratio": round(self.coverage_ratio, 4),
            "mean_risk": round(self.mean_risk, 4),
            "max_risk": round(self.max_risk, 4),
            "changed_assignments": self.changed_assignments,
            "frozen_assignments": self.frozen_assignments,
            "risk_savings_vs_coverage_first": round(self.risk_savings_vs_coverage_first, 4),
        }


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    version: int
    status: PlanStatus
    variant: PlanVariant
    assignments: tuple[Assignment, ...]
    metrics: PlanMetrics
    created_at: datetime
    parent_plan_id: str | None = None
    approved_at: datetime | None = None
    approved_by: ActorRole | None = None
    is_fallback: bool = False
    solver_status: str = "UNKNOWN"
    solve_time_ms: int = 0
    note: str = ""

    @property
    def assignment_by_mission(self) -> dict[str, Assignment]:
        return {a.mission_id: a for a in self.assignments}


# --------------------------------------------------------------------------------------
# Operational state
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class OperationalState:
    """Fused view of every synthetic feed, in one object."""

    scenario_id: str
    seed: int
    scenario_name: str
    scenario_start: datetime
    now_minute: int
    horizon_minutes: int
    hubs: dict[str, Hub]
    assets: dict[str, Asset]
    crews: dict[str, CrewMember]
    stock: list[StockItem]
    missions: dict[str, Mission]
    hazards: list[HazardZone] = field(default_factory=list)
    weather: list[WeatherWindow] = field(default_factory=list)
    events: list[SyntheticEvent] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)

    # -- time -----------------------------------------------------------------
    def minute_to_iso(self, minute: int) -> str:
        return (self.scenario_start + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")

    @property
    def now_iso(self) -> str:
        return self.minute_to_iso(self.now_minute)

    # -- lookups --------------------------------------------------------------
    def hub(self, hub_id: str) -> Hub:
        return self.hubs[hub_id]

    def asset(self, asset_id: str) -> Asset:
        return self.assets[asset_id]

    def crew(self, crew_id: str) -> CrewMember:
        return self.crews[crew_id]

    def mission(self, mission_id: str) -> Mission:
        return self.missions[mission_id]

    def stock_at(self, hub_id: str, category: PayloadCategory) -> int:
        return sum(s.quantity for s in self.stock if s.hub_id == hub_id and s.category == category)

    # -- derived collections --------------------------------------------------
    def active_hazards(self, minute: int) -> list[HazardZone]:
        return [z for z in self.hazards if z.is_active(minute)]

    def blocking_hazards(self, minute: int) -> list[HazardZone]:
        return [z for z in self.hazards if z.blocking and z.is_active(minute)]

    def weather_for_hub(self, hub_id: str) -> list[WeatherWindow]:
        return [w for w in self.weather if w.hub_id == hub_id]

    def weather_for_region(self, region_id: str | None) -> list[WeatherWindow]:
        if region_id is None:
            return []
        return [w for w in self.weather if w.region_id == region_id]

    def missions_in_priority_order(self) -> list[Mission]:
        return sorted(self.missions.values(), key=lambda m: (m.priority, m.latest_end, m.id))

    def assignable_missions(self) -> list[Mission]:
        return [m for m in self.missions.values() if m.status != MissionStatus.CANCELLED]

    # -- cloning --------------------------------------------------------------
    def clone(self) -> OperationalState:
        """Deep-enough copy for the what-if sandbox. Never mutates the live state."""
        return replace(
            self,
            hubs=dict(self.hubs),
            assets=dict(self.assets),
            crews=dict(self.crews),
            stock=list(self.stock),
            missions=dict(self.missions),
            hazards=list(self.hazards),
            weather=list(self.weather),
            events=list(self.events),
            observations=list(self.observations),
        )

    def snapshot_digest(self) -> str:
        """Cheap deterministic digest, used to prove what-if never mutates live state."""
        import hashlib
        import json

        payload = {
            "assets": sorted((a.id, a.status.value, round(a.readiness_probability, 4)) for a in self.assets.values()),
            "missions": sorted((m.id, m.priority, m.status.value) for m in self.missions.values()),
            "hazards": sorted((z.id, round(z.severity, 3), z.active_start, z.active_end) for z in self.hazards),
            "weather": sorted((w.id, round(w.severity, 3), w.start, w.end) for w in self.weather),
            "stock": sorted((s.hub_id, s.category.value, s.quantity) for s in self.stock),
            "now_minute": self.now_minute,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# API schemas
# --------------------------------------------------------------------------------------


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(ApiModel):
    status: str
    app: str
    version: str
    synthetic_data_notice: str
    advisory_notice: str
    database: str


class ErrorResponse(ApiModel):
    detail: str
    synthetic_data_notice: str = "SYNTHETIC DEMONSTRATION DATA"
    advisory_notice: str = "ADVISORY ONLY — HUMAN APPROVAL REQUIRED"


class RoleRequest(ApiModel):
    actor_role: ActorRole
    note: str = ""


class ClockRequest(ApiModel):
    advance_to_minute: int = Field(ge=0)
    actor_role: ActorRole = ActorRole.PLANNER


class ScenarioGenerateRequest(ApiModel):
    seed: int | None = None
    name: str = "Synthetic demonstration scenario"
    mission_count: int | None = Field(default=None, ge=4, le=40)
    asset_count: int | None = Field(default=None, ge=6, le=40)
    crew_count: int | None = Field(default=None, ge=8, le=80)
    horizon_hours: int | None = Field(default=None, ge=2, le=24)
    actor_role: ActorRole = ActorRole.PLANNER


class ScenarioSummary(ApiModel):
    scenario_id: str
    name: str
    seed: int
    created_at: datetime
    now_minute: int
    now_iso: str
    horizon_minutes: int
    counts: dict[str, int]


class HubOut(ApiModel):
    id: str
    name: str
    x: float
    y: float
    runway_capacity_per_slot: int
    status: HubStatus
    slot_minutes: int


class AssetOut(ApiModel):
    id: str
    label: str
    class_: AssetClass
    home_hub_id: str
    status: AssetStatus
    available_from_minute: int
    cruise_speed: float
    endurance_minutes: int
    range_units: float
    maintenance_hours_since: float
    recent_fault_count: int
    readiness_probability: float
    readiness_low: float
    readiness_high: float
    readiness_method: str = "synthetic heuristic"
    assigned_mission_id: str | None = None


class CrewOut(ApiModel):
    id: str
    label: str
    roles: list[CrewRole]
    home_hub_id: str
    qualified_classes: list[AssetClass]
    available_from_minute: int
    duty_minutes_used: int
    duty_limit_minutes: int
    last_duty_end_minute: int
    min_rest_minutes: int


class StockOut(ApiModel):
    hub_id: str
    category: PayloadCategory
    quantity: int


class MissionOut(ApiModel):
    id: str
    title: str
    mission_kind: MissionKind
    priority: int
    priority_weight: int
    required_class: AssetClass
    required_payload_category: PayloadCategory | None
    required_asset_count: int
    origin_hub_id: str
    objective_x: float
    objective_y: float
    earliest_start: int
    latest_end: int
    station_duration_minutes: int
    maximum_risk: float
    required_crew_roles: list[CrewRole]
    status: MissionStatus
    region_id: str | None = None
    description: str = ""
    earliest_start_iso: str = ""
    latest_end_iso: str = ""


class HazardZoneOut(ApiModel):
    id: str
    name: str
    cells: list[list[int]]
    severity: float
    band: ZoneSeverityBand
    blocking: bool
    active_start: int
    active_end: int
    source_event_id: str | None = None


class WeatherWindowOut(ApiModel):
    id: str
    name: str
    hub_id: str | None = None
    region_id: str | None = None
    start: int
    end: int
    severity: float
    restriction_type: RestrictionType
    blocking: bool
    source_event_id: str | None = None


class EventOut(ApiModel):
    id: str
    kind: EventKind
    occurred_at: datetime
    occurred_minute: int
    label: str
    payload: dict
    status: EventStatus
    source: str
    affected_mission_ids: list[str] = Field(default_factory=list)


class RiskBreakdownOut(ApiModel):
    hazard_risk: float
    weather_risk: float
    readiness_risk: float
    combined: float
    formula: str


class AssignmentOut(ApiModel):
    id: str
    mission_id: str
    asset_id: str
    crew_ids: list[str]
    payload_category: PayloadCategory | None
    payload_units: int
    takeoff_minute: int
    landing_minute: int
    return_minute: int
    transit_minutes: int
    takeoff_iso: str = ""
    landing_iso: str = ""
    return_iso: str = ""
    risk: float
    risk_breakdown: RiskBreakdownOut
    status: MissionStatus
    is_frozen: bool
    reason_codes: list[ReasonCode] = Field(default_factory=list)
    explanation: str = ""


class PlanMetricsOut(ApiModel):
    missions_total: int
    missions_covered: int
    p1_covered: int
    p1_total: int
    weighted_coverage: float
    coverage_ratio: float
    mean_risk: float
    max_risk: float
    changed_assignments: int
    frozen_assignments: int
    risk_savings_vs_coverage_first: float


class PlanOut(ApiModel):
    id: str
    version: int
    status: PlanStatus
    variant: PlanVariant
    is_fallback: bool
    solver_status: str
    solve_time_ms: int
    created_at: datetime
    parent_plan_id: str | None = None
    approved_at: datetime | None = None
    approved_by: ActorRole | None = None
    metrics: PlanMetricsOut
    assignments: list[AssignmentOut]
    unassigned_mission_ids: list[str] = Field(default_factory=list)
    auditor_valid: bool = True
    auditor_findings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PlanSetOut(ApiModel):
    parent_plan_id: str | None = None
    trigger_event_id: str | None = None
    coverage_reference: float | None = None
    plans: list[PlanOut]
    solve_time_ms_total: int = 0
    distinct_variants: int = 0


class FeasibilityOut(ApiModel):
    mission_id: str
    feasible: bool
    option_count: int
    best_option: AssignmentOut | None = None
    blocking_reason_counts: dict[ReasonCode, int] = Field(default_factory=dict)
    candidates_examined: int = 0
    explanations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProposalDiffOut(ApiModel):
    added: list[dict] = Field(default_factory=list)
    removed: list[dict] = Field(default_factory=list)
    changed: list[dict] = Field(default_factory=list)
    unchanged_count: int = 0
    frozen_held_count: int = 0


@dataclass(slots=True)
class Proposal:
    """A stored re-plan option awaiting a Commander decision. Never self-activating."""

    id: str
    plan_id: str
    rank: int
    label: PlanVariant
    is_fallback: bool
    diff: ProposalDiffOut
    status: ProposalStatus = ProposalStatus.PENDING
    parent_plan_id: str | None = None
    trigger_event_id: str | None = None
    explanations: list[str] = field(default_factory=list)
    key_reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    decided_by: ActorRole | None = None
    decision_note: str = ""


class ProposalOut(ApiModel):
    id: str
    parent_plan_id: str | None = None
    trigger_event_id: str | None = None
    plan_id: str
    rank: int
    label: PlanVariant
    is_fallback: bool
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: ActorRole | None = None
    decision_note: str = ""
    metrics: PlanMetricsOut
    diff: ProposalDiffOut
    explanations: list[str] = Field(default_factory=list)
    key_reasons: list[str] = Field(default_factory=list)
    approval_warning: str = "Approval activates this synthetic plan version."


class WhatIfEventSpec(ApiModel):
    kind: EventKind
    payload: dict = Field(default_factory=dict)


class WhatIfRequest(ApiModel):
    events: list[WhatIfEventSpec] = Field(default_factory=list)
    overrides: dict = Field(default_factory=dict)
    label: str = "what-if experiment"
    actor_role: ActorRole = ActorRole.PLANNER


class WhatIfComparisonRow(ApiModel):
    mission_id: str
    active_asset_id: str | None = None
    experiment_asset_id: str | None = None
    active_takeoff: int | None = None
    experiment_takeoff: int | None = None
    changed: bool = False


class WhatIfResultOut(ApiModel):
    label: str
    state_digest_before: str
    state_digest_after: str
    active_plan_id: str | None = None
    experiment_plan: PlanOut | None = None
    comparison: list[WhatIfComparisonRow] = Field(default_factory=list)
    coverage_delta: float = 0.0
    mean_risk_delta: float = 0.0
    isolation_verified: bool = True
    notes: list[str] = Field(default_factory=list)


class LedgerEntryOut(ApiModel):
    id: int
    sequence: int
    occurred_at: datetime
    actor_role: ActorRole
    action: LedgerAction
    entity_ref: str
    description: str
    payload: dict
    previous_hash: str
    entry_hash: str


class LedgerOut(ApiModel):
    scenario_id: str
    entries: list[LedgerEntryOut]
    chain_status: str
    chain_length: int
    head_hash: str
    verification_detail: dict


class KpiTile(BaseModel):
    key: str
    label: str
    value: str
    detail: str = ""
    tone: str = "neutral"


class OverviewOut(ApiModel):
    scenario_id: str
    scenario_name: str
    synthetic_data_notice: str = "SYNTHETIC DEMONSTRATION DATA"
    advisory_notice: str = "ADVISORY ONLY — HUMAN APPROVAL REQUIRED"
    now_minute: int
    now_iso: str
    horizon_minutes: int
    active_plan: PlanOut | None = None
    pending_proposals: int = 0
    kpis: list[KpiTile] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    recent_events: list[EventOut] = Field(default_factory=list)
    readiness_alerts: list[dict] = Field(default_factory=list)
    affected_mission_ids: list[str] = Field(default_factory=list)
    chain_status: str = "VALID"


class MapOut(ApiModel):
    width_units: float
    height_units: float
    grid_cell: int
    now_minute: int
    hubs: list[dict] = Field(default_factory=list)
    assets: list[dict] = Field(default_factory=list)
    missions: list[dict] = Field(default_factory=list)
    hazard_zones: list[HazardZoneOut] = Field(default_factory=list)
    weather_windows: list[WeatherWindowOut] = Field(default_factory=list)
    assignments: list[dict] = Field(default_factory=list)
    legend: list[dict] = Field(default_factory=list)


class AssetConditionUpdateRequest(ApiModel):
    asset_id: str
    status: AssetStatus | None = None
    maintenance_hours_since: float | None = Field(default=None, ge=0, le=2000)
    recent_fault_count: int | None = Field(default=None, ge=0, le=20)
    available_from_minute: int | None = Field(default=None, ge=0)
    note: str = "maintainer demo action"


class ReplayStepOut(ApiModel):
    sequence: int
    occurred_at: datetime
    action: LedgerAction
    actor_role: ActorRole
    entity_ref: str
    description: str
    payload: dict
    entry_hash: str


class ReplayOut(ApiModel):
    plan_id: str
    scenario_id: str
    steps: list[ReplayStepOut]
    chain_status: str
    head_hash: str


class EventInjectRequest(ApiModel):
    kind: EventKind | None = None
    scripted_event: str | None = None
    payload: dict = Field(default_factory=dict)
    actor_role: ActorRole = ActorRole.PLANNER


class EventInjectResultOut(ApiModel):
    event: EventOut
    affected_mission_ids: list[str]
    frozen_mission_ids: list[str]
    proposals: list[ProposalOut] = Field(default_factory=list)
    proposals_generated: bool = False
    requires_commander_approval: bool = True
    message: str = ""


class ScriptedEventInfo(ApiModel):
    key: str
    title: str
    kind: EventKind
    description: str
    payload_preview: dict