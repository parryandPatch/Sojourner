# SOJOURNER

Dynamic air-operations decision support. A laptop-runnable prototype: FastAPI +
React, offline, synthetic data only.

> **SYNTHETIC DEMONSTRATION DATA**
>
> Every hub, asset, crew member, mission, hazard zone, weather window, coordinate
> and call sign in this system is fictional and generated from a seed. Nothing
> here describes a real place, organisation, or operation.
>
> **ADVISORY ONLY — HUMAN APPROVAL REQUIRED**
>
> The planner produces options. It never issues them. A plan becomes the active
> plan only when a human in the `COMMANDER` role approves it. There is no
> auto-activation path, no autonomous control, and no weapon or targeting
> capability of any kind anywhere in this codebase.

---

## What it does

You are given a synthetic air-operations picture: three hubs, 22 air assets,
36 crew members, 13 missions with priority weights, two hazard zones, two weather
windows. You ask for plans. You get **three trade-off options** — coverage first,
safety first, stability first — each a complete assignment set, each independently
audited, none of them active.

Then something goes wrong. A transport fails and goes to repair. A crew member
goes below rest minimum. A hub closes its airspace. The system identifies exactly
which missions the event invalidated, holds every sortie that has already taken
off absolutely still, re-decides only the affected missions, and produces three
fresh proposals. You compare them, inject an adverse weather window into a
sandbox to see what it would have cost, and either approve or reject.

Every decision is recorded in an append-only SHA-256 hash-chained ledger that
you can verify and replay from the UI.

## Quick start

Two ways to run it. Pick one.

### With Docker (one command)

```bash
cp .env.example .env
make up
# frontend  http://localhost:8080
# backend   http://localhost:8000/api/v1/health
# API docs  http://localhost:8000/docs
```

### Without Docker (no container runtime needed)

```bash
make install     # creates backend/.venv, installs Python + Node deps
make dev         # backend on :8000, Vite on :5173 with hot reload
# open http://localhost:5173
```

`make dev` runs both servers in the foreground. Open a second terminal for
`make demo` (see below).

Requirements without Docker: Python 3.11 or newer and Node 20 or newer. No
network access is required at runtime, and none is made: there is no outbound
call anywhere in the application.

## What to look at first

In priority order, because this is the order the guarantees nest:

| # | Where | What it shows you |
| --- | --- | --- |
| 1 | **A · Command deck** | Both mandatory labels, the role selector, KPIs, the schematic map, the live alert feed |
| 2 | **B · Planning** → *Generate* | Three visibly different options, side by side, with the 85% coverage floor drawn on each card. Nothing becomes active. |
| 3 | **C · Disruption centre** → *Inject EVENT_A* | Which missions the event affected, which were frozen and held, three new proposals pending |
| 4 | Switch role to **Observer**, try to approve | Refused, with the reason shown verbatim. Switch back to **Commander** and approve. Now exactly one plan is ACTIVE and its two siblings are SUPERSEDED. |
| 5 | **D · What-if lab** | Run an experiment. The panel at the bottom shows the live state digest before and after — byte-identical, because the sandbox is a clone |
| 6 | **E · Decision trail** | The hash chain, its VALID/INVALID status, and a replay of how the active plan came to be |
| 7 | **F · Asset readiness** | Synthetic readiness bands, labelled as synthetic |

Or run the whole thing from the command line, with assertions rather than
narration:

```bash
make demo
```

It walks the same path with `curl` and fails loudly: PLANNER approval must return
403, exactly one plan may be ACTIVE, a what-if must leave the live state digest
unchanged, the chain must verify. Non-zero exit on the first failure.

## How it is put together

A modular monolith. 15 services, one API layer, one SQLite file. FastAPI and
Pydantic v2 on the back, React + TypeScript + Vite + Tailwind on the front,
OR-Tools CP-SAT for the planning itself. No message broker, no cache, no
external service.

```
sojourner/
  backend/app/
    domain/       enums, immutable domain objects, API schemas
    services/     15 services — all the behaviour lives here
    api/          11 route modules, dependencies, websocket
    repositories/ SQLite persistence and state loading
    main.py       app assembly, CORS, the mandatory-label middleware
    tests/        179 tests
  frontend/src/
    pages/        six screens
    components/   presentational pieces
    types/api.ts  a typed mirror of every backend response
    hooks/        async loading, role, websocket feed
  benchmarks/     the measurement harness
  scripts/        the scripted walkthrough
  docs/           architecture, runbook, limitations, decisions
```

`docs/ARCHITECTURE.md` explains the design and the interesting decisions.
`docs/DECISIONS.md` explains *why* each one went the way it did, including the
ones I got wrong first.

## Verification

```bash
make check     # pytest + ruff + tsc --noEmit + vite build
make test      # 179 backend tests
make lint      # ruff
make typecheck # tsc --noEmit
make bench     # regenerates docs/benchmark-results.md
```

Everything in `docs/benchmark-results.md` was produced by `make bench` on the
machine named in that file. The numbers are wall clock for a whole three-option
solve on synthetic data, and they are a regression smoke test, not a capacity
claim.

## Three things worth knowing before you trust it

**The planner does not always return three different options.** It always returns
three, but a re-plan re-opens only the missions the event affected, and when that
leaves one or two decisions all three objectives frequently pick the same option.
The API reports `duplicate_options` and `free_mission_count`, and the UI marks the
matching cards rather than dressing one up as a trade-off the solver never
optimised. Measured at 7/24 re-plans with the shipping weights; a sweep found a
legal weighting that reaches 13/24, which was deliberately not adopted because the
weights were set before that measurement existed. Full argument, and the part of it
that is unresolved, in `docs/LIMITATIONS.md` §2.

**The greedy fallback ignores the variant weights.** It walks the option pool in a
fixed order, so if the solver hits its budget, all three fallback plans are the
same plan. That is measured and labelled `FALLBACK` on every card, and reported
in the benchmark output.

**The ledger hash does not cover every field.** The spec's hash formula covers
`previous_hash + canonical_JSON(payload) + occurred_at + action + actor_role`.
The human-readable `description` and `entity_ref` sit outside it, so an attacker
with database write access could rewrite a description without breaking the
chain. A test pins that scope so the limitation cannot quietly widen.

## Scope

This is a prototype, and these are the boundaries. `docs/LIMITATIONS.md` has the
full list with detail.

- Synthetic and fictional throughout. No real data source, no external service,
  no network call at runtime.
- Single-machine, single-user, no concurrency control beyond SQLite's own.
- The role selector is a header, not authentication. It demonstrates the
  capability model; it is not a security boundary and must not be treated as one.
- The hash chain detects tampering, not a determined forger with write access to
  the whole database.
- The Docker stack is authored and reviewed but was never built — no container
  runtime was available on the build machine. Everything verified end to end was
  verified by running the two services directly.

## Documentation

| Document | What it is for |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the system is built, and why the pieces are where they are |
| [docs/DEMO_RUNBOOK.md](docs/DEMO_RUNBOOK.md) | Step-by-step guide to walking the demo, with what to point at |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What this does not do, and the measurements behind each claim |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every significant choice, including the ones that were wrong first |
| [docs/benchmark-results.md](docs/benchmark-results.md) | Generated measurements — regenerate with `make bench` |