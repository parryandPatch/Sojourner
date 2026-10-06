"""Feasibility engine.

Implements the ten hard constraints from the spec. Every option keeps structured
reason codes so the explanation service can render sentences from them (never
invented text, never LLM-generated).

Hard constraints
----------------
1. Asset class supports the mission class requirement.
2. Asset is available and not unavailable / under repair.
3. Asset can complete transit plus station time within endurance and range limits.
4. Required payload category exists at the origin hub in sufficient quantity.
5. Required crew roles are qualified, available, rested and within duty limits.
6. Mission can be completed inside its time window.
7. Asset and crew do not overlap with other assignments.
8. Origin hub is open at departure / return times and slot capacity is not exceeded.
9. Route does not cross an active hard-restriction zone.
10. Combined risk does not exceed the mission maximum.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from app.domain.enums import AssetClass, AssetStatus, MissionStatus, PayloadCategory, ReasonCode
from app.domain.schemas import (
    Asset,
    Assignment,
    CrewMember,
    Mission,
    OperationalState,
    RiskBreakdown,
    distance,
)
from app.services.risk_engine import (
    blocking_zone_hit,
    compute_risk,
    hub_blocking_window,
    route_samples,
)

# Crew enumeration limits, chosen so the candidate pool stays small enough for CP-SAT
# while still covering the realistic combinations on the seeded scenario.
CREW_CANDIDATES_PER_ROLE = 12
MAX_CREW_TEAMS = 32
MAX_OPTIONS_PER_MISSION = 48
# Assumed deadhead cruise rate for a crew relocating between hubs (map units / minute).
DEADHEAD_SPEED = 0.55


@dataclass(frozen=True, slots=True)
class CandidateOption:
    """A fully validated (mission, asset, crew, timing) assignment candidate."""

    mission_id: str
    asset_id: str
    asset_class: AssetClass
    crew_ids: tuple[str, ...]
    payload_category: PayloadCategory | None
    payload_units: int
    takeoff_minute: int
    landing_minute: int
    return_minute: int
    transit_minutes: int
    risk: float
    risk_breakdown: RiskBreakdown
    cost_fraction: float
    reason_codes: tuple[ReasonCode, ...]
    blocking_zone_id: str | None = None

    @property
    def sortie_minutes(self) -> int:
        return self.return_minute - self.takeoff_minute

    def to_assignment(self, plan_prefix: str = "ASG") -> Assignment:
        return Assignment(
            id=f"{plan_prefix}-{self.mission_id}-{self.asset_id}-{self.takeoff_minute}",
            mission_id=self.mission_id,
            asset_id=self.asset_id,
            crew_ids=tuple(sorted(self.crew_ids)),
            payload_category=self.payload_category,
            payload_units=self.payload_units,
            takeoff_minute=self.takeoff_minute,
            landing_minute=self.landing_minute,
            return_minute=self.return_minute,
            transit_minutes=self.transit_minutes,
            risk=self.risk,
            risk_breakdown=self.risk_breakdown,
            reason_codes=self.reason_codes,
        )

    def hub_movements(self) -> list[int]:
        """Minutes at which this option touches its origin hub."""
        return [self.takeoff_minute, self.return_minute]


@dataclass(slots=True)
class FeasibilityReport:
    mission_id: str
    options: list[CandidateOption] = field(default_factory=list)
    blocking_counts: Counter = field(default_factory=Counter)
    candidates_examined: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return bool(self.options)

    @property
    def option_count(self) -> int:
        return len(self.options)

    def best(self) -> CandidateOption | None:
        if not self.options:
            return None
        return min(self.options, key=lambda o: (o.takeoff_minute, round(o.risk, 6), o.asset_id, o.crew_ids))


@dataclass(slots=True)
class FeasibilityContext:
    """Everything that narrows the search space beyond the mission itself."""

    now_minute: int
    committed: tuple[Assignment, ...] = ()
    blocked_asset_ids: frozenset[str] = frozenset()
    droppable_mission_ids: frozenset[str] = frozenset()
    extra_hub_load: dict[tuple[str, int], int] = field(default_factory=dict)

    def is_committed_mission(self, mission_id: str) -> bool:
        return any(a.mission_id == mission_id for a in self.committed)


# --------------------------------------------------------------------------------------
# Small pure helpers (each corresponds to one hard constraint, each unit-testable)
# --------------------------------------------------------------------------------------


def check_class_support(asset_class: AssetClass, required_class: AssetClass) -> bool:
    """Rule 1. Prototype scope: the asset class must match the mission requirement."""
    return asset_class == required_class


def check_asset_based_at_origin(asset: Asset, mission: Mission) -> bool:
    """Rule 1b. Prototype scope: inter-hub repositioning flights are not modelled.

    An asset may only serve a mission that departs its home hub. Crew may still
    deadhead between hubs (see :func:`crew_ready_minute`).
    """
    return asset.home_hub_id == mission.origin_hub_id


def check_asset_available(asset: Asset, minute: int) -> bool:
    """Rule 2. UNAVAILABLE assets (e.g. under repair after a fault) can never be used.

    ``minute`` is the earliest departure the mission could realistically use, so an
    asset that is only released later still qualifies as long as it is released before
    the mission has to depart.
    """
    return asset.status != AssetStatus.UNAVAILABLE and asset.available_from_minute <= minute


def transit_minutes_for(origin_x: float, origin_y: float, obj_x: float, obj_y: float, cruise_speed: float) -> int:
    if cruise_speed <= 0:
        return math.inf  # type: ignore[return-value]
    return max(1, int(math.ceil(distance(origin_x, origin_y, obj_x, obj_y) / cruise_speed)))


def check_endurance_and_range(
    asset: Asset, one_way_minutes: int, station_minutes: int, one_way_distance: float
) -> bool:
    """Rule 3."""
    return (
        2 * one_way_minutes + station_minutes <= asset.endurance_minutes
        and 2 * one_way_distance <= asset.range_units
    )


def check_payload(state: OperationalState, mission: Mission) -> bool:
    """Rule 4."""
    if mission.required_payload_category is None:
        return True
    return state.stock_at(mission.origin_hub_id, mission.required_payload_category) >= mission.required_asset_count


def crew_ready_minute(
    crew: CrewMember, state: OperationalState, origin_hub_id: str, earliest_start: int
) -> int:
    """Rule 5 (availability + rest + deadhead). Returns the minute crew can be airborne."""
    hub = state.hub(origin_hub_id)
    home = state.hub(crew.home_hub_id)
    deadhead = 0
    if crew.home_hub_id != origin_hub_id:
        deadhead = max(1, int(math.ceil(distance(home.x, home.y, hub.x, hub.y) / DEADHEAD_SPEED)))
    rested_from = crew.last_duty_end_minute + crew.min_rest_minutes
    base = max(earliest_start, state.now_minute, crew.available_from_minute, rested_from)
    return base + deadhead


def check_crew_duty(crew: CrewMember, sortie_minutes: int) -> bool:
    """Rule 5 (duty limits)."""
    return crew.duty_minutes_used + sortie_minutes <= crew.duty_limit_minutes


def check_time_window(takeoff_minute: int, return_minute: int, mission: Mission) -> bool:
    """Rule 6."""
    return mission.earliest_start <= takeoff_minute and return_minute <= mission.latest_end


def check_no_overlap(
    mission_id: str,
    asset_id: str,
    crew_ids: tuple[str, ...],
    takeoff_minute: int,
    return_minute: int,
    committed: tuple[Assignment, ...],
) -> bool:
    """Rule 7. Half-open intervals: an assignment ending at t does not block one starting at t."""
    for assignment in committed:
        if assignment.mission_id == mission_id:
            continue
        overlaps = assignment.takeoff_minute < return_minute and takeoff_minute < assignment.return_minute
        if not overlaps:
            continue
        if assignment.asset_id == asset_id or set(assignment.crew_ids) & set(crew_ids):
            return False
    return True


def check_hub_open(state: OperationalState, hub_id: str, minutes: list[int]) -> bool:
    """Rule 8 (openness)."""
    return all(hub_blocking_window(state, hub_id, m) is None for m in minutes)


def check_hub_capacity(
    hub_id: str,
    minutes: list[int],
    slot_minutes: int,
    capacity: int,
    extra_load: dict[tuple[str, int], int] | None = None,
) -> bool:
    """Rule 8 (capacity). Movement count per hub per slot must not exceed capacity."""
    extra = extra_load or {}
    load: Counter = Counter()
    for minute in minutes:
        load[(hub_id, minute // slot_minutes)] += 1
    for key, count in load.items():
        if count > capacity:
            return False
        if extra.get(key, 0) + count > capacity:
            return False
    return True


def check_route_clear(state: OperationalState, mission: Mission, option_timing: dict[str, int]) -> bool:
    """Rule 9. Route must not cross an active blocking (hard-restriction) zone."""
    origin = state.hub(mission.origin_hub_id)
    samples = route_samples(
        origin,
        mission.objective_x,
        mission.objective_y,
        option_timing["transit_minutes"],
        option_timing["takeoff_minute"],
        mission.station_duration_minutes,
    )
    return blocking_zone_hit(state, samples) is None


def check_risk_within_max(combined_risk: float, mission: Mission) -> bool:
    """Rule 10."""
    return combined_risk <= mission.maximum_risk + 1e-9


# --------------------------------------------------------------------------------------
# Crew team enumeration
# --------------------------------------------------------------------------------------


def enumerate_crew_teams(
    state: OperationalState, mission: Mission, ctx: FeasibilityContext
) -> list[tuple[str, ...]]:
    """Deterministically build up to ``MAX_CREW_TEAMS`` candidate crews for a mission."""
    if not mission.required_crew_roles:
        return [()]

    per_role: list[list[CrewMember]] = []
    for role in mission.required_crew_roles:
        candidates = [
            crew
            for crew in state.crews.values()
            if role in crew.roles and mission.required_class in crew.qualified_classes
        ]
        candidates.sort(
            key=lambda c: (
                c.available_from_minute,
                c.duty_minutes_used,
                c.last_duty_end_minute,
                c.id,
            )
        )
        if not candidates:
            return []
        per_role.append(candidates[:CREW_CANDIDATES_PER_ROLE])

    teams: list[tuple[str, ...]] = []
    indices = [0] * len(per_role)
    while len(teams) < MAX_CREW_TEAMS:
        team: list[str] = []
        for role_index, role_candidates in enumerate(per_role):
            crew = role_candidates[indices[role_index]]
            if crew.id in team:
                break
            team.append(crew.id)
        else:
            teams.append(tuple(team))

        # Odometer increment over the candidate lists.
        position = len(per_role) - 1
        while position >= 0:
            indices[position] += 1
            if indices[position] < len(per_role[position]):
                break
            indices[position] = 0
            position -= 1
        if position < 0:
            break

    teams.sort(
        key=lambda t: (
            max(crew_ready_minute(state.crews[c], state, mission.origin_hub_id, mission.earliest_start) for c in t),
            sum(state.crews[c].duty_minutes_used for c in t),
            t,
        )
    )
    return teams


# --------------------------------------------------------------------------------------
# Mission-level enumeration
# --------------------------------------------------------------------------------------


class _RiskCache:
    """Risk depends on (mission, asset, takeoff) only, never on crew."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str, int], tuple[RiskBreakdown, str | None]] = {}

    def get(
        self,
        state: OperationalState,
        mission: Mission,
        asset: Asset,
        takeoff_minute: int,
    ) -> tuple[RiskBreakdown, str | None]:
        key = (mission.id, asset.id, takeoff_minute)
        cached = self._store.get(key)
        if cached is not None:
            return cached
        origin = state.hub(mission.origin_hub_id)
        one_way_minutes = transit_minutes_for(
            origin.x, origin.y, mission.objective_x, mission.objective_y, asset.cruise_speed
        )
        landing = takeoff_minute + one_way_minutes
        returning = landing + mission.station_duration_minutes + one_way_minutes
        breakdown = compute_risk(
            state,
            mission,
            asset,
            (),
            takeoff_minute=takeoff_minute,
            landing_minute=landing,
            return_minute=returning,
        )
        samples = route_samples(
            origin,
            mission.objective_x,
            mission.objective_y,
            one_way_minutes,
            takeoff_minute,
            mission.station_duration_minutes,
        )
        blocker = blocking_zone_hit(state, samples)
        self._store[key] = (breakdown, blocker)
        return breakdown, blocker


def cost_fraction(asset: Asset, crew: tuple[CrewMember, ...], sortie_minutes: int) -> float:
    """Transparent resource cost in [0, 1]: half endurance usage, half duty usage."""
    endurance_use = sortie_minutes / asset.endurance_minutes if asset.endurance_minutes else 1.0
    duty_use = 0.0
    if crew:
        duty_use = sum(c.duty_minutes_used + sortie_minutes for c in crew) / sum(
            c.duty_limit_minutes for c in crew
        )
    return min(1.0, 0.5 * endurance_use + 0.5 * duty_use)


def evaluate_mission(
    state: OperationalState,
    mission: Mission,
    ctx: FeasibilityContext,
    risk_cache: _RiskCache | None = None,
    *,
    limit: int | None = None,
) -> FeasibilityReport:
    """Enumerate valid options for one mission, recording per-rule blocking counts."""
    cache = risk_cache or _RiskCache()
    pool_limit = MAX_OPTIONS_PER_MISSION if limit is None else limit
    report = FeasibilityReport(mission_id=mission.id)
    blocking: Counter = Counter()

    if mission.status == MissionStatus.CANCELLED:
        report.notes.append("Mission is cancelled and is not eligible for planning.")
        return report

    origin = state.hub(mission.origin_hub_id)
    one_way_distance = distance(origin.x, origin.y, mission.objective_x, mission.objective_y)
    total_station = mission.station_duration_minutes

    # Rule 4 is mission-level; record it once and stop early when it fails.
    if not check_payload(state, mission):
        blocking[ReasonCode.PAYLOAD_SHORTFALL] += 1
        report.blocking_counts = blocking
        report.notes.append(
            f"{mission.origin_hub_id} does not hold enough {mission.required_payload_category.value} stock."
        )
        return report

    teams = enumerate_crew_teams(state, mission, ctx)
    if not teams:
        blocking[ReasonCode.CREW_QUALIFICATION_MISSING] += 1
        report.blocking_counts = blocking
        report.notes.append("No crew member holds the required role set for this asset class.")
        return report

    for asset_id in sorted(state.assets):
        asset = state.assets[asset_id]
        report.candidates_examined += 1

        if not check_class_support(asset.class_, mission.required_class):
            blocking[ReasonCode.CLASS_MISMATCH] += 1
            continue
        if not check_asset_based_at_origin(asset, mission):
            blocking[ReasonCode.ASSET_UNAVAILABLE] += 1
            continue
        if asset_id in ctx.blocked_asset_ids or not check_asset_available(asset, mission.earliest_start):
            blocking[ReasonCode.ASSET_UNAVAILABLE] += 1
            continue

        one_way_minutes = transit_minutes_for(
            origin.x, origin.y, mission.objective_x, mission.objective_y, asset.cruise_speed
        )
        sortie_minutes = 2 * one_way_minutes + total_station
        if not check_endurance_and_range(asset, one_way_minutes, total_station, one_way_distance):
            blocking[ReasonCode.ENDURANCE_EXCEEDED] += 1
            continue

        latest_takeoff = mission.latest_end - sortie_minutes
        if latest_takeoff < max(mission.earliest_start, ctx.now_minute):
            blocking[ReasonCode.TIME_WINDOW_MISSED] += 1
            continue

        for crew_ids in teams:
            crew = tuple(state.crews[c] for c in crew_ids)
            if not all(check_crew_duty(c, sortie_minutes) for c in crew):
                blocking[ReasonCode.CREW_DUTY_LIMIT_EXCEEDED] += 1
                continue

            ready = max(
                (crew_ready_minute(c, state, mission.origin_hub_id, mission.earliest_start) for c in crew),
                default=ctx.now_minute,
            )
            ready = max(ready, asset.available_from_minute, ctx.now_minute, mission.earliest_start)
            if ready > latest_takeoff:
                # Distinguish "the asset is not released in time" from "the crew is not ready in time".
                if asset.available_from_minute > latest_takeoff:
                    blocking[ReasonCode.TIME_WINDOW_MISSED] += 1
                else:
                    blocking[ReasonCode.CREW_REST_NOT_MET] += 1
                continue

            candidate_takeoffs = sorted({ready, latest_takeoff})
            for takeoff in candidate_takeoffs:
                landing = takeoff + one_way_minutes
                returning = landing + total_station + one_way_minutes

                if not check_time_window(takeoff, returning, mission):
                    blocking[ReasonCode.TIME_WINDOW_MISSED] += 1
                    continue
                if not check_hub_open(state, mission.origin_hub_id, [takeoff, returning]):
                    blocking[ReasonCode.HUB_CLOSED_AT_DEPARTURE] += 1
                    continue
                if not check_hub_capacity(
                    mission.origin_hub_id,
                    [takeoff, returning],
                    origin.slot_minutes,
                    origin.runway_capacity_per_slot,
                    ctx.extra_hub_load,
                ):
                    blocking[ReasonCode.HUB_CAPACITY_EXCEEDED] += 1
                    continue
                if not check_no_overlap(
                    mission.id, asset.id, crew_ids, takeoff, returning, ctx.committed
                ):
                    blocking[ReasonCode.TIME_OVERLAP] += 1
                    continue

                breakdown, blocker = cache.get(state, mission, asset, takeoff)
                if blocker is not None:
                    blocking[ReasonCode.RESTRICTION_ZONE_CROSSED] += 1
                    continue
                if not check_risk_within_max(breakdown.combined, mission):
                    blocking[ReasonCode.RISK_EXCEEDS_MISSION_MAX] += 1
                    continue

                codes = list(
                    (
                        ReasonCode.CLASS_MATCH,
                        ReasonCode.ASSET_AVAILABLE,
                        ReasonCode.WITHIN_ENDURANCE,
                        ReasonCode.WITHIN_RANGE,
                        ReasonCode.PAYLOAD_AVAILABLE,
                        ReasonCode.CREW_QUALIFIED,
                        ReasonCode.CREW_AVAILABLE,
                        ReasonCode.CREW_RESTED,
                        ReasonCode.WITHIN_TIME_WINDOW,
                        ReasonCode.NO_TIME_OVERLAP,
                        ReasonCode.HUB_OPEN_AT_TIMES,
                        ReasonCode.HUB_CAPACITY_OK,
                        ReasonCode.ROUTE_CLEAR,
                        ReasonCode.RISK_WITHIN_MAX,
                    )
                )
                if takeoff == ready:
                    codes.append(ReasonCode.EARLIEST_READY)
                codes.sort(key=lambda c: c.value)

                report.options.append(
                    CandidateOption(
                        mission_id=mission.id,
                        asset_id=asset.id,
                        asset_class=asset.class_,
                        crew_ids=crew_ids,
                        payload_category=mission.required_payload_category,
                        payload_units=mission.required_asset_count,
                        takeoff_minute=takeoff,
                        landing_minute=landing,
                        return_minute=returning,
                        transit_minutes=one_way_minutes,
                        risk=breakdown.combined,
                        risk_breakdown=breakdown,
                        cost_fraction=cost_fraction(asset, crew, sortie_minutes),
                        reason_codes=tuple(codes),
                    )
                )

    # Keep a bounded, deterministic option pool. Truncation is round-robin over
    # (asset, crew) groups so that neither the late-takeoff options (which can dodge a
    # time-limited hazard zone) nor the lower-risk ones are squeezed out.
    report.options.sort(key=lambda o: (o.takeoff_minute, round(o.risk, 6), o.asset_id, o.crew_ids))
    if len(report.options) > pool_limit:
        kept = _diverse_truncate(report.options, pool_limit)
        report.notes.append(
            f"Option pool truncated from {len(report.options)} to {len(kept)} diverse candidates "
            "for solver tractability."
        )
        report.options = kept

    report.blocking_counts = blocking
    return report


def _diverse_truncate(options: list[CandidateOption], limit: int) -> list[CandidateOption]:
    """Round-robin over (asset, crew) groups, best (lowest-risk, earliest) first."""
    groups: dict[tuple[str, tuple[str, ...]], list[CandidateOption]] = {}
    for option in options:
        groups.setdefault((option.asset_id, option.crew_ids), []).append(option)
    ordered_keys = sorted(
        groups,
        key=lambda k: (
            min(round(o.risk, 6) for o in groups[k]),
            min(o.takeoff_minute for o in groups[k]),
            k,
        ),
    )
    for key in ordered_keys:
        groups[key].sort(key=lambda o: (o.takeoff_minute, round(o.risk, 6), o.asset_id))
    kept: list[CandidateOption] = []
    while len(kept) < limit:
        progressed = False
        for key in ordered_keys:
            bucket = groups[key]
            if bucket:
                kept.append(bucket.pop(0))
                progressed = True
                if len(kept) >= limit:
                    break
        if not progressed:
            break
    kept.sort(key=lambda o: (o.takeoff_minute, round(o.risk, 6), o.asset_id, o.crew_ids))
    return kept


def enumerate_options(
    state: OperationalState,
    ctx: FeasibilityContext,
    *,
    missions: list[Mission] | None = None,
) -> tuple[dict[str, list[CandidateOption]], dict[str, FeasibilityReport]]:
    """Evaluate all (or a subset of) missions, sharing one risk cache."""
    risk_cache = _RiskCache()
    target = missions if missions is not None else state.missions_in_priority_order()
    by_mission: dict[str, list[CandidateOption]] = {}
    reports: dict[str, FeasibilityReport] = {}
    for mission in target:
        report = evaluate_mission(state, mission, ctx, risk_cache)
        reports[mission.id] = report
        by_mission[mission.id] = report.options
    return by_mission, reports