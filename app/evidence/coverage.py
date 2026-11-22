"""Verified, source-qualified weighted evidence coverage."""

from enum import StrEnum
import math


class EvidenceStatus(StrEnum):
    NOT_RETRIEVED = "NOT_RETRIEVED"
    RETRIEVED = "RETRIEVED"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INSUFFICIENT = "INSUFFICIENT"
    OUTDATED = "OUTDATED"


PRIORITY_WEIGHTS = {"high": 3.0, "medium": 2.0, "low": 1.0}


def is_covered(item):
    return (item.get("status") == EvidenceStatus.SUPPORTED and item.get("verified") is True
            and item.get("source_requirements_met") is True and item.get("fresh") is True
            and not item.get("has_conflict", False))


def calculate_coverage(claims):
    weights = [float(item.get("weight", PRIORITY_WEIGHTS.get(item.get("priority"), 1))) for item in claims]
    if any(not math.isfinite(weight) or weight < 0 for weight in weights):
        raise ValueError("Coverage weights must be finite and non-negative")
    total = sum(weights)
    return sum(weight for item, weight in zip(claims, weights) if is_covered(item)) / total if total else 0.0


def coverage_items(state):
    return [{**claim, **state.get("claim_statuses", {}).get(claim["id"], {}),
             "weight": claim.get("weight", PRIORITY_WEIGHTS.get(claim.get("priority"), 1))}
            for claim in state.get("claims", [])]


def all_critical_claims_supported(state):
    items = coverage_items(state)
    critical = [item for item in items if item.get("priority") == "high"] or items
    return bool(critical) and all(is_covered(item) for item in critical)
