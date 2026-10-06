# Demo runbook

How to walk this prototype, and what to point at on each screen. Written for
someone reviewing it cold who has not seen the spec.

Total time if you follow this: about 10 minutes, most of it the initial solve.

---

## Before you start

You need Python 3.11+ and Node 20+. No network access is required at runtime.

```bash
cd sojourner
make install      # ~1 minute: creates backend/.venv, installs both dep sets
make dev          # backend on :8000, Vite on :5173
```

Open **http://localhost:5173**.

`make dev` runs both servers in the foreground. Leave it running and use a second
terminal for anything below.

To reset at any point: **F · Asset readiness → Reset scenario**, or
`curl -X POST localhost:8000/api/v1/scenarios/reset -H 'X-SOJOURNER-Role: COMMANDER' -d '{"seed":20260101}'`.

**Prefer Docker?** `cp .env.example .env && make up` → http://localhost:8080. But
see the warning at the bottom of this document: the container stack has never been
executed.

---

## The shortest useful demo (3 minutes)

If you only have three minutes:

1. **Screen B · Planning** → *Generate three options*. Watch the three cards.
2. Switch the role selector to **Observer**. Try to approve anything. It refuses.
3. Switch back to **Commander**. Approve. One plan goes Active, two go Superseded.
4. **Screen C · Disruption centre** → *Inject EVENT_A*. Read the affected missions
   and the three new proposals.
5. **Screen E · Decision trail**. The chain says VALID.

Everything below is the longer version.

---

## 1 · The command deck — what am I looking at?

Start here because it establishes what the system is.

**Point at these, in order:**

- The two labels at the top of the shell, above the navigation. `SYNTHETIC
  DEMONSTRATION DATA` and `ADVISORY ONLY — HUMAN APPROVAL REQUIRED`. They are in the
  shell, so they are on every screen, not just this one. That placement is
  deliberate — in each page individually, one of them would eventually be forgotten.
- The role selector, top right. Currently `Planner`. **Point out** that the enabled
  buttons are driven by the capability matrix the server publishes from
  `GET /roles`, not by logic duplicated in the frontend. The interface cannot offer
  an action the API would refuse.
- The KPI tiles: weighted coverage, mean risk, active plan, pending proposals.
- The schematic hub map. Three hubs and a distance grid, not a real map. There are
  no real coordinates in this system.
- The timeline along the bottom. Internal time is integer minutes from scenario
  start; the ISO timestamps are for display only.
- The live alert feed on the right. It updates over a WebSocket at `/ws/live`.

**Say:** the whole picture is synthetic and regenerable from a seed. Nothing here is
persistent except the overlay — the base world is generated on demand, which is why
reset is exact.

---

## 2 · Generate the three options

**Screen B · Planning** → *Generate three options*.

Wait about a second. Three cards appear.

**Point at these:**

- **The three metrics on each card differ visibly.** Missions covered (13 vs 10 vs 11),
  weighted coverage (695 vs 605 vs 665), mean risk. This is the spec's acceptance
  criterion, and it is the thing to check first.
- **The 85% floor line** on the SAFETY FIRST and STABILITY FIRST cards. Both must sit
  at or above 85% of COVERAGE FIRST's weighted coverage. This is a hard constraint in
  the solver, not a display filter — a plan below the floor is never proposed.
- **`AUDITOR-VALID` on each card.** Each plan is checked by an independent auditor
  that re-derives the ten hard constraints from the assignments, rather than trusting
  the solver's own claim. It agrees.
- **`distinct variants 3` and `free missions 13`** in the strip below the header. All
  13 missions were free to be decided, so all three objectives had something to
  disagree about. Remember these two numbers — they come back later.

**Then point out what did *not* happen:**

- No plan is active. Go back to the command deck: `Active plan` still reads "none".
- Nothing was recorded as an order. Generation produces options. It never produces
  a decision.

---

## 3 · Approval requires a human

This is the central safety property. Demonstrate it properly.

**Step 1 — refuse it.** Switch the role selector to **Observer**. Go to Screen C ·
Disruption centre and try to approve a proposal (you'll need to inject first — see
§4 — or come back after §4). Watch it return 403 with the reason shown verbatim:

> Role PLANNER may not approve or reject a plan. Only COMMANDER can activate a
> synthetic plan version.

The message is not swallowed into a generic error toast. It tells you who could have
done it.

**Step 2 — prove it at the API layer,** so it is not just a UI behaviour. In a second
terminal:

```bash
curl -s -X POST localhost:8000/api/v1/proposals/PRP-2-01/approve \
  -H 'Content-Type: application/json' \
  -H 'X-SOJOURNER-Role: PLANNER' -d '{}' -w '\nHTTP %{http_code}\n'
```

`HTTP 403`. There is no request you can construct that activates a plan without a
Commander role, because `POST /plans/generate` returns `DRAFT` plans and only an
approval performs the `PROPOSED → ACTIVE` transition.

**Step 3 — do it.** Switch back to **Commander** and approve.

**Point at:** exactly one plan is now `Active`. The other two went to `Superseded` —
not deleted. The proposals for them show `SUPERSEDED` too. Everything is still in
the ledger and still in the replay.

---

## 4 · Disrupt something

**Screen C · Disruption centre** → pick `EVENT_A` → *Inject*.

Four scripted events are available (`EVENT_A` through `EVENT_D`): an asset fault with
a synthetic repair window, a crew rest shortfall, a hub closure, an adverse weather
window.

**Point at these:**

- **The message.** EVENT_A fails an asset for 180 minutes and names the affected
  missions explicitly: `MIS-001, MIS-010`. The system does not re-plan everything; it
  identifies what the event invalidated.
- **`frozen and held`.** Any sortie with `takeoff <= now` has departed and is carried
  across byte-identically. It is never even offered to the solver. This is what makes
  re-planning safe while aircraft are in the air.
- **`free missions 4`** — and understand this number. The event flagged 2 missions; 2
  more were left unassigned by the parent and are also available, because a mission
  the parent skipped has nothing to keep. Locking those would have made the revision
  button incapable of improving on its parent. That was a real bug in this build;
  `test_replan_free_set.py` guards the corrected rule.
- **Three proposals, all `PENDING`.** Nothing is active. Approving is still a separate,
  human act.

---

## 5 · Advance the clock and watch frozen sorties hold

**Screen C** → advance the clock to minute 180.

Then **Screen B · Planning** → *Revise active plan*.

**Point at these:**

- **`frozen_assignments`** is now non-zero. Four sorties had already departed at
  minute 180. They appear in the new plan unchanged — same asset, same crew, same
  times.
- **`changed_assignments` versus the parent.** The re-plan knows exactly what it moved
  and what it left alone, and the count is on the card.
- **`AUDITOR-VALID`** again, on the revision.

The clock defaults in `make demo` to 180 rather than 60 specifically so this step
demonstrates something. At minute 60 on the seeded scenario nothing has departed yet
and the freeze count is legitimately zero — which is true but uninformative.

---

## 6 · The what-if sandbox cannot touch live state

**Screen D · What-if lab** → pick a variant and a scripted event → *Run experiment*.

**Point at the isolation panel**, which is the point of the whole screen:

- **`ISOLATION VERIFIED`** and the live state digest before and after, byte-identical.
- That is not a claim, it is a proof: the handler digests the live state before and
  after the run and derives the boolean from comparing them. The sandbox solves
  against a clone.
- **The result is not a proposal.** There is nothing in the response a Commander could
  approve. An experiment is not a decision, and the data model does not let it become
  one.
- The coverage and risk deltas versus the active plan — what the event *would* have
  cost, without anyone having committed to anything.

---

## 7 · The audit trail

**Screen E · Decision trail**.

**Point at these:**

- **The chain status: `VALID`.** `verify_chain()` walks every entry, re-hashing each
  from its predecessor's stored hash.
- **The columns in the table.** Sequence, actor role, action, description. Every row
  is one state change with a SHA-256 hash linking it to the one before.
- **Replay.** Select the active plan to see the decision path reconstructed — which
  entries led to this plan and which are context.

**Then say the honest part,** because a reviewer will find it anyway:

The hash covers `previous_hash + canonical_JSON(payload) + occurred_at + action +
actor_role`. It does **not** cover the human-readable `description` or `entity_ref`,
because the specified formula does not include them. So someone with write access to
the database could rewrite a description and the chain would still verify. This is a
property of the specified formula, not an oversight, and it is pinned by a test so it
cannot quietly widen. Full detail in `docs/LIMITATIONS.md` §4.

---

## 8 · Readiness

**Screen F · Asset readiness**.

Per-asset condition, crew rest, and a readiness band. **Point out** that the bands are
labelled estimates. They are a visualisation of two synthetic numbers, not a
measurement, and nothing validates them because there is nothing to validate against.

This screen also has the **Reset scenario** button.

---

## The command-line version

`make demo` (or `./scripts/demo_walkthrough.sh`) drives the whole path with `curl` and
**asserts** rather than narrates:

- PLANNER, OBSERVER and MAINTAINER approval all must return 403
- no plan may be ACTIVE before an approval
- exactly one plan may be ACTIVE after
- a what-if must leave the live state digest unchanged
- the hash chain must verify

It exits non-zero on the first failure. Requirements: `curl`; `jq` optional.

```bash
make demo
EVENT=EVENT_C CLOCK_MINUTE=300 make demo    # a different disruption, later in the day
```

`CLOCK_MINUTE=0` skips the frozen-sortie demonstration (nothing has departed yet).

---

## If something looks wrong

**The three cards are not all different.** Expected in some states after a disruption,
and it is reported rather than hidden. Look at `free missions` in the strip below the
header: if it is 1 or 2, there are only one or two real decisions and all three
objectives agree, so one plan exists. The cards are badged `= SAFETY FIRST` and the
`distinct options note` explains it. Full explanation in `docs/LIMITATIONS.md` §2.

**A card says `FALLBACK`.** The solver hit its 8-second budget and the deterministic
greedy pass ran instead. Note that the greedy pass ignores the variant weights, so all
three fallback plans are the same plan — also reported. On the seeded scenario this
should not happen: measured solve times are ~850 ms initial and 24–125 ms for a
re-plan, against an 8000 ms budget.

**`distinct options note` says options collided.** Same as the first case. The UI is
telling you the truth about a state that does not have three answers.

**Nothing activates when I approve.** Check the role selector is `Commander`. If it
is not, the request was refused with a 403 and the message says so — check the error
text in the Disruption centre, it is displayed verbatim rather than swallowed.

**Docker does not start.** The container stack has never been executed — no container
runtime was available on the build machine. Use `make dev`, which is what was
verified. See `docs/LIMITATIONS.md` §8.

---

## The questions worth asking back

If you are reviewing this, these are the places where a decision was made and the
alternative is defensible. Each is written up in `docs/DECISIONS.md`.

1. **Why does it sometimes return fewer than three distinct options?** This is the
   spec deviation, and the reasoning is in `LIMITATIONS.md` §2. It is the one place
   the implementation knowingly does not do what the spec literally says.
2. **Is the risk score meaningful?** It is arithmetically simple and makes an
   independence assumption that is wrong in general. It is transparent, which is what
   the spec asked for, and that is not the same as accurate.
3. **Does the ledger actually protect anything?** Against accidental corruption, yes.
   Against a determined forger with database write access, no. And it does not cover
   two of its own fields.
4. **Where are the UI tests?** There are none. `make check` covers the backend
   thoroughly and the frontend only with `tsc --noEmit` and a build.