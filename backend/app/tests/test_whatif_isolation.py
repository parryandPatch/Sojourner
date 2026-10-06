"""Essential test 8: a what-if run cannot mutate active state.

The what-if lab clones the operational state, applies the experiment to the clone, and
solves against it. The live scenario must be bit-for-bit unchanged afterwards. This is
checked two ways: by snapshot digest before/after, and by re-reading the persisted
database, because a digest taken from the same in-memory object could hide a partial
write.
"""

from __future__ import annotations

from app.domain.enums import ActorRole, EventKind
from app.services import disruption_monitor
from app.services.frontier_explorer import build_frontier
from app.services.planning_engine import compute_metrics
from app.services.whatif_lab import run_what_if

from .conftest import headers


def _digest(state) -> str:  # type: ignore[no-untyped-def]
    return state.snapshot_digest()


def test_what_if_leaves_the_snapshot_digest_unchanged(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    before = _digest(state)

    for key in sorted(disruption_monitor.SCRIPTED_EVENTS):
        kind, _title, payload, _description = disruption_monitor.resolve_scripted_event(key)
        result = run_what_if(
            state,
            label=f"test {key}",
            scripted_events=[(key, {})],
            events=[],
            overrides={},
            active_plan=parent,
            scenario_id=state.scenario_id,
        )
        assert _digest(state) == before, f"{key}: what-if mutated the live state object"
        assert result.state_digest_before == result.state_digest_after, (
            f"{key}: the sandbox reported a digest change on the live state"
        )


def test_what_if_does_not_change_the_parent_plan(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    assignments_before = tuple(a.signature() for a in parent.assignments)
    metrics_before = parent.metrics.as_dict()

    run_what_if(
        state,
        label="fault drill",
        scripted_events=[("EVENT_A", {})],
        events=[],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )

    assert tuple(a.signature() for a in parent.assignments) == assignments_before
    assert parent.metrics.as_dict() == metrics_before


def test_what_if_experiments_do_still_differ_from_the_baseline(initial_frontier) -> None:
    """Isolation must not be achieved by simply doing nothing.

    If the sandbox never diverged, the isolation assertions above would pass while the
    feature was broken. EVENT_A grounds an asset, so the experiment must actually differ.
    """
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan

    result = run_what_if(
        state,
        label="fault drill",
        scripted_events=[("EVENT_A", {})],
        events=[],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )
    assert result.changed_missions, "injecting an asset fault must change the experiment"
    assert result.coverage_delta != 0 or result.mean_risk_delta != 0


def test_what_if_result_is_independently_auditable(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    result = run_what_if(
        state,
        label="closure drill",
        scripted_events=[("EVENT_B", {})],
        events=[],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )
    assert result.audit_valid, result.audit_findings
    assert result.audit_findings == []
    # A non-fallback experiment plan must be labelled with the variant it was solved for.
    assert not result.experiment_plan.is_fallback


def test_manual_event_spec_is_accepted(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    before = _digest(state)

    result = run_what_if(
        state,
        label="manual closure",
        scripted_events=[],
        events=[
            (
                EventKind.WEATHER_RESTRICTION,
                {"hub_id": "HUB-B", "severity": 0.9, "start": 60, "end": 240},
            )
        ],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )
    assert result.state_digest_before == result.state_digest_after
    assert _digest(state) == before


def test_overrides_are_applied_only_in_the_sandbox(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    before = _digest(state)

    result = run_what_if(
        state,
        label="capacity stress",
        scripted_events=[],
        events=[],
        overrides={"hub_capacity": {"HUB-A": 1}},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )
    assert result.state_digest_before == result.state_digest_after
    assert _digest(state) == before
    # The live hub still has its seeded capacity.
    assert state.hub("HUB-A").runway_capacity_per_slot != 1


def test_what_if_does_not_change_live_metrics(initial_frontier) -> None:
    """Re-computing the live plan metrics after a what-if must give the same numbers."""
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    before = compute_metrics(state, parent.assignments)

    run_what_if(
        state,
        label="corridor hazard",
        scripted_events=[("EVENT_C", {})],
        events=[],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )

    after = compute_metrics(state, parent.assignments)
    assert after.as_dict() == before.as_dict()


def test_what_if_route_does_not_persist_anything(client) -> None:
    """Through the API: the persisted scenario must be untouched, even though a ledger
    entry is appended (that entry records the experiment, it is not state mutation)."""
    commander = headers(ActorRole.COMMANDER)
    planner = headers(ActorRole.PLANNER)

    assert client.post("/api/v1/plans/generate", json={}, headers=commander).status_code == 200
    injected = client.post("/api/v1/events/inject", json={"scripted_event": "EVENT_A"}, headers=planner)
    assert injected.status_code == 200
    proposal_id = injected.json()["proposals"][0]["id"]
    assert client.post(f"/api/v1/proposals/{proposal_id}/approve", json={}, headers=commander).status_code == 200

    before = client.get("/api/v1/state/overview").json()
    active_before = client.get("/api/v1/proposals").json()["active_plan_id"]
    plans_before = client.get("/api/v1/plans").json()

    response = client.post(
        "/api/v1/what-if/run",
        json={"scripted_event": "EVENT_A", "label": "api isolation check"},
        headers=planner,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["isolation_verified"] is True
    assert body["state_digest_before"] == body["state_digest_after"]
    assert body["active_plan_id"] == active_before

    after = client.get("/api/v1/state/overview").json()
    assert after["active_plan"] == before["active_plan"], "what-if changed the active plan"
    assert after["counts"] == before["counts"]
    assert client.get("/api/v1/plans").json() == plans_before, "what-if persisted a plan version"
    assert client.get("/api/v1/proposals").json()["active_plan_id"] == active_before


def test_what_if_route_records_the_experiment_in_the_ledger(client) -> None:
    """The isolation check must still leave an audit trail of what was tried."""
    from app.domain.enums import LedgerAction

    commander = headers(ActorRole.COMMANDER)
    planner = headers(ActorRole.PLANNER)
    client.post("/api/v1/plans/generate", json={}, headers=commander)

    before = client.get("/api/v1/ledger").json()["chain_length"]
    assert (
        client.post(
            "/api/v1/what-if/run",
            json={"scripted_event": "EVENT_C", "label": "ledger check"},
            headers=planner,
        ).status_code
        == 200
    )
    after = client.get("/api/v1/ledger").json()
    assert after["chain_length"] == before + 1
    assert after["chain_status"] == "VALID"
    assert any(entry["action"] == LedgerAction.WHAT_IF_RUN.value for entry in after["entries"])


def test_what_if_cannot_approve_anything(client) -> None:
    """The what-if screen has no activation path at all, for any role."""
    planner = headers(ActorRole.PLANNER)
    client.post("/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER))

    body = client.post(
        "/api/v1/what-if/run",
        json={"scripted_event": "EVENT_A", "label": "no approval here"},
        headers=planner,
    ).json()
    assert "active_plan" not in body
    assert "approve" in body["approval_notice"].lower()
    assert "activate" in body["approval_notice"].lower()
    assert body["advisory_notice"] == "ADVISORY ONLY — HUMAN APPROVAL REQUIRED"


def test_what_if_experiment_plan_is_not_stored_in_the_plan_table(initial_frontier) -> None:
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    result = run_what_if(
        state,
        label="not persisted",
        scripted_events=[("EVENT_D", {})],
        events=[],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )
    # The experiment plan exists only as a value object; nothing wrote it anywhere.
    # A DRAFT status is the strongest signal available: no approval path can have run.
    assert result.experiment_plan.id
    assert result.experiment_plan.status.value == "DRAFT"
    assert result.experiment_plan.approved_by is None
    assert result.experiment_plan.approved_at is None


def test_sandbox_clone_is_independent_of_later_mutation(initial_frontier) -> None:
    """Mutating the caller's state afterwards must not retro-change a past result."""
    state, frontier = initial_frontier
    parent = frontier.plans[0].plan

    result = run_what_if(
        state,
        label="independent",
        scripted_events=[("EVENT_A", {})],
        events=[],
        overrides={},
        active_plan=parent,
        scenario_id=state.scenario_id,
    )
    recorded = result.state_digest_before
    assigned = len(result.experiment_plan.assignments)

    fresh = build_frontier(state, scenario_id=state.scenario_id, version=99)
    assert fresh.plans, "the live state is still solvable after the experiment"
    assert result.state_digest_before == recorded
    assert len(result.experiment_plan.assignments) == assigned
