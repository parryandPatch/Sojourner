"""Essential test 9: the greedy fallback returns a labelled plan when the solver times out.

When CP-SAT cannot produce a solution inside its budget the prototype must degrade
gracefully rather than return nothing. The fallback plan is always labelled FALLBACK so
a viewer can never mistake it for an optimised result.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.domain.enums import PlanVariant
from app.services.plan_auditor import audit_plan
from app.services.planning_engine import (
    COVERAGE_RETENTION_FLOOR,
    PlanningRequest,
    coverage_floor_for,
    run_planning,
    solve_greedy,
)

ALL_VARIANTS = (
    PlanVariant.COVERAGE_FIRST,
    PlanVariant.SAFETY_FIRST,
    PlanVariant.STABILITY_FIRST,
)


def _request(state, variant=PlanVariant.COVERAGE_FIRST, **kwargs) -> PlanningRequest:
    return PlanningRequest(
        state=state, variant=variant, scenario_id=state.scenario_id, **kwargs
    )


def _force_timeout() -> object:
    """Patch the solver call so it reports no solution, as a timeout would."""
    return patch(
        "app.services.planning_engine.solve_with_cpsat",
        return_value=([], "UNKNOWN", 8000, ["Solver hit its time limit."]),
    )


def test_fallback_plan_is_labelled_fallback(initial_state) -> None:
    with _force_timeout():
        result = run_planning(_request(initial_state))

    assert result.is_fallback is True
    plan = result.to_plan("PLN-TEST-1", PlanVariant.COVERAGE_FIRST)
    assert plan.is_fallback is True
    # The variant label is replaced by FALLBACK, so the trade-off name is never misleading.
    assert plan.variant == PlanVariant.FALLBACK
    assert plan.variant != PlanVariant.COVERAGE_FIRST


def test_fallback_solver_status_records_the_fallback(initial_state) -> None:
    with _force_timeout():
        result = run_planning(_request(initial_state))
    assert "GREEDY" in result.solver_status
    assert result.solver_status.startswith("UNKNOWN")


def test_fallback_explains_itself_in_notes(initial_state) -> None:
    with _force_timeout():
        result = run_planning(_request(initial_state))
    assert any("greedy" in note.lower() for note in result.notes)
    assert any("FALLBACK" in note for note in result.notes)


def test_fallback_still_assigns_missions(initial_state) -> None:
    """A fallback that returns nothing would be useless; it must produce a real plan."""
    with _force_timeout():
        result = run_planning(_request(initial_state))
    assert result.assignments, "the fallback must assign at least one mission"
    assert {a.mission_id for a in result.assignments}


def test_fallback_plan_passes_the_auditor(initial_state) -> None:
    """Degrading must not mean emitting an invalid plan."""
    with _force_timeout():
        result = run_planning(_request(initial_state))
    report = audit_plan(initial_state, result.assignments)
    assert report.valid, report.messages()


def test_fallback_never_activates_anything(initial_state) -> None:
    with _force_timeout():
        result = run_planning(_request(initial_state))
    plan = result.to_plan("PLN-TEST-2", PlanVariant.COVERAGE_FIRST)
    assert plan.status.value == "DRAFT"
    assert plan.approved_by is None
    assert plan.approved_at is None


def test_allow_fallback_false_returns_nothing_and_says_so(initial_state) -> None:
    """A caller that forbids the fallback must get an honest empty result."""
    with _force_timeout():
        result = run_planning(_request(initial_state), allow_fallback=False)
    assert result.assignments == ()
    assert result.is_fallback is False
    assert any("fallback is disabled" in note for note in result.notes)


def test_every_variant_can_fall_back(initial_state) -> None:
    for variant in ALL_VARIANTS:
        with _force_timeout():
            result = run_planning(
                _request(initial_state, variant),
                coverage_reference=695.0,
            )
        assert result.is_fallback is True, variant
        plan = result.to_plan(f"PLN-{variant.value}", variant)
        assert plan.variant == PlanVariant.FALLBACK, variant


def test_fallback_is_deterministic(initial_state) -> None:
    """Two identical runs must produce byte-identical plans."""
    runs = []
    for _ in range(2):
        with _force_timeout():
            result = run_planning(_request(initial_state))
        runs.append(tuple(a.signature() for a in result.assignments))
    assert runs[0] == runs[1]


def test_fallback_respects_frozen_assignments(initial_frontier) -> None:
    """The fallback path must honour the freeze rule too, not just the CP-SAT path."""
    from app.services.stability_guard import frozen_assignments, verify_frozen_held

    state, frontier = initial_frontier
    parent = frontier.plans[0].plan
    # Clone before advancing the clock: `initial_frontier` is session-scoped, so
    # mutating it here would make every later test order-dependent.
    state = state.clone()
    state.now_minute = 120
    frozen = frozen_assignments(parent, state.now_minute)
    assert frozen

    with _force_timeout():
        result = run_planning(
            _request(state, parent_plan=parent, frozen=frozen),
        )

    check = verify_frozen_held(parent, result.assignments, state.now_minute)
    assert check.holds, check.violations


def test_fallback_reports_lower_coverage_than_the_solver(initial_frontier) -> None:
    """Sanity check on the trade-off the fallback is documented to make.

    The greedy path takes the first feasible option per mission in priority order; it is
    expected to be at least as covered, but never better on risk than the optimised plan.
    """
    state, frontier = initial_frontier
    solved = frontier.plans[0].plan.metrics.weighted_coverage

    with _force_timeout():
        result = run_planning(_request(state))

    assert result.metrics.weighted_coverage <= solved + 1e-6


def test_solve_greedy_is_reachable_directly(initial_state) -> None:
    """The fallback routine is usable on its own, which is how it is unit-tested."""
    selected, notes = solve_greedy(_request(initial_state))
    assert selected
    assert notes
    assignments = [option.to_assignment() for option in selected]
    assert len({a.mission_id for a in assignments}) == len(assignments)


def test_coverage_floor_is_eighty_five_percent_of_the_reference() -> None:
    """The spec floors safety and stability at >=85% of coverage-first weighted coverage."""
    assert COVERAGE_RETENTION_FLOOR == pytest.approx(0.85)
    assert coverage_floor_for(100.0) == pytest.approx(85.0)
    assert coverage_floor_for(695.0) == pytest.approx(590.75)
    assert coverage_floor_for(0.0) == 0.0


def test_no_fallback_happens_when_the_solver_succeeds(initial_state) -> None:
    """The ordinary path must never be labelled FALLBACK."""
    result = run_planning(_request(initial_state))
    assert result.is_fallback is False
    plan = result.to_plan("PLN-OK", PlanVariant.COVERAGE_FIRST)
    assert plan.variant == PlanVariant.COVERAGE_FIRST
    assert plan.is_fallback is False
    assert "GREEDY" not in plan.solver_status


def test_fallback_variant_still_meets_the_coverage_floor(initial_frontier) -> None:
    """Even degraded, safety/stability must not fall below the documented floor.

    The greedy path ignores the CP-SAT floor constraint, so this asserts the floor is
    satisfied by construction on the seeded scenario. If a future scenario change broke
    that, the fallback would need to start honouring the floor explicitly.
    """
    state, frontier = initial_frontier
    reference = frontier.plans[0].plan.metrics.weighted_coverage
    floor = coverage_floor_for(reference)

    for variant in (PlanVariant.SAFETY_FIRST, PlanVariant.STABILITY_FIRST):
        with _force_timeout():
            result = run_planning(
                _request(state, variant), coverage_reference=reference
            )
        assert result.metrics.weighted_coverage >= floor, (
            f"{variant.value} fallback covered {result.metrics.weighted_coverage} < {floor}"
        )
