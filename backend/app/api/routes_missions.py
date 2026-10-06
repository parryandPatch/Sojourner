"""Mission list/filter routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import ScenarioDep, SessionDep
from app.repositories import scenario_repo
from app.services import frontier_explorer, state_hub

router = APIRouter(tags=["missions"])


@router.get("/missions")
def list_missions(
    session: SessionDep,
    scenario: ScenarioDep,
    priority: int | None = Query(default=None, ge=1, le=5),
    origin_hub_id: str | None = Query(default=None),
    required_class: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assigned: bool | None = Query(default=None),
) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    active = scenario_repo.active_plan(session, scenario.id)
    covered = {a.mission_id for a in (active.assignments if active else ())}
    asset_by_mission = {a.mission_id: a.asset_id for a in (active.assignments if active else ())}

    rows: list[dict] = []
    for mission in state.missions_in_priority_order():
        if priority is not None and mission.priority != priority:
            continue
        if origin_hub_id and mission.origin_hub_id != origin_hub_id:
            continue
        if required_class and mission.required_class.value != required_class.upper():
            continue
        if status and mission.status.value != status.upper():
            continue
        is_assigned = mission.id in covered
        if assigned is not None and is_assigned != assigned:
            continue
        rows.append(
            {
                **state_hub.mission_out(state, mission),
                "is_assigned": is_assigned,
                "assigned_asset_id": asset_by_mission.get(mission.id),
            }
        )

    return {
        "missions": rows,
        "total": len(state.missions),
        "matched": len(rows),
        "active_plan_id": active.id if active else None,
    }


@router.get("/missions/{mission_id}")
def get_mission(session: SessionDep, scenario: ScenarioDep, mission_id: str) -> dict:
    state = scenario_repo.load_state(session, scenario.id)
    if mission_id not in state.missions:
        raise HTTPException(status_code=404, detail=f"Unknown mission: {mission_id}")
    mission = state.mission(mission_id)

    active = scenario_repo.active_plan(session, scenario.id)
    assignment = active.assignment_by_mission.get(mission_id) if active else None
    reports = frontier_explorer.feasibility_reports(state, parent=active)
    report = reports.get(mission_id)

    return {
        **state_hub.mission_out(state, mission),
        "assignment": (
            state_hub.assignment_out(state, assignment) if assignment is not None else None
        ),
        "feasibility": {
            "feasible": report.feasible if report else False,
            "option_count": report.option_count if report else 0,
            "candidates_examined": report.candidates_examined if report else 0,
            "blocking_reason_counts": (
                {code.value: count for code, count in report.blocking_counts.items()}
                if report
                else {}
            ),
        },
    }


__all__ = ["router"]