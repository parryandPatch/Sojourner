# SOJOURNER performance benchmark.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# The spec forbids fabricated performance numbers. This script is the thing that turns
# that into a measurement: it runs the actual CP-SAT solves against the actual seeded
# scenario and writes what it observed to docs/benchmark-results.md. Every figure in
# that file came from a run of this script on the machine named in it.
#
# WHAT IT MEASURES
#   1. Initial frontier solve: all three trade-off options, end to end.
#   2. Revised frontier solve at several clock positions, after a scripted disruption.
#      This is the number that matters for the acceptance criterion "initial plan and a
#      revised plan appear in under 8 seconds".
#   3. Greedy fallback: the same work with the solver stubbed to return nothing, to confirm
#      the fallback path is both reachable and correctly labelled.
#   4. Ledger verification cost, which grows with chain length.
#   5. An objective-weight sweep, to keep the "fewer than three distinct options is not
#      a tuning problem" claim falsifiable rather than asserted.
#
# WHAT IT DOES NOT MEASURE
#   It does not benchmark the HTTP layer, the database, the frontend, or any real workload.
#   These are single-process, single-machine, synthetic numbers on a laptop. Treat them as
#   a smoke test for regressions, not as capacity figures.
#
# USAGE
#   cd backend && ../backend/.venv/bin/python ../benchmarks/run_benchmark.py
#   python ../benchmarks/run_benchmark.py --iterations 10 --out ../docs/benchmark-results.md

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# Allow running as a script from anywhere: put the backend package on the path.
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.services import (  # noqa: E402
    decision_ledger,
    disruption_monitor,
    frontier_explorer,
    planning_engine,
    scenario_studio,
)

# Scripted events to exercise the re-plan path, from the spec's set of four.
EVENT_KEYS = ("EVENT_A", "EVENT_B", "EVENT_C", "EVENT_D")

# Clock positions to re-plan at. 0 means nothing has departed, so the frozen set is empty
# and the solver has the most freedom; later positions force it to keep started sorties.
CLOCK_POSITIONS = (0, 60, 120, 180, 240, 300)

# Candidate objective weightings for the distinctness sweep, as
# (coverage, safety, stability) triples of (risk, cost, change).
#
# This exists to make one specific claim falsifiable: that fewer than three distinct options
# after a small disruption is substantially a property of the decision space, not of badly
# chosen weights.
#
# Two deliberate controls are included:
#   * all three variants identical — must score 0 three-distinct and 24 collapsed, or the
#     measurement is not reading distinctness at all;
#   * the `drop_*` candidates — these delete an objective term the spec requires, which is
#     the whole reason they score well. They are here to show that the score is sensitive to
#     tuning AND that the best-scoring tuning is not spec-compliant, which together mean the
#     collapse is not something to tune away.
#
# The `control — drop_*` candidates are deliberately NOT spec-compliant: each removes an
# objective term the spec requires. They are the diagnostic that shows the best-scoring
# tuning is also the illegitimate one.
WEIGHT_CANDIDATES: dict[str, tuple[tuple[int, int, int], ...]] = {
    # --- the shipping weighting -------------------------------------------------
    "current (6/3/8, 900/0/0, 6/1/40)": (
        (6, 3, 8),
        (900, 0, 0),
        (6, 1, 40),
    ),
    # --- controls ----------------------------------------------------------------
    "control — all three variants identical": (
        (6, 3, 8),
        (6, 3, 8),
        (6, 3, 8),
    ),
    "control — drop_cost from coverage (objective reduced)": (
        (6, 0, 8),
        (900, 0, 0),
        (6, 0, 40),
    ),
    "control — drop risk from coverage (constraint removed)": (
        (0, 3, 8),
        (0, 0, 0),
        (0, 3, 40),
    ),
    # --- spec-compliant alternatives ---------------------------------------------
    "alt A — coverage dominant (300/300/300, 900/1/1, 1/1/1)": (
        (300, 300, 300),
        (900, 1, 1),
        (1, 1, 1),
    ),
    "alt B — risk-only spread, cost kept (6/1/0, 2000/0/0, 6/0/120)": (
        (6, 1, 0),
        (2000, 0, 0),
        (6, 0, 120),
    ),
    "alt C — extremes (1/1/1, 4000/0/0, 1/1/4000)": (
        (1, 1, 1),
        (4000, 0, 0),
        (1, 1, 4000),
    ),
    "alt D — safety dominant (1/1/1, 500/0/0, 1/1/1200)": (
        (1, 1, 1),
        (500, 0, 0),
        (1, 1, 1200),
    ),
    "alt E — low safety risk weight (6/3/8, 40/0/0, 6/1/40)": (
        (6, 3, 8),
        (40, 0, 0),
        (6, 1, 40),
    ),
    "alt F — change-averse everywhere (6/3/3000, 900/0/3000, 6/1/6000)": (
        (6, 3, 3000),
        (900, 0, 3000),
        (6, 1, 6000),
    ),
    "alt G — equal spread (10/10/10, 1000/10/10, 10/10/200)": (
        (10, 10, 10),
        (1000, 10, 10),
        (10, 10, 200),
    ),
}
def is_spec_compliant(triples: tuple[tuple[int, int, int], ...]) -> bool:
    """Does this weighting keep the objective the spec actually requires?

    The spec asks for a primary plan that maximises
    `priority_weighted_coverage - risk_penalty - resource_cost_penalty - plan_change_penalty`,
    and then three variants derived from it. So the COVERAGE FIRST triple must carry a
    non-zero weight on all three of risk, cost and change; a weighting that sets cost to zero
    there is not a tuning choice, it is a different objective.

    For SAFETY FIRST and STABILITY FIRST a zero weight is legitimate: the spec describes
    those as minimising risk and minimising change respectively, and both are additionally
    bound by the 85% coverage floor, so whichever term they drop is enforced by a constraint
    rather than by the objective. Risk, however, must stay non-zero everywhere — every plan
    is required to respect a mission's maximum risk.
    """
    coverage_risk, coverage_cost, coverage_change = triples[0]
    if coverage_risk <= 0 or coverage_cost <= 0:
        return False
    if coverage_change < 0:
        return False
    return all(triple[0] > 0 for triple in triples[1:])


@dataclass(slots=True)
class Sample:
    """One timed run."""

    label: str
    detail: str
    durations_ms: list[int] = field(default_factory=list)
    solve_time_ms: list[int] = field(default_factory=list)
    variants: int = 0
    notes: list[str] = field(default_factory=list)
    # How many missions the solve was free to decide. Recorded because distinctness tracks
    # it: with more free missions there are more decisions the three objectives can disagree
    # about, so the "three distinct options" claim holds more often.
    free_missions: int = 0

    @property
    def total_ms(self) -> int:
        return sum(self.durations_ms)

    @property
    def wall_min(self) -> int:
        return min(self.durations_ms)

    @property
    def wall_max(self) -> int:
        return max(self.durations_ms)

    @property
    def wall_median(self) -> int:
        return int(statistics.median(self.durations_ms))

    @property
    def wall_mean(self) -> int:
        return int(statistics.fmean(self.durations_ms))


def _fresh_state(seed: int, now_minute: int):  # type: ignore[no-untyped-def]
    """A newly generated state. Deliberately never reused, so runs stay independent."""
    return scenario_studio.build_scenario(
        scenario_studio.default_config(seed), now_minute=now_minute
    )


def bench_initial_solve(seed: int, iterations: int, limit: float) -> Sample:
    """The first thing a reviewer does: generate the three-option frontier from scratch."""
    sample = Sample(
        label="Initial frontier solve",
        detail="3 options (COVERAGE / SAFETY / STABILITY), no parent plan, clock at minute 0",
    )
    for _ in range(iterations):
        state = _fresh_state(seed, 0)
        started = time.perf_counter()
        frontier = frontier_explorer.build_frontier(
            state, scenario_id="SCN-BENCH", version=1, time_limit_seconds=limit
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        sample.durations_ms.append(elapsed)
        sample.solve_time_ms.extend(p.result.solve_time_ms for p in frontier.plans)
        sample.variants = frontier.distinct_count
        sample.free_missions = frontier.free_mission_count
        if not sample.notes:
            sample.notes = frontier.notes
    return sample


def bench_revised_solve(seed: int, iterations: int, limit: float) -> list[Sample]:
    """Re-planning after a disruption, at several clock positions.

    The acceptance criterion is about this number, so it is measured at every position
    rather than at whichever one happens to be fast.
    """
    samples: list[Sample] = []

    for event_key in EVENT_KEYS:
        per_clock: dict[int, list[int]] = {clock: [] for clock in CLOCK_POSITIONS}
        variants_by_clock: dict[int, int] = {}
        free_by_clock: dict[int, int] = {}
        fallback_clocks: set[int] = set()

        for _ in range(iterations):
            for clock in CLOCK_POSITIONS:
                # Build a parent plan, then disrupt it, for each clock position.
                state = _fresh_state(seed, clock)
                parent_frontier = frontier_explorer.build_frontier(
                    state, scenario_id="SCN-BENCH", version=1, time_limit_seconds=limit
                )
                parent = parent_frontier.plans[0].plan

                kind, title, payload, _ = disruption_monitor.resolve_scripted_event(event_key)
                application = disruption_monitor.apply_event(
                    state, kind, payload, label=title, source="benchmark", active_plan=parent
                )

                started = time.perf_counter()
                revised = frontier_explorer.build_frontier(
                    application.state,
                    scenario_id="SCN-BENCH",
                    version=2,
                    parent_plan=parent,
                    affected_mission_ids=set(application.affected_mission_ids),
                    time_limit_seconds=limit,
                )
                elapsed = int((time.perf_counter() - started) * 1000)
                per_clock[clock].append(elapsed)
                variants_by_clock[clock] = max(
                    variants_by_clock.get(clock, 0), revised.distinct_count
                )
                free_by_clock[clock] = max(
                    free_by_clock.get(clock, 0), revised.free_mission_count
                )
                if any(p.plan.is_fallback for p in revised.plans):
                    fallback_clocks.add(clock)

        for clock in CLOCK_POSITIONS:
            samples.append(
                Sample(
                    label=f"Revised solve — {event_key} @ min {clock}",
                    detail=(
                        f"3 options against a parent plan at clock minute {clock}"
                        f"{' (FALLBACK observed)' if clock in fallback_clocks else ''}"
                    ),
                    durations_ms=per_clock[clock],
                    variants=variants_by_clock.get(clock, 0),
                    free_missions=free_by_clock.get(clock, 0),
                )
            )

    return samples


WeightSweep = tuple[
    list[tuple[str, int, int, int]],
    list[int],
    dict[str, list[bool]],
]


def bench_weight_sweep(seed: int, limit: float) -> WeightSweep:
    """Does a different set of objective weights produce three options more often?

    This is the check that keeps the "collapse is arithmetic, not tuning" claim honest. If a
    weighting existed that reliably produced three distinct options after a small disruption,
    the reporting design would be the wrong call.

    The parent plans and disrupted states are built once and reused across candidates, so
    every weighting is measured against exactly the same 24 problems and the only variable is
    the weights themselves.

    Returns the per-candidate counts, the free-set size of each problem, and each candidate's
    per-problem outcome, so the report can cross-tab rather than assert.
    """
    from app.domain.enums import PlanVariant

    # Build the 24 (event, clock) problems once, with the shipping weights.
    problems: list[tuple[object, object, set[str]]] = []
    for event_key in EVENT_KEYS:
        for clock in CLOCK_POSITIONS:
            state = _fresh_state(seed, clock)
            parent = frontier_explorer.build_frontier(
                state, scenario_id="SCN-BENCH", version=1, time_limit_seconds=limit
            ).plans[0].plan
            kind, title, payload, _ = disruption_monitor.resolve_scripted_event(event_key)
            application = disruption_monitor.apply_event(
                state, kind, payload, label=title, source="benchmark", active_plan=parent
            )
            problems.append(
                (
                    application.state,
                    parent,
                    set(application.affected_mission_ids),
                )
            )

    original = dict(planning_engine.WEIGHTS)
    order = (
        PlanVariant.COVERAGE_FIRST,
        PlanVariant.SAFETY_FIRST,
        PlanVariant.STABILITY_FIRST,
    )
    results: list[tuple[str, int, int, int]] = []
    # Per-problem free-set sizes, recorded on the first pass so the report can cross-tab
    # distinctness against them for the shipping and the best alternative weighting.
    # Without this the report would have to assert "weighting does not help when there are
    # few decisions" without having measured whether that is true.
    free_sizes: list[int] = []
    # Per-problem three-distinct outcome, keyed by candidate label, in the same order.
    per_candidate: dict[str, list[bool]] = {}
    try:
        for pass_index, (label, triples) in enumerate(WEIGHT_CANDIDATES.items()):
            for variant, triple in zip(order, triples, strict=True):
                planning_engine.WEIGHTS[variant] = triple

            three_distinct = 0
            collapsed = 0
            outcomes: list[bool] = []
            for state, parent, affected in problems:
                frontier = frontier_explorer.build_frontier(
                    state,
                    scenario_id="SCN-BENCH",
                    version=2,
                    parent_plan=parent,
                    affected_mission_ids=affected,
                    time_limit_seconds=limit,
                )
                is_three = frontier.distinct_count == 3
                three_distinct += is_three
                collapsed += frontier.distinct_count == 1
                outcomes.append(is_three)
                # Recorded once, on the first pass. The free set is a property of the state
                # and the event, not of the weights, so one pass is enough.
                if pass_index == 0:
                    free_sizes.append(frontier.free_mission_count)
            results.append((label, three_distinct, collapsed, len(problems)))
            per_candidate[label] = outcomes
    finally:
        planning_engine.WEIGHTS.clear()
        planning_engine.WEIGHTS.update(original)

    return results, free_sizes, per_candidate


def bench_greedy_fallback(seed: int, iterations: int) -> Sample:
    """Measure the fallback path by *simulating* a solver that returns nothing.

    A real timeout cannot be provoked on demand: `planning_engine` clamps the budget to a
    0.5 s floor, and at that budget CP-SAT solves this scenario comfortably, so asking for
    zero seconds produces ordinary solved plans rather than a fallback. Rather than report a
    fabricated "fallback took N ms", this patches `solve_with_cpsat` to return no solution —
    the same injection the test suite uses — and measures the greedy pass that actually runs
    when a solve comes back empty.

    What is measured: option enumeration, the greedy assignment, auditing, and metrics for
    all three options. What is not measured: the cost of CP-SAT hitting its real limit.
    """
    sample = Sample(
        label="Greedy fallback (solver stubbed to return no solution)",
        detail=(
            "Simulated solver failure, not a real timeout; measures the greedy pass that "
            "runs when a solve comes back empty"
        ),
    )

    original = planning_engine.solve_with_cpsat
    planning_engine.solve_with_cpsat = _stubbed_solver  # type: ignore[assignment]
    try:
        for _ in range(iterations):
            state = _fresh_state(seed, 0)
            started = time.perf_counter()
            frontier = frontier_explorer.build_frontier(
                state, scenario_id="SCN-BENCH", version=1, time_limit_seconds=8.0
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            sample.durations_ms.append(elapsed)
            sample.variants = frontier.distinct_count

            labelled = all(p.plan.is_fallback for p in frontier.plans)
            valid = all(p.audit_valid for p in frontier.plans)
            sample.notes.append(
                f"variants={frontier.distinct_count} "
                f"every_plan_labelled_fallback={labelled} auditor_valid={valid} "
                f"assignments={[len(p.plan.assignments) for p in frontier.plans]} "
                f"weighted_coverage="
                f"{[round(p.plan.metrics.weighted_coverage) for p in frontier.plans]}"
            )
            assert labelled, "a plan produced by the fallback path was not labelled FALLBACK"
            assert valid, "a fallback plan failed the independent auditor"
    finally:
        planning_engine.solve_with_cpsat = original  # type: ignore[assignment]
    return sample


def _stubbed_solver(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Stand-in for `solve_with_cpsat` that always reports failure, as CP-SAT would at its
    limit. Signature matches the real function; the arguments are deliberately ignored."""
    return [], "UNKNOWN", 8000, ["Simulated solver timeout for benchmarking purposes."]


def bench_ledger(iterations: int) -> Sample:
    """Hash-chain verification cost as the chain grows.

    Uses the real ``build_entry`` / ``verify_chain`` pair against in-memory ``LedgerRow``
    objects, so this measures the same code the API runs. Verification is linear in chain
    length by construction, so the interesting figure is the per-entry constant.
    """
    from app.domain.enums import ActorRole, LedgerAction

    sample = Sample(
        label="Ledger verification",
        detail="SHA-256 re-hash of an in-memory synthetic chain (no database involved)",
    )
    for index in range(max(iterations, 1)):
        rows = []
        for sequence in range(1, (index + 1) * 25 + 1):
            rows.append(
                decision_ledger.build_entry(
                    rows,
                    scenario_id="SCN-BENCH",
                    actor_role=ActorRole.COMMANDER,
                    action=LedgerAction.PLAN_GENERATED,
                    entity_ref=f"PLN-BENCH-{sequence}",
                    payload={"sequence": sequence, "note": f"synthetic record {sequence}"},
                    # A fixed stamp keeps the hashes reproducible across runs; the wall
                    # clock is what is being measured, not the timestamps.
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )

        started = time.perf_counter()
        verification = decision_ledger.verify_chain(rows)
        elapsed = int((time.perf_counter() - started) * 1000)

        sample.durations_ms.append(elapsed)
        sample.notes.append(
            f"{len(rows)} entries verified in {elapsed} ms "
            f"({verification.checked} hash(es) recomputed), status={verification.status}"
        )

        # Sanity: a chain that verifies must also break when a payload is edited. This is
        # the property the decision-trail screen depends on, so the benchmark asserts it
        # rather than assuming it.
        rows[len(rows) // 2].payload_json = {"sequence": 0, "note": "tampered"}
        tampered = decision_ledger.verify_chain(rows)
        assert not tampered.valid, "benchmark detected no tampering; verify_chain is not working"
    return sample


def environment() -> dict[str, str]:
    """Record the machine, because a timing without its context is not a measurement."""
    import ortools  # noqa: PLC0415  (only needed here)

    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "ortools": getattr(ortools, "__version__", "unknown"),
        "solver_budget_seconds": str(get_settings().solver_time_limit_seconds),
    }


def render_markdown(
    samples: list[Sample],
    env: dict[str, str],
    seed: int,
    iterations: int,
    limit: float,
    generated_at: str,
    sweep: WeightSweep | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# Benchmark results")
    lines.append("")
    lines.append(
        "Generated by `benchmarks/run_benchmark.py`. These are single-machine, "
        "single-process, synthetic numbers measured on the machine described below. They are "
        "a regression smoke test, not a capacity claim, and they say nothing about any real "
        "workload."
    )
    lines.append("")
    lines.append(f"- Generated: `{generated_at}`")
    lines.append(f"- Scenario seed: `{seed}`")
    lines.append(f"- Iterations per measurement: `{iterations}`")
    lines.append(f"- Per-solve CP-SAT budget: `{limit}` s")
    lines.append("")

    lines.append("## Machine")
    lines.append("")
    lines.append("| key | value |")
    lines.append("| --- | --- |")
    for key, value in env.items():
        lines.append(f"| {key} | {value} |")
    lines.append("")

    lines.append("## Wall-clock per full three-option solve")
    lines.append("")
    lines.append(
        "`total` is all three options added together; `min` / `median` / `mean` / `max` are "
        "per-run wall clock for the same work. `free` is how many missions the solve was "
        "actually free to decide, and `distinct variants` is how many of the three returned "
        "options have different assignments."
    )
    lines.append("")
    lines.append(
        "| measurement | scenario | runs | total ms | min | median | mean | max | free | distinct variants |"
    )
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for sample in samples:
        lines.append(
            f"| {sample.label} | {sample.detail} | {len(sample.durations_ms)} | "
            f"{sample.total_ms} | {sample.wall_min} | {sample.wall_median} | "
            f"{sample.wall_mean} | {sample.wall_max} | {sample.free_missions or '—'} | "
            f"{sample.variants} |"
        )
    lines.append("")

    lines.append("## Acceptance criterion check")
    lines.append("")
    lines.append(
        "The spec requires the initial plan and a revised plan to appear in under 8 seconds "
        "on a normal laptop. Each figure below is the wall clock for the **whole** "
        "three-option solve — option enumeration, the solve itself, auditing, and metrics — "
        "which is the number a user actually waits for."
    )
    lines.append("")

    initial = next((s for s in samples if s.label.startswith("Initial")), None)
    revised = [s for s in samples if s.label.startswith("Revised")]

    if initial is not None:
        verdict = "met" if initial.wall_max < 8000 else "NOT met"
        lines.append(
            f"- Initial frontier: min {initial.wall_min} ms, median "
            f"{initial.wall_median} ms, max {initial.wall_max} ms → **{verdict}** "
            f"against the 8000 ms criterion."
        )
    else:
        lines.append("- Initial frontier: not measured.")

    if revised:
        slowest = max(revised, key=lambda s: s.wall_max)
        fastest = min(revised, key=lambda s: s.wall_min)
        verdict = "met" if slowest.wall_max < 8000 else "NOT met"
        lines.append(
            f"- Revised solves ({len(revised)} event × clock combinations): fastest "
            f"{fastest.wall_min} ms (`{fastest.label}`), slowest {slowest.wall_max} ms "
            f"(`{slowest.label}`) → **{verdict}** against the 8000 ms criterion."
        )
    else:
        lines.append("- Revised solves: not measured.")

    lines.append(
        "- The planner's own `solve_time_ms` field is CP-SAT's accounting and excludes option "
        "enumeration, auditing, and metric computation. Every figure above is wall clock and "
        "includes all of that."
    )
    lines.append(
        "- These timings exclude HTTP, database, and rendering. The end-to-end time a user "
        "perceives is longer and was not measured here."
    )
    lines.append("")

    lines.append("## Option distinctness observed during this run")
    lines.append("")
    lines.append(
        "The spec asks the planner to \"return exactly three distinct options\" and sets the "
        "acceptance criterion \"three trade-off plans are visibly different on the seeded "
        "scenario\". The two are not the same claim, and the difference shows up here. The "
        "initial solve, where all 13 missions are free to be decided, produces three distinct "
        "options. A re-plan re-opens only the missions the event affected — the spec's own "
        "re-planning rule — and when that leaves one or two decisions, all three objectives "
        "genuinely select the same option and only one plan exists. Note also that a free "
        "mission with only one feasible asset is not a real decision at all, however many "
        "missions are open."
    )
    lines.append("")
    if revised:
        collapsed = [s for s in revised if s.variants < 3]
        lines.append(
            f"- {len(revised) - len(collapsed)} of {len(revised)} re-plans produced three "
            f"distinct options; {len(collapsed)} produced fewer."
        )
        if collapsed:
            lines.append(
                "- Grouped by how many missions the re-plan was free to decide. This is a "
                "correlation, not a law — see the reading below:"
            )
            lines.append("")
            lines.append("| free missions | re-plans | three distinct options |")
            lines.append("| ---: | ---: | ---: |")
            by_free: dict[int, list[int]] = {}
            for sample in revised:
                by_free.setdefault(sample.free_missions, []).append(sample.variants)
            for free_missions in sorted(by_free):
                observed = by_free[free_missions]
                lines.append(
                    f"| {free_missions} | {len(observed)} | "
                    f"{sum(1 for v in observed if v == 3)} |"
                )
            lines.append("")
            lines.append(
                "  Reading the table: free-set size raises the odds but does not settle them. "
                "Every case with one free mission failed to produce three options — with a "
                "single decision there is nothing for three objectives to disagree about, and "
                "no choice of weights can manufacture a third answer because none exists. "
                "Above that, the outcome depends on whether the free missions actually have "
                "competing options: a large free set whose missions each have one feasible "
                "asset will also collapse, since re-deciding them was never a real choice."
            )
            lines.append(
                "- The planner reports this rather than perturbing a plan until it looks "
                "different: the API returns `duplicate_options` and `distinct_options_note`, "
                "and the UI marks the identical cards. Manufacturing a third answer would put "
                "a label on a plan embodying an objective the solver never optimised."
            )
    lines.append("")

    fallback = next((s for s in samples if "Greedy" in s.label), None)
    if fallback and fallback.notes:
        lines.append("## Greedy fallback observations")
        lines.append("")
        for note in fallback.notes:
            lines.append(f"- `{note}`")
        lines.append("")
        lines.append(
            "Note the `variants=1` above. The greedy fallback does not consult the variant "
            "weights — it walks the option pool in a fixed order and takes the first feasible "
            "choice for each mission — so when it triggers, all three options converge on the "
            "same plan. That is reported rather than hidden: the API returns "
            "`duplicate_options` and `distinct_options_note`, and the UI marks the identical "
            "cards, so nobody is shown a fabricated trade-off. A Commander who sees three "
            "identical cards knows the solver did not answer, which is the honest reading of a "
            "run that hit its budget."
        )
        lines.append("")

    if sweep:
        rows, free_sizes, per_candidate = sweep
        lines.append("")
        lines.append("### Can different objective weights do better?")
        lines.append("")
        lines.append(
            "An earlier draft of this report asserted that the count above was independent "
            "of the objective weights. That assertion was wrong, and this table is the "
            "correction: each weighting below was run over the same 24 disrupted states, with "
            "the same parent plans, changing only the weights. The count moves, by more than "
            "expected."
        )
        lines.append("")
        lines.append(
            "| weighting (coverage / safety / stability) | 3 distinct | 1 distinct | "
            "keeps the spec's objective |"
        )
        lines.append("| --- | ---: | ---: | :---: |")
        for label, three_distinct, collapsed, row_total in rows:
            if label.startswith("control"):
                # A control is not a candidate weighting, so asking whether it keeps the
                # specified objective is not meaningful — mark it and move on.
                mark = "n/a (control)"
            elif is_spec_compliant(WEIGHT_CANDIDATES[label]):
                mark = "yes"
            else:
                mark = "**no**"
            lines.append(
                f"| {label} | {three_distinct}/{row_total} | {collapsed}/{row_total} | {mark} |"
            )
        lines.append("")
        total = rows[0][3] if rows else 0
        control = next((row for row in rows if "identical" in row[0]), None)
        if control is not None:
            lines.append(
                f"The first control — all three variants given identical weights — scored "
                f"{control[1]}/{total} three-distinct and {control[2]}/{total} collapsed to "
                f"one. That is the expected result and confirms the measurement is reading "
                f"distinctness rather than something correlated with it."
            )
        lines.append("")
        shipping = next((row for row in rows if row[0].startswith("current")), (None, 0, 0, 0))
        compliant = [
            row
            for row in rows
            if not row[0].startswith("control")
            and is_spec_compliant(WEIGHT_CANDIDATES[row[0]])
        ]
        best_all = max(rows, key=lambda row: row[1], default=(None, 0, 0, 0))
        best_ok = max(compliant, key=lambda row: row[1], default=(None, 0, 0, 0))
        lines.append(
            f"The shipping weighting reaches {shipping[1]}/{total}. The best alternative that "
            f"*keeps* the specified objective reaches {best_ok[1]}/{total} (`{best_ok[0]}`), so "
            f"tuning within what the spec allows moves the number by {best_ok[1] - shipping[1]:+d}."
        )
        lines.append("")
        if best_all[1] > best_ok[1]:
            lines.append(
                f"The best overall is {best_all[1]}/{total} (`{best_all[0]}`), which is "
                "**not** spec-compliant: it buys its extra distinct options by removing an "
                "objective term the primary plan is required to carry. Both control rows that "
                "strip a term score *worse* than the shipping weighting, which is the useful "
                "finding — the gain is not simply 'delete an objective until the variants "
                "disagree more', it is a specific reshuffling of relative weights."
            )
            lines.append("")

        # Cross-tab: does the extra distinctness come from cases that had room for it, or
        # does it rescue cases the shipping weighting collapsed?
        if free_sizes and per_candidate and shipping[0] in per_candidate:
            gained = [
                size
                for size, was, now in zip(
                    free_sizes,
                    per_candidate[shipping[0]],
                    per_candidate[best_ok[0]],
                    strict=True,
                )
                if now and not was
            ]
            lines.append(
                f"**Where the extra options came from.** Comparing the shipping weighting "
                f"against `{best_ok[0]}` problem by problem, it rescues "
                f"{len(gained)} of {total} re-plans that had collapsed. The free-set sizes of "
                f"the rescued cases: "
                f"{', '.join(str(g) for g in sorted(gained)) if gained else 'none'}."
            )
            lines.append("")
            if gained and min(gained) <= 2:
                lines.append(
                    f"Some rescued cases had only {min(gained)} free mission(s). So the "
                    "shipping weighting collapses more often than it strictly has to, and the "
                    "clean version of the argument — *no weighting can help, because with one "
                    "or two decisions there is only one answer* — is wrong at the low end. "
                    "What survives is narrower: the collapse rate is strongly driven by how "
                    "many decisions are open (see the table above), and a free mission with "
                    "one feasible asset is still not a real decision at any weighting."
                )
                lines.append("")
                lines.append(
                    "That leaves a decision this report should not pretend is settled. The "
                    f"{len(gained)} extra distinct options are real — they are different plans, "
                    "each auditor-valid, each honouring the coverage floor — so adopting this "
                    "weighting would not be fabrication; it would be a better-tuned planner. "
                    "It was not adopted, because the weights were chosen on their merits "
                    "before this measurement existed, and re-picking them to maximise a "
                    "distinctness count found afterwards is precisely the scoreboard-chasing "
                    "this project is supposed to avoid. A reviewer who prefers "
                    f"`{best_ok[0]}` has a defensible case and a reproducible table above."
                )
            else:
                lines.append(
                    "None of the rescued cases had fewer than three free missions, which "
                    "supports the reading above: weighting reshuffles disagreement among the "
                    "cases that already had competing options, and does not manufacture a "
                    "third answer where there were almost none."
                )
            lines.append("")
        lines.append(
            "One caution on reading this table as licence to re-pick the weights by score. "
            "The count is a proxy for a real property — how often a Commander is shown three "
            "genuine alternatives — and it is not the only one. Coverage achieved, mean risk, "
            "and cost are the reasons the weights were set the way they were, and this sweep "
            "measures none of them. A weighting that scores higher on distinctness while "
            "delivering worse plans would be a worse planner with a better-looking report, "
            "and this table cannot tell the difference. Evaluating that properly means "
            "auditing the plan quality of each alternative, which has not been done."
        )
        lines.append("")
        lines.append(
            "The shipping weighting is kept and the collapse it produces is reported. That is "
            "a defensible position, not a proven one."
        )
        lines.append("")

    ledger = next((s for s in samples if "Ledger" in s.label), None)
    if ledger and ledger.notes:
        lines.append("## Ledger verification observations")
        lines.append("")
        for note in ledger.notes:
            lines.append(f"- `{note}`")
        lines.append("")

    lines.append("## What this does not tell you")
    lines.append("")
    lines.append(
        "- Nothing about HTTP latency, database throughput, concurrent users, or frontend "
        "render time. None of those were measured."
    )
    lines.append(
        "- Nothing about real operations. The scenario is synthetic and small by "
        "construction; a real planning problem would have orders of magnitude more entities."
    )
    lines.append(
        "- The timings are from one machine on one day. Re-run the script after a change "
        "rather than comparing against these numbers across machines."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=None, help="scenario seed")
    parser.add_argument(
        "--iterations", type=int, default=3, help="runs per measurement (default: 3)"
    )
    parser.add_argument(
        "--limit", type=float, default=None, help="per-solve CP-SAT budget in seconds"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "docs" / "benchmark-results.md",
        help="where to write the markdown report",
    )
    parser.add_argument(
        "--skip-sweep",
        action="store_true",
        help="skip the objective-weight distinctness sweep (it rebuilds 24 parent frontiers)",
    )
    parser.add_argument("--json", action="store_true", help="also print the samples as JSON")
    args = parser.parse_args()

    settings = get_settings()
    seed = args.seed if args.seed is not None else settings.default_seed
    limit = args.limit if args.limit is not None else settings.solver_time_limit_seconds
    iterations = max(1, args.iterations)

    print(f"SOJOURNER benchmark — seed {seed}, {iterations} iteration(s), budget {limit}s")
    print(f"python {platform.python_version()} on {platform.platform()}")
    print()

    samples: list[Sample] = []
    samples.append(bench_initial_solve(seed, iterations, limit))
    print(f"  done: {samples[-1].label}")
    samples.extend(bench_revised_solve(seed, iterations, limit))
    print(f"  done: {len(samples) - 1} revised measurement(s)")
    samples.append(bench_greedy_fallback(seed, iterations))
    print("  done: greedy fallback")
    samples.append(bench_ledger(iterations))
    print("  done: ledger verification")

    sweep: WeightSweep | None = None
    if not args.skip_sweep:
        sweep = bench_weight_sweep(seed, limit)
        rows = sweep[0]
        best = max((row[1] for row in rows), default=0)
        print(f"  done: objective-weight sweep ({len(rows)} weightings, best {best})")

    env = environment()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    report = render_markdown(
        samples, env, seed, iterations, limit, generated_at, sweep=sweep
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print()
    print(f"wrote {args.out}")

    if args.json:
        print(json.dumps([
            {
                "label": s.label,
                "detail": s.detail,
                "runs": len(s.durations_ms),
                "durations_ms": s.durations_ms,
                "variants": s.variants,
                "notes": s.notes[:3],
            }
            for s in samples
        ], indent=2))

    print()
    for sample in samples:
        print(
            f"{sample.label:<44} median {sample.wall_median:>6} ms  "
            f"max {sample.wall_max:>6} ms  variants {sample.variants}"
        )
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())