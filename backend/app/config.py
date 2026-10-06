"""Application configuration.

All settings are local/demo oriented. No cloud dependencies, no paid APIs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    """Runtime settings, overridable through environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=(REPO_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SOJOURNER"
    app_version: str = "0.1.0"
    api_prefix: str = "/api/v1"

    # Storage -----------------------------------------------------------------
    # SQLite by default; set SOJOURNER_DATABASE_URL to a PostgreSQL DSN to switch.
    database_url: str = f"sqlite:///{BACKEND_DIR / 'sojourner.db'}"
    sql_echo: bool = False

    # CORS (local dev: Vite dev server) ---------------------------------------
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Scenario defaults -------------------------------------------------------
    default_seed: int = 20260101
    horizon_hours: int = 12
    asset_count: int = 22
    crew_count: int = 36
    mission_count: int = 13

    # Planning ----------------------------------------------------------------
    # Hard wall-clock budget per CP-SAT solve, per option (seconds).
    solver_time_limit_seconds: float = 8.0
    # Deterministic greedy fallback threshold: used when the solver cannot
    # produce a feasible solution inside solver_time_limit_seconds.
    greedy_fallback: bool = True

    # Ledger ------------------------------------------------------------------
    ledger_genesis_hash: str = "0" * 64

    # Optional local assistant (disabled by default, read-only by design) ------
    assistant_enabled: bool = False
    assistant_base_url: str = "http://localhost:11434"
    assistant_model: str = "llama3.1"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()