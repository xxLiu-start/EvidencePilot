# Source Connector Framework

The connector contract is in `app/connectors/base.py`.

A connector provides:

- `search(query, limit, filters, access_context)`
- `fetch(source_id, access_context)`
- `health_check()`
- `close()`

All connectors return the same `ConnectorDocument` model, including source identity, type, canonical location, timestamps, content hash, version, authority hint, access context, and metadata.

## Implemented adapters

- `FileConnector`: read-only local files under an explicitly configured root. It supports the repository's existing PDF, DOCX, Markdown, and TXT parsers, enforces a maximum file size, and rejects path traversal outside its root.
- `WebSearchConnector`: adapter over the existing Tavily discovery and webpage snapshot pipeline. Search results are discovery hints; a source becomes a full document only after the existing page parser fetches and snapshots it.
- `ConnectorRegistry`: explicit in-process registration. It does not dynamically import code or allow the LLM to register tools.

The existing Agent Web Search path now uses `WebSearchConnector` for discovery while retaining its existing task-specific source authorization and commit fencing.

## Not yet implemented

S3/MinIO, SharePoint, Confluence, GitHub and enterprise knowledge-base connectors are extension targets, not current capabilities. Per-user permission delegation and source-system ACL propagation must be implemented before these connectors are used with multi-tenant enterprise data. Passing an `access_context` is an interface contract, not proof that a connector enforces an upstream ACL.
