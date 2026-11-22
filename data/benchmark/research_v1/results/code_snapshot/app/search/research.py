"""Bounded multi-query web research with persisted claims, pages and evidence."""

from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.db.models import ClaimRecord, WebSearchRecord
from app.evidence.extraction import extract_evidence, save_evidences
from app.evidence.schema import Evidence
from app.llm_client import LLMError
from app.search.cache import CacheMiss, write_json
from app.search.parser import PageError, fetch_page, normalize_url
from app.search.query_generator import QueryPlan, generate_queries
from app.search.web_search import SearchError, get_web_search_client


def run_web_research(db, claims, *, max_queries=4, max_results=3, max_pages=4,
                     client=None, search_client=None, fixed_plan=None, cache_only=False):
    if len({c.claim_id for c in claims}) != len(claims):
        raise ValueError("Claim IDs must be unique per research request")
    records = {}
    for claim in claims:
        record = db.scalar(select(ClaimRecord).where(
            ClaimRecord.claim_id == claim.claim_id, ClaimRecord.claim_text == claim.claim_text
        ))
        if record is None:
            record = ClaimRecord(claim_id=claim.claim_id, claim_text=claim.claim_text)
            db.add(record)
            db.flush()
        records[claim.claim_id] = record
    db.commit()
    traces = []
    if fixed_plan is None:
        plan, trace = generate_queries(claims, max_queries=max_queries, client=client)
        traces.append(asdict(trace))
    else:
        plan = QueryPlan.model_validate(fixed_plan)
        if len(plan.queries) > max_queries or any(q.claim_id not in records for q in plan.queries):
            raise ValueError("Fixed plan has unknown claims or exceeds the query limit")
    searcher = search_client if search_client is not None else get_web_search_client()
    urls, errors, seen, evidence, pages = {}, [], set(), [], []
    claims_by_id = {c.claim_id: c for c in claims}
    for query in plan.queries:
        result = searcher.search(query.query, max_results=max_results, cache_only=cache_only)
        found = []
        for item in result["results"]:
            try:
                url = normalize_url(item["url"])
            except (PageError, KeyError):
                continue
            found.append(url)
            entry = urls.setdefault(url, {"claims": set(), "queries": []})
            entry["claims"].add(query.claim_id)
            if query.query not in entry["queries"]:
                entry["queries"].append(query.query)
        db.add(WebSearchRecord(claim_record_id=records[query.claim_id].id, query=query.query,
                               elapsed_ms=result["elapsed_ms"], cache_hit=int(result["cache_hit"]), source_urls=found))
        db.commit()
    for url, context in list(urls.items())[:max_pages]:
        try:
            page = None
            for query in context["queries"]:
                page = fetch_page(db, url, search_query=query, cache_only=cache_only)
            pages.append({key: value for key, value in asdict(page).items() if key != "body"})
            for claim_id in sorted(context["claims"]):
                if (claim_id, page.content_hash) in seen:
                    continue
                seen.add((claim_id, page.content_hash))
                extracted, trace = extract_evidence(claims_by_id[claim_id], page, client=client)
                traces.append(asdict(trace))
                save_evidences(db, extracted)
                evidence.extend(extracted)
        except (PageError, CacheMiss, LLMError) as exc:
            errors.append({"source_url": url, "category": getattr(exc, "category", type(exc).__name__)})
    run_id = str(uuid4())
    result = {"run_id": run_id, "claims": [c.model_dump() for c in claims],
              "status": "complete" if evidence and not errors else "partial" if evidence else "no_evidence",
              "queries": plan.model_dump()["queries"], "pages": pages,
              "evidences": [e.model_dump(mode="json") for e in evidence], "errors": errors, "traces": traces}
    write_json(Path(settings.web_cache_dir) / "research" / (run_id + ".json"), result)
    return result


def replay_snapshot(db, run_id):
    from uuid import UUID
    path = Path(settings.web_cache_dir) / "research" / (str(UUID(run_id)) + ".json")
    if not path.is_file():
        raise CacheMiss("No fixed research snapshot")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    evidences = [Evidence.model_validate(value) for value in snapshot["evidences"]]
    save_evidences(db, evidences)
    return {**snapshot, "replayed": True}
