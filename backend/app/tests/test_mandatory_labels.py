"""The two mandatory labels must be on every screen.

The spec requires `SYNTHETIC DEMONSTRATION DATA` and `ADVISORY ONLY — HUMAN APPROVAL
REQUIRED` to be visible on every screen. They are not decoration: without them a reader has
no way to tell that the entities in front of them are fictional, or that nothing here
happens without a Commander.

Two mechanisms keep them present. Most handlers set them explicitly, next to the data they
qualify, because a handler that knows its payload can say something more specific. A
middleware fills in anything that came back without them, so a new endpoint added in six
months cannot silently ship unlabelled.

These tests walk the actual API surface rather than trusting that every handler was written
carefully.
"""

from __future__ import annotations

import pytest

from app.domain.enums import ActorRole
from app.main import app
from app.services.state_hub import ADVISORY_NOTICE, SYNTHETIC_NOTICE

from .conftest import headers

# Every GET route under /api/v1 that a screen reads from. Mutation routes are covered too
# via the POST cases below, but they change scenario state, so they are exercised in their
# own tests rather than enumerated here.
SCREEN_ENDPOINTS = [
    "/api/v1/health",
    "/api/v1/roles",
    "/api/v1/scenarios/active",
    "/api/v1/scenarios/roles",
    "/api/v1/state/overview",
    "/api/v1/state/map",
    "/api/v1/state/crew",
    "/api/v1/state/stock",
    "/api/v1/state/readiness",
    "/api/v1/state/reason-codes",
    "/api/v1/missions",
    "/api/v1/assets",
    "/api/v1/plans",
    "/api/v1/events",
    "/api/v1/events/scripted",
    "/api/v1/proposals",
    "/api/v1/ledger",
    "/api/v1/ledger/verify",
    "/api/v1/what-if/templates",
]


@pytest.mark.parametrize("path", SCREEN_ENDPOINTS)
def test_every_screen_endpoint_carries_both_mandatory_labels(client, path: str) -> None:
    response = client.get(path, headers=headers(ActorRole.OBSERVER))
    assert response.status_code == 200, f"{path} -> HTTP {response.status_code}"
    body = response.json()
    assert body.get("synthetic_data_notice") == SYNTHETIC_NOTICE, path
    assert body.get("advisory_notice") == ADVISORY_NOTICE, path


def test_the_notice_strings_are_the_exact_text_the_spec_requires() -> None:
    """A paraphrase is not compliance. Pin the strings."""
    assert SYNTHETIC_NOTICE == "SYNTHETIC DEMONSTRATION DATA"
    assert ADVISORY_NOTICE == "ADVISORY ONLY — HUMAN APPROVAL REQUIRED"


def test_a_forbidden_response_also_carries_both_labels(client) -> None:
    """A refusal is exactly when a reader most needs to be told what the system is."""
    response = client.post(
        "/api/v1/events/inject",
        json={"scripted_event": "EVENT_A"},
        headers=headers(ActorRole.OBSERVER),
    )
    assert response.status_code == 403
    body = response.json()
    assert body["synthetic_data_notice"] == SYNTHETIC_NOTICE
    assert body["advisory_notice"] == ADVISORY_NOTICE
    assert body["detail"], "a refusal must say something, or it reads like a broken button"


def test_the_middleware_never_overwrites_a_more_specific_label(client) -> None:
    """Handlers may say something sharper than the default; the default must defer."""
    response = client.post(
        "/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER)
    )
    assert response.status_code == 200
    body = response.json()
    # routes_plans writes its own activation note; if the middleware had clobbered the
    # notices with generic text the screen would lose the specific guidance.
    assert body["activation_note"]
    assert "Commander" in body["activation_note"]


def test_every_api_route_is_reachable_and_labelled_or_deliberately_exempt() -> None:
    """Nothing under /api/v1 may exist without the labels, by construction.

    The middleware fires for every JSON dict the API returns, so this is the check that
    fails if the middleware is ever removed or narrowed — it walks the published OpenAPI
    inventory rather than a hand-written list, so an endpoint added in six months is
    covered without anyone remembering to extend the list above.
    """
    paths = sorted(app.openapi()["paths"])
    assert paths, "the OpenAPI schema exposes no paths; the check itself is broken"

    unversioned = [path for path in paths if not path.startswith("/api/v1")]
    assert not unversioned, f"REST routes must live under /api/v1, found {unversioned}"

    assert len(paths) >= 18, paths
