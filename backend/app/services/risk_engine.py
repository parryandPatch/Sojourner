"""Synthetic risk engine.

Every component is stored and displayed; there are no black-box claims.

    hazard_risk    = severity-weighted fraction of the route inside active hazard zones
    weather_risk   = highest restriction severity affecting departure / return / objective
    readiness_risk = 1 - readiness_probability

    combined_risk  = 1 - (1 - hazard_risk) * (1 - weather_risk) * (1 - readiness_risk)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain.enums import AssetStatus, RestrictionType
from app.domain.schemas import (
    GRID_CELL,
    Asset,
    CrewMember,
    HazardZone,
    Hub,
    Mission,
    OperationalState,
    RiskBreakdown,
    WeatherWindow,
    cell_of,
    distance,
)
from app.services.readiness_estimator import estimate_readiness

# Number of samples taken per map unit along a route leg.
SAMPLES_PER_UNIT = 0.8
# Weather that only degrades rather than blocks.
DEGRADING_RESTRICTIONS = (RestrictionType.WIND_LIMIT, RestrictionType.VISIBILITY_LIMIT)


@dataclass(frozen=True, slots=True)
class RouteSample:
    minute: int
    x: float
    y: float
    leg: str


def route_samples(
    origin: Hub,
    objective_x: float,
    objective_y: float,
    transit_minutes: int,
    takeoff_minute: int,
    station_minutes: int,
) -> list[RouteSample]:
    """Sample the outbound, station and return legs at fixed spatial resolution."""
    out_leg = max(1, int(math.ceil(distance(origin.x, origin.y, objective_x, objective_y) * SAMPLES_PER_UNIT)))
    back_leg = max(1, int(math.ceil(distance(origin.x, origin.y, objective_x, objective_y) * SAMPLES_PER_UNIT)))

    samples: list[RouteSample] = []
    for i in range(out_leg + 1):
        frac = i / out_leg
        samples.append(
            RouteSample(
                minute=takeoff_minute + int(round(frac * transit_minutes)),
                x=origin.x + (objective_x - origin.x) * frac,
                y=origin.y + (objective_y - origin.y) * frac,
                leg="outbound",
            )
        )
    for i in range(1, station_samples(station_minutes)):
        frac = i / station_samples(station_minutes)
        samples.append(
            RouteSample(
                minute=takeoff_minute + transit_minutes + int(round(frac * station_minutes)),
                x=objective_x,
                y=objective_y,
                leg="station",
            )
        )
    for i in range(1, back_leg + 1):
        frac = i / back_leg
        samples.append(
            RouteSample(
                minute=takeoff_minute + transit_minutes + station_minutes + int(round(frac * transit_minutes)),
                x=objective_x + (origin.x - objective_x) * frac,
                y=objective_y + (origin.y - objective_y) * frac,
                leg="return",
            )
        )
    return samples


def station_samples(station_minutes: int) -> int:
    return max(1, int(math.ceil(station_minutes * SAMPLES_PER_UNIT)))


def zone_cells_for_geometry(state: OperationalState, x: float, y: float) -> tuple[int, int]:
    return cell_of(x, y)


def hazard_risk_for_route(
    state: OperationalState,
    samples: list[RouteSample],
) -> tuple[float, list[str]]:
    """Severity-weighted fraction of route samples inside active hazard zones."""
    if not samples:
        return 0.0, []
    zones: list[HazardZone] = [
        z for z in state.hazards if z.active_start <= samples[0].minute and z.active_end >= samples[-1].minute
    ]
    if not zones:
        zones = state.hazards
    if not zones:
        return 0.0, []

    zone_map = {z.id: z for z in zones}
    exposed = 0.0
    touched: set[str] = set()
    for sample in samples:
        cell = zone_cells_for_geometry(state, sample.x, sample.y)
        worst = 0.0
        worst_id = ""
        for zone in zones:
            if cell in zone.cells:
                if zone.is_active(sample.minute) and zone.severity > worst:
                    worst = zone.severity
                    worst_id = zone.id
        exposed += worst
        if worst_id:
            touched.add(worst_id)

    risk = exposed / len(samples)
    _ = zone_map  # kept for readability of the routing intent
    return min(1.0, risk), sorted(touched)


def blocking_zone_hit(
    state: OperationalState,
    samples: list[RouteSample],
) -> str | None:
    """Return the id of the first blocking zone the route crosses, if any."""
    for zone in state.hazards:
        if not zone.blocking:
            continue
        for sample in samples:
            if zone.is_active(sample.minute) and zone_cells_for_geometry(state, sample.x, sample.y) in zone.cells:
                return zone.id
    return None


def _weather_severity_for(windows: list[WeatherWindow], start: int, end: int) -> float:
    best = 0.0
    for window in windows:
        if not window.overlaps(start, end):
            continue
        if window.blocking or window.restriction_type in DEGRADING_RESTRICTIONS:
            best = max(best, window.severity)
    return best


def weather_risk_for_sortie(
    state: OperationalState,
    mission: Mission,
    origin: Hub,
    takeoff_minute: int,
    landing_minute: int,
    return_minute: int,
) -> tuple[float, list[str]]:
    hub_windows = state.weather_for_hub(mission.origin_hub_id)
    region_windows = state.weather_for_region(mission.region_id)

    components = {
        "departure": _weather_severity_for(hub_windows, takeoff_minute, takeoff_minute),
        "return": _weather_severity_for(hub_windows, return_minute, return_minute),
        "objective": _weather_severity_for(region_windows, landing_minute, landing_minute),
    }
    _ = origin
    risk = min(1.0, max(components.values()))
    touched = sorted(
        {
            w.id
            for w in (*hub_windows, *region_windows)
            if w.overlaps(takeoff_minute, return_minute) and (w.blocking or w.restriction_type in DEGRADING_RESTRICTIONS)
        }
    )
    return risk, touched


def hub_blocking_window(state: OperationalState, hub_id: str, minute: int) -> WeatherWindow | None:
    for window in state.weather_for_hub(hub_id):
        if window.blocking and window.restriction_type == RestrictionType.CLOSURE and window.covers(minute):
            return window
    return None


def compute_risk(
    state: OperationalState,
    mission: Mission,
    asset: Asset,
    crew: tuple[CrewMember, ...],
    *,
    takeoff_minute: int,
    landing_minute: int,
    return_minute: int,
) -> RiskBreakdown:
    """Compute the transparent three-component risk score for a proposed sortie."""
    origin = state.hub(mission.origin_hub_id)
    transit = max(0, landing_minute - takeoff_minute)
    samples = route_samples(
        origin,
        mission.objective_x,
        mission.objective_y,
        transit,
        takeoff_minute,
        mission.station_duration_minutes,
    )
    hazard, _zones = hazard_risk_for_route(state, samples)
    weather, _windows = weather_risk_for_sortie(state, mission, origin, takeoff_minute, landing_minute, return_minute)

    if asset.status == AssetStatus.UNAVAILABLE:
        readiness_probability = 0.0
    else:
        readiness_probability = estimate_readiness(
            asset.maintenance_hours_since, asset.recent_fault_count, asset.status
        ).probability
    readiness = min(1.0, max(0.0, 1.0 - readiness_probability))
    _ = crew

    combined = 1.0 - (1.0 - hazard) * (1.0 - weather) * (1.0 - readiness)
    return RiskBreakdown(
        hazard_risk=hazard,
        weather_risk=weather,
        readiness_risk=readiness,
        combined=min(1.0, combined),
    )


__all__ = [
    "GRID_CELL",
    "RouteSample",
    "blocking_zone_hit",
    "compute_risk",
    "hazard_risk_for_route",
    "hub_blocking_window",
    "route_samples",
    "weather_risk_for_sortie",
]