"""Scope-aware candidates; different quantities alone never prove a conflict."""
from itertools import combinations
import re

from app.verification.scope import normalized, scope_of, value


def has_opposing_relations(evidences):
    relations = {value(evidence, "relation") for evidence in evidences}
    return "supports" in relations and bool({"refutes", "contradicts"} & relations)


def detect_contradiction(claim, evidences):
    pairs = []
    for left, right in combinations(evidences[:20], 2):
        a, b = scope_of(left), scope_of(right)
        qa = set(re.findall(r"\b\d+(?:\.\d+)?\s*[kKmMgG]\b", value(left, "supporting_text", "")))
        qb = set(re.findall(r"\b\d+(?:\.\d+)?\s*[kKmMgG]\b", value(right, "supporting_text", "")))
        if not has_opposing_relations([left, right]) and not (qa and qb and qa != qb):
            continue
        keys = ("model_name", "model_version", "deployment_mode")
        mismatches = [key for key in keys if a.get(key) and b.get(key) and normalized(a[key]) != normalized(b[key])]
        unknown = [key for key in keys if not a.get(key) or not b.get(key)]
        dates_differ = bool(value(left, "published_at") and value(right, "published_at")
                            and str(value(left, "published_at")) != str(value(right, "published_at")))
        classification = "DIFFERENT_SCOPE" if mismatches else (
            "POSSIBLE_TEMPORAL_UPDATE" if dates_differ else "UNKNOWN_SCOPE" if unknown else "SAME_SCOPE_CANDIDATE")
        pairs.append({"evidence_ids": [str(value(item, "evidence_id", value(item, "id"))) for item in (left, right)],
                      "classification": classification, "different_fields": mismatches, "unknown_fields": unknown,
                      "dates_differ": dates_differ, "requires_verification": not bool(mismatches)})
    return {"has_conflict": False, "requires_verification": any(pair["requires_verification"] for pair in pairs),
            "pairs": pairs, "reason": "Only explicit semantic verification can confirm a same-scope contradiction."}


def has_conflict(state):
    return bool(state.get("has_conflict")) or any(
        item.get("has_conflict") or item.get("potential_conflict") or item.get("status") == "CONTRADICTED"
        for item in state.get("claim_statuses", {}).values())
