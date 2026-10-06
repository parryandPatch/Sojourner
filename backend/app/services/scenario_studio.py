"""Seeded, reproducible synthetic scenario generator.

Everything in this module is fictional. No real place names, call signs, routes,
coordinates, inventories or personnel are used.

Reproducibility contract
-----------------------
Given the same ``seed`` and config, :func:`build_scenario` produces a byte-identical
:class:`OperationalState`. All variation comes from a single ``random.Random(seed)``
instance; iteration order over dictionaries is always sorted by id.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

from app.domain.enums import (
    AssetClass,
    AssetStatus,
    CrewRole,
    HubStatus,
    MissionKind,
    MissionStatus,
    PayloadCategory,
    RestrictionType,
)
from app.domain.schemas import (
    GRID_CELL,
    Asset,
    CrewMember,
    HazardZone,
    Hub,
    Mission,
    Observation,
    OperationalState,
    StockItem,
    WeatherWindow,
)

# --------------------------------------------------------------------------------------
# Fixed fictional geography (map space 0..100 by 0..70)
# --------------------------------------------------------------------------------------

HUB_LAYOUT: tuple[tuple[str, str, float, float, int, int], ...] = (
    # id, display name, x, y, runway capacity per slot, slot length (minutes)
    ("HUB-A", "HUB-A (synthetic)", 20.0, 50.0, 2, 30),
    ("HUB-B", "HUB-B (synthetic)", 78.0, 46.0, 2, 30),
    ("HUB-C", "HUB-C (synthetic)", 50.0, 12.0, 2, 30),
)

REGIONS: dict[str, tuple[float, float]] = {
    "RGN-NORTH": (50.0, 62.0),
    "RGN-EAST": (88.0, 30.0),
    "RGN-SOUTH": (30.0, 20.0),
    "RGN-CENTRAL": (50.0, 40.0),
    "RGN-WEST": (14.0, 40.0),
}

# id, home hub, class
ASSET_PLAN: tuple[tuple[str, str, AssetClass], ...] = (
    ("AST-001", "HUB-B", AssetClass.TRANSPORT),
    ("AST-002", "HUB-B", AssetClass.TRANSPORT),
    ("AST-003", "HUB-C", AssetClass.TRANSPORT),
    ("AST-004", "HUB-C", AssetClass.TRANSPORT),
    ("AST-005", "HUB-A", AssetClass.TRANSPORT),
    ("AST-006", "HUB-A", AssetClass.TRANSPORT),  # scripted fault target (Event A)
    ("AST-007", "HUB-A", AssetClass.TRANSPORT),
    ("AST-008", "HUB-A", AssetClass.SCOUT),
    ("AST-009", "HUB-A", AssetClass.SCOUT),
    ("AST-010", "HUB-A", AssetClass.SCOUT),
    ("AST-011", "HUB-B", AssetClass.SCOUT),
    ("AST-012", "HUB-B", AssetClass.SCOUT),
    ("AST-013", "HUB-C", AssetClass.SCOUT),
    ("AST-014", "HUB-A", AssetClass.PATROL),
    ("AST-015", "HUB-A", AssetClass.PATROL),
    ("AST-016", "HUB-B", AssetClass.PATROL),
    ("AST-017", "HUB-B", AssetClass.PATROL),
    ("AST-018", "HUB-C", AssetClass.PATROL),
    ("AST-019", "HUB-A", AssetClass.UTILITY),
    ("AST-020", "HUB-A", AssetClass.UTILITY),
    ("AST-021", "HUB-B", AssetClass.UTILITY),
    ("AST-022", "HUB-C", AssetClass.UTILITY),
)

CLASS_PROFILE: dict[AssetClass, dict[str, float]] = {
    # Speeds are map units per minute; ranges are round-trip map units.
    AssetClass.SCOUT: {"cruise": 1.30, "endurance": (420, 520), "range": (158.0, 195.0)},
    AssetClass.TRANSPORT: {"cruise": 0.95, "endurance": (360, 470), "range": (118.0, 152.0)},
    AssetClass.PATROL: {"cruise": 1.25, "endurance": (400, 500), "range": (142.0, 178.0)},
    AssetClass.UTILITY: {"cruise": 0.80, "endurance": (300, 400), "range": (96.0, 126.0)},
}

# The scripted-fault asset is deliberately the strongest HUB-A transport so that the
# seeded plan assigns it to a P1 mission and Event A is unambiguous.
FORCED_ASSET_PROFILE: dict[str, dict[str, float]] = {
    "AST-006": {"maintenance_hours_since": 6.0, "recent_fault_count": 0, "endurance": 465.0, "cruise": 0.995},
    "AST-005": {"maintenance_hours_since": 61.0, "recent_fault_count": 1, "endurance": 385.0, "cruise": 0.905},
}

CLASS_QUALIFICATIONS: dict[CrewRole, tuple[AssetClass, ...]] = {
    CrewRole.PILOT: (AssetClass.SCOUT, AssetClass.TRANSPORT, AssetClass.PATROL, AssetClass.UTILITY),
    CrewRole.SYSTEMS: (AssetClass.SCOUT, AssetClass.PATROL),
    CrewRole.MISSION_SPECIALIST: (AssetClass.SCOUT, AssetClass.TRANSPORT),
    CrewRole.LOADMASTER: (AssetClass.TRANSPORT, AssetClass.UTILITY),
}

CREW_PLAN: tuple[tuple[str, CrewRole, CrewRole | None], ...] = (
    ("CRW-001", CrewRole.PILOT, None),
    ("CRW-002", CrewRole.PILOT, None),
    ("CRW-003", CrewRole.PILOT, None),
    ("CRW-004", CrewRole.PILOT, CrewRole.MISSION_SPECIALIST),
    ("CRW-005", CrewRole.PILOT, None),
    ("CRW-006", CrewRole.PILOT, None),
    ("CRW-007", CrewRole.PILOT, None),
    ("CRW-008", CrewRole.PILOT, CrewRole.SYSTEMS),
    ("CRW-009", CrewRole.PILOT, None),
    ("CRW-010", CrewRole.PILOT, None),
    ("CRW-011", CrewRole.PILOT, None),
    ("CRW-012", CrewRole.PILOT, CrewRole.LOADMASTER),
    ("CRW-013", CrewRole.PILOT, None),
    ("CRW-014", CrewRole.PILOT, None),
    ("CRW-015", CrewRole.SYSTEMS, None),
    ("CRW-016", CrewRole.SYSTEMS, None),
    ("CRW-017", CrewRole.SYSTEMS, CrewRole.MISSION_SPECIALIST),
    ("CRW-018", CrewRole.SYSTEMS, None),
    ("CRW-019", CrewRole.SYSTEMS, None),
    ("CRW-020", CrewRole.SYSTEMS, None),
    ("CRW-021", CrewRole.SYSTEMS, None),
    ("CRW-022", CrewRole.SYSTEMS, None),
    ("CRW-023", CrewRole.SYSTEMS, None),
    ("CRW-024", CrewRole.SYSTEMS, None),
    ("CRW-025", CrewRole.MISSION_SPECIALIST, None),
    ("CRW-026", CrewRole.MISSION_SPECIALIST, None),
    ("CRW-027", CrewRole.MISSION_SPECIALIST, CrewRole.SYSTEMS),
    ("CRW-028", CrewRole.MISSION_SPECIALIST, None),
    ("CRW-029", CrewRole.MISSION_SPECIALIST, None),
    ("CRW-030", CrewRole.MISSION_SPECIALIST, None),
    ("CRW-031", CrewRole.LOADMASTER, None),
    ("CRW-032", CrewRole.LOADMASTER, None),
    ("CRW-033", CrewRole.LOADMASTER, None),
    ("CRW-034", CrewRole.LOADMASTER, None),
    ("CRW-035", CrewRole.LOADMASTER, None),
    ("CRW-036", CrewRole.LOADMASTER, CrewRole.PILOT),
)


@dataclass(frozen=True, slots=True)
class MissionTemplate:
    id: str
    title: str
    mission_kind: MissionKind
    priority: int
    required_class: AssetClass
    required_payload_category: PayloadCategory | None
    payload_units: int
    origin_hub_id: str
    region_id: str
    earliest_start: int
    latest_end: int
    station_duration_minutes: int
    maximum_risk: float
    required_crew_roles: tuple[CrewRole, ...]
    description: str
    priority_change_target: bool = False


MISSION_TEMPLATES: tuple[MissionTemplate, ...] = (
    MissionTemplate(
        "MIS-001",
        "Priority medical relay to northern grid",
        MissionKind.LOGISTICS,
        1,
        AssetClass.TRANSPORT,
        PayloadCategory.MEDICAL,
        2,
        "HUB-A",
        "RGN-NORTH",
        60,
        330,
        45,
        0.55,
        (CrewRole.PILOT, CrewRole.LOADMASTER),
        "Synthetic medical relay. AST-006 is the strongest HUB-A transport in the seeded state.",
    ),
    MissionTemplate(
        "MIS-002",
        "High-priority synthetic reconnaissance sweep",
        MissionKind.RECONNAISSANCE,
        1,
        AssetClass.SCOUT,
        PayloadCategory.SENSOR_KIT,
        1,
        "HUB-A",
        "RGN-EAST",
        30,
        330,
        60,
        0.60,
        (CrewRole.PILOT, CrewRole.SYSTEMS),
        "Route crosses the central corridor that Event C will expand a hazard zone over.",
    ),
    MissionTemplate(
        "MIS-003",
        "Supply consolidation to eastern grid",
        MissionKind.LOGISTICS,
        2,
        AssetClass.TRANSPORT,
        PayloadCategory.SUPPLY,
        3,
        "HUB-B",
        "RGN-EAST",
        120,
        450,
        50,
        0.60,
        (CrewRole.PILOT, CrewRole.LOADMASTER),
        "Origin hub HUB-B is affected by Event B (closure window).",
    ),
    MissionTemplate(
        "MIS-004",
        "Patrol of southern grid cells",
        MissionKind.PATROL,
        2,
        AssetClass.PATROL,
        None,
        0,
        "HUB-C",
        "RGN-SOUTH",
        90,
        480,
        90,
        0.50,
        (CrewRole.PILOT, CrewRole.SYSTEMS),
        "Long station window, moderate risk ceiling.",
    ),
    MissionTemplate(
        "MIS-005",
        "Survey of eastern approach corridor",
        MissionKind.SURVEY,
        3,
        AssetClass.SCOUT,
        PayloadCategory.SENSOR_KIT,
        1,
        "HUB-B",
        "RGN-CENTRAL",
        240,
        600,
        75,
        0.65,
        (CrewRole.PILOT, CrewRole.SYSTEMS, CrewRole.MISSION_SPECIALIST),
        "Needs a three-role crew, which constrains scheduling.",
    ),
    MissionTemplate(
        "MIS-006",
        "Utility support to western relay point",
        MissionKind.SUPPORT,
        3,
        AssetClass.UTILITY,
        PayloadCategory.SUPPLY,
        2,
        "HUB-A",
        "RGN-WEST",
        300,
        660,
        60,
        0.60,
        (CrewRole.PILOT,),
        "Single-seat option; the only mission with a one-person crew requirement.",
    ),
    MissionTemplate(
        "MIS-007",
        "Medical resupply from southern hub",
        MissionKind.LOGISTICS,
        2,
        AssetClass.TRANSPORT,
        PayloadCategory.MEDICAL,
        2,
        "HUB-C",
        "RGN-CENTRAL",
        150,
        480,
        45,
        0.55,
        (CrewRole.PILOT, CrewRole.LOADMASTER),
        "Competing with MIS-001 and MIS-003 for transports.",
    ),
    MissionTemplate(
        "MIS-008",
        "Extended patrol of northern cells",
        MissionKind.PATROL,
        4,
        AssetClass.PATROL,
        None,
        0,
        "HUB-C",
        "RGN-NORTH",
        60,
        540,
        120,
        0.50,
        (CrewRole.PILOT, CrewRole.SYSTEMS),
        "Long station time; also crosses the Event C corridor.",
    ),
    MissionTemplate(
        "MIS-009",
        "Low-priority survey of south-east cells",
        MissionKind.SURVEY,
        5,
        AssetClass.SCOUT,
        PayloadCategory.SENSOR_KIT,
        1,
        "HUB-B",
        "RGN-SOUTH",
        360,
        690,
        60,
        0.65,
        (CrewRole.PILOT, CrewRole.MISSION_SPECIALIST),
        "Event D raises this mission to priority 1.",
        priority_change_target=True,
    ),
    MissionTemplate(
        "MIS-010",
        "Bulk supply movement north from HUB-A",
        MissionKind.LOGISTICS,
        2,
        AssetClass.TRANSPORT,
        PayloadCategory.SUPPLY,
        4,
        "HUB-A",
        "RGN-NORTH",
        210,
        540,
        40,
        0.60,
        (CrewRole.PILOT, CrewRole.LOADMASTER),
        "Directly competes with MIS-001 for HUB-A transports.",
    ),
    MissionTemplate(
        "MIS-011",
        "Priority patrol of central cells",
        MissionKind.PATROL,
        1,
        AssetClass.PATROL,
        None,
        0,
        "HUB-B",
        "RGN-CENTRAL",
        240,
        540,
        90,
        0.50,
        (CrewRole.PILOT, CrewRole.SYSTEMS),
        "HUB-B origin: constrained by the Event B closure window.",
    ),
    MissionTemplate(
        "MIS-012",
        "Medical support sortie from southern hub",
        MissionKind.SUPPORT,
        4,
        AssetClass.UTILITY,
        PayloadCategory.MEDICAL,
        1,
        "HUB-C",
        "RGN-EAST",
        180,
        660,
        45,
        0.60,
        (CrewRole.PILOT, CrewRole.LOADMASTER),
        "Long-endurance utility sortie.",
    ),
    MissionTemplate(
        "MIS-013",
        "Reconnaissance of eastern boundary",
        MissionKind.RECONNAISSANCE,
        3,
        AssetClass.SCOUT,
        None,
        0,
        "HUB-B",
        "RGN-EAST",
        150,
        480,
        50,
        0.60,
        (CrewRole.PILOT, CrewRole.SYSTEMS),
        "No payload requirement.",
    ),
)

STOCK_LAYOUT: tuple[tuple[str, PayloadCategory, int], ...] = (
    ("HUB-A", PayloadCategory.MEDICAL, 6),
    ("HUB-A", PayloadCategory.SUPPLY, 12),
    ("HUB-A", PayloadCategory.SENSOR_KIT, 5),
    ("HUB-B", PayloadCategory.MEDICAL, 4),
    ("HUB-B", PayloadCategory.SUPPLY, 10),
    ("HUB-B", PayloadCategory.SENSOR_KIT, 4),
    ("HUB-C", PayloadCategory.MEDICAL, 5),
    ("HUB-C", PayloadCategory.SUPPLY, 8),
    ("HUB-C", PayloadCategory.SENSOR_KIT, 4),
)


def rectangle_cells(x0: float, y0: float, x1: float, y1: float) -> frozenset[tuple[int, int]]:
    cells: list[tuple[int, int]] = []
    for cx in range(int(x0 // GRID_CELL), int(x1 // GRID_CELL) + 1):
        for cy in range(int(y0 // GRID_CELL), int(y1 // GRID_CELL) + 1):
            cells.append((cx, cy))
    return frozenset(cells)


@dataclass(frozen=True, slots=True)
class ScenarioConfig:
    seed: int
    name: str
    mission_count: int
    asset_count: int
    crew_count: int
    horizon_hours: int
    scenario_id: str

    @property
    def horizon_minutes(self) -> int:
        return self.horizon_hours * 60

    @property
    def scenario_start(self) -> datetime:
        # Fixed, deterministic epoch for the synthetic scenario.
        return datetime(2026, 3, 2, 6, 0, 0, tzinfo=UTC)


def _build_hubs() -> dict[str, Hub]:
    hubs: dict[str, Hub] = {}
    for hub_id, name, x, y, capacity, slot in HUB_LAYOUT:
        hubs[hub_id] = Hub(
            id=hub_id,
            name=name,
            x=x,
            y=y,
            runway_capacity_per_slot=capacity,
            status=HubStatus.OPEN,
            slot_minutes=slot,
        )
    return hubs


def _build_assets(rng: random.Random, count: int) -> dict[str, Asset]:
    assets: dict[str, Asset] = {}
    ordered = ASSET_PLAN[:count]
    if count > len(ASSET_PLAN):  # pragma: no cover - guarded by API validation
        raise ValueError("asset_count exceeds the seeded asset plan size")

    for asset_id, home_hub, class_ in ordered:
        profile = CLASS_PROFILE[class_]
        forced = FORCED_ASSET_PROFILE.get(asset_id, {})
        endurance_lo, endurance_hi = profile["endurance"]
        cruise = float(forced.get("cruise", profile["cruise"] + rng.uniform(-0.02, 0.02)))
        endurance = int(round(float(forced.get("endurance", rng.uniform(endurance_lo, endurance_hi)))))
        range_units = round(profile["range"][0] + rng.random() * (profile["range"][1] - profile["range"][0]), 1)
        maintenance = float(forced.get("maintenance_hours_since", rng.choice((8.0, 16.0, 26.0, 34.0, 42.0))))
        faults = int(forced.get("recent_fault_count", rng.choice((0, 0, 0, 0, 1, 1, 2))))
        status = AssetStatus.AVAILABLE
        if faults >= 2 and rng.random() < 0.5:
            status = AssetStatus.LIMITED
        available_from = 0
        if rng.random() < 0.18:
            available_from = rng.choice((60, 90, 120))

        readiness = _readiness_fields(maintenance, faults, status)
        assets[asset_id] = Asset(
            id=asset_id,
            class_=class_,
            home_hub_id=home_hub,
            status=status,
            available_from_minute=available_from,
            cruise_speed=round(cruise, 4),
            endurance_minutes=endurance,
            range_units=range_units,
            maintenance_hours_since=maintenance,
            recent_fault_count=faults,
            label=f"{asset_id} ({class_.value.lower()}, {home_hub})",
            **readiness,
        )
    return assets


def _readiness_fields(maintenance: float, faults: int, status: AssetStatus) -> dict[str, float]:
    from app.services.readiness_estimator import estimate_readiness

    readiness = estimate_readiness(maintenance, faults, status)
    return {
        "readiness_probability": readiness.probability,
        "readiness_low": readiness.low,
        "readiness_high": readiness.high,
    }


def _build_crew(rng: random.Random, count: int) -> dict[str, CrewMember]:
    crews: dict[str, CrewMember] = {}
    ordered = CREW_PLAN[:count]
    hub_ids = [h[0] for h in HUB_LAYOUT]
    for index, (crew_id, primary, secondary) in enumerate(ordered):
        roles = (primary,) if secondary is None else (primary, secondary)
        qualified: list[AssetClass] = []
        for role in roles:
            for class_ in CLASS_QUALIFICATIONS[role]:
                if class_ not in qualified:
                    qualified.append(class_)
        home_hub = hub_ids[index % len(hub_ids)]
        available_from = 0
        roll = rng.random()
        if roll < 0.12:
            available_from = 60
        elif roll < 0.18:
            available_from = 120
        duty_used = rng.choice((0, 60, 90, 120, 150, 210))
        duty_limit = rng.choice((540, 600, 600, 660))
        last_duty_end = rng.choice((-120, -60, 0, 0, 30, 45))
        min_rest = 45
        crews[crew_id] = CrewMember(
            id=crew_id,
            roles=roles,
            home_hub_id=home_hub,
            qualified_classes=tuple(sorted(qualified, key=lambda c: c.value)),
            available_from_minute=available_from,
            duty_minutes_used=duty_used,
            duty_limit_minutes=duty_limit,
            last_duty_end_minute=last_duty_end,
            min_rest_minutes=min_rest,
            label=f"{crew_id} ({'+'.join(r.value for r in roles)})",
        )
    return crews


def _build_missions(rng: random.Random, count: int) -> dict[str, Mission]:
    missions: dict[str, Mission] = {}
    templates = MISSION_TEMPLATES[:count]
    if count > len(MISSION_TEMPLATES):  # pragma: no cover - guarded by API validation
        raise ValueError("mission_count exceeds the seeded mission template size")

    for template in templates:
        rx, ry = REGIONS[template.region_id]
        jitter_x = rng.uniform(-3.0, 3.0)
        jitter_y = rng.uniform(-3.0, 3.0)
        missions[template.id] = Mission(
            id=template.id,
            title=template.title,
            mission_kind=template.mission_kind,
            priority=template.priority,
            required_class=template.required_class,
            required_payload_category=template.required_payload_category,
            # Prototype scope: one aircraft per sortie (see docs/LIMITATIONS.md).
            required_asset_count=1,
            origin_hub_id=template.origin_hub_id,
            objective_x=round(min(98.0, max(2.0, rx + jitter_x)), 2),
            objective_y=round(min(68.0, max(2.0, ry + jitter_y)), 2),
            earliest_start=template.earliest_start,
            latest_end=template.latest_end,
            station_duration_minutes=template.station_duration_minutes,
            maximum_risk=template.maximum_risk,
            required_crew_roles=template.required_crew_roles,
            status=MissionStatus.REQUESTED,
            description=template.description,
            region_id=template.region_id,
        )
    return missions


def _build_hazards() -> list[HazardZone]:
    return [
        HazardZone(
            id="HZ-BASE-1",
            name="Synthetic central training exclusion area",
            cells=rectangle_cells(42.0, 22.0, 56.0, 32.0),
            severity=0.35,
            active_start=0,
            active_end=720,
            blocking=False,
        ),
        HazardZone(
            id="HZ-BASE-2",
            name="Synthetic terrain advisory area",
            cells=rectangle_cells(20.0, 18.0, 34.0, 30.0),
            severity=0.25,
            active_start=0,
            active_end=720,
            blocking=False,
        ),
    ]


def _build_weather(horizon: int) -> list[WeatherWindow]:
    return [
        WeatherWindow(
            id="WX-BASE-1",
            name="Synthetic visibility limit (east region)",
            start=int(horizon * 0.35),
            end=int(horizon * 0.60),
            severity=0.20,
            restriction_type=RestrictionType.VISIBILITY_LIMIT,
            region_id="RGN-EAST",
            blocking=False,
        ),
        WeatherWindow(
            id="WX-BASE-2",
            name="Synthetic wind limit (west region)",
            start=int(horizon * 0.45),
            end=int(horizon * 0.75),
            severity=0.25,
            restriction_type=RestrictionType.WIND_LIMIT,
            region_id="RGN-WEST",
            blocking=False,
        ),
    ]


def _build_observations(config: ScenarioConfig) -> list[Observation]:
    """Fused synthetic feed records, mirroring the sources named in the spec."""
    sources = ("fleet-feed", "crew-roster-feed", "stock-feed", "weather-feed", "restriction-feed")
    observations: list[Observation] = []
    observed_at = config.scenario_start
    for index, source in enumerate(sources):
        observations.append(
            Observation(
                id=f"OBS-{index + 1:03d}",
                entity_type="SCENARIO",
                entity_id=config.scenario_id,
                field="feed_ingested",
                value_json={"source": source, "synthetic": True},
                source_name=source,
                observed_at=observed_at,
                confidence=0.9,
            )
        )
    return observations


def build_scenario(config: ScenarioConfig, now_minute: int = 0) -> OperationalState:
    """Build the deterministic operational state for ``config``."""
    rng = random.Random(config.seed)
    state = OperationalState(
        scenario_id=config.scenario_id,
        seed=config.seed,
        scenario_name=config.name,
        scenario_start=config.scenario_start,
        now_minute=now_minute,
        horizon_minutes=config.horizon_minutes,
        hubs=_build_hubs(),
        assets=_build_assets(rng, config.asset_count),
        crews=_build_crew(rng, config.crew_count),
        stock=[StockItem(hub_id=h, category=c, quantity=q) for h, c, q in STOCK_LAYOUT],
        missions=_build_missions(rng, config.mission_count),
        hazards=_build_hazards(),
        weather=_build_weather(config.horizon_minutes),
        events=[],
        observations=_build_observations(config),
    )
    return state


def default_config(
    seed: int,
    *,
    name: str = "Synthetic demonstration scenario",
    mission_count: int = 13,
    asset_count: int = 22,
    crew_count: int = 36,
    horizon_hours: int = 12,
) -> ScenarioConfig:
    return ScenarioConfig(
        seed=seed,
        name=name,
        mission_count=mission_count,
        asset_count=asset_count,
        crew_count=crew_count,
        horizon_hours=horizon_hours,
        scenario_id=f"SCN-{seed}",
    )


# --------------------------------------------------------------------------------------
# Scripted demo events (Event A..D from the spec)
# --------------------------------------------------------------------------------------

SCRIPTED_EVENTS: dict[str, dict] = {
    "EVENT_A": {
        "title": "Event A — asset fault on AST-006",
        "kind": "ASSET_FAULT",
        "description": (
            "AST-006 (transport, HUB-A) develops a synthetic sensor fault before a "
            "priority mission. The asset becomes unavailable for a repair window."
        ),
        "payload": {
            "asset_id": "AST-006",
            "fault_code": "SYNTH-FLT-F2",
            "repair_minutes": 180,
            "note": "synthetic fault, no real-world system involved",
        },
    },
    "EVENT_B": {
        "title": "Event B — HUB-B weather closure",
        "kind": "WEATHER_RESTRICTION",
        "description": (
            "A synthetic weather window closes HUB-B for a fixed block, blocking departures "
            "and arrivals at that hub."
        ),
        "payload": {
            "hub_id": "HUB-B",
            "start_minute": 180,
            "end_minute": 390,
            "severity": 0.90,
            "restriction_type": "CLOSURE",
            "blocking": True,
        },
    },
    "EVENT_C": {
        "title": "Event C — hazard zone expands over central corridor",
        "kind": "HAZARD_ZONE",
        "description": (
            "A high-severity synthetic hazard zone expands across the HUB-A/HUB-C corridor. "
            "Routes crossing it take a large, visible hazard-risk penalty."
        ),
        "payload": {
            "zone_id": "HZ-EVENT-C",
            "name": "Synthetic central corridor expansion",
            "x0": 40.0,
            "y0": 40.0,
            "x1": 54.0,
            "y1": 58.0,
            "severity": 0.85,
            "blocking": False,
            "duration_minutes": 300,
        },
    },
    "EVENT_D": {
        "title": "Event D — mission priority change",
        "kind": "MISSION_PRIORITY_CHANGE",
        "description": "MIS-009 is raised to priority 1 by a synthetic request update.",
        "payload": {"mission_id": "MIS-009", "new_priority": 1},
    },
}