"""Health check and local role selector metadata."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ActorRoleDep
from app.config import get_settings
from app.services import state_hub

router = APIRouter(tags=["system"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
        "advisory_notice": state_hub.ADVISORY_NOTICE,
        "database": (
            "sqlite" if settings.database_url.startswith("sqlite") else "postgresql"
        ),
        "solver": "ortools cp-sat",
        "solver_time_limit_seconds": settings.solver_time_limit_seconds,
    }


@router.get("/roles")
def roles(role: ActorRoleDep) -> dict:
    return {
        "acting_role": role.value,
        "can_approve": state_hub.can_approve(role),
        "permissions": state_hub.role_permissions()[role.value],
        "notice": (
            "Local demo role selector only. There is no identity provider, no token and no "
            "external authentication in this prototype."
        ),
        "synthetic_data_notice": state_hub.SYNTHETIC_NOTICE,
        "advisory_notice": state_hub.ADVISORY_NOTICE,
    }


__all__ = ["router"]