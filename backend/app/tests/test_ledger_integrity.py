"""Essential test 7: ledger verification succeeds, then fails after a deliberate mutation.

The ledger is a SHA-256 hash chain. Per the spec, each entry hashes

    previous_hash + canonical_JSON(payload) + occurred_at + action + actor_role

so editing any hashed field of a historical entry breaks every link after it. These
tests prove the tamper-evidence actually works rather than always returning "VALID".
"""

from __future__ import annotations

from datetime import timedelta

from app.domain.enums import ActorRole, LedgerAction
from app.repositories import scenario_repo
from app.services.decision_ledger import (
    GENESIS_HASH,
    compute_entry_hash,
    verify_chain,
)

HASHED_FIELDS = ("previous_hash", "payload_json", "occurred_at", "action", "actor_role")


def _write(session, scenario_id, description="test entry", *, action=LedgerAction.PLAN_GENERATED, **payload):
    return scenario_repo.append_ledger(
        session,
        scenario_id=scenario_id,
        actor_role=ActorRole.COMMANDER,
        action=action,
        entity_ref="REF-1",
        payload=payload or {"note": description},
        description=description,
    )


def _scenario(session):  # type: ignore[no-untyped-def]
    scenario = scenario_repo.active_scenario(session)
    assert scenario is not None
    return scenario


def test_a_fresh_chain_verifies(session) -> None:
    result = scenario_repo.verify_ledger(session, _scenario(session).id)
    assert result.valid
    assert result.status == "VALID"
    assert result.broken_at is None


def test_appending_entries_keeps_the_chain_valid(session) -> None:
    scenario = _scenario(session)
    for index in range(5):
        _write(session, scenario.id, description=f"entry {index}", index=index)

    result = scenario_repo.verify_ledger(session, scenario.id)
    assert result.valid, result.detail
    assert result.length == 5


def test_chain_verification_fails_after_a_deliberate_payload_mutation(session) -> None:
    """The core test: verify VALID, mutate one historical entry, verify INVALID."""
    scenario = _scenario(session)
    for index in range(3):
        _write(session, scenario.id, description=f"entry {index}", index=index)

    assert scenario_repo.verify_ledger(session, scenario.id).valid, "precondition: chain is valid"

    rows = scenario_repo.ledger_rows(session, scenario.id)
    target = rows[1]
    target.payload_json = {"note": "quietly rewritten"}
    session.commit()

    after = scenario_repo.verify_ledger(session, scenario.id)
    assert not after.valid
    assert after.status == "INVALID"
    # The first entry still hashes correctly, so the break is reported at the tampered one.
    assert after.broken_at == target.sequence
    assert after.detail["issue"] == "entry_hash_mismatch"


def test_chain_verification_fails_after_a_timestamp_mutation(session) -> None:
    scenario = _scenario(session)
    for index in range(3):
        _write(session, scenario.id, description=f"entry {index}", index=index)

    rows = scenario_repo.ledger_rows(session, scenario.id)
    rows[1].occurred_at = rows[1].occurred_at + timedelta(minutes=5)
    session.commit()

    after = scenario_repo.verify_ledger(session, scenario.id)
    assert not after.valid
    assert after.broken_at == rows[1].sequence


def test_chain_verification_fails_after_an_actor_role_mutation(session) -> None:
    """Rewriting who approved a decision must break the chain."""
    scenario = _scenario(session)
    _write(session, scenario.id, action=LedgerAction.PROPOSAL_APPROVED, note="approved")
    assert scenario_repo.verify_ledger(session, scenario.id).valid

    rows = scenario_repo.ledger_rows(session, scenario.id)
    rows[0].actor_role = ActorRole.OBSERVER
    session.commit()

    after = scenario_repo.verify_ledger(session, scenario.id)
    assert not after.valid
    assert after.broken_at == rows[0].sequence


def test_deleting_an_entry_breaks_the_chain(session) -> None:
    """A removal must be detectable too, not only an edit."""
    scenario = _scenario(session)
    for index in range(4):
        _write(session, scenario.id, description=f"entry {index}", index=index)
    assert scenario_repo.verify_ledger(session, scenario.id).valid

    rows = scenario_repo.ledger_rows(session, scenario.id)
    session.delete(rows[1])
    session.commit()

    after = scenario_repo.verify_ledger(session, scenario.id)
    assert not after.valid
    assert after.detail["issue"] == "sequence_gap"


def test_recomputing_a_forged_entry_hash_still_breaks_the_next_link(session) -> None:
    """Confirms this is a real hash comparison, not a heuristic.

    Forging the tampered entry's own hash does not help, because the following entry
    still stores the old ``previous_hash``. That is what makes the chain tamper-evident.
    """
    scenario = _scenario(session)
    for index in range(3):
        _write(session, scenario.id, description=f"entry {index}", index=index)

    rows = scenario_repo.ledger_rows(session, scenario.id)
    target = rows[1]
    target.payload_json = {"note": "rewritten"}
    target.entry_hash = compute_entry_hash(
        previous_hash=target.previous_hash,
        payload=target.payload_json,
        occurred_at=target.occurred_at,
        action=target.action,
        actor_role=target.actor_role,
    )
    session.commit()

    after = scenario_repo.verify_ledger(session, scenario.id)
    assert not after.valid
    assert after.broken_at == rows[2].sequence
    assert after.detail["issue"] == "previous_hash_mismatch"


def test_first_entry_links_to_the_genesis_hash(session) -> None:
    scenario = _scenario(session)
    _write(session, scenario.id, description="first")
    rows = scenario_repo.ledger_rows(session, scenario.id)
    assert rows[0].previous_hash == GENESIS_HASH
    assert scenario_repo.verify_ledger(session, scenario.id).valid


def test_sequence_numbers_are_unique_and_ordered(session) -> None:
    scenario = _scenario(session)
    for index in range(4):
        _write(session, scenario.id, description=f"entry {index}", index=index)
    sequences = [row.sequence for row in scenario_repo.ledger_rows(session, scenario.id)]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)


def test_verify_chain_is_pure_and_repeatable(session) -> None:
    """Verification must not mutate the chain it inspects."""
    scenario = _scenario(session)
    for index in range(3):
        _write(session, scenario.id, description=f"entry {index}", index=index)

    first = scenario_repo.verify_ledger(session, scenario.id)
    second = scenario_repo.verify_ledger(session, scenario.id)
    assert first.valid and second.valid
    assert first.length == second.length
    assert first.head_hash == second.head_hash


def test_empty_chain_is_valid_and_hashes_to_genesis() -> None:
    assert verify_chain([], GENESIS_HASH).valid
    assert verify_chain([], GENESIS_HASH).head_hash == GENESIS_HASH


def test_only_the_specified_fields_are_hashed(session) -> None:
    """Documents the tamper-evidence boundary instead of leaving it implied.

    The spec's hash formula covers ``previous_hash``, payload, ``occurred_at``,
    ``action`` and ``actor_role``. ``description`` and ``entity_ref`` are display fields
    outside that formula, so editing them does NOT invalidate the chain. This is a known
    limitation (see docs/LIMITATIONS.md), pinned here so a future change to the hashed
    field set is deliberate rather than accidental.
    """
    scenario = _scenario(session)
    _write(session, scenario.id, description="wording")
    assert scenario_repo.verify_ledger(session, scenario.id).valid

    rows = scenario_repo.ledger_rows(session, scenario.id)
    rows[0].description = "different wording"
    rows[0].entity_ref = "REF-999"
    session.commit()

    assert scenario_repo.verify_ledger(session, scenario.id).valid, (
        "description/entity_ref are outside the spec's hash formula and are not tamper-evident"
    )
    assert set(HASHED_FIELDS) == {
        "previous_hash",
        "payload_json",
        "occurred_at",
        "action",
        "actor_role",
    }


def test_ledger_route_reports_valid_then_invalid(client) -> None:
    """The same verify-then-mutate property through the HTTP surface."""
    from sqlmodel import Session, select

    from app.domain.models import LedgerRow

    from .conftest import engine_for

    headers = {"X-SOJOURNER-Role": ActorRole.COMMANDER.value}
    generated = client.post("/api/v1/plans/generate", json={}, headers=headers)
    assert generated.status_code == 200

    good = client.get("/api/v1/ledger/verify")
    assert good.status_code == 200
    assert good.json()["chain_status"] == "VALID"

    with Session(engine_for("client")) as db_session:
        row = db_session.exec(select(LedgerRow).order_by(LedgerRow.sequence)).first()
        assert row is not None
        row.payload_json = {"tampered": True}
        db_session.commit()

    bad = client.get("/api/v1/ledger/verify")
    assert bad.status_code == 200
    assert bad.json()["chain_status"] == "INVALID"
    assert bad.json()["broken_at"] is not None

    # The ledger listing surfaces the same verdict and a human-readable badge.
    listing = client.get("/api/v1/ledger")
    assert listing.json()["chain_status"] == "INVALID"
    assert "INVALID" in listing.json()["chain_badge"]
