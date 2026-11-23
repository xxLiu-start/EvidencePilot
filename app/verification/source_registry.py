"""Configurable source classification hints for cross-domain evidence policies.

Classification is a routing hint, not a proof that a source or statement is trustworthy.
High-risk decisions still require primary-source review and, where configured, human approval.
"""
from __future__ import annotations

from urllib.parse import urlsplit


class SourceAuthorityRegistry:
    def __init__(self) -> None:
        self._official_hosts = {
            "qwen.readthedocs.io", "qwenlm.github.io", "api-docs.deepseek.com",
            "platform.deepseek.com", "docs.anthropic.com", "platform.claude.com",
            "docs.vllm.ai", "help.aliyun.com", "fastapi.tiangolo.com",
            "docs.langchain.com", "www.postgresql.org", "docs.pydantic.dev",
            "pydantic.dev", "sec.gov", "www.sec.gov", "www.gov.uk",
            "eur-lex.europa.eu", "ec.europa.eu", "www.who.int", "www.un.org",
            "www.iso.org", "www.iec.ch", "www.nist.gov", "standards.ieee.org",
            "www.federalregister.gov", "www.regulations.gov", "www.govinfo.gov",
            "www.census.gov", "data.gov",
        }
        self._academic_hosts = {
            "arxiv.org", "aclanthology.org", "proceedings.neurips.cc",
            "pubmed.ncbi.nlm.nih.gov", "nature.com", "science.org",
            "sciencedirect.com", "springer.com", "link.springer.com",
            "onlinelibrary.wiley.com", "journals.plos.org", "pnas.org",
            "ieee.org", "dl.acm.org", "acm.org",
        }
        self._github_official_owners = {
            "qwenlm", "deepseek-ai", "anthropics", "vllm-project",
            "pgvector", "pydantic", "langchain-ai",
        }
        self._huggingface_official_owners = {"qwen", "deepseek-ai", "baai"}

    @staticmethod
    def _matches(host: str, domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    def register_host(self, host: str, classification: str) -> None:
        normalized = host.strip().lower().rstrip(".")
        if not normalized or classification not in {"official", "academic", "web"}:
            raise ValueError("Invalid source host classification")
        if classification == "official":
            self._official_hosts.add(normalized)
            self._academic_hosts.discard(normalized)
        elif classification == "academic":
            self._academic_hosts.add(normalized)
            self._official_hosts.discard(normalized)
        else:
            self._official_hosts.discard(normalized)
            self._academic_hosts.discard(normalized)

    def classify(self, source_type: str, source_url: str | None = None) -> str:
        if source_type in {"document", "database", "enterprise_connector"}:
            return "internal"
        host = (urlsplit(source_url or "").hostname or "").lower().rstrip(".")
        if not host:
            return "web"
        if any(self._matches(host, domain) for domain in self._official_hosts):
            return "official"
        if host.endswith(".gov") or ".gov." in host or host.endswith(".gov.uk") or host.endswith(".europa.eu"):
            return "official"
        if any(self._matches(host, domain) for domain in self._academic_hosts):
            return "academic"
        parts = host.split(".")
        if len(parts) >= 2 and parts[-2] == "github":
            owner = parts[0]
            path = urlsplit(source_url or "").path.strip("/").split("/")
            if path and path[0].lower() in self._github_official_owners:
                return "official"
        if host == "huggingface.co" or host.endswith(".huggingface.co"):
            path = urlsplit(source_url or "").path.strip("/").split("/")
            if path and path[0].lower() in self._huggingface_official_owners:
                return "official"
        return "web"


DEFAULT_SOURCE_REGISTRY = SourceAuthorityRegistry()
