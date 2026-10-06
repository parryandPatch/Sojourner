"""Explanation service.

Every sentence is rendered from stored structured reason codes by a template. No
explanation is invented and no LLM is involved in producing these strings.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import MissionKind, ReasonCode
from app.domain.schemas import (
    Assignment,
    Mission,
    OperationalState,
    Plan,
)
from app.services.feasibility_engine import FeasibilityReport

# Human-readable labels for each reason code.
REASON_LABELS: dict[ReasonCode, str] = {
    ReasonCode.CLASS_MISMATCH: "asset class does not match the mission requirement",
    ReasonCode.ASSET_UNAVAILABLE: "asset is unavailable or based at another hub",
    ReasonCode.ENDURANCE_EXCEEDED: "transit plus station time exceeds endurance or range",
    ReasonCode.RANGE_EXCEEDED: "round trip exceeds the asset range",
    ReasonCode.PAYLOAD_SHORTFALL: "origin hub does not hold enough payload stock",
    ReasonCode.CREW_QUALIFICATION_MISSING: "no qualified crew holds the required role set",
    ReasonCode.CREW_DUTY_LIMIT_EXCEEDED: "sortie would exceed a crew duty limit",
    ReasonCode.CREW_REST_NOT_MET: "required crew rest period is not met before departure",
    ReasonCode.TIME_WINDOW_MISSED: "sortie cannot complete inside the mission window",
    ReasonCode.TIME_OVERLAP: "asset or crew is already committed during that period",
    ReasonCode.HUB_CLOSED_AT_DEPARTURE: "origin hub is closed at departure or return",
    ReasonCode.HUB_CAPACITY_EXCEEDED: "origin hub runway capacity for that slot is full",
    ReasonCode.RESTRICTION_ZONE_CROSSED: "route crosses an active hard-restriction zone",
    ReasonCode.RISK_EXCEEDS_MISSION_MAX: "combined risk would exceed the mission maximum",
}

# Ordering used when reporting the most common blocking reasons.
BLOCKING_PRIORITY: tuple[ReasonCode, ...] = (
    ReasonCode.RESTRICTION_ZONE_CROSSED,
    ReasonCode.HUB_CLOSED_AT_DEPARTURE,
    ReasonCode.ASSET_UNAVAILABLE,
    ReasonCode.CLASS_MISMATCH,
    ReasonCode.ENDURANCE_EXCEEDED,
    ReasonCode.CREW_DUTY_LIMIT_EXCEEDED,
    ReasonCode.CREW_REST_NOT_MET,
    ReasonCode.TIME_OVERLAP,
    ReasonCode.PAYLOAD_SHORTFALL,
    ReasonCode.CREW_QUALIFICATION_MISSING,
    ReasonCode.TIME_WINDOW_MISSED,
    ReasonCode.HUB_CAPACITY_EXCEEDED,
    ReasonCode.RISK_EXCEEDS_MISSION_MAX,
)

KIND_WORDS: dict[MissionKind, str] = {
    MissionKind.LOGISTICS: "logistics",
    MissionKind.RECONNAISSANCE: "reconnaissance",
    MissionKind.PATROL: "patrol",
    MissionKind.SURVEY: "survey",
    MissionKind.SUPPORT: "support",
}


@dataclass(frozen=True, slots=True)
class AssignmentExplanation:
    mission_id: str
    assignment_id: str
    text: str
    reason_codes: tuple[ReasonCode, ...]


def explain_assignment(
    state: OperationalState,
    mission: Mission,
    assignment: Assignment,
    *,
    alternatives: list[Assignment] | None = None,
) -> AssignmentExplanation:
    """Render the 'why this asset' sentence for a chosen assignment."""
    asset = state.assets.get(assignment.asset_id)
    crew = [state.crews[c] for c in assignment.crew_ids if c in state.crews]
    asset_label = asset.label if asset else assignment.asset_id

    clauses: list[str] = [
        f"meets the {mission.required_class.value} class requirement for {KIND_WORDS.get(mission.mission_kind, mission.mission_kind.value)} mission {mission.id}"
    ]

    if ReasonCode.EARLIEST_READY in assignment.reason_codes:
        ready_clause = f"is the earliest-ready {asset.class_.value} based at {mission.origin_hub_id}"
    else:
        ready_clause = f"is available and based at {mission.origin_hub_id}"

    alternatives = alternatives or []
    rival = next(
        (a for a in alternatives if a.asset_id != assignment.asset_id and a.mission_id == mission.id),
        None,
    )
    if rival is not None:
        delta = assignment.takeoff_minute - rival.takeoff_minute
        if delta < 0:
            ready_clause += f" and is ready {abs(delta)} minutes earlier than {rival.asset_id}"
        risk_delta = assignment.risk - rival.risk
        if risk_delta < -0.005:
            ready_clause += f", with lower combined risk ({assignment.risk:.2f} vs {rival.risk:.2f})"
        elif risk_delta > 0.005:
            ready_clause += f", accepting higher combined risk ({assignment.risk:.2f} vs {rival.risk:.2f})"
        else:
            ready_clause += f", with equal combined risk ({assignment.risk:.2f})"
    clauses.append(ready_clause)

    if crew:
        crew_desc = " and ".join(c.label.split(" (")[0] for c in crew)
        clauses.append(f"crewed by {crew_desc}")
    breakdown = assignment.risk_breakdown
    clauses.append(
        f"combined risk {breakdown.combined:.2f} "
        f"(hazard {breakdown.hazard_risk:.2f}, weather {breakdown.weather_risk:.2f}, "
        f"readiness {breakdown.readiness_risk:.2f}) against a mission maximum of {mission.maximum_risk:.2f}"
    )
    clauses.append(
        f"departs {state.minute_to_iso(assignment.takeoff_minute)} and returns at "
        f"{state.minute_to_iso(assignment.return_minute)}"
    )

    return AssignmentExplanation(
        mission_id=mission.id,
        assignment_id=assignment.id,
        text=f"{asset_label} was selected because it " + ", ".join(clauses) + ".",
        reason_codes=assignment.reason_codes,
    )


def explain_unassigned(
    state: OperationalState,
    mission: Mission,
    report: FeasibilityReport,
) -> str:
    """Render the 'why not assigned' sentence from blocking reason counts."""
    if report.feasible:  # pragma: no cover - defensive
        return f"Mission {mission.id} has valid options but none were selected by this variant."

    ordered = [code for code in BLOCKING_PRIORITY if report.blocking_counts.get(code, 0) > 0]
    parts = [f"{report.blocking_counts[code]} candidate option(s) {REASON_LABELS[code]}" for code in ordered]

    if parts:
        body = "; ".join(parts)
        sentence = f"Mission {mission.id} could not be assigned: {body}."
    else:
        sentence = (
            f"Mission {mission.id} could not be assigned: no candidate satisfied every hard constraint "
            f"across {report.candidates_examined} assets examined."
        )
    if report.notes:
        sentence += " " + " ".join(report.notes)
    return sentence


def explain_plan_variant(
    variant_label: str,
    metrics,
    *,
    reference_metrics=None,
    parent: Plan | None = None,
    stability_summary: str = "",
    fallback: bool = False,
) -> list[str]:
    """Key differences for one trade-off card."""
    reasons: list[str] = []
    reasons.append(
        f"{variant_label}: {metrics.missions_covered} of {metrics.missions_total} missions covered, "
        f"priority-weighted coverage {metrics.weighted_coverage:.0f} "
        f"({metrics.coverage_ratio * 100:.0f}% of achievable weight), "
        f"mean risk {metrics.mean_risk:.2f}, max risk {metrics.max_risk:.2f}."
    )
    if fallback:
        reasons.append("Labelled FALLBACK: the solver did not return a feasible solution in the time budget.")
    if parent is not None:
        reasons.append(f"{metrics.changed_assignments} assignment(s) changed relative to the active plan.")
    if stability_summary:
        reasons.append(stability_summary)
    if reference_metrics is not None and reference_metrics is not metrics:
        delta = metrics.weighted_coverage - reference_metrics.weighted_coverage
        risk_delta = metrics.mean_risk - reference_metrics.mean_risk
        reasons.append(
            f"Versus coverage-first: weighted coverage {delta:+.0f}, mean risk {risk_delta:+.2f}."
        )
    return reasons


def explain_revision(
    state: OperationalState,
    parent: Plan,
    candidate: Plan,
    *,
    trigger_label: str,
    stability_summary: str,
    frozen_count: int,
) -> str:
    """Render the single-paragraph revision sentence required by the spec."""
    parent_by_mission = parent.assignment_by_mission
    candidate_by_mission = candidate.assignment_by_mission

    replaced_assets: list[str] = []
    dropped: list[str] = []
    added: list[str] = []
    for mission_id, assignment in sorted(candidate_by_mission.items()):
        previous = parent_by_mission.get(mission_id)
        if previous is None:
            added.append(f"{mission_id} on {assignment.asset_id}")
        elif previous.asset_id != assignment.asset_id:
            replaced_assets.append(f"{mission_id} from {previous.asset_id} to {assignment.asset_id}")
    for mission_id in sorted(parent_by_mission):
        if mission_id not in candidate_by_mission:
            dropped.append(mission_id)

    parts: list[str] = [f"{trigger_label} triggered this revision."]

    if replaced_assets:
        parts.append("It replaces " + "; ".join(replaced_assets) + ".")
    if dropped:
        parts.append(f"Missions {', '.join(dropped)} are no longer covered.")
    if added:
        parts.append(f"Missions {', '.join(added)} are newly covered.")

    parent_p1 = {m for m in parent_by_mission if state.missions[m].priority == 1}
    candidate_p1 = {m for m in candidate_by_mission if state.missions[m].priority == 1}
    if parent_p1 <= candidate_p1:
        parts.append("It preserves all P1 missions.")
    else:
        lost = ", ".join(sorted(parent_p1 - candidate_p1))
        parts.append(f"It does not preserve P1 coverage for: {lost}.")

    risk_delta = candidate.metrics.mean_risk - parent.metrics.mean_risk
    direction = "increases" if risk_delta >= 0 else "reduces"
    parts.append(f"It {direction} mean risk by {abs(risk_delta):.2f}")
    parts.append(
        f"and changes {candidate.metrics.changed_assignments} assignment(s) relative to the active plan."
    )
    parts.append(stability_summary or f"No frozen assignments were present ({frozen_count} held).")
    return " ".join(parts)


def render_plan_explanations(state: OperationalState, plan: Plan, reports: dict[str, FeasibilityReport]) -> list[str]:
    """Sentence list for the planning screen."""
    lines: list[str] = []
    by_mission = plan.assignment_by_mission
    for mission in state.missions_in_priority_order():
        assignment = by_mission.get(mission.id)
        if assignment is not None:
            alternatives = list(by_mission.values())
            lines.append(explain_assignment(state, mission, assignment, alternatives=alternatives).text)
        else:
            report = reports.get(mission.id)
            if report is not None:
                lines.append(explain_unassigned(state, mission, report))
    return lines


def reason_code_table() -> list[dict]:
    """Structured reason-code dictionary, exposed for the UI."""
    return [
        {"code": code.value, "label": REASON_LABELS.get(code, "")}
        for code in ReasonCode
        if code in REASON_LABELS
    ]