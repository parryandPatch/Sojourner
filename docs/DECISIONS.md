# Decisions

Every significant choice, why it went the way it did, and — where it matters — what
the alternative was and why it lost. Several of these were wrong first and are
recorded that way, because the reasoning is more useful than the conclusion.

---

## 1. Modular monolith, one process

**Chosen:** a single FastAPI process, one SQLite file, no broker, no cache.

**Why:** the spec asks for a modular monolith explicitly, and for a prototype whose
whole claim is "a laptop runs this", a second process is a second thing to install,
start, and debug. Boundaries that cost nothing are boundaries people actually keep.

**Cost:** nothing is concurrent and nothing is horizontally scalable. Both are
correct for the scope and both would need revisiting immediately in production.

The layer rule that makes the spec's "modules can be extracted later" real rather
than aspirational: **no service imports another service's internals**. They call each
other's public functions. `whatif_lab` needs to apply an event, so it calls
`disruption_monitor.apply_event` rather than reaching into a helper. Extracting a
service later means replacing imports, not untangling them.

---

## 2. Base scenario generated, not stored

**Chosen:** hubs, assets, crew, missions, hazards, weather and stock are generated
deterministically from a seed on demand. Only the mutable overlay — asset condition,
the clock, events, plans, proposals, ledger — is persisted.

**Why:** `POST /scenarios/reset` becomes exact rather than approximate. There is no
half-deleted prior world to reason about, and the same seed produces the same scenario
on any machine. The database stays small enough to delete and regenerate, which
matters when the demo is going to be run repeatedly.

**Cost:** generating the world is part of every request that loads state. At this
size it is negligible (3 hubs, 22 assets, 13 missions) and it is cached in process.
On a larger world this would need to be persisted and invalidated properly.

---

## 3. Integer minutes for internal time

**Chosen:** every scheduling decision works in integer minutes from
`scenario_start`. UTC ISO timestamps exist only for display and for ledger
`occurred_at`.

**Why:** no float arithmetic on times, no timezone handling in the planning path, no
`datetime.now()` anywhere in it. The consequence is that a scenario is reproducible
by `(seed, minute)` — which is what makes `test_determinism.py` and the replay view
meaningful rather than decorative.

**Cost:** a scenario can only be observed at whole-minute resolution. For a prototype
with a 720-minute horizon that is entirely adequate; for anything shorter it would
not be.

---

## 4. Integer-scaled CP-SAT objective

**Chosen:** `SCALE = 1000`. Priority weights P1=100, P2=60, P3=35, P4=20, P5=10.
Coverage, risk, cost and change all scaled to integers before they enter the model.

**Why:** the spec requires integer scaling, and for a good reason — floating-point
objectives make solver results harder to reason about and reproduce. With integers,
an objective value is a number you can inspect and compare.

**A bug worth recording.** The 85% floor constraint sums `priority_weight * SCALE`,
so the `required` bound must also be multiplied by `SCALE`. Omitting it produced a
floor that looked correct in the code and was three orders of magnitude too loose in
the solver. It passed visual review because the expression *read* right. The
`test_option_distinctness.py` and feasibility tests catch it now.

---

## 5. The three variants differ only in weights, not in code

**Chosen:** one objective function, three `(risk, cost, change)` weight triples:

| Variant | risk | cost | change |
| --- | ---: | ---: | ---: |
| COVERAGE FIRST | 6 | 3 | 8 |
| SAFETY FIRST | 900 | 0 | 0 |
| STABILITY FIRST | 6 | 1 | 40 |

**Why:** three code paths would drift. Three weight triples cannot — if one variant's
objective is wrong, it is wrong in a way you can read in one line.

SAFETY FIRST is allowed to give up coverage to buy a large risk reduction, bounded by
the 85% floor. STABILITY FIRST buys a small risk reduction in exchange for heavily
weighting churn against the parent, so it converges towards what was already approved.

**The weights are a choice, not a derivation.** The spec constrains the objective and
leaves the numbers open. They were chosen for reasonable relative magnitudes, and the
distinctness sweep (§9) exists partly to document what a different choice would do.
Choosing them by which produced the best demo would have been fabrication with extra
steps — and the sweep result (§9) shows that discipline cost something, since a
different legal weighting gives three distinct options more often.

---

## 6. 85% floor as a hard constraint, not a filter

**Chosen:** SAFETY FIRST and STABILITY FIRST are constrained to meet
`weighted_coverage(coverage_first) * 0.85` inside CP-SAT. A plan below the floor is
infeasible and is never proposed. The reference coverage comes from the actual
coverage-first solve, passed as `coverage_reference`.

**Why, both halves:** as a filter it would mean solving, discarding, and re-solving
when a variant came back below the floor. As a constraint it costs nothing and cannot
be forgotten.

The second half matters more. The floor is computed from the coverage-first run's
*actual* result, not predicted. Deriving it from a plan that has not been solved yet
would be a guess, and a floor derived from a guess is not a floor.

---

## 7. Determinism enforced, not hoped for

**Chosen:** `num_search_workers = 1`, `random_seed = 0`, and a `-index` coefficient
tie-break on otherwise-equal options.

**Why:** CP-SAT is free to break ties arbitrarily and will do it differently between
runs, which would make "the same seed gives the same plan" false in practice. Three
changes make it true. Without them the determinism tests would pass most of the time
and fail in front of a reviewer, which is worse than not having them.

---

## 8. Crew no-overlap modelled per individual, not per team

**Chosen:** `add_no_overlap` per crew member, not per team.

**Why:** a team of three that is free as a unit is still a problem if one member of it
is on another sortie. A per-team constraint waves that straight through, and the
resulting plan is one the auditor would reject — so it fails loudly rather than
silently, which is the good version of this bug.

**A related trap:** OR-Tools 9.15's `CpModel` exposes only snake_case method names
(`add_at_most_one`, `add_no_overlap`). The CamelCase wrappers exist and return
`None` silently — they build *no constraint at all* and raise nothing. An early
version of the model called them and appeared to work, because an unconstrained model
also produces plans. Worth knowing before touching this file.

---

## 9. Fewer than three distinct options is reported, not manufactured

**Chosen:** when the solver produces identical assignments for two or more variants,
the API returns `duplicate_options`, `free_mission_count` and a
`distinct_options_note`, and the UI badges the matching cards.

**Why:** a re-plan re-opens only the missions the event affected. When that leaves
one or two decisions, all three objectives genuinely agree, and one plan exists.

Measured: 7 of 24 re-plans produced three distinct options with the shipping weights.
The initial solve, where all 13 missions are free, produces three distinct options
every time — so the spec's stated acceptance criterion ("on the seeded scenario") is
met.

**Alternatives tried and rejected:**

- *Weight tuning* — swept 11 weightings over the same 24 disrupted states. The best
  alternative keeping the specified objective reached 13/24, and rescued 8 collapsed
  re-plans including two with only 2 free missions. **This is the uncomfortable one.**
  Not taken, because the weights were chosen before the measurement existed and
  re-picking them to maximise a count found afterwards is scoreboard-chasing — but the
  sweep measures distinctness only, not coverage, risk or cost, so it does not actually
  establish that the shipping weights are better. `LIMITATIONS.md` §2 states this as
  defensible rather than proven.
- *Asymmetric free sets* — giving STABILITY FIRST a different free set from the other
  two. Tried, made distinctness **worse**, reverted.
- *Perturbing plans until they differ* — never seriously considered, because the
  result is a card labelled STABILITY FIRST embodying an objective nobody optimised.

**The judgement:** the spec is broadly a "do not show the operator something false"
specification — every displayed plan must be auditor-valid, every explanation must
come from stored reason codes. A mislabelled plan is false in exactly the way those
rules exist to prevent. A Commander comparing two cards that admit they match is
better informed than one comparing two real plans and one fake.

**This is arguable and is flagged as a deviation.** See `LIMITATIONS.md` §2.

---

## 10. Re-planning: frozen, locked, free

**Chosen:** `split_locked_and_free` partitions every mission into exactly one of
three sets — frozen (`takeoff <= now`, carried across byte-identically and never
offered to the solver), locked (parent assignment still valid, kept), free (affected
by the event, or unassigned by the parent, or invalidated without being flagged).

**Why the frozen set matters:** it is the only reason re-planning is safe while
sorties are in the air. A departed sortie is a fact about the world, not a preference.

**The bug this found.** A mission the parent did **not** assign was being locked
alongside the ones it did. That meant a re-plan was *structurally incapable* of ever
picking that mission up — and a mission that was infeasible when the parent was built
may be perfectly flyable now that the clock has moved, a repair has finished, or a
weather window has opened. Locking it made the revise button incapable of improving
on its parent, which is the exact case a revision exists for.

Confirmed on the seeded scenario: a revision at minute 60 went from 11/13 missions
and 665 weighted coverage to 12/13 and 675.

The fix is three lines and a comment explaining why the branch exists. The test
(`test_replan_free_set.py`) asserts the rule and its consequence, and both were
confirmed to **fail against the pre-fix code** — which is how you know a test is
guarding something real.

---

## 11. Fault modelling: airborne assets finish their sortie

**Chosen:** an asset that is airborne when the fault hits finishes the frozen sortie
it is on, then goes to repair:
`released = max(available_from, minute, airborne_return) + repair_duration`.

**Why:** the alternative — pulling the asset immediately — contradicts the frozen
rule. An asset mid-flight cannot teleport into a repair hangar, and treating it as
grounded would mean the model believed something false about the world.

---

## 12. Assets based at the mission's hub, crew may deadhead

**Chosen:** an asset must be based at the mission's origin hub. Crew may deadhead
between hubs at `DEADHEAD_SPEED = 0.55` of cruise.

**Why:** repositioning an aircraft is a flight operation with its own cost, risk and
scheduling consequences. Modelling it as free would make the planner indifferent to
fleet geometry, which is one of the things that actually makes air operations hard.
Crew deadheading is realistic and keeps the constraint on the scarce, movable resource.

`DEADHEAD_SPEED = 0.55` is a synthetic assumption with no basis in anything. It is a
plausible-looking number, which is not the same as a justified one.

---

## 13. Independent auditor, not a solver self-report

**Chosen:** `plan_auditor` re-derives the ten hard constraints from the finished
assignment set, sharing no code with the feasibility path that produced the
assignments.

**Why:** the same check that produced the answer cannot meaningfully check it. This
is the only thing that makes `AUDITOR-VALID` on a card mean anything.

**Cost:** it validates legality, never optimality. It confirms a plan is a *legal*
plan, not that it is the *best* legal plan. That distinction is stated wherever
`auditor_valid` appears.

`test_plan_auditor.py` includes a case where the auditor must **reject** a plan the
solver produced, which is what stops it from being decoration.

---

## 14. Ledger hash exactly as specified, and the gap stated

**Chosen:** `SHA-256(previous_hash + canonical_JSON(payload) + occurred_at + action +
actor_role)`, with `canonical_json` sorting keys and using fixed separators.

**Why:** the formula is the spec's. Deviating to cover more fields would be more
secure but would not be what was asked for, and silently widening a hash chain's
scope is how someone later comes to believe in a guarantee it does not give.

**The gap, stated rather than hidden:** `description` and `entity_ref` are outside the
hash. Someone with database write access can rewrite a description and
`verify_chain()` still returns VALID. `test_ledger_integrity.py` pins the exact scope
— including asserting that tampering with a *covered* field does break the chain, so
the tamper-evidence test cannot pass for the wrong reason.

The UI's ledger panel says this, and so does `LIMITATIONS.md` §4.

---

## 15. Explanations from reason codes, no model anywhere

**Chosen:** `explanation_service` renders sentences from stored `ReasonCode` values.
No language model exists in this repository and the module imports nothing that could
call one.

**Why:** the spec requires it, and it is right to. An explanation is the audit
trail's readable face. If the words came from a model they could be fluent and wrong,
and a Commander has no way to tell. Boring, traceable and verifiable beats eloquent.

**Cost, stated plainly:** the prose is repetitive. Five plans rejected for the same
reason produce five nearly identical sentences. That is a real usability weakness and
it is preferable to the alternative.

---

## 16. What-if sandbox proves isolation rather than asserting it

**Chosen:** clone the state, solve against the clone, then digest the live state
before and after and return both plus `isolation_verified` derived from comparing
them.

**Why:** "does not mutate live state" is a claim every sandbox makes. Returning the
digest turns it into something the reviewer can check.

Also: the sandbox result is **never** created as a proposal. There is nothing in the
response a Commander could approve — an experiment is not a decision, and the data
model does not let it become one. `test_whatif_isolation.py` checks both.

---

## 17. Three independent mechanisms for advisory-only

**Chosen, deliberately redundant:**

1. **Server side** — approval endpoints require `COMMANDER`; anything else gets 403
   with a message naming who could have done it.
2. **No auto-activation path** — generate returns `DRAFT`. Only an approval performs
   `PROPOSED → ACTIVE`. There is no code path from generate to active.
3. **Client side** — the UI renders the capability matrix the server publishes, so it
   cannot advertise an action the API would refuse. When a write is refused anyway,
   the 403 is shown verbatim.

**Why three:** this is the central safety claim and it should not rest on one
mechanism. A single check is a single point of failure, and the failure mode is
silent — a plan becomes active with nobody having decided anything.

The frontend mirroring the server's matrix (rather than re-implementing it) is the
part that is easy to get backwards: duplicated role logic drifts, and once it drifts
the UI will offer a button the API refuses, which trains the reviewer to click
through warnings.

---

## 18. Mandatory labels: handlers plus middleware

**Chosen:** handlers set `SYNTHETIC DEMONSTRATION DATA` and `ADVISORY ONLY — HUMAN
APPROVAL REQUIRED` explicitly where they can add something sharper;
`mandatory_label_middleware` fills in anything missing with `setdefault`, never
overwriting.

**Why the middleware:** relying on ~35 handlers each remembering is exactly how a
label goes missing from the one endpoint nobody re-reads. It also covers 403
refusals, which is when a reader most needs to be told what the system is.

`setdefault` rather than assignment matters: a handler that wrote something more
specific is believed over the default. `test_mandatory_labels.py` asserts both the
presence and the deference.

The UI puts the labels in the shell, above the navigation, so they are on every
screen by construction rather than per page.

---

## 19. Hand-rolled `StrEnum`

**Chosen:** a local `class StrEnum(str, Enum)` with `# noqa: UP042` and a comment
explaining why.

**Why:** `enum.StrEnum` exists in Python 3.11+ and Ruff's UP042 correctly suggests
it. The local version is kept because the project targets 3.11+ *and* the explicit
base makes the string behaviour unambiguous to a reader who has not memorised when
`StrEnum` changed. Suppressing the lint with a reason is better than either silently
ignoring it or churning the codebase.

**A trap in the same area:** OR-Tools enum members are spaced strings —
`PlanVariant.SAFETY_FIRST` is `"SAFETY FIRST"`, not `"SAFETY_FIRST"`. So
`PlanVariant("SAFETY_FIRST")` raises `ValueError`. This cost real time.

---

## 20. Frontend: no router, global refresh, typed API mirror

**Chosen:**

- **No router library.** `useState<ScreenId>` in `App.tsx`.
- **One global `RefreshTick` counter** instead of per-screen cache invalidation.
- **`types/api.ts` mirrors every backend response field by field.**

**Why:** a router adds a dependency and a URL scheme to a prototype whose point is
the backend. Six screens and one piece of state is the whole navigation model needed.

The global refresh is deliberately coarse: with six screens and a local SQLite file
it is simpler and less bug-prone than a cache layer, and it cannot serve a stale plan
after an approval. It would not scale, and `LIMITATIONS.md` §13 says so.

The typed mirror is the highest-value piece. An endpoint change the UI has not
adopted becomes a `tsc` error rather than `undefined` at runtime. That file is the
first thing to edit when an endpoint changes — `test_mandatory_labels.py` walks the
OpenAPI inventory for the same reason on the backend side.

---

## 21. Dependency choices made against advisories

**Chosen:** Tailwind 4.1 via `@tailwindcss/vite`, Vite 8.3, React 19, TypeScript 5.9.

**Why:** the scaffold's default line was Tailwind 3.x with Vite ≤6, which carries
published advisories. The chosen set audits clean. Upgrading the toolchain to avoid
known vulnerabilities is a reasonable default for anything a reviewer will run.

`npm run typecheck` and `npm run build` are both clean; output is 293 kB JS / 27 kB
CSS, gzipped to 87 kB / 5.8 kB. No bundle splitting, because at this size splitting
would add request overhead for no benefit.

---

## 22. Docker authored, never executed

**Chosen:** write the full container stack — multi-stage backend build on a non-root
uid, SQLite on a named volume, `/health` probe, nginx frontend image with the dev
proxy mirrored — and state plainly that it was never built.

**Why state it rather than quietly omit:** no container runtime was available. The
alternative was to ship `docker-compose.yml` and let a reviewer discover on their
machine that the image does not build. Everything verified was verified by running
the two services directly. `README.md`, `DEMO_RUNBOOK.md` and `LIMITATIONS.md` §8 all
say which path is tested and which is not.

The nginx config mirrors the Vite dev proxy exactly, so the container behaves like the
dev server. That part is a real correctness concern — if they diverged, the container
would work while the dev server did not, or worse.

---

## 23. Benchmarks that refuse to fabricate

**Chosen:** the benchmark stubs `solve_with_cpsat` rather than pretending to trigger a
timeout, and labels the row "simulated solver failure, not a real timeout".

**Why:** a real timeout cannot be provoked — `planning_engine` clamps the budget with
`max(0.5, ...)`, so requesting 0.0 s yields an ordinary 0.5 s solve. Reporting that as
a timeout measurement would be a fabricated number in a document whose entire purpose
is not containing fabricated numbers.

The fallback row asserts that every plan came back labelled `FALLBACK` and
auditor-valid rather than just printing a duration. And the report's `variants=1` for
that row is left visible and explained, because a measurement that reveals a weakness
is more useful than one that hides it.

The distinctness sweep was added for the same reason: it makes a claim I had made
assertively falsifiable, and when it falsified it, the report says so in its own
words. The shipping weights were kept anyway, and §9 explains why that is not a
contradiction.

---

## 24. Roles as a header

**Chosen:** `X-SOJOURNER-Role`, defaulting to `OBSERVER`.

**Why:** the spec explicitly says "implement simple local demo authentication or a
role selector. No external identity provider is required." A header is the smallest
thing that demonstrates the capability model end to end.

**Stated as a limitation, not a feature:** this is not authentication. Anyone can send
any role. `GET /roles` says so in its own `notice` field, so a reviewer learns it
without reading the source. The *authorisation* model is real and enforced — the
identity behind it is not. `LIMITATIONS.md` §5.