# Architecture

SOJOURNER is a modular monolith. One process, one SQLite file, no broker, no
cache, no external service. That is a deliberate choice for a prototype whose
whole claim is "a laptop can run this" — and it also happens to keep the
boundaries honest, because a module boundary you have to enforce with a network
call is a boundary you will regret.

## Shape

```
                  ┌──────────────────────────────────────────┐
  Browser ───────▶│  FastAPI  ·  /api/v1/*  +  /ws/live      │
   (React/Vite)   │                                          │
                  │  routes_*.py ─▶ services/ ─▶ repositories │
                  └────────────────────┬─────────────────────┘
                                       │
                       ┌───────────────┴───────────────┐
                       │   SQLite (mutable overlay)     │
                       └───────────────────────────────┘
```

The WebSocket sits outside the versioned REST prefix so a browser reaches it at a
stable path, while the REST surface stays under `/api/v1` as the spec requires.

## Layering

Dependencies point inwards only. `api` knows about `services`, `services` know
about `repositories` and `domain`, and `domain` knows about nothing.

| Layer | Contents | Rule |
| --- | --- | --- |
| `domain/` | enums, frozen dataclasses, Pydantic schemas | Pure. No I/O, no imports from services or api. |
| `services/` | All behaviour. 15 modules. | May import `domain`. Must not import `api`. |
| `repositories/` | SQLite reads/writes, state loading | May import `domain`. Must not import `services`. |
| `api/` | 11 route modules, `deps.py`, `websocket.py` | May import everything below. Holds no behaviour. |

The rule that matters most is that **no service imports another service's
internals**. They call each other's public functions. `whatif_lab` needs to apply
an event; it calls `disruption_monitor.apply_event`. It does not reach into its
helpers. That is what makes the extraction path in the spec's §5 real rather than
aspirational — moving a service out of the process later means replacing its
imports, not untangling them.

## The 15 services

| Service | Responsibility |
| --- | --- |
| `scenario_studio` | Seeded generation of the whole synthetic world. |
| `risk_engine` | The transparent three-component risk score. |
| `feasibility_engine` | Option enumeration and the ten hard-constraint checks. |
| `planning_engine` | The CP-SAT model, the variant objectives, the greedy fallback. |
| `frontier_explorer` | Builds the three options, diffs them, reports distinctness. |
| `plan_auditor` | Re-derives the constraints from the assignments, independently of the solver. |
| `stability_guard` | The frozen / locked / free split for re-planning. |
| `disruption_monitor` | Scripted events, and which missions each one invalidates. |
| `decision_ledger` | The SHA-256 hash-chained append-only log and `verify_chain()`. |
| `explanation_service` | Prose rendered from stored reason codes. |
| `whatif_lab` | The isolated sandbox, with a before/after digest proof. |
| `readiness_estimator` | Synthetic readiness bands, labelled as synthetic. |
| `state_hub` | The single place that maps domain objects to API payloads. |

(`state_hub` also carries the two mandatory label constants, which is why every
service can assert on them without a circular import.)

## Time

Two clocks, and the distinction is load-bearing:

- **Internal time** is an integer number of minutes from `scenario_start`. Every
  scheduling decision — endurance, duty limits, rest, weather windows, takeoff
  ordering — is computed in these integers. No float time arithmetic, no
  timezone, no `datetime.now()` anywhere in the planning path.
- **External time** is UTC ISO-8601, used only for display and for ledger
  `occurred_at` stamps.

The consequence is that the whole scenario is reproducible by (seed, minute).
`test_determinism.py` asserts that two independent builds at the same minute
produce byte-identical output.

## The planning model

### Ten hard constraints

An assignment is allowed only if all ten hold:

1. Asset class supports the mission class requirement.
2. Asset is available, not unavailable or under repair.
3. Transit plus station time fits inside endurance and range limits.
4. The required payload category exists at the origin hub in sufficient quantity.
5. Required crew roles are qualified, available, rested, and within duty limits.
6. The mission can be completed inside its time window.
7. The asset and its crew do not overlap with any other assignment.
8. The origin hub is open at departure and arrival, and its capacity is not exceeded.
9. The route does not cross an active hard-restriction zone.
10. Combined risk does not exceed the mission maximum.

Constraints 1–10 map to reason codes in `domain/enums.py` and are checked in
`feasibility_engine.py` before an option ever reaches the solver. The solver's job
is *selection* among already-feasible options, not feasibility — which is what
lets the auditor re-derive the answer independently and trust the result.

Crew no-overlap is modelled **per individual crew member**, not per team. A team of
three that is free as a unit is still a problem if one member of it is on another
sortie, and that is the case a per-team constraint silently waves through.

### The objective

Integer-scaled, so CP-SAT gets integers and the answer is reproducible:

```
SCALE = 1000

maximise  Σ  priority_weight(mission) * SCALE * chosen_option
         - Σ  risk_weight    * round(combined_risk * SCALE)
         - Σ  cost_weight    * scaled_cost
         - Σ  change_weight  * scaled_change_from_parent
```

Priority weights are `P1=100, P2=60, P3=35, P4=20, P5=10`, so weighted coverage
is a meaningful number and not a mission count in disguise.

The three variants differ only in the second and third terms:

| Variant | risk weight | cost weight | change weight |
| --- | ---: | ---: | ---: |
| COVERAGE FIRST | 6 | 3 | 8 |
| SAFETY FIRST | 900 | 0 | 0 |
| STABILITY FIRST | 6 | 1 | 40 |

SAFETY FIRST is allowed to give up coverage to buy a large risk reduction, up to a
floor. STABILITY FIRST buys a small risk reduction in exchange for heavily
weighting churn against the parent plan, so it converges towards what was already
approved.

### The 85% floor

```
floor = weighted_coverage(COVERAGE FIRST) * 0.85
```

SAFETY FIRST and STABILITY FIRST are both constrained to meet it. This is a hard
CP-SAT constraint, not a post-hoc filter — a plan below the floor is infeasible
and never gets proposed. The floor is computed from the *coverage-first run's
actual* weighted coverage, passed explicitly as `coverage_reference`, because
computing it from a plan that has not been solved yet would be a guess.

One implementation detail worth recording: the floor constraint sums
`priority_weight * SCALE`, so the `required` bound must also be multiplied by
`SCALE`. Omitting it produced a floor that looked right in the code and was
three orders of magnitude too loose in the solver.

### Determinism

CP-SAT is free to break ties arbitrarily and will do so differently across runs,
which would make the "same seed gives the same plan" claim false. Three things
make it true:

- `num_search_workers = 1` — no parallel search, no nondeterministic portfolio.
- `random_seed = 0`.
- A `-index` coefficient tie-break on equal options, so even a single-worker
  search has a total order to resolve ties by.

Together these are what make `test_determinism.py` and the replay view meaningful.

### The budget and the fallback

The solver gets 8 seconds per variant, clamped with `max(0.5, ...)`. If it returns
no solution inside the budget, `planning_engine` runs a deterministic greedy pass
that walks the option pool in a fixed order and takes the first feasible choice.
Every plan that came from the fallback is flagged `is_fallback`, rendered with a
`FALLBACK` badge, and carries a `FALLBACK_GREEDY` reason code.

The greedy fallback does **not** consult the variant weights, so when it triggers
all three fallback plans are the same plan. That is reported, not smoothed over —
see `docs/LIMITATIONS.md`.

## Re-planning: frozen, locked, free

This is the part with the most subtle rules, so it is worth stating plainly.

After a disruption, `stability_guard.split_locked_and_free` partitions every
mission into exactly one of three sets:

- **frozen** — the parent assignment has `takeoff_minute <= now`, so the sortie
  has departed. It is carried across byte-identically and is never even offered to
  the solver. Re-planning is only safe at all because this set exists.
- **locked** — the parent assigned it and the assignment is still valid in the
  current state. It keeps that assignment.
- **free** — the event affected it, or the parent left it unassigned, or the event
  invalidated it without flagging it. These are the only decisions the solver
  makes.

The third clause is a bug this build found and fixed. A mission the parent did
**not** assign was being locked alongside the ones it did, which meant a re-plan
was structurally incapable of ever picking that mission up — and a mission that
was infeasible when the parent was built may be perfectly flyable now that the
clock has moved, a repair has finished, or a weather window has opened. Locking it
made the "revise" button incapable of improving on its parent. `test_replan_free_set.py`
guards the corrected rule.

## The option frontier and honest distinctness

`frontier_explorer.build_frontier` solves the same state three times, once per
variant, and returns the three plans plus a comparison.

The spec asks for "exactly three distinct options". The planner always returns
three. It does **not** always return three *different* ones, and that is not a
shortfall in the implementation — it is arithmetic. When the free set contains one
mission, there is one decision to make and all three objectives agree on it.
Free-set size is the strongest predictor: with one mission free, no case in the
measured set produced three options, and no weighting tried recovered one. Beyond
that it is only a tendency, because a free mission with a single feasible asset is
not a real decision however many missions are open.

Weights do move the number, by more than expected: an eleven-way sweep over the same
24 disrupted states found a legal alternative reaching 13/24 three-distinct against
the shipping 7/24, rescuing two cases with only 2 free missions. So the clean claim —
no weighting can help, because few decisions means one answer — is false at the low
end. That tuning was not adopted; the reasoning, and the fact that the sweep does not
measure plan quality, is in `DECISIONS.md` §9 and `LIMITATIONS.md` §2. The sweep is
reproduced on every benchmark run so the choice stays visible.

So the planner reports the truth instead:

```json
{
  "distinct_variants": 2,
  "duplicate_options": [["SAFETY FIRST", "STABILITY FIRST"]],
  "free_mission_count": 2,
  "distinct_options_note": "Only 2 distinct option(s) exist for this state; 2 mission(s) were free to be re-decided. Reported rather than perturbed into a cosmetic difference."
}
```

and the UI marks the matching cards. The alternative — nudging an objective until
the cards diverge — would produce a card labelled STABILITY FIRST that embodies an
objective nobody optimised, sitting next to a Commander who is about to weigh it.
A Commander comparing three identical plans and being told so is better informed
than one comparing two real plans and one fake.

## The ledger

Every state change appends one entry:

```
entry_hash = SHA-256(previous_hash + canonical_JSON(payload)
                     + occurred_at + action + actor_role)
```

`canonical_json` sorts keys and uses fixed separators, so the same logical payload
always produces the same bytes. `verify_chain()` walks the whole chain, re-hashing
each entry from its predecessor's stored hash, and reports the sequence number of
the first break.

What the chain does and does not cover is worth being precise about, and it is
pinned by a test in `test_ledger_integrity.py`:

- **Covered:** the payload, the timestamp, the action, and the acting role. Any
  change to any of those breaks verification at that entry and every one after it.
- **Not covered:** the human-readable `description` and the `entity_ref`. They are
  not inputs to the hash, because the spec's formula does not include them. An
  attacker with write access to the database could rewrite a description and the
  chain would still verify.

That is a property of the specified formula, not an oversight in the
implementation, and it is stated in the UI's ledger panel and in
`docs/LIMITATIONS.md` rather than left for someone to discover.

## Explanations

`explanation_service` renders sentences from stored reason codes. There is no
language model anywhere in this repository, and the module imports nothing that
could call one. The reason code carries the meaning — `CREW_REST_NOT_MET` is not
"the crew were tired", it is a stable token with a known mapping — and the prose
is a lookup plus formatting.

This is a deliberate constraint from the spec and it matters more than it looks.
An explanation is supposed to be the audit trail's readable face: if the words
came from a model, they could be fluent and wrong, and a Commander has no way to
tell. Boring, traceable, and verifiable beats eloquent.

## Advisory-only enforcement

Three independent mechanisms, because this is the safety claim and it should not
rest on one:

1. **Server side.** `POST /proposals/{id}/approve` and `/reject` require
   `ActorRole.COMMANDER`. Anything else gets 403 with a message naming who could
   have done it.
2. **No auto-activation path.** `POST /plans/generate` returns `DRAFT` plans.
   Only an approval produces a `PROPOSED` → `ACTIVE` transition. There is no
   code path from generate to active.
3. **Client side.** The role selector renders the capability matrix the server
   publishes from `GET /roles`. The UI mirrors the server's own matrix rather than
   re-implementing it, so it cannot advertise an action the API would refuse — and
   when a write is refused anyway, the 403 is shown verbatim instead of swallowed.

`test_role_enforcement.py` covers all 25 combinations of role × write endpoint.

## The what-if sandbox

`whatif_lab` clones the live state, applies the experiment to the clone, solves
against the clone, and returns the comparison. It never touches the live state.

The isolation is not asserted, it is *proved*: the handler digests the live state
before and after the run and returns both, along with an `isolation_verified`
boolean derived from comparing them. `test_whatif_isolation.py` checks the digest
is unchanged for every variant and every scripted event, and checks that the
sandbox result is never created as a proposal — there is nothing in the response
a Commander could approve.

## Data model: generated, not stored

The base world — hubs, assets, crew, missions, hazards, weather, stock — is
**generated deterministically from the seed on demand**. Only the mutable overlay
is persisted: asset condition changes, the clock, injected events, plans,
proposals, ledger entries.

This is why `POST /scenarios/reset` is exact rather than approximate. There is no
half-deleted prior world to reason about, and a reset with the same seed produces
the same scenario on any machine. It also keeps the database small enough to
delete and regenerate, which matters for a demo you are going to run repeatedly.

## API surface

35 REST routes under `/api/v1` and one WebSocket at `/ws/live`:

| Module | Covers |
| --- | --- |
| `routes_system` | health, roles |
| `routes_scenarios` | active scenario, role matrix, reset |
| `routes_state` | overview, map, crew, stock, readiness, reason codes, clock |
| `routes_missions` | missions, mission detail |
| `routes_assets` | assets, asset detail, condition update |
| `routes_plans` | plans, plan detail, generate, explanations, feasibility |
| `routes_events` | events, scripted list, inject |
| `routes_proposals` | proposals, approve, reject |
| `routes_whatif` | templates, run |
| `routes_ledger` | ledger, verify, replay |
| `websocket` | live event and alert feed |

`state_hub` is the single place that maps domain objects to payload shapes. A
field appearing in two payloads because two routes built it by hand is a field
that will eventually disagree with itself.

## The mandatory labels

`SYNTHETIC DEMONSTRATION DATA` and `ADVISORY ONLY — HUMAN APPROVAL REQUIRED` must
be visible on every screen. Two mechanisms keep them there:

- Most handlers set them explicitly, next to the data they qualify, so a handler
  that knows its payload can say something sharper than the default.
- `mandatory_label_middleware` in `main.py` fills in anything that came back
  without them. It uses `setdefault`, so it never overwrites a more specific value.

The middleware exists because relying on ~35 handlers each remembering is exactly
how a label goes missing from the one endpoint nobody re-reads. It also covers
403 refusals, which is when a reader most needs to be told what the system is.

`test_mandatory_labels.py` walks the published OpenAPI inventory rather than a
hand-written endpoint list, so an endpoint added later is covered automatically.

## Frontend

Six screens, no router library — `useState<ScreenId>` in `App.tsx`. A router
would add a dependency and a URL scheme to a prototype whose point is the backend.

One global `RefreshTick` counter instead of per-screen cache invalidation: any
mutation bumps it and every screen reloads. With six screens and a local SQLite
file that is simpler and less bug-prone than a cache layer, and it cannot serve a
stale plan after an approval.

`types/api.ts` mirrors every backend response field by field. An endpoint change
the UI has not adopted becomes a typecheck error rather than a `undefined` at
runtime. That file is the first thing to edit when an endpoint changes.

Dev uses a Vite proxy for `/api` and `/ws`, mirrored exactly by the nginx config
in the frontend container image, so the container behaves like the dev server.

## Docker

Two images and a compose file. The backend is a multi-stage build running as a
non-root uid with SQLite on a named volume and `/health` as the liveness probe.
The frontend builds with Node and serves the static output from nginx, which
proxies `/api` and `/ws` to the backend.

The container stack is authored and reviewed but **was never built or run** — no
container runtime was available on the build machine. Everything verified end to
end was verified by running the two services directly. Treat `docker compose up`
as untested; treat `make dev` as tested.

## Testing

179 tests, roughly one minute:

| File | Covers |
| --- | --- |
| `test_no_overlap` | 6 — a crew member cannot be in two places at once |
| `test_feasibility_rules` | 23 — the ten hard constraints |
| `test_frozen_immutability` | 13 — a departed sortie is never re-decided |
| `test_plan_auditor` | 9 — the auditor catches what the solver did not |
| `test_ledger_integrity` | 13 — the chain verifies; tampering breaks it |
| `test_role_enforcement` | 25 — only COMMANDER may approve or reject |
| `test_whatif_isolation` | 12 — the sandbox cannot mutate live state |
| `test_determinism` | 5 — same seed and clock, byte-identical output |
| `test_greedy_fallback` | 15 — the fallback is valid and labelled |
| `test_option_distinctness` | 23 — three options, and honest reporting when fewer are possible |
| `test_replan_free_set` | 12 — hold what the parent did, re-open what it did not |
| `test_mandatory_labels` | 23 — both labels everywhere, refusals included |

The first nine map to the spec's essential test list. The last three guard
specific behaviours this build had to work out, and each of them failed against
the code as it was before the corresponding fix.