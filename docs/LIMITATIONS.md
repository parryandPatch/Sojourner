# Limitations

What this prototype does not do, and what is known to be wrong with it. Every claim
here is either measured, pinned by a test, or explicitly marked as an untested
assumption.

Ordered roughly by how much it would matter if you relied on this for something
real.

---

## 1. The data is fictional, and the fiction is load-bearing

Every hub, asset, crew member, mission, hazard zone, weather window, coordinate and
call sign is generated from a seed. Nothing describes a real place, organisation,
vehicle, or operation. The IDs look operational (`AST-006`, `MIS-001`, `HUB-C`)
because that is how the demo reads well, but there is nothing behind them.

**Consequence:** any conclusion drawn from this system is about the synthetic
scenario and nothing else. The timings, the option structures, the risk scores and
the coverage figures all describe a 13-mission toy world. They say nothing about
whether the approach scales, and they were never intended to.

There is no import path, no adapter, and no connector to a real data source. The
`scenario_studio` module is the only source of entities and it is not designed to be
replaced.

---

## 2. "Exactly three distinct options" is not achievable in every state

The spec asks the planner to "return exactly three distinct options" and sets the
acceptance criterion "three trade-off plans are visibly different on the seeded
scenario."

The planner always returns three options. It does **not** always return three
*different* ones.

### What was measured

Over 24 re-plans (4 scripted events × 6 clock positions), the shipping weighting
produced three distinct options **7 times**. Grouped by how many missions the
re-plan was actually free to decide:

| free missions | re-plans | three distinct options |
| ---: | ---: | ---: |
| 1 | 1 | 0 |
| 2 | 3 | 0 |
| 3 | 7 | 0 |
| 4 | 1 | 1 |
| 5 | 5 | 3 |
| 6 | 4 | 2 |
| 7 | 2 | 1 |
| 8 | 1 | 0 |

Free-set size raises the odds. It does not settle them: the row for 8 free missions
scored 0, because a free mission with only one feasible asset is not a real decision
however many missions are open.

### The cause

The spec's own re-planning rule — reconsider only the missions the event
invalidated or materially affected — is what creates this. Re-plan EVENT_A at
minute 60 and the free set is two missions. Two decisions and three objectives very
often agree, and then only one plan exists.

The earlier version of this document claimed no weighting could change that. **That
claim was wrong**, and the correction is below.

### What was tried

**Weight sweep.** Eleven weightings, run over the same 24 disrupted states with the
same parent plans, changing only the weights. The count moves substantially:

- shipping weights: **7/24** three-distinct
- best alternative keeping the specified objective (`alt B`): **13/24**
- control, all three variants given identical weights: **0/24**, collapsing 24/24 to
  a single plan — which is what it must do, and confirms the metric reads
  distinctness rather than something correlated with it

A problem-by-problem comparison shows `alt B` rescues 8 re-plans that had collapsed,
**including two with only 2 free missions**. So the strong version — *with one or two
decisions no weighting can help* — is false at the low end. The shipping weighting
collapses more often than it strictly has to.

What survives is narrower and is what the planner's design actually rests on: the
collapse rate is strongly driven by how many decisions are open, and a free mission
with a single feasible asset is not a real decision at any weighting.

### Why the weights were not re-picked

Adopting `alt B` would not have been fabrication — the 8 extra options are real
plans, each auditor-valid and each honouring the coverage floor. It was not adopted
because the weights were chosen on their merits *before* this measurement existed,
and re-picking them to maximise a distinctness count discovered afterwards is the
scoreboard-chasing this project exists to avoid.

**This is a defensible position, not a proven one, and the sweep does not settle it.**
The sweep measures distinctness only. It does not measure coverage achieved, mean
risk, or cost, which are the reasons the weights were set as they were. A weighting
that scores higher on distinctness while delivering worse plans would be a worse
planner with a better-looking report. Settling the question properly means auditing
plan quality across the candidate weightings, which has not been done.

The full table is regenerated on every `make bench`, so a reviewer who prefers
`alt B` has both a reproducible measurement and a defensible case.

**Asymmetric free sets.** Giving STABILITY FIRST a different free set from the other
two variants was tried and **reverted** — it made distinctness worse, not better.

### What was built instead

The planner reports the truth:

```json
{
  "distinct_variants": 2,
  "duplicate_options": [["SAFETY FIRST", "STABILITY FIRST"]],
  "free_mission_count": 2,
  "distinct_options_note": "Only 2 distinct option(s) exist for this state; 2 mission(s) were free to be re-decided. Reported rather than perturbed into a cosmetic difference."
}
```

The planning screen marks matching cards `= SAFETY FIRST` and shows the free-mission
count. `test_option_distinctness.py` pins all of it, including that the identical
options still pass the independent auditor — so a Commander looking at two matching
cards is looking at two *valid* plans, which is the real situation.

### Why this is a deviation, and the judgement behind it

This is a deliberate deviation from a literal reading of the spec. The argument for
it: the spec's purpose in asking for three options is that a Commander has something
to weigh. A card labelled STABILITY FIRST that embodies an objective nobody optimised
is not something to weigh — it is a fabricated trade-off, and the label actively
misleads. Two cards that admit they match, with the reason stated, leave the
Commander better informed than three cards where one is a lie.

**This is a judgement call and it is arguable.** Someone reviewing the spec strictly
could reasonably say the system should return three options regardless and let the
Commander see they coincide. The counter is that the spec also says every displayed
plan must be auditor-valid and every explanation must come from stored reason codes
— it is broadly a "do not show the operator something false" specification, and a
mislabelled plan is false.

The sweep result weakens this argument somewhat and it should be read that way. If
three genuinely different options are available at a different weighting, then the
Commander *can* weigh something, and reporting a collapse where a third option was
reachable is under-delivering rather than being honest. The response is that the
weights were not re-picked after the fact — but that is a process argument, not a
correctness one, and a reviewer is entitled to weigh the alternative as a design
decision rather than accept it.

---

## 3. The greedy fallback ignores the variant weights

If CP-SAT returns no solution inside its 8-second budget, `planning_engine` runs a
deterministic greedy pass. That pass walks the option pool in a fixed order and takes
the first feasible choice — it does not consult the variant weights at all.

**Consequence:** when the fallback triggers, all three options are the same plan.
The benchmark measures this and it is consistently `variants=1`, with all three
fallback plans at identical weighted coverage.

This is reported rather than smoothed over: every affected plan is flagged
`is_fallback`, badged `FALLBACK` in the UI, and carries a `FALLBACK_GREEDY` reason
code. The API also returns `duplicate_options` and `distinct_options_note`, so the
same distinctness reporting covers this case.

It is a real weakness, not just a reporting gap. A weight-aware greedy fallback is
straightforward to build — order the pool by the variant's own objective rather than
a fixed key — and was not built. On the seeded scenario the fallback is not reached
in normal operation (measured solve times are ~850 ms for the initial frontier and
24–125 ms for a re-plan, against an 8000 ms budget), so the path is exercised by
tests and by the benchmark's stub rather than by normal use.

**Also note:** a genuine solver timeout could never be provoked for the benchmark.
`planning_engine` clamps the budget with `max(0.5, request.time_limit_seconds)`, so
asking for 0.0 s yields an ordinary 0.5 s solve. The benchmark stubs
`solve_with_cpsat` — the same injection the tests use — and the row is labelled
"simulated solver failure, not a real timeout".

---

## 4. The ledger hash covers less than the whole ledger entry

The hash formula is the one the spec specifies:

```
entry_hash = SHA-256(previous_hash + canonical_JSON(payload)
                     + occurred_at + action + actor_role)
```

Covered, so a change to any of these breaks verification at that entry and every
one after it:

- `payload` — the structured content
- `occurred_at` — the timestamp
- `action` — what happened
- `actor_role` — who did it

**Not covered**, because the specified formula does not include them:

- `description` — the human-readable sentence
- `entity_ref` — the ID of the thing acted on

**Consequence:** an attacker with write access to the SQLite file can rewrite any
`description` to say something else, and `verify_chain()` still returns VALID. The
chain proves the *structured record* was not altered. It does not prove the prose
was not.

`test_ledger_integrity.py` pins this scope explicitly, so the limitation cannot
quietly widen if someone later decides to hash the description. The same test
verifies that tampering *with a covered field* does break the chain — otherwise a
test asserting "the chain is tamper-evident" would pass for the wrong reason.

Related: the chain is not signed and the head hash is not published anywhere
external. It detects accidental corruption and naive post-hoc edits. It does not
detect a forger who recomputed the entire chain after changing it, which requires
only recomputing hashes in sequence. That is a property of a self-contained hash
chain, and this one has no external anchor.

---

## 5. The role selector is not authentication

Roles arrive in the `X-SOJOURNER-Role` header. Anyone can send any role. The
capability matrix is real and enforced server-side — only `COMMANDER` can approve
or reject, and `test_role_enforcement.py` covers all 25 role × endpoint
combinations — but the *identity* behind the role is not established, because there
is no identity provider, no session, no token, and no password.

**Consequence:** this is a demonstration of the authorisation model, not a security
boundary. It must not be exposed beyond localhost, and no deployment should treat it
as access control. The `GET /roles` response says this in its own `notice` field so
it is not something a reviewer has to discover in the source.

---

## 6. Single-machine, single-user, no concurrency

SQLite in WAL mode, one process, one user, one scenario at a time. There is no
transactional isolation between two concurrent planning requests, no request queue,
no rate limiting, and no lock around "generate → propose → approve". Two browser tabs
generating plans simultaneously will interleave.

The scenario is single-active by design: `POST /scenarios/reset` clears everything
and regenerates from the seed. There is one scenario, not a scenario library, so
there is no way to compare two runs side by side except by recording the outputs.

Verified only on macOS 26.7.1 / arm64 / Python 3.14.6. The Docker stack targets
Linux, which is unverified — see §8.

---

## 7. The planning problem is small by construction

13 missions, 22 assets, 36 crew, 3 hubs, 2 hazard zones, 2 weather windows, 2
stock lines per hub.

Several constants in `feasibility_engine` exist purely to keep the option pool
tractable at this size and are not principled limits:

| Constant | Value | Why it exists |
| --- | ---: | --- |
| `CREW_CANDIDATES_PER_ROLE` | 12 | Bounds crew combinations per role |
| `MAX_CREW_TEAMS` | 32 | Bounds crew team enumeration |
| `MAX_OPTIONS_PER_MISSION` | 48 | Bounds per-mission option enumeration |

**Consequence:** these cap the search space. At this scenario size the CP-SAT solve
completes in ~850 ms, so they are not currently binding in a way that hides better
solutions — but on a larger world they would silently truncate the candidate pool
and produce plans that are optimal *within the truncated pool*, not within the real
problem. There is no warning when a mission hits the cap. A real deployment would
need to either raise these or replace option enumeration with column generation.

Crew deadheading between hubs is modelled at a fixed `DEADHEAD_SPEED = 0.55` of
cruise, which is a synthetic assumption with no basis in anything.

---

## 8. The Docker stack was never executed

`docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile` and
`frontend/nginx.conf` are authored and reviewed but **were never built or run** — no
container runtime was available on the build machine.

Everything verified end to end was verified by running the two services directly on
the host: `make dev`, `make check`, `make demo`, and a browser walkthrough of all six
screens against a live backend with no console errors.

Treat `docker compose up` as untested. Treat `make dev` as tested. This is stated in
the README rather than left for someone to discover by running it and finding out.

---

## 9. Benchmarks exclude most of the system

`docs/benchmark-results.md` measures wall clock for whole three-option solves:
option enumeration, the solve, auditing, metric computation.

**Not measured:** HTTP latency, database throughput, concurrent users, frontend
render time, WebSocket fan-out, container startup, or the time for a human to read
the three options and decide. The end-to-end time a user perceives is longer than
the reported figures and was not measured.

The numbers are single-machine, single-process, synthetic, from one day. They are a
regression smoke test. Do not compare them across machines or read them as capacity
figures. The report states this in its own "What this does not tell you" section.

---

## 10. The risk score is synthetic and arithmetically simple

```
hazard_risk   = weighted fraction of the route through active hazard zones
weather_risk  = highest restriction severity affecting departure / return / objective
readiness_risk= 1 - (asset condition factor × crew rest factor)
combined      = 1 - (1 - hazard)(1 - weather)(1 - readiness)
```

The independence assumption in that last line is wrong in general — a route through
a hazard zone during bad weather is worse than the product of the two — and it is a
convenience that keeps the score explainable. Every component is stored and
displayed, which is what the spec asks for and what makes the score auditable, but
"transparent" and "accurate" are different claims and only the first is made.

---

## 11. Readiness bands are labelled estimates, not measurements

`readiness_estimator` produces bands (READY / DEGRADED / NOT READY) from synthetic
condition and rest figures. They are labelled as estimates in the UI and in the API
payload.

**Consequence:** they are a visualisation of two numbers the scenario already holds,
presented in a form that looks like a measurement. Nothing validates them against
anything, because there is nothing to validate against.

---

## 12. Explanations are templated, so they can read as filler

`explanation_service` renders sentences from stored reason codes. There is no
language model anywhere in this repository, and the module imports nothing that
could call one. That constraint is from the spec and it is the right one — an
explanation a Commander cannot trace is not an explanation, and a model can be
fluent and wrong with no way for the reader to tell.

The cost is that the prose is repetitive. Five plans can be rejected for the same
reason and produce five nearly identical sentences. That is a real usability
weakness. It is preferable to the alternative, but it is a weakness.

---

## 13. The frontend has no error boundaries, routing, or tests

- **No router.** `useState<ScreenId>` in `App.tsx`. Navigating away from a screen
  discards its state, and there are no URLs — a reviewer cannot link to a specific
  screen or share one.
- **No component or integration tests.** The frontend has `tsc --noEmit` and a
  production build, both clean, and it was walked manually in a browser. There are no
  automated UI tests, so a regression in rendering would not be caught by `make
  check`.
- **No error boundary.** An unhandled render error unmounts the whole tree to a blank
  page. Errors from the fetch wrapper are caught per-screen, but a rendering failure
  is not.
- **Global refresh.** One `RefreshTick` counter reloads every screen after any
  mutation. Simple and correct for six screens; it would not scale, and it means a
  read-only screen re-fetches when an unrelated write happens.

---

## 14. Single-solver, single-objective

Only CP-SAT, only the objective the spec describes. There is no second solver to
cross-check against, so there is no way to know whether the returned plan is optimal
or merely feasible-plus-good. The `solve_time_ms` field is CP-SAT's own accounting
and does not indicate optimality — a solve that hits its budget and returns the best
it found reports time, not proof.

`plan_auditor` validates constraints, not optimality. It confirms a plan is *legal*,
never that it is the *best* legal plan.