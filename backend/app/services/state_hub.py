"""State hub.

Fuses the persisted overlays into one live operational state and renders every
API-facing projection from it. Nothing here plans anything: this module only reads,
shapes and labels. Keeping it separate from the planning engines means the command deck
can poll the overview without touching the solver.

Persistent safety labels are defined once here and echoed on every response.
"""

from __future__ import annotations

from app.domain.enums import (
    ActorRole,
    AssetStatus,
    HubStatus,
    PlanStatus,
    ProposalStatus,
    ZoneSeverityBand,
)
from app.domain.schemas import (
    GRID_CELL,
    MAP_HEIGHT_UNITS,
    MAP_WIDTH_UNITS,
    Asset,
    Assignment,
    AssignmentOut,
    CrewMember,
    HazardZone,
    Hub,
    KpiTile,
    Mission,
    OperationalState,
    Plan,
    PlanMetricsOut,
    Proposal,
    RiskBreakdown,
    StockItem,
    WeatherWindow,
)
from app.services.explanation_service import REASON_LABELS
from app.services.plan_auditor import audit_plan
from app.services.readiness_estimator import estimate_readiness

SYNTHETIC_NOTICE = "SYNTHETIC DEMONSTRATION DATA"
ADVISORY_NOTICE = "ADVISORY ONLY — HUMAN APPROVAL REQUIRED"
APPROVAL_WARNING = "Approval activates this synthetic plan version."
READINESS_METHOD = "synthetic heuristic"

RISK_FORMULA = "1 - (1 - hazard) * (1 - weather) * (1 - readiness)"


# --------------------------------------------------------------------------------------
# Banding helpers (labels, never colour-only decisions)
# --------------------------------------------------------------------------------------


def severity_band(severity: float) -> ZoneSeverityBand:
    if severity >= 0.8:
        return ZoneSeverityBand.RESTRICTED
    if severity >= 0.5:
        return ZoneSeverityBand.ELEVATED
    return ZoneSeverityBand.ADVISORY


def hub_status(state: OperationalState, hub: Hub) -> HubStatus:
    from app.services.risk_engine import hub_blocking_window

    if hub_blocking_window(state, hub.id, state.now_minute) is not None:
        return HubStatus.CLOSED
    if any(w.overlaps(state.now_minute, state.now_minute) and not w.blocking
           for w in state.weather_for_hub(hub.id)):
        return HubStatus.RESTRICTED
    return HubStatus.OPEN


def readiness_band(probability: float) -> str:
    if probability >= 0.85:
        return "HIGH"
    if probability >= 0.6:
        return "MEDIUM"
    return "LOW"


# --------------------------------------------------------------------------------------
# Entity projections
# --------------------------------------------------------------------------------------


def hub_out(state: OperationalState, hub: Hub) -> dict:
    return {
        "id": hub.id,
        "name": hub.name,
        "x": hub.x,
        "y": hub.y,
        "runway_capacity_per_slot": hub.runway_capacity_per_slot,
        "status": hub_status(state, hub),
        "slot_minutes": hub.slot_minutes,
    }


def asset_out(asset: Asset, assigned_mission_id: str | None = None) -> dict:
    readiness = estimate_readiness(
        asset.maintenance_hours_since, asset.recent_fault_count, asset.status
    )
    return {
        "id": asset.id,
        "label": asset.label,
        "class_": asset.class_,
        "home_hub_id": asset.home_hub_id,
        "status": asset.status,
        "available_from_minute": asset.available_from_minute,
        "cruise_speed": asset.cruise_speed,
        "endurance_minutes": asset.endurance_minutes,
        "range_units": asset.range_units,
        "maintenance_hours_since": round(asset.maintenance_hours_since, 2),
        "recent_fault_count": asset.recent_fault_count,
        "readiness_probability": readiness.probability,
        "readiness_low": readiness.low,
        "readiness_high": readiness.high,
        "readiness_method": READINESS_METHOD,
        "readiness_band": readiness_band(readiness.probability),
        "assigned_mission_id": assigned_mission_id,
    }


def crew_out(crew: CrewMember) -> dict:
    return {
        "id": crew.id,
        "label": crew.label,
        "roles": list(crew.roles),
        "home_hub_id": crew.home_hub_id,
        "qualified_classes": list(crew.qualified_classes),
        "available_from_minute": crew.available_from_minute,
        "duty_minutes_used": crew.duty_minutes_used,
        "duty_limit_minutes": crew.duty_limit_minutes,
        "duty_remaining_minutes": max(0, crew.duty_limit_minutes - crew.duty_minutes_used),
        "last_duty_end_minute": crew.last_duty_end_minute,
        "min_rest_minutes": crew.min_rest_minutes,
    }


def stock_out(item: StockItem) -> dict:
    return {"hub_id": item.hub_id, "category": item.category, "quantity": item.quantity}


def mission_out(state: OperationalState, mission: Mission) -> dict:
    return {
        "id": mission.id,
        "title": mission.title,
        "mission_kind": mission.mission_kind,
        "priority": mission.priority,
        "priority_weight": mission.priority_weight,
        "required_class": mission.required_class,
        "required_payload_category": mission.required_payload_category,
        "required_asset_count": mission.required_asset_count,
        "origin_hub_id": mission.origin_hub_id,
        "objective_x": mission.objective_x,
        "objective_y": mission.objective_y,
        "earliest_start": mission.earliest_start,
        "latest_end": mission.latest_end,
        "station_duration_minutes": mission.station_duration_minutes,
        "maximum_risk": mission.maximum_risk,
        "required_crew_roles": list(mission.required_crew_roles),
        "status": mission.status,
        "region_id": mission.region_id,
        "description": mission.description,
        "earliest_start_iso": state.minute_to_iso(mission.earliest_start),
        "latest_end_iso": state.minute_to_iso(mission.latest_end),
    }


def hazard_out(zone: HazardZone) -> dict:
    return {
        "id": zone.id,
        "name": zone.name,
        "cells": [list(cell) for cell in sorted(zone.cells)],
        "severity": round(zone.severity, 3),
        "band": severity_band(zone.severity),
        "blocking": zone.blocking,
        "active_start": zone.active_start,
        "active_end": zone.active_end,
        "source_event_id": zone.source_event_id,
    }


def weather_out(window: WeatherWindow) -> dict:
    return {
        "id": window.id,
        "name": window.name,
        "hub_id": window.hub_id,
        "region_id": window.region_id,
        "start": window.start,
        "end": window.end,
        "severity": round(window.severity, 3),
        "restriction_type": window.restriction_type,
        "blocking": window.blocking,
        "source_event_id": window.source_event_id,
    }


def risk_breakdown_out(breakdown: RiskBreakdown) -> dict:
    return {
        "hazard_risk": round(breakdown.hazard_risk, 4),
        "weather_risk": round(breakdown.weather_risk, 4),
        "readiness_risk": round(breakdown.readiness_risk, 4),
        "combined": round(breakdown.combined, 4),
        "formula": RISK_FORMULA,
    }


def assignment_out(state: OperationalState, assignment: Assignment, *, explanation: str = "") -> dict:
    return {
        "id": assignment.id,
        "mission_id": assignment.mission_id,
        "asset_id": assignment.asset_id,
        "crew_ids": list(assignment.crew_ids),
        "payload_category": assignment.payload_category,
        "payload_units": assignment.payload_units,
        "takeoff_minute": assignment.takeoff_minute,
        "landing_minute": assignment.landing_minute,
        "return_minute": assignment.return_minute,
        "transit_minutes": assignment.transit_minutes,
        "takeoff_iso": state.minute_to_iso(assignment.takeoff_minute),
        "landing_iso": state.minute_to_iso(assignment.landing_minute),
        "return_iso": state.minute_to_iso(assignment.return_minute),
        "risk": round(assignment.risk, 4),
        "risk_breakdown": risk_breakdown_out(assignment.risk_breakdown),
        "status": assignment.status,
        "is_frozen": assignment.is_frozen,
        "reason_codes": list(assignment.reason_codes),
        "explanation": explanation,
    }


def metrics_out(metrics) -> dict:
    return PlanMetricsOut(
        missions_total=metrics.missions_total,
        missions_covered=metrics.missions_covered,
        p1_covered=metrics.p1_covered,
        p1_total=metrics.p1_total,
        weighted_coverage=metrics.weighted_coverage,
        coverage_ratio=metrics.coverage_ratio,
        mean_risk=metrics.mean_risk,
        max_risk=metrics.max_risk,
        changed_assignments=metrics.changed_assignments,
        frozen_assignments=metrics.frozen_assignments,
        risk_savings_vs_coverage_first=metrics.risk_savings_vs_coverage_first,
    )


def plan_out(
    state: OperationalState,
    plan: Plan,
    *,
    explanations: dict[str, str] | None = None,
    notes: list[str] | None = None,
    audit: bool = True,
) -> dict:
    """Render a plan for the UI, always re-audited before it is shown."""
    explanations = explanations or {}
    report = audit_plan(state, plan.assignments) if audit else None
    covered = {a.mission_id for a in plan.assignments}
    return {
        "id": plan.id,
        "version": plan.version,
        "status": plan.status,
        "variant": plan.variant,
        "is_fallback": plan.is_fallback,
        "solver_status": plan.solver_status,
        "solve_time_ms": plan.solve_time_ms,
        "created_at": plan.created_at,
        "parent_plan_id": plan.parent_plan_id,
        "approved_at": plan.approved_at,
        "approved_by": plan.approved_by,
        "metrics": metrics_out(plan.metrics),
        "assignments": [
            assignment_out(state, a, explanation=explanations.get(a.mission_id, ""))
            for a in sorted(plan.assignments, key=lambda x: (x.takeoff_minute, x.mission_id))
        ],
        "unassigned_mission_ids": sorted(
            m.id for m in state.missions_in_priority_order() if m.id not in covered
        ),
        "auditor_valid": report.valid if report else True,
        "auditor_findings": report.messages() if report else [],
        "notes": list(notes or []),
    }


def proposal_out(state: OperationalState, proposal: Proposal) -> dict:
    plan_metrics = proposal.metrics or {}
    return {
        "id": proposal.id,
        "parent_plan_id": proposal.parent_plan_id,
        "trigger_event_id": proposal.trigger_event_id,
        "plan_id": proposal.plan_id,
        "rank": proposal.rank,
        "label": proposal.label,
        "is_fallback": proposal.is_fallback,
        "status": proposal.status,
        "created_at": proposal.created_at,
        "decided_at": proposal.decided_at,
        "decided_by": proposal.decided_by,
        "decision_note": proposal.decision_note,
        "metrics": plan_metrics,
        "diff": proposal.diff.model_dump(),
        "explanations": list(proposal.explanations),
        "key_reasons": list(proposal.key_reasons),
        "approval_warning": APPROVAL_WARNING,
    }


# --------------------------------------------------------------------------------------
# Map projection
# --------------------------------------------------------------------------------------


def map_payload(state: OperationalState, active_plan: Plan | None) -> dict:
    assignments = active_plan.assignments if active_plan else ()
    assigned_mission_by_asset = {a.asset_id: a.mission_id for a in assignments}
    airborne_assets = {
        a.asset_id for a in assignments if a.takeoff_minute <= state.now_minute < a.return_minute
    }

    return {
        "width_units": MAP_WIDTH_UNITS,
        "height_units": MAP_HEIGHT_UNITS,
        "grid_cell": GRID_CELL,
        "now_minute": state.now_minute,
        "hubs": [hub_out(state, hub) for hub in sorted(state.hubs.values(), key=lambda h: h.id)],
        "assets": [
            {
                **asset_out(asset, assigned_mission_by_asset.get(asset.id)),
                "x": state.hub(asset.home_hub_id).x,
                "y": state.hub(asset.home_hub_id).y,
                "is_airborne": asset.id in airborne_assets,
            }
            for asset in sorted(state.assets.values(), key=lambda a: a.id)
        ],
        "missions": [
            {
                **mission_out(state, mission),
                "assigned_asset_id": next(
                    (a.asset_id for a in assignments if a.mission_id == mission.id), None
                ),
            }
            for mission in state.missions_in_priority_order()
        ],
        "hazard_zones": [hazard_out(z) for z in state.hazards],
        "weather_windows": [weather_out(w) for w in state.weather],
        "assignments": [
            {
                "mission_id": a.mission_id,
                "asset_id": a.asset_id,
                "crew_ids": list(a.crew_ids),
                "takeoff_minute": a.takeoff_minute,
                "return_minute": a.return_minute,
                "is_frozen": a.takeoff_minute <= state.now_minute,
                "risk": round(a.risk, 4),
            }
            for a in sorted(assignments, key=lambda x: x.takeoff_minute)
        ],
        "legend": [
            {"key": "hub", "label": "Fictional hub"},
            {"key": "asset_available", "label": "Asset available"},
            {"key": "asset_limited", "label": "Asset limited"},
            {"key": "asset_unavailable", "label": "Asset unavailable"},
            {"key": "airborne", "label": "Airborne (frozen assignment)"},
            {"key": "zone_advisory", "label": "Hazard zone: advisory"},
            {"key": "zone_elevated", "label": "Hazard zone: elevated"},
            {"key": "zone_restricted", "label": "Hazard zone: restricted / blocking"},
            {"key": "weather", "label": "Weather restriction window"},
        ],
    }


# --------------------------------------------------------------------------------------
# Command-deck KPIs
# --------------------------------------------------------------------------------------


def readiness_alerts(state: OperationalState, limit: int = 6) -> list[dict]:
    alerts: list[dict] = []
    for asset in sorted(state.assets.values(), key=lambda a: a.id):
        readiness = estimate_readiness(
            asset.maintenance_hours_since, asset.recent_fault_count, asset.status
        )
        if asset.status == AssetStatus.UNAVAILABLE:
            alerts.append(
                {
                    "asset_id": asset.id,
                    "label": asset.label,
                    "severity": "HIGH",
                    "message": (
                        f"{asset.id} is UNAVAILABLE from minute {asset.available_from_minute} "
                        f"after {asset.recent_fault_count} synthetic fault(s)."
                    ),
                    "readiness_probability": readiness.probability,
                }
            )
        elif readiness.probability < 0.7:
            alerts.append(
                {
                    "asset_id": asset.id,
                    "label": asset.label,
                    "severity": "MEDIUM",
                    "message": (
                        f"{asset.id} synthetic readiness {readiness.probability:.2f} "
                        f"(band {readiness.low:.2f}-{readiness.high:.2f}) after "
                        f"{asset.maintenance_hours_since:.1f} maintenance hours."
                    ),
                    "readiness_probability": readiness.probability,
                }
            )
    return alerts[:limit]


def kpi_tiles(
    state: OperationalState,
    active_plan: Plan | None,
    *,
    pending_proposals: int = 0,
    chain_status: str = "VALID",
) -> list[dict]:
    metrics = active_plan.metrics if active_plan else None
    alerts = readiness_alerts(state)
    covered = f"{metrics.missions_covered}/{metrics.missions_total}" if metrics else "0/0"
    p1 = f"{metrics.p1_covered}/{metrics.p1_total}" if metrics else "0/0"
    mean_risk = f"{metrics.mean_risk:.2f}" if metrics else "Not measured"
    max_risk = f"{metrics.max_risk:.2f}" if metrics else "Not measured"

    return [
        KpiTile(
            key="active_plan",
            label="Active plan",
            value=active_plan.id if active_plan else "None approved",
            detail=(
                f"variant {active_plan.variant.value}" if active_plan else "generate a plan set to begin"
            ),
            tone="neutral",
        ).model_dump(),
        KpiTile(
            key="covered_missions",
            label="Covered missions",
            value=covered,
            detail=(
                f"weighted coverage {metrics.weighted_coverage:.0f}" if metrics else "no active plan"
            ),
            tone="positive" if metrics and metrics.missions_covered == metrics.missions_total else "neutral",
        ).model_dump(),
        KpiTile(
            key="p1_coverage",
            label="P1 coverage",
            value=p1,
            detail="priority 1 missions covered / total",
            tone="positive" if metrics and metrics.p1_covered == metrics.p1_total else "warning",
        ).model_dump(),
        KpiTile(
            key="mean_risk",
            label="Mean risk",
            value=mean_risk,
            detail=f"max {max_risk} (synthetic 3-component score)",
            tone="neutral",
        ).model_dump(),
        KpiTile(
            key="readiness_alerts",
            label="Readiness alerts",
            value=str(len(alerts)),
            detail=f"{READINESS_METHOD}, not predictive ML",
            tone="warning" if alerts else "positive",
        ).model_dump(),
        KpiTile(
            key="pending_proposals",
            label="Pending proposals",
            value=str(pending_proposals),
            detail="commander approval required before activation",
            tone="warning" if pending_proposals else "neutral",
        ).model_dump(),
        KpiTile(
            key="ledger",
            label="Ledger chain",
            value=chain_status,
            detail="SHA-256 hash chain of recorded decisions",
            tone="positive" if chain_status == "VALID" else "critical",
        ).model_dump(),
    ]


def counts(state: OperationalState) -> dict[str, int]:
    return {
        "hubs": len(state.hubs),
        "assets": len(state.assets),
        "crew": len(state.crews),
        "missions": len(state.missions),
        "assignable_missions": len(state.assignable_missions()),
        "hazard_zones": len(state.hazards),
        "weather_windows": len(state.weather),
        "events": len(state.events),
        "stock_lines": len(state.stock),
    }


def timeline_rows(state: OperationalState, active_plan: Plan | None) -> list[dict]:
    """Lightweight Gantt rows for the command deck timeline."""
    if active_plan is None:
        return []
    rows: list[dict] = []
    for assignment in sorted(active_plan.assignments, key=lambda a: (a.takeoff_minute, a.mission_id)):
        mission = state.missions.get(assignment.mission_id)
        if mission is None:  # pragma: no cover - defensive
            continue
        rows.append(
            {
                "mission_id": assignment.mission_id,
                "mission_title": mission.title,
                "priority": mission.priority,
                "asset_id": assignment.asset_id,
                "crew_ids": list(assignment.crew_ids),
                "start_minute": assignment.takeoff_minute,
                "end_minute": assignment.return_minute,
                "start_iso": state.minute_to_iso(assignment.takeoff_minute),
                "end_iso": state.minute_to_iso(assignment.return_minute),
                "risk": round(assignment.risk, 4),
                "phase": (
                    "AIRBORNE"
                    if assignment.takeoff_minute <= state.now_minute < assignment.return_minute
                    else ("DEPARTED" if state.now_minute >= assignment.return_minute else "PLANNED")
                ),
                "is_frozen": assignment.takeoff_minute <= state.now_minute,
            }
        )
    return rows


def reason_code_dictionary() -> list[dict]:
    return [
        {"code": code.value, "label": label} for code, label in REASON_LABELS.items()
    ]


def role_permissions() -> dict[str, list[str]]:
    return {
        ActorRole.PLANNER.value: [
            "view state",
            "generate plans",
            "run what-if simulation",
            "inject synthetic events",
            "view proposals",
        ],
        ActorRole.COMMANDER.value: [
            "view state",
            "generate plans",
            "run what-if simulation",
            "inject synthetic events",
            "view proposals",
            "approve proposals",
            "reject proposals",
            "reset scenario",
        ],
        ActorRole.MAINTAINER.value: [
            "view state",
            "update synthetic asset condition (controlled demo action)",
        ],
        ActorRole.OBSERVER.value: ["read-only access"],
    }


def can_approve(role: ActorRole) -> bool:
    return role == ActorRole.COMMANDER


def can_write(role: ActorRole) -> bool:
    """Mirrors ``require_write_access``: OBSERVER is read-only."""
    return role != ActorRole.OBSERVER


def role_matrix() -> dict[str, dict]:
    """Server-published capability matrix.

    The frontend renders its role selector from this rather than hard-coding the rules,
    so the UI and the API guard cannot drift apart. It is a display convenience only:
    ``require_commander`` / ``require_write_access`` remain the actual enforcement.
    """
    return {
        role.value: {
            "can_view": True,
            "can_write": can_write(role),
            "can_generate_plans": can_write(role),
            "can_inject_events": can_write(role),
            "can_run_what_if": can_write(role),
            "can_approve": can_approve(role),
            "capabilities": role_permissions().get(role.value, []),
        }
        for role in ActorRole
    }


def plan_status_label(status: PlanStatus) -> str:
    return {
        PlanStatus.DRAFT: "Draft (not active)",
        PlanStatus.PROPOSED: "Proposed (awaiting commander approval)",
        PlanStatus.ACTIVE: "Active",
        PlanStatus.SUPERSEDED: "Superseded",
        PlanStatus.REJECTED: "Rejected",
    }[status]


def proposal_status_label(status: ProposalStatus) -> str:
    return {
        ProposalStatus.PENDING: "Awaiting commander decision",
        ProposalStatus.APPROVED: "Approved and activated",
        ProposalStatus.REJECTED: "Rejected by commander",
        ProposalStatus.SUPERSEDED: "Superseded by a newer proposal set",
    }[status]


__all__ = [
    "ADVISORY_NOTICE",
    "APPROVAL_WARNING",
    "RISK_FORMULA",
    "SYNTHETIC_NOTICE",
    "AssignmentOut",
    "asset_out",
    "can_approve",
    "can_write",
    "counts",
    "crew_out",
    "hazard_out",
    "hub_out",
    "kpi_tiles",
    "map_payload",
    "mission_out",
    "plan_out",
    "plan_status_label",
    "proposal_out",
    "proposal_status_label",
    "readiness_alerts",
    "readiness_band",
    "reason_code_dictionary",
    "role_matrix",
    "role_permissions",
    "stock_out",
    "timeline_rows",
    "weather_out",
]