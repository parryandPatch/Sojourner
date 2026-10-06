"""Enumerations for the SOJOURNER synthetic domain.

Every value here describes a fictional, synthetic demonstration environment.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):  # noqa: UP042 - deliberately not enum.StrEnum
    """str-backed enum so values serialise cleanly to JSON.

    Hand-rolled rather than ``enum.StrEnum`` (which Ruff suggests) so the explicit
    ``__str__`` keeps ``f"{member}"`` and ``str(member)`` returning the bare value on
    every supported interpreter, rather than inheriting version-specific behaviour.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class AssetStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    LIMITED = "LIMITED"
    UNAVAILABLE = "UNAVAILABLE"
    IN_USE = "IN_USE"


class AssetClass(StrEnum):
    SCOUT = "SCOUT"
    TRANSPORT = "TRANSPORT"
    PATROL = "PATROL"
    UTILITY = "UTILITY"


class PayloadCategory(StrEnum):
    MEDICAL = "MEDICAL"
    SUPPLY = "SUPPLY"
    SENSOR_KIT = "SENSOR_KIT"


class MissionStatus(StrEnum):
    REQUESTED = "REQUESTED"
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class MissionKind(StrEnum):
    LOGISTICS = "LOGISTICS"
    RECONNAISSANCE = "RECONNAISSANCE"
    PATROL = "PATROL"
    SURVEY = "SURVEY"
    SUPPORT = "SUPPORT"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


class EventKind(StrEnum):
    ASSET_FAULT = "ASSET_FAULT"
    WEATHER_RESTRICTION = "WEATHER_RESTRICTION"
    HAZARD_ZONE = "HAZARD_ZONE"
    MISSION_PRIORITY_CHANGE = "MISSION_PRIORITY_CHANGE"


class EventStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"


class ProposalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class PlanVariant(StrEnum):
    """The three trade-off options, plus the deterministic fallback label."""

    COVERAGE_FIRST = "COVERAGE FIRST"
    SAFETY_FIRST = "SAFETY FIRST"
    STABILITY_FIRST = "STABILITY FIRST"
    FALLBACK = "FALLBACK"


class ActorRole(StrEnum):
    PLANNER = "PLANNER"
    COMMANDER = "COMMANDER"
    MAINTAINER = "MAINTAINER"
    OBSERVER = "OBSERVER"


class HubStatus(StrEnum):
    OPEN = "OPEN"
    RESTRICTED = "RESTRICTED"
    CLOSED = "CLOSED"


class RestrictionType(StrEnum):
    CLOSURE = "CLOSURE"
    WIND_LIMIT = "WIND_LIMIT"
    VISIBILITY_LIMIT = "VISIBILITY_LIMIT"


class CrewRole(StrEnum):
    PILOT = "PILOT"
    SYSTEMS = "SYSTEMS"
    MISSION_SPECIALIST = "MISSION_SPECIALIST"
    LOADMASTER = "LOADMASTER"


class LedgerAction(StrEnum):
    SCENARIO_GENERATED = "SCENARIO_GENERATED"
    SCENARIO_RESET = "SCENARIO_RESET"
    PLAN_GENERATED = "PLAN_GENERATED"
    EVENT_INJECTED = "EVENT_INJECTED"
    PROPOSALS_GENERATED = "PROPOSALS_GENERATED"
    PROPOSAL_APPROVED = "PROPOSAL_APPROVED"
    PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
    WHAT_IF_RUN = "WHAT_IF_RUN"
    ASSET_CONDITION_UPDATED = "ASSET_CONDITION_UPDATED"
    CLOCK_ADVANCED = "CLOCK_ADVANCED"
    CHAIN_VERIFIED = "CHAIN_VERIFIED"


class ZoneSeverityBand(StrEnum):
    """How the UI labels a hazard zone. Risk itself stays numeric."""

    ADVISORY = "ADVISORY"
    ELEVATED = "ELEVATED"
    RESTRICTED = "RESTRICTED"


class ReasonCode(StrEnum):
    """Structured reason codes. Sentences are rendered from these, never invented."""

    # Hard-constraint failures -------------------------------------------------
    CLASS_MISMATCH = "CLASS_MISMATCH"
    ASSET_UNAVAILABLE = "ASSET_UNAVAILABLE"
    ENDURANCE_EXCEEDED = "ENDURANCE_EXCEEDED"
    RANGE_EXCEEDED = "RANGE_EXCEEDED"
    PAYLOAD_SHORTFALL = "PAYLOAD_SHORTFALL"
    CREW_QUALIFICATION_MISSING = "CREW_QUALIFICATION_MISSING"
    CREW_DUTY_LIMIT_EXCEEDED = "CREW_DUTY_LIMIT_EXCEEDED"
    CREW_REST_NOT_MET = "CREW_REST_NOT_MET"
    TIME_WINDOW_MISSED = "TIME_WINDOW_MISSED"
    TIME_OVERLAP = "TIME_OVERLAP"
    HUB_CLOSED_AT_DEPARTURE = "HUB_CLOSED_AT_DEPARTURE"
    HUB_CAPACITY_EXCEEDED = "HUB_CAPACITY_EXCEEDED"
    RESTRICTION_ZONE_CROSSED = "RESTRICTION_ZONE_CROSSED"
    RISK_EXCEEDS_MISSION_MAX = "RISK_EXCEEDS_MISSION_MAX"

    # Positive confirmations ----------------------------------------------------
    CLASS_MATCH = "CLASS_MATCH"
    ASSET_AVAILABLE = "ASSET_AVAILABLE"
    WITHIN_ENDURANCE = "WITHIN_ENDURANCE"
    WITHIN_RANGE = "WITHIN_RANGE"
    PAYLOAD_AVAILABLE = "PAYLOAD_AVAILABLE"
    CREW_QUALIFIED = "CREW_QUALIFIED"
    CREW_AVAILABLE = "CREW_AVAILABLE"
    CREW_RESTED = "CREW_RESTED"
    WITHIN_TIME_WINDOW = "WITHIN_TIME_WINDOW"
    NO_TIME_OVERLAP = "NO_TIME_OVERLAP"
    HUB_OPEN_AT_TIMES = "HUB_OPEN_AT_TIMES"
    HUB_CAPACITY_OK = "HUB_CAPACITY_OK"
    ROUTE_CLEAR = "ROUTE_CLEAR"
    RISK_WITHIN_MAX = "RISK_WITHIN_MAX"
    EARLIEST_READY = "EARLIEST_READY"
    LOWER_RISK_THAN_ALTERNATIVE = "LOWER_RISK_THAN_ALTERNATIVE"

    # Plan-level / comparison ---------------------------------------------------
    MISSION_COMPLETED = "MISSION_COMPLETED"
    MISSION_UNASSIGNED = "MISSION_UNASSIGNED"
    REPLACED_ASSET = "REPLACED_ASSET"
    FROZEN_ASSIGNMENT_HELD = "FROZEN_ASSIGNMENT_HELD"
    DEFERRED_BY_WINDOW = "DEFERRED_BY_WINDOW"
    COVERAGE_CONSTRAINT = "COVERAGE_CONSTRAINT"
    FALLBACK_GREEDY = "FALLBACK_GREEDY"


# Priority weights used by the CP-SAT objective (P1 highest).
PRIORITY_WEIGHTS: dict[int, int] = {1: 100, 2: 60, 3: 35, 4: 20, 5: 10}


def priority_weight(priority: int) -> int:
    """Priority weight for the plan objective. Unknown priorities get the P5 weight."""
    return PRIORITY_WEIGHTS.get(int(priority), PRIORITY_WEIGHTS[5])