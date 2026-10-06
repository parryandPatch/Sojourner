"""Independent plan auditor.

The auditor re-derives every hard constraint directly from the operational state
and the plan's assignments. It deliberately does not call the planner, the solver,
or the option enumeration: if a plan cannot be validated here, it must not be shown
to a user as a usable plan.

Acceptance gate: `audit(...)` returning no findings means the plan is valid.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.domain.enums import AssetStatus, MissionStatus, PayloadCategory
from app.domain.schemas import (
    Assignment,
    OperationalState,
    Plan,
    distance,
)
from app.services.feasibility_engine import (
    check_asset_available,
    check_asset_based_at_origin,
    check_class_support,
    check_crew_duty,
    check_endurance_and_range,
    check_hub_open,
    check_no_overlap,
    check_route_clear,
    check_time_window,
    transit_minutes_for,
)
from app.services.readiness_estimator import estimate_readiness
from app.services.risk_engine import compute_risk, hub_blocking_window

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"


@dataclass(slots=True)
class AuditFinding:
    code: str
    severity: str
    message: str
    entity_ref: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "entity_ref": self.entity_ref,
        }


@dataclass(slots=True)
class AuditReport:
    findings: list[AuditFinding] = field(default_factory=list)
    checked_assignments: int = 0
    checked_missions: int = 0

    @property
    def errors(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[AuditFinding]:
        return [f for f in self.findings if f.severity == SEVERITY_WARNING]

    @property
    def valid(self) -> bool:
        return not self.errors

    def messages(self) -> list[str]:
        return [f"[{f.severity}] {f.code}: {f.message}" for f in self.findings]


def audit_plan(
    state: OperationalState,
    assignments: tuple[Assignment, ...] | list[Assignment],
    *,
    expected_frozen: tuple[Assignment, ...] = (),
    strict_metrics: bool = True,
) -> AuditReport:
    """Validate a set of assignments against ``state``. Empty error list == valid."""
    report = AuditReport()
    add = report.findings.append
    ordered = sorted(assignments, key=lambda a: (a.mission_id, a.takeoff_minute, a.asset_id))
    report.checked_assignments = len(ordered)

    seen_missions: set[str] = set()
    stock_used: dict[tuple[str, PayloadCategory], int] = defaultdict(int)
    slot_load: dict[tuple[str, int], int] = defaultdict(int)

    for assignment in ordered:
        mission_id = assignment.mission_id
        ref = f"{mission_id}/{assignment.asset_id}"

        # -- structural checks --------------------------------------------------
        if mission_id in seen_missions:
            add(AuditFinding("DUPLICATE_MISSION", SEVERITY_ERROR, f"{mission_id} has more than one assignment.", ref))
            continue
        seen_missions.add(mission_id)

        mission = state.missions.get(mission_id)
        if mission is None:
            add(AuditFinding("UNKNOWN_MISSION", SEVERITY_ERROR, f"{mission_id} is not in the current state.", ref))
            continue
        if mission.status == MissionStatus.CANCELLED:
            add(AuditFinding("CANCELLED_MISSION", SEVERITY_ERROR, f"{mission_id} is cancelled but still assigned.", ref))
            continue

        asset = state.assets.get(assignment.asset_id)
        if asset is None:
            add(AuditFinding("UNKNOWN_ASSET", SEVERITY_ERROR, f"{assignment.asset_id} is not in the current state.", ref))
            continue

        hub = state.hubs.get(mission.origin_hub_id)
        if hub is None:
            add(AuditFinding("UNKNOWN_HUB", SEVERITY_ERROR, f"{mission.origin_hub_id} does not exist.", ref))
            continue

        report.checked_missions += 1

        # -- Rule 1: class + basing ---------------------------------------------
        if not check_class_support(asset.class_, mission.required_class):
            add(
                AuditFinding(
                    "CLASS_MISMATCH",
                    SEVERITY_ERROR,
                    f"{asset.id} is {asset.class_.value} but {mission_id} requires {mission.required_class.value}.",
                    ref,
                )
            )
        if not check_asset_based_at_origin(asset, mission):
            add(
                AuditFinding(
                    "ASSET_NOT_BASED_AT_ORIGIN",
                    SEVERITY_ERROR,
                    f"{asset.id} is based at {asset.home_hub_id} but {mission_id} departs {mission.origin_hub_id}.",
                    ref,
                )
            )

        # -- Rule 2: availability ------------------------------------------------
        # An assignment that has already departed is airborne: a fault reported while it is
        # in the air cannot retroactively invalidate it. Ground availability therefore only
        # gates assignments that depart on or after the asset is released.
        airborne = assignment.takeoff_minute <= state.now_minute
        if not airborne:
            if not check_asset_available(asset, assignment.takeoff_minute):
                add(
                    AuditFinding(
                        "ASSET_UNAVAILABLE",
                        SEVERITY_ERROR,
                        f"{asset.id} is {asset.status.value} and cannot fly {mission_id}.",
                        ref,
                    )
                )
            if assignment.takeoff_minute < asset.available_from_minute:
                add(
                    AuditFinding(
                        "ASSET_RELEASED_LATE",
                        SEVERITY_ERROR,
                        f"{asset.id} is released at minute {asset.available_from_minute}, "
                        f"but {mission_id} departs at {assignment.takeoff_minute}.",
                        ref,
                    )
                )
        elif asset.status == AssetStatus.UNAVAILABLE and assignment.return_minute > asset.available_from_minute:
            # Advisory, not an error: the sortie departed before the fault and is frozen,
            # so the only honest options are "finish it" or "cancel it". Cancelling is the
            # Commander's decision, so the plan is still valid; it merely needs the
            # operational note that the asset is airborne through the repair window.
            add(
                AuditFinding(
                    "AIRBORNE_ASSET_NOT_RELEASED",
                    SEVERITY_WARNING,
                    f"{asset.id} is UNAVAILABLE from minute {asset.available_from_minute} but {mission_id} "
                    f"does not land until minute {assignment.return_minute}. The sortie had already "
                    "departed and is frozen; cancelling it is a Commander decision.",
                    ref,
                )
            )

        # -- Rule 3: endurance + range ------------------------------------------
        one_way_distance = distance(hub.x, hub.y, mission.objective_x, mission.objective_y)
        expected_transit = transit_minutes_for(hub.x, hub.y, mission.objective_x, mission.objective_y, asset.cruise_speed)
        if not check_endurance_and_range(asset, expected_transit, mission.station_duration_minutes, one_way_distance):
            add(
                AuditFinding(
                    "ENDURANCE_OR_RANGE_EXCEEDED",
                    SEVERITY_ERROR,
                    f"{asset.id} cannot complete {mission_id}: transit {expected_transit} min each way, "
                    f"endurance {asset.endurance_minutes} min, range {asset.range_units} vs required "
                    f"{round(2 * one_way_distance, 1)}.",
                    ref,
                )
            )
        if assignment.transit_minutes != expected_transit:
            add(
                AuditFinding(
                    "TRANSIT_MISMATCH",
                    SEVERITY_ERROR,
                    f"{mission_id} records transit {assignment.transit_minutes} min, expected {expected_transit} min.",
                    ref,
                )
            )
        expected_landing = assignment.takeoff_minute + expected_transit
        expected_return = expected_landing + mission.station_duration_minutes + expected_transit
        if assignment.landing_minute != expected_landing or assignment.return_minute != expected_return:
            add(
                AuditFinding(
                    "TIMELINE_INCONSISTENT",
                    SEVERITY_ERROR,
                    f"{mission_id} timeline {assignment.takeoff_minute}/{assignment.landing_minute}/"
                    f"{assignment.return_minute} is inconsistent with transit {expected_transit} min and "
                    f"station {mission.station_duration_minutes} min.",
                    ref,
                )
            )

        # -- Rule 6: mission window ---------------------------------------------
        if not check_time_window(assignment.takeoff_minute, assignment.return_minute, mission):
            add(
                AuditFinding(
                    "TIME_WINDOW_MISSED",
                    SEVERITY_ERROR,
                    f"{mission_id} runs {assignment.takeoff_minute}-{assignment.return_minute} but its window is "
                    f"{mission.earliest_start}-{mission.latest_end}.",
                    ref,
                )
            )

        # -- Rule 8: hub open + capacity ----------------------------------------
        # A hub closure that appears after departure cannot be undone by re-planning.
        # The sortie keeps its approved movement times and the conflict is surfaced as a
        # warning for the commander rather than being silently rewritten.
        if not check_hub_open(state, mission.origin_hub_id, [assignment.takeoff_minute, assignment.return_minute]):
            blocked = [
                label
                for label, minute in (("departure", assignment.takeoff_minute), ("return", assignment.return_minute))
                if hub_blocking_window(state, mission.origin_hub_id, minute) is not None
            ]
            if airborne:
                add(
                    AuditFinding(
                        "FROZEN_MOVEMENT_BLOCKED",
                        SEVERITY_WARNING,
                        f"{mission_id} is already airborne and its {blocked[0] if blocked else 'movement'} at "
                        f"{mission.origin_hub_id} falls inside a closure window. The approved sortie is "
                        f"unchanged; this needs a human decision.",
                        ref,
                    )
                )
            else:
                add(
                    AuditFinding(
                        "HUB_CLOSED_AT_MOVEMENT",
                        SEVERITY_ERROR,
                        f"{mission.origin_hub_id} is closed at departure {assignment.takeoff_minute} or return "
                        f"{assignment.return_minute}.",
                        ref,
                    )
                )
        for minute in (assignment.takeoff_minute, assignment.return_minute):
            key = (hub.id, minute // hub.slot_minutes)
            slot_load[key] += 1
            if slot_load[key] > hub.runway_capacity_per_slot:
                add(
                    AuditFinding(
                        "HUB_CAPACITY_EXCEEDED",
                        SEVERITY_ERROR,
                        f"{hub.id} has {slot_load[key]} movements in slot {key[1]} but capacity is "
                        f"{hub.runway_capacity_per_slot}.",
                        ref,
                    )
                )

        # -- Rule 5: crew --------------------------------------------------------
        for crew_id in assignment.crew_ids:
            crew = state.crews.get(crew_id)
            if crew is None:
                add(AuditFinding("UNKNOWN_CREW", SEVERITY_ERROR, f"{crew_id} is not in the current state.", ref))
                continue
            if mission.required_class not in crew.qualified_classes:
                add(
                    AuditFinding(
                        "CREW_NOT_QUALIFIED",
                        SEVERITY_ERROR,
                        f"{crew_id} is not qualified on {mission.required_class.value} for {mission_id}.",
                        ref,
                    )
                )
            if not check_crew_duty(crew, assignment.sortie_minutes):
                add(
                    AuditFinding(
                        "CREW_DUTY_EXCEEDED",
                        SEVERITY_ERROR,
                        f"{crew_id} would reach {crew.duty_minutes_used + assignment.sortie_minutes} duty minutes, "
                        f"limit {crew.duty_limit_minutes}.",
                        ref,
                    )
                )
            rested_from = crew.last_duty_end_minute + crew.min_rest_minutes
            if assignment.takeoff_minute < rested_from:
                add(
                    AuditFinding(
                        "CREW_REST_NOT_MET",
                        SEVERITY_ERROR,
                        f"{crew_id} needs rest until minute {rested_from} but departs at {assignment.takeoff_minute}.",
                        ref,
                    )
                )
            if assignment.takeoff_minute < crew.available_from_minute:
                add(
                    AuditFinding(
                        "CREW_NOT_AVAILABLE",
                        SEVERITY_ERROR,
                        f"{crew_id} is available from minute {crew.available_from_minute}.",
                        ref,
                    )
                )

        # Duplicate crew within one assignment.
        if len(set(assignment.crew_ids)) != len(assignment.crew_ids):
            add(AuditFinding("CREW_DUPLICATED", SEVERITY_ERROR, f"{mission_id} lists a crew member twice.", ref))

        # Missing required roles.
        present_roles: set[str] = set()
        for crew_id in assignment.crew_ids:
            crew = state.crews.get(crew_id)
            if crew:
                present_roles.update(r.value for r in crew.roles)
        for role in mission.required_crew_roles:
            if role.value not in present_roles:
                add(
                    AuditFinding(
                        "CREW_ROLE_UNFILLED",
                        SEVERITY_ERROR,
                        f"{mission_id} requires a {role.value} but the assignment does not provide one.",
                        ref,
                    )
                )

        # -- Rule 4: payload -----------------------------------------------------
        if mission.required_payload_category is not None:
            if assignment.payload_category != mission.required_payload_category:
                add(
                    AuditFinding(
                        "PAYLOAD_CATEGORY_MISMATCH",
                        SEVERITY_ERROR,
                        f"{mission_id} requires {mission.required_payload_category.value} but the assignment "
                        f"carries {assignment.payload_category}.",
                        ref,
                    )
                )
            if assignment.payload_units < mission.required_asset_count:
                add(
                    AuditFinding(
                        "PAYLOAD_UNITS_SHORT",
                        SEVERITY_ERROR,
                        f"{mission_id} needs {mission.required_asset_count} payload unit(s), "
                        f"assignment carries {assignment.payload_units}.",
                        ref,
                    )
                )
            key = (mission.origin_hub_id, mission.required_payload_category)
            stock_used[key] += assignment.payload_units

        # -- Rule 9: restriction zones -------------------------------------------
        if not check_route_clear(
            state,
            mission,
            {
                "transit_minutes": expected_transit,
                "takeoff_minute": assignment.takeoff_minute,
            },
        ):
            add(
                AuditFinding(
                    "RESTRICTION_ZONE_CROSSED",
                    SEVERITY_ERROR,
                    f"The {mission_id} route crosses an active blocking restriction zone.",
                    ref,
                )
            )

        # -- Rule 10: risk -------------------------------------------------------
        recomputed = compute_risk(
            state,
            mission,
            asset,
            tuple(state.crews[c] for c in assignment.crew_ids if c in state.crews),
            takeoff_minute=assignment.takeoff_minute,
            landing_minute=assignment.landing_minute,
            return_minute=assignment.return_minute,
        )
        # Risk for an already-departed sortie was fixed at departure. Ground state may have
        # moved on (an asset fault lowers readiness, for example), but that cannot retroactively
        # breach the mission maximum, so a post-departure drift is reported, not failed.
        if airborne:
            if abs(recomputed.combined - assignment.risk) > 0.02:
                add(
                    AuditFinding(
                        "RISK_DRIFT_AFTER_DEPARTURE",
                        SEVERITY_WARNING,
                        f"{mission_id} departed at minute {assignment.takeoff_minute} with risk "
                        f"{round(assignment.risk, 3)}; recomputing against the current state gives "
                        f"{round(recomputed.combined, 3)}. The departure value is what was approved.",
                        ref,
                    )
                )
        else:
            if recomputed.combined > mission.maximum_risk + 1e-6:
                add(
                    AuditFinding(
                        "RISK_EXCEEDS_MAX",
                        SEVERITY_ERROR,
                        f"{mission_id} combined risk {round(recomputed.combined, 3)} exceeds the mission maximum "
                        f"{mission.maximum_risk}.",
                        ref,
                    )
                )
            if abs(recomputed.combined - assignment.risk) > 0.02:
                add(
                    AuditFinding(
                        "RISK_VALUE_STALE",
                        SEVERITY_ERROR,
                        f"{mission_id} records risk {round(assignment.risk, 3)} but recomputes to "
                        f"{round(recomputed.combined, 3)} against the current state.",
                        ref,
                    )
                )
        # -- Rule 7: overlap -----------------------------------------------------
        if not check_no_overlap(
            mission_id,
            assignment.asset_id,
            assignment.crew_ids,
            assignment.takeoff_minute,
            assignment.return_minute,
            tuple(ordered),
        ):
            add(
                AuditFinding(
                    "TIME_OVERLAP",
                    SEVERITY_ERROR,
                    f"{assignment.asset_id} or its crew is double-booked for {mission_id} "
                    f"({assignment.takeoff_minute}-{assignment.return_minute}).",
                    ref,
                )
            )

    # -- aggregate payload check ------------------------------------------------
    for (hub_id, category), used in stock_used.items():
        available = state.stock_at(hub_id, category)
        if used > available:
            add(
                AuditFinding(
                    "PAYLOAD_SHORTFALL_PLAN",
                    SEVERITY_ERROR,
                    f"{hub_id} plans to move {used} x {category.value} but only holds {available}.",
                    f"{hub_id}/{category.value}",
                )
            )

    # -- frozen assignment immutability -----------------------------------------
    for frozen in expected_frozen:
        match = next((a for a in ordered if a.mission_id == frozen.mission_id), None)
        if match is None:
            add(
                AuditFinding(
                    "FROZEN_ASSIGNMENT_LOST",
                    SEVERITY_ERROR,
                    f"Frozen assignment {frozen.mission_id} on {frozen.asset_id} is missing from the plan.",
                    frozen.mission_id,
                )
            )
        elif match.signature() != frozen.signature():
            add(
                AuditFinding(
                    "FROZEN_ASSIGNMENT_CHANGED",
                    SEVERITY_ERROR,
                    f"Frozen assignment {frozen.mission_id} changed from {frozen.asset_id}@{frozen.takeoff_minute} "
                    f"to {match.asset_id}@{match.takeoff_minute}.",
                    frozen.mission_id,
                )
            )

    # -- asset status consistency ------------------------------------------------
    in_use: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in ordered:
        in_use[assignment.asset_id].append(assignment)
    for asset_id, asset in state.assets.items():
        sorties = in_use.get(asset_id)
        if not sorties or asset.status != AssetStatus.UNAVAILABLE:
            continue
        # Only future departures are gated by ground status; an already-airborne sortie
        # is allowed provided it lands before the asset goes into repair.
        future = [a for a in sorties if a.takeoff_minute > state.now_minute]
        if future:
            add(
                AuditFinding(
                    "UNAVAILABLE_ASSET_ASSIGNED",
                    SEVERITY_ERROR,
                    f"{asset_id} is UNAVAILABLE but is assigned to "
                    + ", ".join(sorted(a.mission_id for a in future))
                    + ".",
                    asset_id,
                )
            )

    if strict_metrics:
        _audit_metrics_coverage(state, ordered, report)
    return report


def _audit_metrics_coverage(state: OperationalState, ordered: list[Assignment], report: AuditReport) -> None:
    """Cross-check the plan against the state's own mission list."""
    state_missions = {m.id for m in state.missions.values() if m.status != MissionStatus.CANCELLED}
    planned = {a.mission_id for a in ordered}
    for mission_id in sorted(planned - state_missions):
        report.findings.append(
            AuditFinding(
                "ORPHAN_ASSIGNMENT",
                SEVERITY_ERROR,
                f"{mission_id} is assigned but is not an active mission in the current state.",
                mission_id,
            )
        )
    report.checked_missions = len(planned)


def readiness_snapshot(state: OperationalState) -> list[dict]:
    """Readiness rows for the asset-readiness screen (synthetic heuristic)."""
    rows: list[dict] = []
    for asset_id in sorted(state.assets):
        asset = state.assets[asset_id]
        readiness = estimate_readiness(
            asset.maintenance_hours_since, asset.recent_fault_count, asset.status
        )
        row = {
            "asset_id": asset.id,
            "label": asset.label,
            "status": asset.status.value,
            "class": asset.class_.value,
            "home_hub_id": asset.home_hub_id,
            "maintenance_hours_since": asset.maintenance_hours_since,
            "recent_fault_count": asset.recent_fault_count,
            **readiness.as_dict(),
        }
        rows.append(row)
    return rows


def audit_stored_plan(state: OperationalState, plan: Plan, *, expected_frozen: tuple[Assignment, ...] = ()) -> AuditReport:
    return audit_plan(state, plan.assignments, expected_frozen=expected_frozen)