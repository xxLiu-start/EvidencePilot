"""Import local evaluation artifacts into long-term PostgreSQL memory."""
from datetime import datetime, timezone
import json
from pathlib import Path
from sqlalchemy import select
from app.db.models import EvaluationResult
from app.search.cache import digest


def import_reports(db, root=Path("data/benchmark")):
    for path in sorted(Path(root).rglob("*.json")):
        if path.name not in {"results.json", "acceptance.json"} or path.stat().st_size > 2_000_000:
            continue
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(result, dict):
            continue
        hashed = digest(json.dumps(result, sort_keys=True, ensure_ascii=False))
        if db.scalar(select(EvaluationResult.id).where(EvaluationResult.content_hash == hashed)) is None:
            db.add(EvaluationResult(name=path.relative_to(root).as_posix(), content_hash=hashed,
                                    result=result, created_at=datetime.now(timezone.utc)))
            db.flush()
    db.commit()
