# syntax=docker/dockerfile:1
#
# SOJOURNER backend image.
#
# Two stages: wheels are built once into a virtualenv, then copied into a slim runtime
# image that never sees a compiler. That keeps the runtime attack surface small and the
# image fast to build on a cold cache only once.

# ---------------------------------------------------------------------------------------
# Stage 1 — build the dependency set
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# OR-Tools and friends have no manylinux wheels for every platform; these are the build
# tools needed only when a wheel is missing. They do not reach the runtime image.
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies are declared separately from the application so that editing app code does
# not invalidate the (slow) dependency layer.
COPY backend/pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.8" \
        "pydantic-settings>=2.4" \
        "sqlmodel>=0.0.22" \
        "sqlalchemy>=2.0" \
        "ortools>=9.10" \
        "httpx>=0.27"

# ---------------------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    SOJOURNER_DATABASE_URL=sqlite:////data/sojourner.db \
    SOJOURNER_SOLVER_TIME_LIMIT_SECONDS=8

RUN apt-get update && apt-get install --no-install-recommends -y \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 sojourner \
    && mkdir -p /data \
    && chown -R sojourner:sojourner /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=sojourner:sojourner backend/app ./app
COPY --chown=sojourner:sojourner backend/pyproject.toml ./

USER sojourner

EXPOSE 8000

# /health reports process liveness and configuration only; it does not touch the database.
# That is deliberate: a liveness probe should not fail because a volume is slow, and the
# UI has its own surfaces for reporting database problems.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]