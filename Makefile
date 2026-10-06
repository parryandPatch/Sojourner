# SOJOURNER — developer task runner.
#
# `make help` lists everything. The targets fall into three groups:
#   * run      — start the stack locally, with or without Docker
#   * quality  — tests, lint, typecheck, benchmarks
#   * demo     — scripted curl walkthroughs of the demo flow
#
# Nothing here mutates state outside the project directory, and no target contacts a
# network service beyond localhost.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

BACKEND  := backend
FRONTEND := frontend
VENV     := $(BACKEND)/.venv
PYTHON   := $(VENV)/bin/python
PIP      := $(VENV)/bin/pip
HOST     ?= 127.0.0.1
PORT     ?= 8000

# -m uvicorn rather than the `uvicorn` script: it guarantees the interpreter that owns the
# installed packages is the one that runs.
UVICORN := $(PYTHON) -m uvicorn app.main:app --host $(HOST) --port $(PORT) --reload --reload-dir app

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# --- Setup --------------------------------------------------------------------------------

.PHONY: install
install: install-backend install-frontend ## Install backend and frontend dependencies

.PHONY: install-backend
install-backend: ## Create the backend virtualenv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

.PHONY: install-frontend
install-frontend: ## Install frontend dependencies from the lockfile
	cd $(FRONTEND) && npm ci

# --- Run ----------------------------------------------------------------------------------

.PHONY: up
up: ## Start the full stack with Docker (frontend on :8080)
	docker compose up --build

.PHONY: down
down: ## Stop the Docker stack, keeping the data volume
	docker compose down

.PHONY: reset
reset: ## Stop the Docker stack AND delete the data volume (clean scenario)
	docker compose down -v

.PHONY: dev
dev: ## Run backend and frontend locally with hot reload (needs `make install` first)
	@echo "backend  -> http://$(HOST):$(PORT)/api/v1/health"
	@echo "frontend -> http://localhost:5173"
	@trap 'kill 0' EXIT INT TERM; \
	( cd $(BACKEND) && $(UVICORN) ) & \
	( cd $(FRONTEND) && npm run dev ) & \
	wait

.PHONY: backend
backend: ## Run only the backend, with reload
	cd $(BACKEND) && $(UVICORN)

.PHONY: frontend
frontend: ## Run only the Vite dev server
	cd $(FRONTEND) && npm run dev

.PHONY: reset-scenario
reset-scenario: ## Regenerate the scenario and wipe plans, proposals and ledger
	@role=$${SOJOURNER_ROLE:-COMMANDER}; \
	curl -fsS -X POST http://$(HOST):$(PORT)/api/v1/scenarios/reset \
		-H "Content-Type: application/json" -H "X-SOJOURNER-Role: $$role" \
		-d '{"seed": 20260101}' && echo

# --- Quality ------------------------------------------------------------------------------

.PHONY: test
test: test-backend ## Run every backend test

.PHONY: test-backend
test-backend: ## Run the pytest suite
	cd $(BACKEND) && ../$(PYTHON) -m pytest -q

.PHONY: lint
lint: ## Run ruff over the backend
	cd $(BACKEND) && ../$(PYTHON) -m ruff check app

.PHONY: format
format: ## Apply ruff's safe automatic fixes
	cd $(BACKEND) && ../$(PYTHON) -m ruff check app --fix

.PHONY: typecheck
typecheck: ## Typecheck the frontend without emitting
	cd $(FRONTEND) && npm run typecheck

.PHONY: build-frontend
build-frontend: ## Production-build the frontend bundle
	cd $(FRONTEND) && npm run build

.PHONY: check
check: lint typecheck test-backend build-frontend ## Everything CI would run

.PHONY: bench
bench: ## Run the performance benchmark and write docs/benchmark-results.md
	cd $(BACKEND) && ../$(PYTHON) ../benchmarks/run_benchmark.py

# --- Demo ---------------------------------------------------------------------------------

.PHONY: demo
demo: ## Scripted end-to-end walkthrough with curl (requires the backend running)
	./scripts/demo_walkthrough.sh

.PHONY: clean
clean: ## Remove caches and build output
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(FRONTEND)/dist $(FRONTEND)/node_modules/.vite
	rm -f $(BACKEND)/.pytest_cache/../../.pytest_cache