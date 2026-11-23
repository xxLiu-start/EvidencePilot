# Domain Profiles

EvidencePilot now exposes a versioned, validated domain-profile contract. The research engine can select a profile when a task is created:

```json
{
  "query": "Assess the current market position of Company A in the US",
  "budget": 8,
  "domain_id": "market_research"
}
```

Available built-ins:

- `general_research`
- `technology_research`
- `market_research`
- `academic_research`
- `compliance_research`
- `business_due_diligence`
- `model_infrastructure_research`

The model-infrastructure profile is retained as one optional profile; it is no longer the only research framing.

## Profile policy

A profile contains:

- Planner guidance and preferred source-type hints.
- Maximum Claim count.
- Default evidence freshness window.
- Minimum independent-source count.
- Whether human review is mandatory before a Claim can count as supported.
- Report-policy metadata for future report renderers.

Freshness, maximum Claim count, minimum independent-source count, and mandatory-human-review behavior are currently applied to new Agent tasks. Preferred source types are supplied to the Planner and Router as guidance; they are not yet a complete connector-level source filter. Report template metadata is available but the Reporter still uses the existing deterministic Markdown renderer.

For source corroboration, independent sources are currently approximated by distinct web hostnames or document IDs. This is a conservative engineering heuristic, not proof of independent ownership or editorial independence.

Compliance and business-due-diligence profiles fail closed: a model verdict alone cannot make a Claim count as supported when the profile requires human review. A dedicated review/approval UI and audit workflow are still required before these profiles can be used as a production compliance process.

## API

- `GET /api/v1/domains`
- `GET /api/v1/domains/{domain_id}`
- `POST /api/v1/research` accepts optional `domain_id`; omitted values use `general_research`.

Unknown profile IDs are rejected with HTTP 422 when creating a task and HTTP 404 when reading a profile.

## Extending profiles

Use `DomainProfile` and `load_domain_profile(path)` to validate a YAML profile. YAML loading uses `yaml.safe_load`; arbitrary Python imports and executable profile content are not supported. Platform security policy is not configurable through a domain profile.
