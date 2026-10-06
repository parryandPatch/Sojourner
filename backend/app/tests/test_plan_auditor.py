"""Essential test 3: a generated CP-SAT plan passes the independent plan auditor.

The auditor deliberately re-derives every hard constraint from the mission and state
objects instead of trusting anything the planning engine recorded, so a plan that
passes it has been validated twice by two independent code paths.
"""

from __future__ import annotations

import dataclasses

from app.domain.enums import AssetStatus
from app.services.plan_auditor import audit_plan

from .conftest import build_state


def test_each_generated_variant_passes_the_auditor(initial_frontier) -> None:
    state, frontier = initial_frontier
    assert len(frontier.plans) == 3, "the frontier must offer exactly three options"

    for built in frontier.plans:
        report = audit_plan(state, built.plan.assignments)
        assert report.valid, (
            f"{built.variant.value} failed the auditor: "
            f"{report.errors} | {report.messages()}"
        )
        assert report.errors == []


def test_auditor_reports_no_errors_for_the_auditor_valid_fixture(coverage_plan, initial_state) -> None:
    report = audit_plan(initial_state, coverage_plan.assignments)
    assert report.valid
    assert report.errors == []
    # A clean audit may still carry informational or advisory messages.
    assert isinstance(report.messages(), list)


def test_auditor_catches_an_overlapping_asset(coverage_plan, initial_state) -> None:
    """Force two assignments onto one asset at the same time and confirm it is rejected."""
    assignments = list(coverage_plan.assignments)
    assert len(assignments) >= 2

    victim = dataclasses.replace(
        assignments[1],
        asset_id=assignments[0].asset_id,
        crew_ids=tuple(f"CRW-FAKE-{i}" for i in range(len(assignments[1].crew_ids))),
        # Keep the two sorties temporally concurrent so the finding is an overlap, not a
        # disjoint reuse of the same asset (which is legal).
        takeoff_minute=assignments[0].takeoff_minute,
        landing_minute=assignments[0].landing_minute,
        return_minute=assignments[0].return_minute,
    )
    report = audit_plan(initial_state, [*assignments[:1], victim, *assignments[2:]])
    assert not report.valid
    assert report.errors, "an asset double-booking must be an auditor error, not a warning"
    assert any("OVERLAP" in f.code or "ASSET" in f.code for f in report.errors)


def test_auditor_catches_a_crew_double_booking(coverage_plan, initial_state) -> None:
    """The same crew member on two concurrent sorties is an error even with distinct assets."""
    assignments = list(coverage_plan.assignments)
    assert len(assignments) >= 2

    victim = dataclasses.replace(
        assignments[1],
        crew_ids=assignments[0].crew_ids,
        takeoff_minute=assignments[0].takeoff_minute,
        landing_minute=assignments[0].landing_minute,
        return_minute=assignments[0].return_minute,
    )
    report = audit_plan(initial_state, [*assignments[:1], victim, *assignments[2:]])
    assert not report.valid
    assert report.errors
    assert any("CREW" in f.code for f in report.errors)


def test_auditor_catches_using_an_unavailable_asset(coverage_plan, initial_state) -> None:
    state = build_state()
    target = state.asset(coverage_plan.assignments[0].asset_id)
    state.assets[target.id] = dataclasses.replace(
        target, status=AssetStatus.UNAVAILABLE, available_from_minute=state.horizon_minutes
    )
    report = audit_plan(state, coverage_plan.assignments)
    assert not report.valid
    assert report.errors


def test_auditor_catches_a_mission_time_window_violation(coverage_plan, initial_state) -> None:
    """Stretching a mission's latest_end so its current assignment no longer fits."""
    state = build_state()
    first = coverage_plan.assignments[0]
    mission = state.mission(first.mission_id)
    state.missions[mission.id] = dataclasses.replace(mission, latest_end=first.takeoff_minute - 1)
    report = audit_plan(state, coverage_plan.assignments)
    assert not report.valid
    assert report.errors


def test_auditor_catches_a_payload_shortfall(coverage_plan, initial_state) -> None:
    state = build_state()
    first = next(a for a in coverage_plan.assignments if a.payload_category is not None)
    mission = state.mission(first.mission_id)
    state.stock = [
        item
        for item in state.stock
        if not (item.hub_id == mission.origin_hub_id and item.category == first.payload_category)
    ]
    report = audit_plan(state, coverage_plan.assignments)
    assert not report.valid
    assert report.errors


def test_validity_is_independent_of_the_number_of_warnings(coverage_plan, initial_state) -> None:
    """Validity is decided by errors alone; warnings are advisory.

    `AuditReport.valid` is ``not errors()``, so a plan carrying warnings is still
    valid and would still be activatable. This pins that contract so a future change
    cannot silently tighten it.
    """
    report = audit_plan(initial_state, coverage_plan.assignments)
    assert report.valid is (len(report.errors) == 0)
    # `errors` / `warnings` are properties on AuditReport, not methods.
    assert report.warnings == [f for f in report.findings if f.severity == "WARNING"]
    assert all(f.severity in {"ERROR", "WARNING"} for f in report.findings)


def test_every_finding_carries_a_code_and_a_readable_message(coverage_plan, initial_state) -> None:
    """Findings are the only thing the UI renders, so both fields must always be present."""
    report = audit_plan(initial_state, coverage_plan.assignments)
    for finding in report.findings:
        assert finding.code
        assert finding.message
        assert finding.severity in {"ERROR", "WARNING"}
