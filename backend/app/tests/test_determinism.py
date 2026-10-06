"""Essential test 1: a seeded scenario is deterministic.

Same seed in, identical world out — entities, readiness, timings and plans.
"""

from __future__ import annotations

from app.services import frontier_explorer
from app.services.scenario_studio import build_scenario, default_config

from .conftest import DEFAULT_SEED, build_state


def test_same_seed_rebuilds_identical_entities() -> None:
    first = build_scenario(default_config(DEFAULT_SEED))
    second = build_scenario(default_config(DEFAULT_SEED))

    assert sorted(first.assets) == sorted(second.assets)
    assert sorted(first.crews) == sorted(second.crews)
    assert sorted(first.missions) == sorted(second.missions)
    assert first.hubs.keys() == second.hubs.keys()

    for asset_id, asset in first.assets.items():
        other = second.assets[asset_id]
        assert asset.class_ == other.class_
        assert asset.home_hub_id == other.home_hub_id
        assert asset.status == other.status
        assert asset.cruise_speed == other.cruise_speed
        assert asset.available_from_minute == other.available_from_minute
        assert asset.readiness_probability == other.readiness_probability

    for crew_id, crew in first.crews.items():
        other = second.crews[crew_id]
        assert crew.roles == other.roles
        assert crew.duty_minutes_used == other.duty_minutes_used
        assert crew.last_duty_end_minute == other.last_duty_end_minute

    for mission_id, mission in first.missions.items():
        other = second.missions[mission_id]
        assert mission.priority == other.priority
        assert mission.earliest_start == other.earliest_start
        assert mission.latest_end == other.latest_end
        assert (mission.objective_x, mission.objective_y) == (other.objective_x, other.objective_y)


def test_state_digest_matches_across_rebuilds() -> None:
    assert build_state().snapshot_digest() == build_state().snapshot_digest()


def test_different_seed_produces_a_different_world() -> None:
    assert build_state(DEFAULT_SEED).snapshot_digest() != build_state(DEFAULT_SEED + 1).snapshot_digest()


def test_planning_is_reproducible() -> None:
    """Two solves of the same state return the same assignments, not merely similar ones."""

    def solve() -> list[tuple]:
        frontier = frontier_explorer.build_frontier(
            build_state(), scenario_id="SCN-DET", version=1
        )
        coverage = frontier.plans[0].plan
        return sorted(assignment.signature() for assignment in coverage.assignments)

    first = solve()
    second = solve()
    assert first == second
    assert len(first) > 0


def test_clock_is_reproducible() -> None:
    assert build_state(now_minute=137).now_minute == 137
    assert build_state(now_minute=137).now_iso == build_state(now_minute=137).now_iso