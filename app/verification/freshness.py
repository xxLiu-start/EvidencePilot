from datetime import datetime, timedelta, timezone
from app.verification.scope import normalized, scope_of, value


def detect_outdated(evidence, current_version=None, *, max_age_days=365, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    version = scope_of(evidence).get("model_version")
    mismatch = bool(current_version and version and normalized(current_version) != normalized(version))
    stamp = value(evidence, "published_at") or value(evidence, "retrieved_at")
    try:
        stamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) else stamp
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        fresh_date = stamp is not None and now - timedelta(days=max_age_days) <= stamp <= now + timedelta(days=1)
    except (ValueError, TypeError, AttributeError):
        fresh_date = False
    return {"outdated": mismatch or not fresh_date, "version_mismatch": mismatch,
            "reference_date": stamp.isoformat() if isinstance(stamp, datetime) else None,
            "reason": "model_version_mismatch" if mismatch else "within_window" if fresh_date else "missing_invalid_or_expired_date"}


def evidence_is_fresh(evidence, max_age_days, now=None, current_version=None):
    return not detect_outdated(evidence, current_version, max_age_days=max_age_days, now=now)["outdated"]
