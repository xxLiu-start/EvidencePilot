"""Explicit scope metadata; document revision is not a model revision."""
from pydantic import BaseModel, ConfigDict, Field


class EvidenceScope(BaseModel):
    model_config = ConfigDict(extra="allow")
    domain_id: str | None = Field(default=None, min_length=1, max_length=64)
    attributes: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    model_name: str | None = Field(default=None, min_length=1, max_length=120)
    model_version: str | None = Field(default=None, min_length=1, max_length=120)
    deployment_mode: str | None = Field(default=None, min_length=1, max_length=120)


def value(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def scope_of(item):
    scope = value(item, "scope", {}) or {}
    raw = scope.model_dump() if isinstance(scope, EvidenceScope) else dict(scope)
    attributes = raw.pop("attributes", {}) or {}
    return {**raw, **attributes}


def normalized(value):
    return str(value).strip().casefold() if value is not None else None
