"""Transparent synthetic readiness estimator.

This is a **synthetic heuristic**, not predictive ML. The formula is fully
documented so the UI can show exactly why a number appears.

    base = 0.98
    p = base
      - 0.003 * maintenance_hours_since
      - 0.04  * recent_fault_count
      - 0.10  if status == LIMITED
    p = clip(p, 0.05, 0.99)

    uncertainty = min(0.25, 0.04 + 0.002 * maintenance_hours_since + 0.03 * recent_fault_count)
    low  = max(0.0, p - uncertainty)
    high = min(1.0, p + uncertainty)

Stretch goal (documented, not implemented): replace this with a scikit-learn model
trained on synthetic maintenance history plus a conformal interval. No claim of
real-world validation is made either way.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import AssetStatus

READINESS_BASE = 0.98
READINESS_FLOOR = 0.05
READINESS_CEILING = 0.99
MAINTENANCE_PENALTY_PER_HOUR = 0.003
FAULT_PENALTY = 0.04
LIMITED_PENALTY = 0.10
UNCERTAINTY_BASE = 0.04
UNCERTAINTY_PER_HOUR = 0.002
UNCERTAINTY_PER_FAULT = 0.03
UNCERTAINTY_CAP = 0.25

METHOD_LABEL = "synthetic heuristic (not predictive ML)"


@dataclass(frozen=True, slots=True)
class Readiness:
    probability: float
    low: float
    high: float
    uncertainty: float
    terms: dict[str, float]

    @property
    def band_label(self) -> str:
        if self.low < 0.70:
            return "LOW"
        if self.probability < 0.90:
            return "WATCH"
        return "NOMINAL"

    def as_dict(self) -> dict:
        return {
            "readiness_probability": round(self.probability, 4),
            "readiness_low": round(self.low, 4),
            "readiness_high": round(self.high, 4),
            "uncertainty": round(self.uncertainty, 4),
            "terms": {k: round(v, 4) for k, v in self.terms.items()},
            "band": self.band_label,
            "method": METHOD_LABEL,
        }


def estimate_readiness(
    maintenance_hours_since: float,
    recent_fault_count: int,
    status: AssetStatus = AssetStatus.AVAILABLE,
) -> Readiness:
    """Compute the documented synthetic readiness heuristic and its uncertainty band."""
    hours_penalty = MAINTENANCE_PENALTY_PER_HOUR * max(0.0, float(maintenance_hours_since))
    fault_penalty = FAULT_PENALTY * max(0, int(recent_fault_count))
    limited_penalty = LIMITED_PENALTY if status == AssetStatus.LIMITED else 0.0

    raw = READINESS_BASE - hours_penalty - fault_penalty - limited_penalty
    probability = min(READINESS_CEILING, max(READINESS_FLOOR, raw))

    uncertainty = min(
        UNCERTAINTY_CAP,
        UNCERTAINTY_BASE
        + UNCERTAINTY_PER_HOUR * max(0.0, float(maintenance_hours_since))
        + UNCERTAINTY_PER_FAULT * max(0, int(recent_fault_count)),
    )
    low = max(0.0, probability - uncertainty)
    high = min(1.0, probability + uncertainty)

    terms = {
        "base": READINESS_BASE,
        "maintenance_hours_since": -hours_penalty,
        "recent_fault_count": -fault_penalty,
        "status_limited": -limited_penalty,
        "raw": raw,
    }
    return Readiness(probability=probability, low=low, high=high, uncertainty=uncertainty, terms=terms)