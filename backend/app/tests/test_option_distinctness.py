"""The three-option frontier: three options, honestly reported when fewer exist.

The spec asks the planner to "return exactly three distinct options" and sets the
acceptance criterion "three trade-off plans are visibly different on the seeded
scenario". The first part is stronger than the second, and the gap matters.

On the seeded scenario's *initial* plan all three objectives genuinely diverge, so
three distinct options exist and this suite asserts that. After a disruption the
planner re-opens only the affected missions (the spec's own re-planning rule), which
can leave as few as one or two decisions. When every objective picks the same option
for those decisions, only one answer genuinely exists.

The behaviour tested here is that the planner reports that collapse instead of
perturbing a plan until it merely *looks* different. A perturbed plan would be
fiction: it would carry the label "STABILITY FIRST" while embodying an objective the
solver never optimised, and the Commander would be weighing a fabricated trade-off.
"""

from __future__ import annotations

import pytest

from app.domain.enums import ActorRole, PlanVariant
from app.services import disruption_monitor, stability_guard
from app.services.frontier_explorer import build_frontier

from .conftest import build_state, headers

SCRIPTED_EVENTS = ("EVENT_A", "EVENT_B", "EVENT_C", "EVENT_D")


# -- The acceptance criterion: three visibly different options on the seeded scenario -----


def test_seeded_initial_plan_yields_three_distinct_options() -> None:
    """The spec's own acceptance criterion, asserted directly."""
    frontier = build_frontier(build_state(), scenario_id="SCN-TEST", version=1)
    assert frontier.distinct_count == 3, frontier.distinct_signatures
    assert frontier.duplicate_groups == ()
    assert frontier.duplicates_by_label == {}


def test_seeded_initial_options_carry_the_exactly_three_variant_labels() -> None:
    frontier = build_frontier(build_state(), scenario_id="SCN-TEST", version=1)
    assert [built.variant for built in frontier.plans] == [
        PlanVariant.COVERAGE_FIRST,
        PlanVariant.SAFETY_FIRST,
        PlanVariant.STABILITY_FIRST,
    ]


def test_seeded_initial_options_are_visibly_different_on_the_metrics_a_user_sees() -> None:
    """Distinct assignments are necessary but not sufficient: the numbers must differ too."""
    frontier = build_frontier(build_state(), scenario_id="SCN-TEST", version=1)
    metrics = [built.plan.metrics for built in frontier.plans]
    assert len({m.weighted_coverage for m in metrics}) > 1
    assert len({m.mean_risk for m in metrics}) > 1
    assert len({m.missions_covered for m in metrics}) > 1


# -- Collapse after a disruption is reported, not hidden ---------------------------------


def _replan(event_key: str, now_minute: int = 60):  # type: ignore[no-untyped-def]
    state = build_state(now_minute=now_minute)
    parent = build_frontier(state, scenario_id="SCN-TEST", version=1).plans[0].plan
    kind, title, payload, _description = disruption_monitor.resolve_scripted_event(event_key)
    application = disruption_monitor.apply_event(
        state, kind, payload, label=title, source="test", active_plan=parent
    )
    frontier = build_frontier(
        application.state,
        scenario_id="SCN-TEST",
        version=2,
        parent_plan=parent,
        affected_mission_ids=set(application.affected_mission_ids),
    )
    return application, frontier


@pytest.mark.parametrize("event_key", SCRIPTED_EVENTS)
def test_every_replan_returns_three_options_even_when_they_coincide(event_key: str) -> None:
    """The contract is three options; distinctness is reported separately.

    Collapsing to one answer must not become collapsing to one *card* — that would hide
    from the Commander that the planner ran and what it weighed.
    """
    _application, frontier = _replan(event_key)
    assert len(frontier.plans) == 3
    assert [built.variant for built in frontier.plans] == [
        PlanVariant.COVERAGE_FIRST,
        PlanVariant.SAFETY_FIRST,
        PlanVariant.STABILITY_FIRST,
    ]


@pytest.mark.parametrize("event_key", SCRIPTED_EVENTS)
def test_a_collapse_is_always_explained(event_key: str) -> None:
    """Whenever two options are identical, a note must say so and say why."""
    _application, frontier = _replan(event_key)
    groups = frontier.duplicate_groups

    if not groups:
        assert frontier.distinct_count == 3
        return

    for group in groups:
        assert len(group) >= 2
        for label in group:
            others = [other for other in group if other != label]
            assert frontier.duplicates_by_label[label] == tuple(others)

    explaining = [note for note in frontier.notes if "identical" in note]
    assert explaining, frontier.notes
    for note in explaining:
        assert "free to be re-decided" in note
        assert str(frontier.free_mission_count) in note
        assert "rather than perturbed" in note


@pytest.mark.parametrize("event_key", SCRIPTED_EVENTS)
def test_collapsed_options_are_still_auditor_valid(event_key: str) -> None:
    """Collapsing is not an excuse to return a plan that fails the hard constraints."""
    _application, frontier = _replan(event_key)
    for built in frontier.plans:
        assert built.audit_valid, (built.label, built.audit_findings)


@pytest.mark.parametrize("event_key", SCRIPTED_EVENTS)
def test_the_collapsed_options_are_genuinely_the_same_plan(event_key: str) -> None:
    """Guard against a false positive: a reported duplicate really must be identical."""
    _application, frontier = _replan(event_key)
    by_signature: dict[tuple, list[str]] = {}
    for built, signature in zip(frontier.plans, frontier.distinct_signatures, strict=True):
        by_signature.setdefault(signature, []).append(built.label)

    reported = {label for group in frontier.duplicate_groups for label in group}
    collapsed = {label for labels in by_signature.values() if len(labels) > 1 for label in labels}
    assert reported == collapsed

    for labels in by_signature.values():
        if len(labels) == 1:
            continue
        plans = [b.plan for b in frontier.plans if b.label in labels]
        first = {a.signature() for a in plans[0].assignments}
        for other in plans[1:]:
            assert {a.signature() for a in other.assignments} == first


# -- The free set is what determines whether three answers can exist ----------------------


def test_free_mission_count_reflects_the_affected_set_not_the_whole_plan() -> None:
    """The recorded free-set size must be the real explanation offered to the user."""
    application, frontier = _replan("EVENT_A")
    _locked, free, _frozen = stability_guard.split_locked_and_free(
        application.state,
        build_frontier(
            build_state(now_minute=60), scenario_id="SCN-TEST", version=1
        ).plans[0].plan,
        set(application.affected_mission_ids),
        60,
    )
    assert frontier.free_mission_count == len(free)
    assert frontier.free_mission_count < len(application.state.missions)


# -- The API surfaces the same facts -----------------------------------------------------


def test_generate_response_reports_three_distinct_options(client) -> None:
    body = client.post(
        "/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER)
    ).json()
    assert body["distinct_variants"] == 3
    assert body["duplicate_options"] == []
    assert body["distinct_options_note"] == "Three genuinely different options."
    # With no parent plan nothing is frozen, so every mission is free to be decided.
    assert body["free_mission_count"] == 13


def test_inject_response_reports_duplicate_options_when_they_collapse(client) -> None:
    client.post("/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER))
    body = client.post(
        "/api/v1/events/inject",
        json={"scripted_event": "EVENT_A"},
        headers=headers(ActorRole.PLANNER),
    ).json()

    assert len(body["proposals"]) == 3
    # The count, the groups, and the prose must all agree with each other.
    assert body["distinct_variants"] == 3 - sum(
        len(group) - 1 for group in body["duplicate_options"]
    )
    if body["duplicate_options"]:
        assert "distinct option(s) exist" in body["distinct_options_note"]
        assert "free to be re-decided" in body["distinct_options_note"]
        assert "rather than perturbed" in body["distinct_options_note"]
        assert body["free_mission_count"] < 13
    else:
        assert body["distinct_options_note"] == "Three genuinely different options."


def test_duplicate_options_are_also_written_to_the_ledger(client) -> None:
    """The record of what the planner did must survive into the audit trail."""
    client.post("/api/v1/plans/generate", json={}, headers=headers(ActorRole.COMMANDER))
    client.post(
        "/api/v1/events/inject",
        json={"scripted_event": "EVENT_A"},
        headers=headers(ActorRole.PLANNER),
    )
    ledger = client.get("/api/v1/ledger").json()
    generations = [
        entry for entry in ledger["entries"] if entry["action"] == "PLAN_GENERATED"
    ]
    assert generations
    payload = generations[-1]["payload"]
    assert payload["distinct_variants"] >= 1