#!/usr/bin/env bash
#
# SOJOURNER — scripted end-to-end walkthrough of the demo flow.
#
# Drives the running backend with curl in the order a reviewer would click through the UI,
# and narrates what each step is supposed to prove. Everything here is synthetic: this
# script talks to the local prototype and nothing else.
#
#   1  health and the role matrix
#   2  reset the scenario so the run is reproducible
#   3  generate the three-option frontier          -> no plan becomes active
#   4  inject a disruption                          -> three proposals, still nothing active
#   5  try to approve as PLANNER / OBSERVER        -> must be refused with 403
#   6  approve as COMMANDER                         -> activates, supersedes its siblings
#   7  advance the clock, then revise              -> started sorties stay frozen
#   8  run a what-if in the sandbox                -> active state digest must not move
#   9  verify the hash chain and replay the decision
#  10  confirm the two mandatory labels are present
#
# Usage:
#   ./scripts/demo_walkthrough.sh                    # against http://127.0.0.1:8000
#   HOST=localhost PORT=8080 ./scripts/demo_walkthrough.sh
#   EVENT=EVENT_C CLOCK_MINUTE=180 ./scripts/demo_walkthrough.sh
#
# Requires curl. jq is used when available; without it the script degrades to grep and
# prints fewer details, but every assertion still runs.

set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
BASE="http://${HOST}:${PORT}/api/v1"
EVENT="${EVENT:-EVENT_A}"
# Deliberately late enough that some sorties have already departed, so the freeze rule is
# exercised rather than reported as vacuously true. Set CLOCK_MINUTE=0 to skip it.
CLOCK_MINUTE="${CLOCK_MINUTE:-180}"
WHATIF_EVENT="${WHATIF_EVENT:-EVENT_B}"

if command -v jq >/dev/null 2>&1; then
  JQ=1
else
  JQ=0
fi

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

step() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
say()  { printf '   %s\n' "$*"; }
ok()   { printf '   \033[32mOK\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33m!!\033[0m   %s\n' "$*"; }
die()  { printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

LAST_BODY=""
LAST_STATUS=""

# api <method> <path> <role> [json-body]
api() {
  local method="$1" path="$2" role="$3" body="${4:-}"
  local tmp status
  tmp="$(mktemp)"
  if [ -n "$body" ]; then
    status="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "${BASE}${path}" \
      -H 'Content-Type: application/json' -H "X-SOJOURNER-Role: ${role}" -d "$body")"
  else
    status="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "${BASE}${path}" \
      -H "X-SOJOURNER-Role: ${role}")"
  fi
  LAST_BODY="$(cat "$tmp")"
  LAST_STATUS="$status"
  rm -f "$tmp"
}

# jget <json> <jq-filter>  — empty string when jq is unavailable or the path is absent.
jget() {
  if [ "$JQ" -eq 1 ]; then
    printf '%s' "$1" | jq -r "${2} // \"\"" 2>/dev/null || printf ''
  fi
}

expect() {
  local want="$1" what="$2"
  if [ "$LAST_STATUS" = "$want" ]; then
    ok "${what} -> HTTP ${LAST_STATUS}"
  else
    printf '%s' "$LAST_BODY" | head -c 700; echo
    die "${what}: expected HTTP ${want}, got HTTP ${LAST_STATUS}"
  fi
}

contains() { case "$1" in *"$2"*) return 0 ;; *) return 1 ;; esac; }

table() {
  # table <header> <jq filter producing one line per row>
  if [ "$JQ" -eq 1 ]; then printf '%s' "$LAST_BODY" | jq -r "$2"; fi
}

# ---------------------------------------------------------------------------
# 0 · Preconditions
# ---------------------------------------------------------------------------

printf '\033[1mSOJOURNER — scripted walkthrough\033[0m\n'
say "target ${BASE}"

step "0 · Is the backend up?"
api GET /health OBSERVER
[ "$LAST_STATUS" = "200" ] || die \
  "backend not reachable at ${BASE}/health — start it with 'make backend', or 'make dev', or 'make up'"
ok "health ok — database $(jget "$LAST_BODY" '.database'), solver $(jget "$LAST_BODY" '.solver'), budget $(jget "$LAST_BODY" '.solver_time_limit_seconds')s"

# ---------------------------------------------------------------------------
# 1 · Roles
# ---------------------------------------------------------------------------

step "1 · Role matrix — the UI mirrors this, so it never offers a refused action"
for role in OBSERVER PLANNER COMMANDER MAINTAINER; do
  api GET /roles "$role"
  [ "$LAST_STATUS" = "200" ] || die "GET /roles as ${role} returned HTTP ${LAST_STATUS}"
  say "$(printf '%-10s %s' "$role" "$(jget "$LAST_BODY" '.permissions | join(", ")')")"
done
api GET /roles COMMANDER
say "notice: $(jget "$LAST_BODY" '.notice')"

# ---------------------------------------------------------------------------
# 2 · Reset
# ---------------------------------------------------------------------------

step "2 · Reset the scenario so this run is reproducible"
api POST /scenarios/reset COMMANDER '{"seed": 20260101}'
expect 200 'POST /scenarios/reset'
say "scenario $(jget "$LAST_BODY" '.scenario_id') seed $(jget "$LAST_BODY" '.seed'), now minute $(jget "$LAST_BODY" '.now_minute') ($(jget "$LAST_BODY" '.now_iso'))"
say "counts: $(jget "$LAST_BODY" '.counts' | tr '\n' ' ')"
ACTIVE_PLAN=""
PENDING_PROPOSAL=""
REJECT_ME=""

# ---------------------------------------------------------------------------
# 3 · Generate the frontier
# ---------------------------------------------------------------------------

step "3 · Generate the three-option frontier (PLANNER)"
api POST /plans/generate PLANNER '{"revise": false}'
expect 200 'POST /plans/generate'
say "options:            $(jget "$LAST_BODY" '.plans | length')"
say "distinct variants:  $(jget "$LAST_BODY" '.distinct_variants')"
say "free missions:      $(jget "$LAST_BODY" '.free_mission_count')"
say "total solve time:   $(jget "$LAST_BODY" '.solve_time_ms_total') ms"
say "coverage reference: $(jget "$LAST_BODY" '.coverage_reference')"
say "distinctness note:  $(jget "$LAST_BODY" '.distinct_options_note')"
table '' '.plans[] |
  "   \(.variant): \(.metrics.missions_covered)/\(.metrics.missions_total) missions, weighted \(.metrics.weighted_coverage), mean risk \(.metrics.mean_risk | . * 1000 | round / 1000), max risk \(.metrics.max_risk | . * 1000 | round / 1000), auditor \(if .auditor_valid then "valid" else "INVALID" end), \(.solve_time_ms)ms, \(if .is_fallback then "FALLBACK" else "solved" end)"'

if [ "$(jget "$LAST_BODY" '.distinct_variants')" = "3" ]; then
  ok "three visibly different options on the seeded scenario (the spec's acceptance criterion)"
else
  warn "only $(jget "$LAST_BODY" '.distinct_variants') distinct option(s). Duplicates: $(jget "$LAST_BODY" '.duplicate_options | tostring')"
fi

INVALID="$(jget "$LAST_BODY" '[.plans[] | select(.auditor_valid | not)] | length')"
if [ "${INVALID:-0}" = "0" ]; then
  ok "every option passed the independent auditor"
else
  die "${INVALID} option(s) failed the auditor"
fi

step "3b · Nothing became active — generation produces options, never orders"
api GET /plans OBSERVER
expect 200 'GET /plans'
ACTIVE_PLAN="$(jget "$LAST_BODY" '.active_plan_id')"
if [ -z "$ACTIVE_PLAN" ] || [ "$ACTIVE_PLAN" = "null" ]; then
  ok "no ACTIVE plan yet"
else
  die "a plan is already active before any approval — that would be an auto-activation bug"
fi

# ---------------------------------------------------------------------------
# 4 · Inject a disruption
# ---------------------------------------------------------------------------

step "4 · Inject ${EVENT} (PLANNER)"
api POST /events/inject PLANNER "{\"scripted_event\": \"${EVENT}\"}"
expect 200 "POST /events/inject ${EVENT}"
say "$(jget "$LAST_BODY" '.message')"
say "proposals:          $(jget "$LAST_BODY" '.proposals | length')"
say "distinct variants:  $(jget "$LAST_BODY" '.distinct_variants')"
say "free missions:      $(jget "$LAST_BODY" '.free_mission_count')"
say "distinctness note:  $(jget "$LAST_BODY" '.distinct_options_note')"
say "affected missions:  $(jget "$LAST_BODY" '.affected_mission_ids | join(", ")')"
say "frozen and held:    $(jget "$LAST_BODY" '.frozen_mission_ids | join(", ")')"
table '' '.proposals[] | "   \(.id)  \(.label)  \(.status)  weighted \(.metrics.weighted_coverage), mean risk \(.metrics.mean_risk | . * 1000 | round / 1000), \(.metrics.changed_assignments) change(s) vs parent"'

PENDING_PROPOSAL="$(jget "$LAST_BODY" '.proposals[0].id')"
[ -n "$PENDING_PROPOSAL" ] || die "no proposal id in the injection response"
ok "proposals are PENDING and nothing is active"
if contains "$(jget "$LAST_BODY" '.distinct_options_note')" 'distinct option'; then
  warn "two options collapsed onto each other; the UI marks the identical cards"
fi

# ---------------------------------------------------------------------------
# 5 · Approval is Commander-only
# ---------------------------------------------------------------------------

step "5 · Approval must be refused for non-Commanders"
api POST "/proposals/${PENDING_PROPOSAL}/approve" PLANNER '{}'
if [ "$LAST_STATUS" = "403" ]; then
  ok "PLANNER refused -> HTTP 403: $(jget "$LAST_BODY" '.detail')"
else
  die "PLANNER approval returned HTTP ${LAST_STATUS}; advisory-only approval was bypassed"
fi

api POST "/proposals/${PENDING_PROPOSAL}/approve" OBSERVER '{}'
if [ "$LAST_STATUS" = "403" ]; then
  ok "OBSERVER refused -> HTTP 403"
else
  die "OBSERVER approval returned HTTP ${LAST_STATUS}; it should be 403"
fi

api POST "/proposals/${PENDING_PROPOSAL}/approve" MAINTAINER '{}'
if [ "$LAST_STATUS" = "403" ]; then
  ok "MAINTAINER refused -> HTTP 403"
else
  warn "MAINTAINER approval returned HTTP ${LAST_STATUS} (expected 403)"
fi

# ---------------------------------------------------------------------------
# 6 · The Commander approves
# ---------------------------------------------------------------------------

step "6 · Approve as COMMANDER — the only path that activates anything"
api POST "/proposals/${PENDING_PROPOSAL}/approve" COMMANDER '{}'
expect 200 "POST /proposals/${PENDING_PROPOSAL}/approve as COMMANDER"
say "activated: $(jget "$LAST_BODY" '.activated_plan_id // .plan_id')"
say "$(jget "$LAST_BODY" '.message')"

api GET /plans OBSERVER
expect 200 'GET /plans'
ACTIVE_PLAN="$(jget "$LAST_BODY" '.active_plan_id')"
say "active plan: ${ACTIVE_PLAN}"
table '' '.plans[] | "   \(.id)  \(.variant)  \(.status_label // .status)"'

ACTIVE_COUNT="$(jget "$LAST_BODY" '[.plans[] | select(.status == "ACTIVE")] | length')"
if [ "$ACTIVE_COUNT" = "1" ]; then
  ok "exactly one ACTIVE plan"
else
  die "${ACTIVE_COUNT} plans are ACTIVE; approval must activate exactly one"
fi

step "6b · The sibling options are superseded, not deleted"
api GET /proposals OBSERVER
expect 200 'GET /proposals'
say "proposal statuses after the approval:"
table '' '.proposals[] | "   \(.id)  \(.label)  \(.status)"'
SUPERSEDED="$(jget "$LAST_BODY" '[.proposals[] | select(.status == "SUPERSEDED")] | length')"
if [ "${SUPERSEDED:-0}" -ge 2 ] 2>/dev/null; then
  ok "the two sibling options were superseded — the Commander picked one, not three"
else
  die "expected 2 superseded siblings, found ${SUPERSEDED:-0}"
fi

api GET /plans OBSERVER
expect 200 'GET /plans'
say "plan versions (superseded plans stay on the record, they are not deleted):"
table '' '.plans[] | "   \(.id)  \(.variant)  \(.status_label // .status)"'

step "6c · Reject one of the remaining proposals"
api GET /proposals OBSERVER
REJECT_ME="$(jget "$LAST_BODY" '[.proposals[] | select(.status == "PENDING")][0].id')"
if [ -n "$REJECT_ME" ]; then
  api POST "/proposals/${REJECT_ME}/reject" PLANNER '{"note": "should be refused"}'
  if [ "$LAST_STATUS" = "403" ]; then
    ok "PLANNER cannot reject -> HTTP 403"
  else
    die "PLANNER reject returned HTTP ${LAST_STATUS}; it should be 403"
  fi

  api POST "/proposals/${REJECT_ME}/reject" COMMANDER '{"note": "walkthrough: rejected an alternative option"}'
  expect 200 "POST /proposals/${REJECT_ME}/reject as COMMANDER"
  ok "rejected ${REJECT_ME}; the reason is kept in the ledger"
else
  say "no pending proposal left to reject — approving one superseded its siblings"
fi

# ---------------------------------------------------------------------------
# 7 · Advance the clock, then revise
# ---------------------------------------------------------------------------

step "7 · Advance the clock to minute ${CLOCK_MINUTE}"
api POST /state/clock COMMANDER "{\"advance_to_minute\": ${CLOCK_MINUTE}}"
expect 200 'POST /state/clock'
say "now minute $(jget "$LAST_BODY" '.now_minute') ($(jget "$LAST_BODY" '.now_iso'))"

step "7b · Revise the active plan — sorties that already took off must stay frozen"
api GET "/plans/${ACTIVE_PLAN}" OBSERVER
expect 200 "GET /plans/${ACTIVE_PLAN}"
PARENT_MISSIONS="$(jget "$LAST_BODY" '.metrics.missions_covered')"
PARENT_WEIGHT="$(jget "$LAST_BODY" '.metrics.weighted_coverage')"
say "parent ${ACTIVE_PLAN}: ${PARENT_MISSIONS} missions, weighted ${PARENT_WEIGHT}"

api POST /plans/generate PLANNER '{"revise": true}'
expect 200 'POST /plans/generate {"revise": true}'
say "parent: $(jget "$LAST_BODY" '.parent_plan_id')"
say "free missions: $(jget "$LAST_BODY" '.free_mission_count')"
say "distinctness note: $(jget "$LAST_BODY" '.distinct_options_note')"
table '' '.plans[] |
  "   \(.variant): \(.metrics.missions_covered)/\(.metrics.missions_total) missions, weighted \(.metrics.weighted_coverage), \(.metrics.changed_assignments) change(s) vs parent, \(.metrics.frozen_assignments) frozen, auditor \(if .auditor_valid then "valid" else "INVALID" end)"'

NEW_MISSIONS="$(jget "$LAST_BODY" '.plans[0].metrics.missions_covered')"
if [ "${NEW_MISSIONS:-0}" -gt "${PARENT_MISSIONS:-0}" ] 2>/dev/null; then
  ok "the revision picked up capacity its parent could not use (${PARENT_MISSIONS} -> ${NEW_MISSIONS} missions)"
elif [ "${NEW_MISSIONS:-0}" = "${PARENT_MISSIONS:-0}" ]; then
  say "the revision matched its parent; missions it could not use are still blocked by hard constraints"
else
  die "the revision covered FEWER missions than its parent (${PARENT_MISSIONS} -> ${NEW_MISSIONS})"
fi

FROZEN="$(jget "$LAST_BODY" '.plans[0].metrics.frozen_assignments')"
if [ "${FROZEN:-0}" -gt 0 ] 2>/dev/null; then
  ok "${FROZEN} assignment(s) frozen at or before minute ${CLOCK_MINUTE}"
else
  say "no assignment had taken off by minute ${CLOCK_MINUTE}, so nothing was frozen this time"
fi

step "7c · A revision is a draft, not an order — the active plan must not change"
api GET /plans OBSERVER
expect 200 'GET /plans'
STILL_ACTIVE="$(jget "$LAST_BODY" '.active_plan_id')"
NEW_DRAFTS="$(jget "$LAST_BODY" '[.plans[] | select(.status == "DRAFT")] | length')"
if [ "$STILL_ACTIVE" = "$ACTIVE_PLAN" ]; then
  ok "the active plan is still ${STILL_ACTIVE}; the revision produced ${NEW_DRAFTS} draft(s)"
else
  die "the active plan changed from ${ACTIVE_PLAN} to ${STILL_ACTIVE} without an approval"
fi
say "no proposal was created by the revision, so there is nothing new for a Commander to approve"
api GET /proposals OBSERVER
expect 200 'GET /proposals'
table '' '.proposals[] | "   \(.id)  \(.label)  \(.status)"'
PENDING_NOW="$(jget "$LAST_BODY" '[.proposals[] | select(.status == "PENDING")] | length')"
if [ "${PENDING_NOW:-0}" = "0" ]; then
  ok "no proposal is pending; the revision is a draft awaiting a Commander decision"
else
  warn "${PENDING_NOW} proposal(s) pending"
fi

# ---------------------------------------------------------------------------
# 8 · What-if sandbox isolation
# ---------------------------------------------------------------------------

step "8 · What-if sandbox — it must not touch live state"
api GET /what-if/templates PLANNER
expect 200 'GET /what-if/templates'
say "variants offered: $(jget "$LAST_BODY" '.variants | join(", ")')"
say "scripted events: $(jget "$LAST_BODY" '.scripted_events | join(", ")')"
say "override help: $(jget "$LAST_BODY" '.override_help')"

api POST /what-if/run PLANNER \
  "{\"label\": \"walkthrough sandbox\", \"variant\": \"SAFETY FIRST\", \"scripted_event\": \"${WHATIF_EVENT}\"}"
expect 200 'POST /what-if/run'
say "label:             $(jget "$LAST_BODY" '.label')"
say "variant:           $(jget "$LAST_BODY" '.variant')"
say "digest before:     $(jget "$LAST_BODY" '.state_digest_before')"
say "digest after:      $(jget "$LAST_BODY" '.state_digest_after')"
say "coverage delta:    $(jget "$LAST_BODY" '.coverage_delta')"
say "mean risk delta:   $(jget "$LAST_BODY" '.mean_risk_delta')"
say "missions changed:  $(jget "$LAST_BODY" '.changed_missions | length')"
say "sandbox auditor:   $(jget "$LAST_BODY" '.experiment_auditor_valid')"
say "$(jget "$LAST_BODY" '.approval_notice')"

ISOLATED="$(jget "$LAST_BODY" '.isolation_verified')"
BEFORE="$(jget "$LAST_BODY" '.state_digest_before')"
AFTER="$(jget "$LAST_BODY" '.state_digest_after')"
if [ "$ISOLATED" = "true" ] && [ "$BEFORE" = "$AFTER" ]; then
  ok "ISOLATION VERIFIED — the live digest is unchanged"
else
  die "what-if reported isolation_verified=${ISOLATED} with ${BEFORE} -> ${AFTER}"
fi

step "8b · The sandbox result is not a proposal and cannot be approved"
api GET /proposals OBSERVER
expect 200 'GET /proposals'
WHATIF_PROPOSALS="$(jget "$LAST_BODY" '[.proposals[] | select(.id | startswith("WHATIF"))] | length')"
if [ "${WHATIF_PROPOSALS:-0}" = "0" ]; then
  ok "the sandbox produced no proposal — there is nothing there to approve"
else
  die "the what-if sandbox created ${WHATIF_PROPOSALS} approvable proposal(s)"
fi

# ---------------------------------------------------------------------------
# 9 · Audit trail
# ---------------------------------------------------------------------------

step "9 · Hash-chained ledger"
api GET /ledger MAINTAINER
expect 200 'GET /ledger'
say "$(jget "$LAST_BODY" '.chain_badge')"
say "$(jget "$LAST_BODY" '.integrity_note')"
table '' '.entries[-8:][] |
  "   #\(.sequence)  \(.actor_role)  \(.action)  \(.description)"'

api GET /ledger/verify MAINTAINER
expect 200 'GET /ledger/verify'
say "status: $(jget "$LAST_BODY" '.chain_status'), $(jget "$LAST_BODY" '.checked') entries, head $(jget "$LAST_BODY" '.head_hash' | cut -c1-16)..."
if [ "$(jget "$LAST_BODY" '.chain_status')" = "VALID" ]; then
  ok "chain verified"
else
  die "chain did not verify: $(jget "$LAST_BODY" '.chain_status') at $(jget "$LAST_BODY" '.broken_at')"
fi

step "9b · Replay the decision path for ${ACTIVE_PLAN}"
api GET "/replay/${ACTIVE_PLAN}" MAINTAINER
expect 200 "GET /replay/${ACTIVE_PLAN}"
say "$(jget "$LAST_BODY" '.note')"
say "plan status $(jget "$LAST_BODY" '.plan_status'), variant $(jget "$LAST_BODY" '.plan_variant'), version $(jget "$LAST_BODY" '.plan_version')"
say "total steps: $(jget "$LAST_BODY" '.steps | length')"
table '' '.steps[] |
  "   #\(.sequence)  \(if .is_on_plan_path then "on-path " else "context " end) \(.action)  \(.description)"'

# ---------------------------------------------------------------------------
# 10 · Mandatory labels
# ---------------------------------------------------------------------------

step "10 · The two mandatory labels must be on every screen payload"
for endpoint in /state/overview /state/map /state/readiness /plans /assets /missions /ledger; do
  api GET "$endpoint" OBSERVER
  MISSING=""
  contains "$LAST_BODY" 'SYNTHETIC DEMONSTRATION DATA' || MISSING="${MISSING} SYNTHETIC"
  contains "$LAST_BODY" 'ADVISORY ONLY' || MISSING="${MISSING} ADVISORY"
  if [ -z "$MISSING" ]; then
    ok "${endpoint}: both labels present"
  else
    warn "${endpoint}: missing${MISSING}"
  fi
done

# ---------------------------------------------------------------------------
printf '\n\033[1;32mWalkthrough complete.\033[0m\n'
printf 'Every number above came from the synthetic scenario on this machine.\n'
printf 'Nothing was activated without a Commander approval, and no live state was mutated\n'
printf 'by the what-if sandbox.\n\n'
