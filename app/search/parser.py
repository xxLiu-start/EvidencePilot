"""Public-page fetching, text extraction, normalized URLs and content snapshots."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import trafilatura
from sqlalchemy import select

from app.config import Settings, settings
from app.db.session import cache_scope, get_cached_content
from app.db.models import WebContent, WebSource
from app.search.cache import CacheMiss, digest, write_json, scoped_cache_config


class PageError(RuntimeError):
    pass


def normalize_url(url):
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise PageError("Invalid HTTP source URL")
    try:
        port = parts.port
    except ValueError:
        raise PageError("Invalid source port") from None
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    scheme = parts.scheme.lower()
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    params = sorted((key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if not key.casefold().startswith("utm_") and key.casefold() not in {"gclid", "fbclid"})
    return urlunsplit((scheme, host, parts.path or "/", urlencode(params), ""))


def assert_public_url(url):
    parts = urlsplit(normalize_url(url))
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))}
    except OSError:
        raise PageError("Source hostname cannot be resolved") from None
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise PageError("Web sources must resolve to public addresses")


@dataclass(frozen=True)
class ParsedPage:
    source_url: str
    fetched_url: str
    title: str
    body: str
    published_at: str | None
    retrieved_at: str
    content_hash: str
    source_type: str = "web"
    cache_hit: bool = False


def extract_html(html, url):
    document = trafilatura.bare_extraction(html, url=url, with_metadata=True, include_comments=False,
                                         include_tables=True, favor_precision=True)
    if document is None or not document.text or not document.text.strip():
        raise PageError("No extractable webpage body")
    date = None
    if document.date:
        try:
            parsed = datetime.fromisoformat(document.date.replace("Z", "+00:00"))
            date = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).isoformat()
        except ValueError:
            pass
    body = document.text.strip()
    return ParsedPage(normalize_url(url), url, document.title or urlsplit(url).hostname, body, date,
                      datetime.now(timezone.utc).isoformat(), digest(body))


def download_page(url, config: Settings = settings):
    target = normalize_url(url)
    try:
        with httpx.Client(timeout=config.webpage_timeout_seconds, follow_redirects=False,
                          trust_env=False, headers={"User-Agent": "EvidencePilot/0.1 (research document parser)"}) as client:
            for _ in range(6):
                assert_public_url(target)
                with client.stream("GET", target) as response:
                    if response.is_redirect:
                        target = normalize_url(urljoin(target, response.headers.get("location", "")))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(value in content_type for value in ["text/html", "application/xhtml+xml", "text/plain"]):
                        raise PageError("Unsupported webpage content type")
                    data = bytearray()
                    for block in response.iter_bytes():
                        data.extend(block)
                        if len(data) > config.webpage_max_bytes:
                            raise PageError("Webpage exceeds configured size limit")
                    page = extract_html(bytes(data), target)
                    return ParsedPage(normalize_url(url), target, page.title, page.body, page.published_at,
                                      page.retrieved_at, page.content_hash)
            raise PageError("Too many webpage redirects")
    except httpx.HTTPError:
        raise PageError("Webpage fetch failed") from None


def fetch_page(db, url, *, search_query="", cache_only=False, refresh=False, config: Settings = settings, before_commit=None):
    config = scoped_cache_config(db, config)
    canonical = normalize_url(url)
    source = db.scalar(select(WebSource).where(WebSource.canonical_url == canonical))
    if source is not None and not refresh:
        content = get_cached_content(db, source.content_hash)
        if content is None or digest(content.body) != source.content_hash:
            raise PageError("Stored webpage hash mismatch")
        if search_query and search_query not in source.search_queries:
            source.search_queries = [*source.search_queries, search_query]
            if before_commit:
                before_commit()
            db.commit()
        return ParsedPage(canonical, source.fetched_url, source.title, content.body,
                          source.published_at.isoformat() if source.published_at else None,
                          source.retrieved_at.isoformat(), source.content_hash, cache_hit=True)
    url_path = Path(config.web_cache_dir) / "urls" / (digest(canonical) + ".json")
    if url_path.is_file() and not refresh:
        metadata = json.loads(url_path.read_text(encoding="utf-8"))
        if not re.fullmatch(r"[0-9a-f]{64}", metadata.get("content_hash", "")):
            raise PageError("Invalid cached content hash")
        content_path = Path(config.web_cache_dir) / "contents" / (metadata["content_hash"] + ".json")
        if not content_path.is_file():
            raise CacheMiss("Cached URL has no body snapshot")
        stored = json.loads(content_path.read_text(encoding="utf-8"))
        if digest(stored["body"]) != metadata["content_hash"] or metadata["source_url"] != canonical:
            raise PageError("Cached webpage hash/URL mismatch")
        page = ParsedPage(**metadata, body=stored["body"], cache_hit=True)
    elif cache_only:
        raise CacheMiss("No fixed webpage snapshot")
    else:
        page = download_page(canonical, config)
    content_path = Path(config.web_cache_dir) / "contents" / (page.content_hash + ".json")
    if before_commit:
        before_commit()
    write_json(content_path, {"body": page.body, "content_hash": page.content_hash})
    metadata = asdict(page)
    metadata.pop("body")
    metadata.pop("cache_hit")
    write_json(url_path, metadata)
    if get_cached_content(db, page.content_hash) is None:
        db.add(WebContent(content_hash=page.content_hash, body=page.body, cache_path=str(content_path)))
        db.flush()
    if source is None:
        source = WebSource(canonical_url=canonical, fetched_url=page.fetched_url, title=page.title,
                           content_hash=page.content_hash, published_at=datetime.fromisoformat(page.published_at) if page.published_at else None,
                           retrieved_at=datetime.fromisoformat(page.retrieved_at), source_type="web", search_queries=[])
        db.add(source)
    else:
        source.fetched_url, source.title, source.content_hash = page.fetched_url, page.title, page.content_hash
        source.published_at = datetime.fromisoformat(page.published_at) if page.published_at else None
        source.retrieved_at = datetime.fromisoformat(page.retrieved_at)
    if search_query and search_query not in source.search_queries:
        source.search_queries = [*source.search_queries, search_query]
    try:
        if before_commit:
            before_commit()
        db.commit()
    except Exception:
        db.rollback()
        raise
    return page


def parse_webpage(url: str):
    return download_page(url).body
