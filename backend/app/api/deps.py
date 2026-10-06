"""Shared FastAPI dependencies: database session, active scenario, and the role guard.

The role guard is the only place that decides who may approve. It is deliberately
server-side: the UI's role selector is a convenience, not a control.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session

from app.db import get_session
from app.domain.enums import ActorRole
from app.domain.models import ScenarioRow
from app.repositories import scenario_repo

SessionDep = Annotated[Session, Depends(get_session)]


def get_actor_role(x_sojourner_role: Annotated[str | None, Header()] = None) -> ActorRole:
    """Resolve the acting role from the ``X-SOJOURNER-Role`` header.

    Local demo auth only: no identity provider, no tokens. Anything missing or unknown
    falls back to the most restrictive role.
    """
    if not x_sojourner_role:
        return ActorRole.OBSERVER
    try:
        return ActorRole(x_sojourner_role.strip().upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role: {x_sojourner_role}. Use PLANNER, COMMANDER, MAINTAINER or OBSERVER.",
        ) from exc


ActorRoleDep = Annotated[ActorRole, Depends(get_actor_role)]


def require_commander(role: ActorRoleDep) -> ActorRole:
    """Only a Commander may approve or reject. Returns HTTP 403 for anyone else."""
    if role != ActorRole.COMMANDER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Role {role.value} may not approve or reject a plan. "
                "Only COMMANDER can activate a synthetic plan version."
            ),
        )
    return role


def require_write_access(role: ActorRoleDep) -> ActorRole:
    """Block Observers (and anything else read-only) from mutating demo state."""
    if role == ActorRole.OBSERVER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Role OBSERVER is read-only. Switch to PLANNER or COMMANDER to change demo state.",
        )
    return role


CommanderDep = Annotated[ActorRole, Depends(require_commander)]
WriterDep = Annotated[ActorRole, Depends(require_write_access)]


def active_scenario_or_404(session: SessionDep) -> ScenarioRow:
    """Load the active scenario, creating the seeded default on first use."""
    row = scenario_repo.active_scenario(session)
    if row is None:
        from app.config import get_settings

        settings = get_settings()
        row = scenario_repo.create_scenario(
            session,
            scenario_repo.default_config(
                settings.default_seed,
                mission_count=settings.mission_count,
                asset_count=settings.asset_count,
                crew_count=settings.crew_count,
                horizon_hours=settings.horizon_hours,
            ),
        )
    return row


ScenarioDep = Annotated[ScenarioRow, Depends(active_scenario_or_404)]


def iter_sessions() -> Iterator[Session]:  # pragma: no cover - convenience for scripts
    yield from get_session()


__all__ = [
    "ActorRoleDep",
    "CommanderDep",
    "ScenarioDep",
    "SessionDep",
    "WriterDep",
    "active_scenario_or_404",
    "get_actor_role",
    "iter_sessions",
    "require_commander",
    "require_write_access",
]