"""SOJOURNER FastAPI application.

A modular monolith for an offline-first, synthetic-data air-operations decision-support
prototype. Advisory only: every plan change requires an explicit human (Commander)
approval. No autonomous execution, no real operational systems, no external services.

Run locally:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from app.api import (
    routes_assets,
    routes_events,
    routes_ledger,
    routes_missions,
    routes_plans,
    routes_proposals,
    routes_scenarios,
    routes_state,
    routes_system,
    routes_whatif,
    websocket,
)
from app.config import get_settings
from app.db import init_db
from app.services.state_hub import ADVISORY_NOTICE, SYNTHETIC_NOTICE

DESCRIPTION = """
SOJOURNER — dynamic air operations decision support.

**SYNTHETIC DEMONSTRATION DATA.** Every hub, asset, crew member, mission, coordinate and
event in this system is fictional and generated from a seed. Nothing here maps to a real
place, unit, person, route or operational system.

**ADVISORY ONLY — HUMAN APPROVAL REQUIRED.** SOJOURNER proposes plans. It never activates
one on its own. Only a `COMMANDER` may approve or reject a proposal, and activation of a
plan version happens solely through that approval route.
"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


async def mandatory_label_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Guarantee the two mandatory labels are on every REST payload.

    The spec requires `SYNTHETIC DEMONSTRATION DATA` and `ADVISORY ONLY — HUMAN APPROVAL
    REQUIRED` to be visible on every screen. Most handlers set them explicitly, which keeps
    the labels next to the data they qualify and lets a handler add something more specific.
    But relying on ~35 handlers each remembering is how the label quietly goes missing from
    the one endpoint nobody re-reads, so anything that came back without them gets them
    here.

    An existing value is never overwritten: a handler that wrote something more specific is
    believed over this default. Non-JSON responses and anything outside the versioned REST
    prefix (the WebSocket, the docs) are passed through untouched.
    """
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return response
    if not request.url.path.startswith("/api/"):
        return response

    body = b""
    async for chunk in response.body_iterator:  # type: ignore[attr-defined]
        body += chunk if isinstance(chunk, bytes) else str(chunk).encode()

    try:
        payload = json.loads(body) if body else None
    except json.JSONDecodeError:
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=content_type,
        )

    if not isinstance(payload, dict):
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=content_type,
        )

    payload.setdefault("synthetic_data_notice", SYNTHETIC_NOTICE)
    payload.setdefault("advisory_notice", ADVISORY_NOTICE)
    return JSONResponse(
        content=payload,
        status_code=response.status_code,
        headers={
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-length", "content-type")
        },
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} (synthetic demonstration)",
        version=settings.app_version,
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.middleware("http")(mandatory_label_middleware)

    for module in (
        routes_system,
        routes_scenarios,
        routes_state,
        routes_missions,
        routes_assets,
        routes_plans,
        routes_events,
        routes_proposals,
        routes_whatif,
        routes_ledger,
    ):
        app.include_router(module.router, prefix=settings.api_prefix)
    # The WebSocket is mounted outside the versioned REST prefix so a browser can reach it
    # at a stable path, while the REST surface stays under /api/v1 as specified.
    app.include_router(websocket.router)

    @app.exception_handler(403)
    async def forbidden_handler(_request: Request, exc) -> JSONResponse:  # type: ignore[no-untyped-def]
        detail = getattr(exc, "detail", "Forbidden.")
        return JSONResponse(
            status_code=403,
            content={
                "detail": detail,
                "synthetic_data_notice": SYNTHETIC_NOTICE,
                "advisory_notice": ADVISORY_NOTICE,
            },
        )

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "app": settings.app_name,
            "version": settings.app_version,
            "api": f"{settings.api_prefix}/health",
            "docs": "/docs",
            "websocket": "/ws/live",
            "synthetic_data_notice": SYNTHETIC_NOTICE,
            "advisory_notice": ADVISORY_NOTICE,
        }

    return app


app = create_app()


__all__ = ["app", "create_app", "lifespan"]