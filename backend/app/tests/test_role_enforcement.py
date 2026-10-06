"""Essential test 6: a non-Commander cannot approve.

This is the safety-critical control of the whole prototype, so it is tested three ways:
directly against the dependency, and end-to-end through the approve/reject routes for
every non-Commander role. The role selector in the UI is only a convenience, so the
guard has to hold server-side no matter what the client claims.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.deps import get_actor_role, require_commander, require_write_access
from app.domain.enums import ActorRole

from .conftest import headers

NON_COMMANDERS = [ActorRole.PLANNER, ActorRole.OBSERVER, ActorRole.MAINTAINER]


# -- The dependency in isolation --------------------------------------------------------


@pytest.mark.parametrize("role", NON_COMMANDERS)
def test_require_commander_rejects_non_commanders(role: ActorRole) -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_commander(role)
    assert excinfo.value.status_code == 403
    assert "COMMANDER" in excinfo.value.detail


def test_require_commander_allows_a_commander() -> None:
    assert require_commander(ActorRole.COMMANDER) == ActorRole.COMMANDER


@pytest.mark.parametrize("role", NON_COMMANDERS)
def test_require_write_access_rejects_observers_only(role: ActorRole) -> None:
    """Planners and maintainers may write; only OBSERVER is read-only."""
    if role == ActorRole.OBSERVER:
        with pytest.raises(HTTPException) as excinfo:
            require_write_access(role)
        assert excinfo.value.status_code == 403
    else:
        assert require_write_access(role) == role


def test_unknown_role_header_is_rejected_rather_than_defaulted() -> None:
    """An unrecognised role must not silently fall back to something permissive."""
    with pytest.raises(HTTPException) as excinfo:
        get_actor_role("SUPERUSER")
    assert excinfo.value.status_code == 400


def test_missing_role_header_defaults_to_the_least_privileged_role() -> None:
    assert get_actor_role(None) == ActorRole.OBSERVER
    assert get_actor_role("") == ActorRole.OBSERVER


def test_role_header_is_case_insensitive() -> None:
    assert get_actor_role("commander") == ActorRole.COMMANDER
    assert get_actor_role("  Commander  ") == ActorRole.COMMANDER


# -- End to end through the approve route ----------------------------------------------


def _pending_proposal(client) -> str:  # type: ignore[no-untyped-def]
    """Drive the API to the point where a proposal is awaiting a decision."""
    generated = client.post("/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER))
    assert generated.status_code == 200, generated.text
    injected = client.post(
        "/api/v1/events/inject",
        json={"scripted_event": "EVENT_A"},
        headers=headers(ActorRole.PLANNER),
    )
    assert injected.status_code == 200, injected.text
    proposals = injected.json()["proposals"]
    assert proposals, "an injected event must produce a proposal to approve"
    return proposals[0]["id"]


@pytest.mark.parametrize("role", NON_COMMANDERS)
def test_non_commander_cannot_approve_over_http(client, role: ActorRole) -> None:
    proposal_id = _pending_proposal(client)

    response = client.post(f"/api/v1/proposals/{proposal_id}/approve", json={}, headers=headers(role))
    assert response.status_code == 403, response.text
    assert "COMMANDER" in response.json()["detail"]

    # The proposal is still pending: a rejected attempt must change nothing.
    still_pending = client.get(f"/api/v1/proposals/{proposal_id}")
    assert still_pending.json()["status"] == "PENDING"


@pytest.mark.parametrize("role", NON_COMMANDERS)
def test_non_commander_cannot_reject_over_http(client, role: ActorRole) -> None:
    proposal_id = _pending_proposal(client)

    response = client.post(f"/api/v1/proposals/{proposal_id}/reject", json={}, headers=headers(role))
    assert response.status_code == 403
    assert client.get(f"/api/v1/proposals/{proposal_id}").json()["status"] == "PENDING"


def test_missing_role_cannot_approve(client) -> None:
    """No header at all must not be treated as implicit approval."""
    proposal_id = _pending_proposal(client)
    response = client.post(f"/api/v1/proposals/{proposal_id}/approve", json={})
    assert response.status_code == 403


def test_observer_cannot_generate_plans(client) -> None:
    response = client.post("/api/v1/plans/generate", json={}, headers=headers(ActorRole.OBSERVER))
    assert response.status_code == 403


def test_observer_cannot_inject_events(client) -> None:
    response = client.post(
        "/api/v1/events/inject", json={"scripted_event": "EVENT_A"}, headers=headers(ActorRole.OBSERVER)
    )
    assert response.status_code == 403


def test_commander_can_approve_and_the_plan_becomes_active(client) -> None:
    """The positive case: the guard blocks others but must not block the Commander."""
    proposal_id = _pending_proposal(client)

    approved = client.post(
        f"/api/v1/proposals/{proposal_id}/approve",
        json={"note": "accepted for demonstration"},
        headers=headers(ActorRole.COMMANDER),
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "APPROVED"
    assert body["approved_by"] == ActorRole.COMMANDER.value

    listing = client.get("/api/v1/proposals")
    assert listing.json()["active_plan_id"] == body["plan_id"]


def test_a_proposal_cannot_be_approved_twice(client) -> None:
    proposal_id = _pending_proposal(client)
    first = client.post(
        f"/api/v1/proposals/{proposal_id}/approve", json={}, headers=headers(ActorRole.COMMANDER)
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/proposals/{proposal_id}/approve", json={}, headers=headers(ActorRole.COMMANDER)
    )
    assert second.status_code == 409


def test_unknown_proposal_id_is_404_not_403(client) -> None:
    """A Commander asking about a proposal that does not exist gets a 404."""
    response = client.post(
        "/api/v1/proposals/PRP-DOES-NOT-EXIST/approve", json={}, headers=headers(ActorRole.COMMANDER)
    )
    assert response.status_code == 404


def test_unknown_proposal_is_404_for_a_non_commander_too(client) -> None:
    """The 403 guard runs first, so this proves ordering as well as status."""
    response = client.post(
        "/api/v1/proposals/PRP-DOES-NOT-EXIST/approve", json={}, headers=headers(ActorRole.PLANNER)
    )
    assert response.status_code == 403


def test_role_matrix_is_published_for_the_ui(client) -> None:
    """The UI reads its capability matrix from the server, not from hard-coded client state."""
    matrix = client.get("/api/v1/state/overview").json()["role_matrix"]
    assert matrix["COMMANDER"]["can_approve"] is True
    for role in NON_COMMANDERS:
        assert matrix[role.value]["can_approve"] is False
    assert matrix["OBSERVER"]["can_write"] is False
    assert matrix["PLANNER"]["can_write"] is True


# -- Proposal labels survive the round trip ---------------------------------------------


def test_stored_proposals_keep_their_own_trade_off_label(client) -> None:
    """Each proposal must read back with the label of the variant it actually holds.

    This is a regression test. ``persist_proposal`` once wrote the variant into a column
    named ``variant`` while the table column is ``label``, so the write fell back to the
    column default and every proposal read back as "COVERAGE FIRST". Three identically
    labelled cards make the three-option frontier look like one option repeated three
    times, which defeats the point of the screen.
    """
    generated = client.post(
        "/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER)
    )
    assert generated.status_code == 200, generated.text
    plan_variants = [plan["variant"] for plan in generated.json()["plans"]]
    assert len(set(plan_variants)) == 3, plan_variants

    injected = client.post(
        "/api/v1/events/inject",
        json={"scripted_event": "EVENT_A"},
        headers=headers(ActorRole.PLANNER),
    )
    assert injected.status_code == 200, injected.text
    proposals = injected.json()["proposals"]
    assert len(proposals) == 3

    labels = [proposal["label"] for proposal in proposals]
    assert len(set(labels)) == 3, f"proposal labels collapsed: {labels}"

    # Each proposal's label must agree with the variant of the plan it points at.
    for proposal in proposals:
        plan = client.get(f"/api/v1/plans/{proposal['plan_id']}").json()
        assert plan["variant"] == proposal["label"], (
            f"{proposal['id']} is labelled {proposal['label']!r} but points at plan "
            f"{plan['id']} whose variant is {plan['variant']!r}"
        )

    # ...and the labels must still be there after a reload from the database, which is the
    # path that the wrong column name broke.
    reloaded = client.get("/api/v1/proposals").json()["proposals"]
    assert {p["label"] for p in reloaded} == set(labels)
