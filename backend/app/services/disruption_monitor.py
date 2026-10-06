"""Disruption monitor.

Applies a synthetic event to an operational state (on a copy, or as a persisted
overlay) and works out which missions are *materially affected* so the planner only
reconsiders those.

Supported event kinds
---------------------
ASSET_FAULT             asset becomes unavailable for a repair window
WEATHER_RESTRICTION     hub or region restriction window (closure by default)
HAZARD_ZONE             hazard zone added or expanded
MISSION_PRIORITY_CHANGE mission priority raised or lowered
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.enums import (
    ActorRole,
    AssetStatus,
    EventKind,
    EventStatus,
    RestrictionType,
)
from app.domain.schemas import (
    HazardZone,
    Mission,
    OperationalState,
    RiskBreakdown,
    SyntheticEvent,
    WeatherWindow,
)
from app.services.risk_engine import route_samples
from app.services.scenario_studio import SCRIPTED_EVENTS, rectangle_cells


@dataclass(slots=True)
class EventApplication:
    """Result of applying one event to a state."""

    state: OperationalState
    event: SyntheticEvent
    affected_mission_ids: list[str] = field(default_factory=list)
    material_reasons: dict[str, list[str]] = field(default_factory=dict)
    summary: str = ""
    frozen_mission_ids: list[str] = field(default_factory=list)


def next_event_id(taken: set[str]) -> str:
    """Lowest free ``EVT-nnn`` identifier."""
    index = 1
    while f"EVT-{index:03d}" in taken:
        index += 1
    return f"EVT-{index:03d}"


# Backwards-compatible alias kept for callers that passed an event object.
def new_event_id(event: SyntheticEvent, taken: set[str]) -> str:  # pragma: no cover - thin alias
    _ = event
    return next_event_id(taken)


def _pending_event_id(state: OperationalState, kind: EventKind) -> str:
    """Event id for the event currently being applied.

    ``apply_event`` allocates the id before mutating the clone, so recomputing it from the
    (still un-appended) event list yields the same value.
    """
    _ = kind
    return next_event_id({event.id for event in state.events})


def resolve_scripted_event(name: str) -> tuple[EventKind, str, dict, str]:
    """Return ``(kind, title, payload, description)`` for a scripted event key."""
    entry = SCRIPTED_EVENTS.get(name.upper())
    if entry is None:
        raise KeyError(f"unknown scripted event: {name}")
    return EventKind(entry["kind"]), entry["title"], dict(entry["payload"]), entry["description"]


# --------------------------------------------------------------------------------------
# Route helpers
# --------------------------------------------------------------------------------------


def route_crosses_zone(state: OperationalState, mission: Mission, zone: HazardZone) -> bool:
    origin = state.hub(mission.origin_hub_id)
    transit = max(1, int(round(_distance(origin, mission) / 0.9)))
    samples = route_samples(
        origin,
        mission.objective_x,
        mission.objective_y,
        transit,
        max(state.now_minute, mission.earliest_start),
        mission.station_duration_minutes,
    )
    from app.domain.schemas import cell_of

    for sample in samples:
        if zone.is_active(sample.minute) and cell_of(sample.x, sample.y) in zone.cells:
            return True
    return False


def _distance(origin, mission: Mission) -> float:
    from app.domain.schemas import distance

    return distance(origin.x, origin.y, mission.objective_x, mission.objective_y)


def mission_touches_hub_closure(
    state: OperationalState, mission: Mission, window: WeatherWindow
) -> bool:
    """Does the hub closure window block this mission's departure or return?"""
    if window.hub_id != mission.origin_hub_id:
        return False
    origin = state.hub(mission.origin_hub_id)
    transit = max(1, int(round(_distance(origin, mission) / 0.9)))
    sortie = 2 * transit + mission.station_duration_minutes
    earliest = max(mission.earliest_start, state.now_minute)
    latest = mission.latest_end - sortie
    if latest < earliest:
        return False
    # The closure blocks any departure/return that falls inside it, so the mission is
    # affected when its feasible departure band overlaps the window.
    return window.start <= latest and earliest <= window.end


def mission_touches_region_weather(state: OperationalState, mission: Mission, window: WeatherWindow) -> bool:
    if window.region_id is not None and mission.region_id == window.region_id:
        return window.overlaps(mission.earliest_start, mission.latest_end)
    return False


def competing_mission_ids(
    state: OperationalState, mission: Mission, candidates: set[str]
) -> list[str]:
    """Missions that share the asset class or hub resources of ``mission``."""
    rivals: list[str] = []
    for other_id in sorted(candidates):
        other = state.missions.get(other_id)
        if other is None or other.id == mission.id:
            continue
        same_class = other.required_class == mission.required_class
        same_hub = other.origin_hub_id == mission.origin_hub_id
        if same_class and same_hub:
            rivals.append(other_id)
    return rivals


# --------------------------------------------------------------------------------------
# Applying events
# --------------------------------------------------------------------------------------


def apply_event(
    state: OperationalState,
    kind: EventKind,
    payload: dict,
    *,
    label: str = "",
    source: str = "scripted",
    actor_role: ActorRole = ActorRole.PLANNER,
    active_plan=None,
    now_minute: int | None = None,
) -> EventApplication:
    """Apply ``kind`` to a *copy* of ``state`` and report the affected missions.

    Never mutates the passed-in state object.
    """
    working = state.clone()
    minute = working.now_minute if now_minute is None else now_minute
    occurred_at = datetime.now(UTC)
    occurred_at = occurred_at.replace(microsecond=0)

    # Allocate the event id up front so the zone/window this event creates can reference it.
    event_id = next_event_id({event.id for event in working.events})

    planned_missions = set(active_plan.assignment_by_mission) if active_plan else set()
    reasons: dict[str, list[str]] = {}

    def mark(mission_id: str, reason: str) -> None:
        reasons.setdefault(mission_id, [])
        if reason not in reasons[mission_id]:
            reasons[mission_id].append(reason)

    summary = ""

    if kind == EventKind.ASSET_FAULT:
        asset_id = payload.get("asset_id")
        repair = int(payload.get("repair_minutes", 180))
        asset = working.assets.get(asset_id)
        if asset is None:
            raise ValueError(f"unknown asset in ASSET_FAULT payload: {asset_id}")
        # An asset that is airborne when the fault is reported finishes its current,
        # already-frozen sortie and only then goes into repair. Modelling it this way keeps
        # frozen assignments valid instead of contradicting the stability rule.
        airborne_return = 0
        if active_plan is not None:
            for assignment in active_plan.assignments:  # type: ignore[union-attr]
                if (
                    assignment.asset_id == asset_id
                    and assignment.takeoff_minute <= minute < assignment.return_minute
                ):
                    airborne_return = max(airborne_return, assignment.return_minute)
        released = max(asset.available_from_minute, minute, airborne_return) + repair
        working.assets[asset_id] = dataclasses.replace(
            asset,
            status=AssetStatus.UNAVAILABLE,
            available_from_minute=released,
            recent_fault_count=asset.recent_fault_count + 1,
            maintenance_hours_since=asset.maintenance_hours_since + repair / 60.0,
            **_readiness(
                asset,
                AssetStatus.UNAVAILABLE,
                asset.recent_fault_count + 1,
                asset.maintenance_hours_since + repair / 60.0,
            ),
        )
        if airborne_return:
            summary = (
                f"{asset_id} reported a synthetic fault at minute {minute} while airborne; it returns at "
                f"minute {airborne_return} and is UNAVAILABLE from then for a repair window of {repair} minutes."
            )
        else:
            summary = (
                f"{asset_id} is UNAVAILABLE from minute {minute} for a synthetic repair window of "
                f"{repair} minutes (released at minute {released})."
            )
        for mission in working.missions.values():
            if mission.required_class != asset.class_ or mission.origin_hub_id != asset.home_hub_id:
                continue
            if mission.id in planned_missions or mission.id in reasons:
                mark(mission.id, f"{asset_id} became unavailable")
        for mission_id in sorted(planned_missions):
            assignment = active_plan.assignment_by_mission.get(mission_id)  # type: ignore[union-attr]
            if assignment is not None and assignment.asset_id == asset_id:
                mark(mission_id, f"{asset_id} became unavailable")

    elif kind == EventKind.WEATHER_RESTRICTION:
        hub_id = payload.get("hub_id")
        region_id = payload.get("region_id")
        start = int(payload.get("start_minute", minute))
        end = int(payload.get("end_minute", minute + 180))
        severity = float(payload.get("severity", 0.8))
        restriction = RestrictionType(payload.get("restriction_type", RestrictionType.CLOSURE))
        blocking = bool(payload.get("blocking", restriction == RestrictionType.CLOSURE))
        window_id = payload.get("window_id", "WX-EVENT")
        window = WeatherWindow(
            id=window_id,
            name=payload.get("name", f"Synthetic {restriction.value} window"),
            start=start,
            end=end,
            severity=severity,
            restriction_type=restriction,
            hub_id=hub_id,
            region_id=region_id,
            blocking=blocking,
            source_event_id=_pending_event_id(state, kind),
        )
        working.weather = [*working.weather, window]
        summary = (
            f"{restriction.value} restriction severity {severity:.2f} on "
            f"{hub_id or region_id} from minute {start} to {end}."
        )
        for mission in working.missions.values():
            if hub_id and mission_touches_hub_closure(working, mission, window):
                mark(mission.id, f"{mission.origin_hub_id} is closed between minutes {start} and {end}")
            elif region_id and mission_touches_region_weather(working, mission, window):
                mark(mission.id, f"weather restriction over {region_id} during the mission window")

    elif kind == EventKind.HAZARD_ZONE:
        zone_id = payload.get("zone_id", "HZ-NEW")
        cells = rectangle_cells(
            float(payload.get("x0", 40.0)),
            float(payload.get("y0", 40.0)),
            float(payload.get("x1", 54.0)),
            float(payload.get("y1", 58.0)),
        )
        duration = int(payload.get("duration_minutes", 300))
        severity = float(payload.get("severity", 0.85))
        blocking = bool(payload.get("blocking", False))
        zone = HazardZone(
            id=zone_id,
            name=payload.get("name", "Synthetic hazard zone"),
            cells=cells,
            severity=severity,
            active_start=minute,
            active_end=minute + duration,
            blocking=blocking,
            source_event_id=_pending_event_id(working, kind),
        )
        working.hazards = [*working.hazards, zone]
        summary = (
            f"Hazard zone {zone_id} active minutes {minute}-{minute + duration} at severity {severity:.2f}"
            f"{' (hard restriction)' if blocking else ''}."
        )
        for mission in working.missions.values():
            if route_crosses_zone(working, mission, zone):
                mark(mission.id, f"route crosses hazard zone {zone_id}")

    elif kind == EventKind.MISSION_PRIORITY_CHANGE:
        mission_id = payload.get("mission_id")
        new_priority = int(payload.get("new_priority", 1))
        mission = working.missions.get(mission_id)
        if mission is None:
            raise ValueError(f"unknown mission in MISSION_PRIORITY_CHANGE payload: {mission_id}")
        old_priority = mission.priority
        working.missions[mission_id] = dataclasses.replace(mission, priority=new_priority)
        summary = f"Mission {mission_id} priority changed from {old_priority} to {new_priority}."
        mark(mission_id, "its own priority changed")
        for rival_id in competing_mission_ids(working, mission, set(working.missions)):
            mark(rival_id, f"competes with {mission_id} for {mission.required_class.value} assets at {mission.origin_hub_id}")
    else:  # pragma: no cover - exhaustive over EventKind
        raise ValueError(f"unsupported event kind: {kind}")

    affected = sorted(reasons)
    frozen: list[str] = []
    if active_plan is not None:
        frozen = [a.mission_id for a in active_plan.assignments if a.takeoff_minute <= minute]

    event = SyntheticEvent(
        id=event_id,
        kind=kind,
        occurred_at=occurred_at,
        occurred_minute=minute,
        label=label or kind.value,
        payload=dict(payload),
        status=EventStatus.OPEN,
        source=source,
        actor_role=actor_role,
    )
    working.events = [*working.events, event]

    return EventApplication(
        state=working,
        event=event,
        affected_mission_ids=affected,
        material_reasons=reasons,
        summary=summary,
        frozen_mission_ids=frozen,
    )





def _readiness(asset, status: AssetStatus, faults: int, hours: float) -> dict[str, float]:
    from app.services.readiness_estimator import estimate_readiness

    readiness = estimate_readiness(hours, faults, status)
    return {
        "readiness_probability": readiness.probability,
        "readiness_low": readiness.low,
        "readiness_high": readiness.high,
    }


def describe_affected(application: EventApplication) -> list[str]:
    """Reason sentences for the disruption centre."""
    lines = [application.summary] if application.summary else []
    for mission_id, reasons in sorted(application.material_reasons.items()):
        lines.append(f"{mission_id}: {'; '.join(reasons)}.")
    if application.frozen_mission_ids:
        lines.append(
            "Frozen (already departed, cannot change): " + ", ".join(sorted(application.frozen_mission_ids)) + "."
        )
    return lines


def whatif_overrides(state: OperationalState, overrides: dict) -> OperationalState:
    """Apply manual field overrides on a cloned state (used by the what-if sandbox)."""
    working = state.clone()
    for key, value in (overrides or {}).items():
        if key.startswith("mission."):
            mission_id = key.split(".", 1)[1]
            mission = working.missions.get(mission_id)
            if mission is None:
                continue
            working.missions[mission_id] = dataclasses.replace(mission, **value)
        elif key.startswith("asset."):
            asset_id = key.split(".", 1)[1]
            asset = working.assets.get(asset_id)
            if asset is None:
                continue
            new_status = value.pop("status", asset.status) if isinstance(value, dict) else asset.status
            working.assets[asset_id] = dataclasses.replace(
                asset,
                status=AssetStatus(new_status),
                **{k: v for k, v in (value or {}).items() if k != "status"},
            )
        elif key == "weather_window":
            working.weather = [
                WeatherWindow(
                    id=w.get("id", "WX-WHATIF"),
                    name=w.get("name", "what-if window"),
                    start=int(w.get("start", 0)),
                    end=int(w.get("end", working.horizon_minutes)),
                    severity=float(w.get("severity", 0.6)),
                    restriction_type=RestrictionType(w.get("restriction_type", RestrictionType.CLOSURE)),
                    hub_id=w.get("hub_id"),
                    region_id=w.get("region_id"),
                    blocking=bool(w.get("blocking", True)),
                )
                for w in (value if isinstance(value, list) else [value])
            ]
        elif key == "hazard_zone":
            working.hazards = [
                *working.hazards,
                *[
                    HazardZone(
                        id=z.get("id", "HZ-WHATIF"),
                        name=z.get("name", "what-if zone"),
                        cells=rectangle_cells(
                            float(z.get("x0", 0.0)),
                            float(z.get("y0", 0.0)),
                            float(z.get("x1", 10.0)),
                            float(z.get("y1", 10.0)),
                        ),
                        severity=float(z.get("severity", 0.7)),
                        active_start=int(z.get("active_start", working.now_minute)),
                        active_end=int(z.get("active_end", working.horizon_minutes)),
                        blocking=bool(z.get("blocking", False)),
                    )
                    for z in (value if isinstance(value, list) else [value])
                ],
            ]
    return working


def zero_risk() -> RiskBreakdown:  # pragma: no cover - convenience for tests
    return RiskBreakdown(0.0, 0.0, 0.0, 0.0)