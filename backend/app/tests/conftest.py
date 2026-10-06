"""Shared test fixtures.

Every test runs against a throwaway SQLite database so no test can see another's state.
The API tests override the session dependency; the service tests never touch a database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.api import deps as api_deps
from app.domain.enums import ActorRole
from app.main import create_app
from app.services import frontier_explorer, scenario_studio

DEFAULT_SEED = 20260101


def build_state(seed: int = DEFAULT_SEED, now_minute: int = 0):  # type: ignore[no-untyped-def]
    """Deterministic in-memory operational state (no database involved)."""
    return scenario_studio.build_scenario(
        scenario_studio.default_config(seed), now_minute=now_minute
    )


def build_initial_frontier(seed: int = DEFAULT_SEED, now_minute: int = 0):  # type: ignore[no-untyped-def]
    """Seeded state plus the three-option frontier, solved once per session."""
    state = build_state(seed, now_minute)
    frontier = frontier_explorer.build_frontier(state, scenario_id="SCN-TEST", version=1)
    return state, frontier


def headers(role: ActorRole = ActorRole.PLANNER) -> dict[str, str]:
    return {"X-SOJOURNER-Role": role.value}


@pytest.fixture(scope="session")
def initial_frontier() -> tuple:
    """The seeded three-option frontier. Session-scoped because solving costs ~1s."""
    return build_initial_frontier()


@pytest.fixture(scope="session")
def initial_state() -> object:
    state, _frontier = build_initial_frontier()
    return state


@pytest.fixture(scope="session")
def coverage_plan(initial_frontier) -> object:
    _state, frontier = initial_frontier
    return frontier.plans[0].plan


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    """A session on a per-test SQLite file, with the seeded scenario already created."""
    from app.repositories import scenario_repo

    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db_session:
        scenario_repo.create_scenario(db_session, scenario_studio.default_config(DEFAULT_SEED))
        yield db_session
    engine.dispose()


#: Maps a fixture name to the SQLite engine that fixture installed. Ledger-integrity
#: tests need this to tamper with a row behind the API's back and prove that
#: verification notices.
ENGINE_REGISTRY: dict[str, object] = {}


def engine_for(name: str):  # type: ignore[no-untyped-def]
    """Return the engine backing a named client/session fixture."""
    return ENGINE_REGISTRY[name]


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """TestClient backed by a per-test SQLite file."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)

    def override_session() -> Iterator[Session]:  # type: ignore[no-untyped-def]
        with Session(engine) as db_session:
            yield db_session

    app = create_app()
    app.dependency_overrides[api_deps.get_session] = override_session
    ENGINE_REGISTRY["client"] = engine
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        ENGINE_REGISTRY.pop("client", None)
        engine.dispose()


@pytest.fixture()
def planned_client(client: TestClient) -> TestClient:
    """A client that already holds an active, approved plan set."""
    from sqlmodel import Session

    from app.repositories import scenario_repo

    # Reuse the same engine the override installs by generating through the API itself.
    response = client.post("/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER))
    assert response.status_code == 200, response.text
    _ = (scenario_repo, Session)
    return client


__all__ = [
    "DEFAULT_SEED",
    "build_initial_frontier",
    "build_state",
    "engine_for",
    "headers",
    "initial_frontier",
    "initial_state",
]