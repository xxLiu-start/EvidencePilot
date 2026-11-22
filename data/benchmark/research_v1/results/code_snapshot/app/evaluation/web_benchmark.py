"""Offline validation of a frozen web-research snapshot; never searches or calls an LLM."""

import argparse
import json
from pathlib import Path

from app.config import settings
from app.evidence.schema import Evidence
from app.search.cache import digest


def validate_snapshot(snapshot_path, cache_dir=None):
    root = Path(cache_dir or settings.web_cache_dir)
    snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    manifest_path = Path(snapshot_path).parent / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (manifest.get("snapshot") == Path(snapshot_path).name
                and manifest.get("snapshot_hash") != digest(json.dumps(snapshot, sort_keys=True))):
            raise ValueError("Frozen snapshot manifest hash mismatch")
    claims = {claim["claim_id"]: claim["claim_text"] for claim in snapshot["claims"]}
    pages = {page["source_url"]: page for page in snapshot["pages"]}
    if not snapshot["evidences"]:
        raise ValueError("Frozen benchmark has no evidence")
    for value in snapshot["evidences"]:
        evidence = Evidence.model_validate(value)
        page = pages.get(evidence.source_url)
        if (page is None or page["content_hash"] != evidence.content_hash
                or claims.get(evidence.claim_id) != evidence.claim_text):
            raise ValueError("Frozen evidence has invalid source/claim bindings")
        path = root / "contents" / (evidence.content_hash + ".json")
        body = json.loads(path.read_text(encoding="utf-8"))["body"]
        if digest(body) != evidence.content_hash or body[evidence.quote_start:evidence.quote_end] != evidence.supporting_text:
            raise ValueError("Frozen quote/content hash validation failed")
    return {"status": "passed", "queries": len(snapshot["queries"]), "pages": len(pages),
            "evidences": len(snapshot["evidences"]), "network_requests": 0, "llm_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate_snapshot(args.snapshot)))


if __name__ == "__main__":
    main()
