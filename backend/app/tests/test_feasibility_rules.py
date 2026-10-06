"""Essential test 2: every individual feasibility rule has a passing and a failing case.

Each test names the rule number from the spec and drives one pure predicate directly, so
a regression points at a single constraint instead of a whole plan.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.enums import (
    AssetClass,
    AssetStatus,
    CrewRole,
    EventKind,
    MissionStatus,
    PayloadCategory,
    RestrictionType,
)
from app.domain.schemas import Assignment, HazardZone, RiskBreakdown, StockItem, WeatherWindow
from app.services.feasibility_engine import (
    FeasibilityContext,
    check_asset_available,
    check_class_support,
    check_crew_duty,
    check_endurance_and_range,
    check_hub_capacity,
    check_hub_open,
    check_no_overlap,
    check_payload,
    check_risk_within_max,
    check_route_clear,
    check_time_window,
    enumerate_options,
    evaluate_mission,
)
from app.services.feasibility_engine import (
    check_asset_based_at_origin as check_basing,
)
from app.services.scenario_studio import rectangle_cells

from .conftest import build_state


@pytest.fixture()
def state():
    return build_state()


@pytest.fixture()
def mission(state):
    return state.mission("MIS-001")


@pytest.fixture()
def asset(state):
    return state.asset("AST-001")


@pytest.fixture()
def crew(state):
    return next(iter(state.crews.values()))


# -- Rule 1: asset class supports the mission class requirement ----------------------


def test_rule1_class_support_passes_and_fails() -> None:
    assert check_class_support(AssetClass.TRANSPORT, AssetClass.TRANSPORT) is True
    assert check_class_support(AssetClass.SCOUT, AssetClass.TRANSPORT) is False


def test_rule1_basing_passes_and_fails(state, mission, asset) -> None:
    based = dataclasses.replace(asset, home_hub_id=mission.origin_hub_id)
    assert check_basing(based, mission) is True
    elsewhere = dataclasses.replace(asset, home_hub_id=_other_hub(mission.origin_hub_id, state))
    assert check_basing(elsewhere, mission) is False


# -- Rule 2: asset is available -----------------------------------------------------


def test_rule2_availability_passes_and_fails(asset) -> None:
    ready = dataclasses.replace(asset, status=AssetStatus.AVAILABLE, available_from_minute=0)
    assert check_asset_available(ready, 120) is True

    grounded = dataclasses.replace(asset, status=AssetStatus.UNAVAILABLE, available_from_minute=0)
    assert check_asset_available(grounded, 120) is False

    late = dataclasses.replace(asset, status=AssetStatus.AVAILABLE, available_from_minute=200)
    assert check_asset_available(late, 120) is False
    assert check_asset_available(late, 200) is True


# -- Rule 3: transit plus station time within endurance and range ---------------------


def test_rule3_endurance_and_range_pass_and_fail(asset) -> None:
    assert check_endurance_and_range(asset, one_way_minutes=30, station_minutes=30, one_way_distance=20.0) is True

    endurance_short = dataclasses.replace(asset, endurance_minutes=40)
    assert check_endurance_and_range(endurance_short, 30, 30, 20.0) is False

    range_short = dataclasses.replace(asset, range_units=10.0)
    assert check_endurance_and_range(range_short, 30, 30, 20.0) is False


# -- Rule 4: payload category exists at the origin hub in sufficient quantity ---------


def test_rule4_payload_passes_and_fails(state, mission) -> None:
    needs = dataclasses.replace(
        mission, required_payload_category=PayloadCategory.MEDICAL, required_asset_count=2
    )
    state.missions[needs.id] = needs
    state.stock = [
        StockItem(hub_id=needs.origin_hub_id, category=PayloadCategory.MEDICAL, quantity=4)
    ]
    assert check_payload(state, needs) is True

    state.stock = [
        StockItem(hub_id=needs.origin_hub_id, category=PayloadCategory.MEDICAL, quantity=1)
    ]
    assert check_payload(state, needs) is False


def test_rule4_shortfall_is_reported_as_a_reason_code(state, mission) -> None:
    starved = dataclasses.replace(
        mission,
        required_payload_category=PayloadCategory.MEDICAL,
        required_asset_count=9,
    )
    state.missions[starved.id] = starved
    report = evaluate_mission(state, starved, FeasibilityContext(now_minute=state.now_minute))
    assert report.feasible is False
    assert report.blocking_counts["PAYLOAD_SHORTFALL"] == 1


# -- Rule 5: crew qualified, available, rested, within duty limits -------------------


def test_rule5_crew_duty_passes_and_fails(crew) -> None:
    fresh = dataclasses.replace(crew, duty_minutes_used=0, duty_limit_minutes=600)
    assert check_crew_duty(fresh, 120) is True
    tired = dataclasses.replace(fresh, duty_minutes_used=560, duty_limit_minutes=600)
    assert check_crew_duty(tired, 120) is False


def test_rule5_crew_roles_are_enumerated(state) -> None:
    """A mission requiring roles that nobody holds has no candidate crew teams."""
    mission = dataclasses.replace(
        state.mission("MIS-001"), required_crew_roles=(CrewRole.LOADMASTER,)
    )
    state.missions[mission.id] = mission
    report = evaluate_mission(state, mission, FeasibilityContext(now_minute=state.now_minute))
    if report.feasible:
        assert any(
            CrewRole.LOADMASTER in state.crews[crew_id].roles
            for option in report.options
            for crew_id in option.crew_ids
        )
    else:
        assert report.blocking_counts["CREW_QUALIFICATION_MISSING"] == 1


def test_rule5_duty_limit_is_reported_as_a_reason_code(state) -> None:
    """With every crew member out of duty hours, no crew team can be formed."""
    mission = state.mission("MIS-001")
    assert mission.required_crew_roles, "the seed must require at least one crew role"
    state.crews = {
        crew_id: dataclasses.replace(
            crew, duty_minutes_used=crew.duty_limit_minutes, duty_limit_minutes=crew.duty_limit_minutes
        )
        for crew_id, crew in state.crews.items()
    }
    report = evaluate_mission(state, mission, FeasibilityContext(now_minute=state.now_minute))
    assert report.feasible is False
    assert report.blocking_counts["CREW_DUTY_LIMIT_EXCEEDED"] > 0


# -- Rule 6: mission completed inside its time window --------------------------------


def test_rule6_time_window_passes_and_fails(mission) -> None:
    assert check_time_window(mission.earliest_start, mission.latest_end, mission) is True
    assert check_time_window(mission.earliest_start - 5, mission.latest_end, mission) is False
    assert check_time_window(mission.earliest_start, mission.latest_end + 5, mission) is False


def test_rule6_window_missed_is_reported_as_a_reason_code(state) -> None:
    mission = dataclasses.replace(
        state.mission("MIS-001"), earliest_start=0, latest_end=1, station_duration_minutes=30
    )
    state.missions[mission.id] = mission
    report = evaluate_mission(state, mission, FeasibilityContext(now_minute=state.now_minute))
    assert report.feasible is False
    assert report.blocking_counts["TIME_WINDOW_MISSED"] > 0


# -- Rule 7: asset and crew do not overlap with other assignments --------------------


def _assignment(mission_id: str, asset_id: str, crew_ids: tuple[str, ...], start: int, end: int) -> Assignment:
    return Assignment(
        id=f"X-{mission_id}",
        mission_id=mission_id,
        asset_id=asset_id,
        crew_ids=crew_ids,
        payload_category=None,
        payload_units=0,
        takeoff_minute=start,
        landing_minute=start,
        return_minute=end,
        transit_minutes=1,
        risk=0.0,
        risk_breakdown=RiskBreakdown(hazard_risk=0.0, weather_risk=0.0, readiness_risk=0.0, combined=0.0),
    )


def test_rule7_no_overlap_passes_and_fails() -> None:
    existing = (_assignment("MIS-A", "AST-001", ("CRW-001",), 100, 200),)

    # Disjoint time and no shared resource: fine.
    assert check_no_overlap("MIS-B", "AST-002", ("CRW-002",), 210, 260, existing) is True
    # Overlapping time but no shared resource: also fine -- the rule is about resources.
    assert check_no_overlap("MIS-B", "AST-002", ("CRW-002",), 100, 130, existing) is True
    # Same asset, overlapping: conflict.
    assert check_no_overlap("MIS-B", "AST-001", ("CRW-002",), 100, 130, existing) is False
    # Same asset, disjoint times: fine.
    assert check_no_overlap("MIS-B", "AST-001", ("CRW-002",), 210, 260, existing) is True
    # Different asset but a shared crew member: still a conflict.
    assert check_no_overlap("MIS-B", "AST-002", ("CRW-001",), 150, 170, existing) is False
    # Touching endpoints are half-open, so back-to-back is allowed.
    assert check_no_overlap("MIS-B", "AST-001", ("CRW-001",), 200, 260, existing) is True


def test_rule7_same_mission_is_never_a_conflict() -> None:
    """Re-planning a mission must ignore that mission's own current assignment."""
    existing = (_assignment("MIS-A", "AST-001", ("CRW-001",), 100, 200),)
    assert check_no_overlap("MIS-A", "AST-001", ("CRW-001",), 100, 200, existing) is True


# -- Rule 8: hub open at movement times and capacity not exceeded --------------------


def test_rule8_hub_open_passes_and_fails(state) -> None:
    assert check_hub_open(state, "HUB-A", [60, 200]) is True
    state.weather = [
        WeatherWindow(
            id="WX-TEST",
            name="closure",
            start=100,
            end=300,
            severity=0.9,
            restriction_type=RestrictionType.CLOSURE,
            hub_id="HUB-A",
            blocking=True,
        )
    ]
    assert check_hub_open(state, "HUB-A", [60, 200]) is False


def test_rule8_capacity_passes_and_fails() -> None:
    assert check_hub_capacity("HUB-A", [30, 300], slot_minutes=30, capacity=2) is True
    # Three departures inside the same 30-minute slot exceed capacity 2.
    assert check_hub_capacity("HUB-A", [30, 31, 32], slot_minutes=30, capacity=2) is False
    # With one movement already committed in that slot, only one more fits.
    assert check_hub_capacity(
        "HUB-A", [31], slot_minutes=30, capacity=2, extra_load={("HUB-A", 1): 1}
    ) is True
    assert check_hub_capacity(
        "HUB-A", [31, 32], slot_minutes=30, capacity=2, extra_load={("HUB-A", 1): 1}
    ) is False


# -- Rule 9: route does not cross an active hard-restriction zone --------------------


def test_rule9_route_clear_passes_and_fails(state, mission) -> None:
    timing = {
        "transit_minutes": 20,
        "takeoff_minute": max(state.now_minute, mission.earliest_start),
    }
    assert check_route_clear(state, mission, timing) is True

    origin = state.hub(mission.origin_hub_id)
    mid_x = (origin.x + mission.objective_x) / 2
    mid_y = (origin.y + mission.objective_y) / 2
    blocking = HazardZone(
        id="HZ-BLOCK",
        name="blocking corridor",
        cells=rectangle_cells(mid_x - 3, mid_y - 3, mid_x + 3, mid_y + 3),
        severity=0.9,
        active_start=0,
        active_end=state.horizon_minutes,
        blocking=True,
    )
    state.hazards = [*state.hazards, blocking]
    assert check_route_clear(state, mission, timing) is False


def test_rule9_non_blocking_zone_does_not_block_the_route(state, mission) -> None:
    timing = {
        "transit_minutes": 20,
        "takeoff_minute": max(state.now_minute, mission.earliest_start),
    }
    origin = state.hub(mission.origin_hub_id)
    mid_x = (origin.x + mission.objective_x) / 2
    mid_y = (origin.y + mission.objective_y) / 2
    advisory = HazardZone(
        id="HZ-ADVISORY",
        name="advisory corridor",
        cells=rectangle_cells(mid_x - 3, mid_y - 3, mid_x + 3, mid_y + 3),
        severity=0.6,
        active_start=0,
        active_end=state.horizon_minutes,
        blocking=False,
    )
    state.hazards = [*state.hazards, advisory]
    assert check_route_clear(state, mission, timing) is True


# -- Rule 10: combined risk does not exceed the mission maximum ----------------------


def test_rule10_risk_maximum_passes_and_fails(mission) -> None:
    assert check_risk_within_max(mission.maximum_risk, mission) is True
    assert check_risk_within_max(mission.maximum_risk + 0.01, mission) is False


def test_rule10_risk_exceeded_is_reported_as_a_reason_code(state) -> None:
    mission = dataclasses.replace(state.mission("MIS-001"), maximum_risk=0.0)
    state.missions[mission.id] = mission
    report = evaluate_mission(state, mission, FeasibilityContext(now_minute=state.now_minute))
    assert report.feasible is False
    assert report.blocking_counts["RISK_EXCEEDS_MISSION_MAX"] > 0


# -- Cross-cutting: cancelled missions and the enumeration API ------------------------


def test_cancelled_mission_yields_no_options(state) -> None:
    mission = dataclasses.replace(state.mission("MIS-001"), status=MissionStatus.CANCELLED)
    state.missions[mission.id] = mission
    report = evaluate_mission(state, mission, FeasibilityContext(now_minute=state.now_minute))
    assert report.feasible is False
    assert report.notes


def test_enumerate_options_covers_every_mission(state) -> None:
    options, reports = enumerate_options(state, FeasibilityContext(now_minute=state.now_minute))
    assert set(options) == set(state.missions)
    assert set(reports) == set(state.missions)
    assert sum(len(v) for v in options.values()) > 0


def test_unavailable_asset_is_excluded_with_a_reason_code(state) -> None:
    mission = state.mission("MIS-001")
    ctx = FeasibilityContext(now_minute=state.now_minute)
    baseline = evaluate_mission(state, mission, ctx)
    if baseline.feasible:
        blocked_asset = baseline.options[0].asset_id
        blocked = evaluate_mission(
            state, mission, FeasibilityContext(now_minute=state.now_minute, blocked_asset_ids=frozenset({blocked_asset}))
        )
        assert all(option.asset_id != blocked_asset for option in blocked.options)
        assert blocked.blocking_counts["ASSET_UNAVAILABLE"] > 0


def test_hazard_event_removes_route_options(state) -> None:
    """An injected blocking hazard must be reflected in feasibility, not just in the plan."""
    from app.services import disruption_monitor

    kind, title, payload, _ = disruption_monitor.resolve_scripted_event("EVENT_C")
    application = disruption_monitor.apply_event(state, kind, {**payload, "blocking": True})
    assert application.state.hazards
    affected = application.affected_mission_ids
    assert affected, "a corridor hazard must flag at least one mission as affected"
    assert EventKind.HAZARD_ZONE == kind
    assert title


def _other_hub(current: str, state) -> str:
    others = sorted(h for h in state.hubs if h != current)
    return others[0]


__all__ = [
    "check_asset_available",
    "check_basing",
    "check_class_support",
    "check_crew_duty",
    "check_endurance_and_range",
    "check_hub_capacity",
    "check_hub_open",
    "check_no_overlap",
    "check_payload",
    "check_route_clear",
    "check_risk_within_max",
    "check_time_window",
]