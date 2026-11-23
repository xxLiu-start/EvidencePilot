"""Validated, versioned domain profiles for domain-neutral research planning.

Profiles tune research policy and presentation. They cannot weaken platform security,
tool authorization, evidence integrity, or tenant access controls.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FreshnessPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_max_age_days: int = Field(default=365, ge=1, le=3650)
    require_publication_date_for_time_sensitive_claims: bool = False


class ClaimPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_claims: int = Field(default=6, ge=1, le=20)
    require_dependencies_validation: bool = True
    default_require_multiple_sources: bool = False


class VerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_exact_citation: bool = True
    require_scope_match: bool = True
    require_contradiction_check: bool = True
    require_human_review: bool = False
    min_independent_sources: int = Field(default=1, ge=1, le=10)


class ReportPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_methodology: bool = True
    include_source_comparison: bool = True
    include_unresolved_gaps: bool = True
    include_uncertainty: bool = True
    template_id: str = "general_research"


class DomainProfile(BaseModel):
    """A domain-specific policy bundle consumed by the universal research core."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    display_name: str = Field(min_length=2, max_length=100)
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    description: str = Field(default="", max_length=500)
    planner_instructions: str = Field(default="", max_length=4000)
    preferred_source_types: list[str] = Field(default_factory=list, max_length=20)
    claim_policy: ClaimPolicy = Field(default_factory=ClaimPolicy)
    freshness_policy: FreshnessPolicy = Field(default_factory=FreshnessPolicy)
    verification_policy: VerificationPolicy = Field(default_factory=VerificationPolicy)
    report_policy: ReportPolicy = Field(default_factory=ReportPolicy)

    @field_validator("preferred_source_types")
    @classmethod
    def normalize_source_types(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower() for value in values]
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) for value in normalized):
            raise ValueError("Source types must be lowercase identifiers")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Source types must be unique")
        return normalized


_BUILTINS: dict[str, dict[str, Any]] = {
    "general_research": {
        "display_name": "General Research",
        "description": "Domain-neutral research with balanced source discovery and evidence review.",
        "planner_instructions": "Keep claims neutral, independently verifiable, and limited to the user's requested scope.",
        "preferred_source_types": ["official_document", "primary_source", "academic_paper", "reputable_secondary"],
        "report_policy": {"template_id": "general_research"},
    },
    "technology_research": {
        "display_name": "Technology Research",
        "description": "Technical capabilities, architecture, standards, and implementation research.",
        "planner_instructions": "Preserve product, version, runtime, and deployment constraints. Prefer primary technical documentation and standards.",
        "preferred_source_types": ["official_documentation", "technical_standard", "academic_paper", "repository"],
        "report_policy": {"template_id": "technology_research"},
    },
    "market_research": {
        "display_name": "Market Research",
        "description": "Market structure, competitors, trends, and customer evidence.",
        "planner_instructions": "Make geography, period, market definition, and metric explicit. Distinguish reported facts from estimates and forecasts.",
        "preferred_source_types": ["regulatory_filing", "official_statistics", "industry_report", "reputable_news"],
        "freshness_policy": {"default_max_age_days": 90, "require_publication_date_for_time_sensitive_claims": True},
        "claim_policy": {"max_claims": 10, "default_require_multiple_sources": True},
        "verification_policy": {"min_independent_sources": 2},
        "report_policy": {"template_id": "market_research", "include_source_comparison": True},
    },
    "academic_research": {
        "display_name": "Academic Research",
        "description": "Literature review, methods, findings, and research-gap analysis.",
        "planner_instructions": "Separate research question, method, population, result, and limitations. Prefer primary papers and clearly distinguish preprints from peer-reviewed work.",
        "preferred_source_types": ["peer_reviewed_paper", "preprint", "dataset", "academic_repository"],
        "claim_policy": {"max_claims": 10, "default_require_multiple_sources": True},
        "verification_policy": {"min_independent_sources": 2},
        "report_policy": {"template_id": "academic_review", "include_methodology": True},
    },
    "compliance_research": {
        "display_name": "Compliance Research",
        "description": "Regulatory obligations, standards, applicability, and compliance evidence.",
        "planner_instructions": "Always identify jurisdiction, applicable entity, effective date, and authoritative legal text. Do not present generated output as legal advice.",
        "preferred_source_types": ["official_regulation", "regulator_guidance", "recognized_standard", "primary_legal_source"],
        "freshness_policy": {"default_max_age_days": 90, "require_publication_date_for_time_sensitive_claims": True},
        "claim_policy": {"max_claims": 12},
        "verification_policy": {"require_human_review": True, "min_independent_sources": 1},
        "report_policy": {"template_id": "compliance_assessment", "include_uncertainty": True},
    },
    "business_due_diligence": {
        "display_name": "Business Due Diligence",
        "description": "Company, supplier, operational, financial, and commercial risk research.",
        "planner_instructions": "Separate verified disclosures, third-party reporting, estimates, and allegations. Record entity identity and reporting period.",
        "preferred_source_types": ["regulatory_filing", "audited_statement", "official_company_source", "reputable_news"],
        "claim_policy": {"max_claims": 12, "default_require_multiple_sources": True},
        "verification_policy": {"min_independent_sources": 2, "require_human_review": True},
        "report_policy": {"template_id": "business_due_diligence"},
    },
    "model_infrastructure_research": {
        "display_name": "Model Infrastructure Research",
        "description": "Specialized profile for model serving, deployment, inference, and GPU infrastructure.",
        "planner_instructions": "Preserve model name, exact version, hardware, serving stack, and deployment mode. Do not generalize results across versions.",
        "preferred_source_types": ["official_model_documentation", "repository", "technical_benchmark", "academic_paper"],
        "report_policy": {"template_id": "technology_research"},
    },
}


@lru_cache(maxsize=1)
def _builtin_profiles() -> dict[str, DomainProfile]:
    profiles: dict[str, DomainProfile] = {}
    for domain_id, raw in _BUILTINS.items():
        profiles[domain_id] = DomainProfile(domain_id=domain_id, **raw)
    return profiles


def list_domain_profiles() -> list[DomainProfile]:
    """Return built-in profiles in stable order."""
    return list(_builtin_profiles().values())


def get_domain_profile(domain_id: str | None = None) -> DomainProfile:
    """Resolve a built-in profile; unknown IDs fail closed instead of silently falling back."""
    selected = domain_id or "general_research"
    try:
        return _builtin_profiles()[selected]
    except KeyError:
        raise ValueError(f"Unknown research domain: {selected}") from None


def load_domain_profile(path: str | Path) -> DomainProfile:
    """Load a user-managed YAML profile without allowing it to override platform security."""
    try:
        import yaml
    except ImportError:
        raise RuntimeError("PyYAML is required to load external domain profiles") from None
    profile_path = Path(path)
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Domain profile must be a YAML mapping")
    return DomainProfile.model_validate(raw)
