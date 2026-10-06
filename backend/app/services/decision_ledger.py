"""Hash-chained decision ledger.

Every key action writes one record. Hashes are computed as::

    entry_hash = SHA-256(previous_hash + canonical_JSON(payload) + occurred_at + action + actor_role)

This is a demonstration integrity feature (tamper-evident history), not a
replacement for an enterprise security system.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.domain.enums import ActorRole, LedgerAction
from app.domain.models import LedgerRow

GENESIS_HASH = "0" * 64


def canonical_json(payload: dict) -> str:
    """Stable JSON: sorted keys, no whitespace, no NaN, ISO dates as strings."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def compute_entry_hash(
    previous_hash: str,
    payload: dict,
    occurred_at: datetime,
    action: LedgerAction,
    actor_role: ActorRole,
) -> str:
    occurred = occurred_at.isoformat() if occurred_at.tzinfo else occurred_at.replace(tzinfo=UTC).isoformat()
    material = f"{previous_hash}{canonical_json(payload)}{occurred}{action.value}{actor_role.value}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ChainVerification:
    valid: bool
    length: int
    head_hash: str
    checked: int = 0
    broken_at: int | None = None
    detail: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "VALID" if self.valid else "INVALID"


def next_sequence(rows: list[LedgerRow]) -> int:
    return (max((r.sequence for r in rows), default=0)) + 1


def head_hash(rows: list[LedgerRow], genesis: str = GENESIS_HASH) -> str:
    return rows[-1].entry_hash if rows else genesis


def build_entry(
    rows: list[LedgerRow],
    *,
    scenario_id: str,
    actor_role: ActorRole,
    action: LedgerAction,
    entity_ref: str,
    payload: dict,
    description: str = "",
    occurred_at: datetime | None = None,
    genesis: str = GENESIS_HASH,
) -> LedgerRow:
    """Create the next chained row. Does not persist; the repository does that."""
    ordered = sorted(rows, key=lambda r: r.sequence)
    previous = head_hash(ordered, genesis)
    stamp = (occurred_at or datetime.now(UTC)).replace(microsecond=0)
    sequence = next_sequence(ordered)
    return LedgerRow(
        scenario_id=scenario_id,
        sequence=sequence,
        occurred_at=stamp,
        actor_role=actor_role,
        action=action,
        entity_ref=entity_ref,
        payload_json=dict(payload),
        previous_hash=previous,
        entry_hash=compute_entry_hash(previous, payload, stamp, action, actor_role),
        description=description,
    )


def verify_chain(rows: list[LedgerRow], genesis: str = GENESIS_HASH) -> ChainVerification:
    """Recompute every hash and every link. Any mutation makes this INVALID."""
    ordered = sorted(rows, key=lambda r: r.sequence)
    previous = genesis
    detail: dict = {}

    for position, row in enumerate(ordered, start=1):
        if row.sequence != position:
            return ChainVerification(
                valid=False,
                length=len(ordered),
                head_hash=head_hash(ordered, genesis),
                checked=position - 1,
                broken_at=row.sequence,
                detail={"issue": "sequence_gap", "expected": position, "found": row.sequence},
            )
        if row.previous_hash != previous:
            return ChainVerification(
                valid=False,
                length=len(ordered),
                head_hash=head_hash(ordered, genesis),
                checked=position - 1,
                broken_at=row.sequence,
                detail={"issue": "previous_hash_mismatch", "expected": previous, "found": row.previous_hash},
            )
        recomputed = compute_entry_hash(
            row.previous_hash, row.payload_json, row.occurred_at, row.action, row.actor_role
        )
        if recomputed != row.entry_hash:
            return ChainVerification(
                valid=False,
                length=len(ordered),
                head_hash=head_hash(ordered, genesis),
                checked=position - 1,
                broken_at=row.sequence,
                detail={"issue": "entry_hash_mismatch", "expected": recomputed, "found": row.entry_hash},
            )
        previous = row.entry_hash

    detail["genesis"] = genesis
    detail["recomputed_head"] = previous
    return ChainVerification(
        valid=True,
        length=len(ordered),
        head_hash=previous,
        checked=len(ordered),
        detail=detail,
    )


def chain_badge(verification: ChainVerification) -> str:
    if verification.valid:
        return f"VALID ({verification.length} entries)"
    return f"INVALID at sequence {verification.broken_at}: {verification.detail.get('issue', 'unknown')}"