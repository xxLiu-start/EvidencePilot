"""Atomic JSON cache writes and explicit offline cache misses."""

import hashlib
import json
from pathlib import Path
import tempfile


class CacheMiss(RuntimeError):
    pass


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)


def scoped_cache_root(root, info, *, production=False):
    """Separate every private disk snapshot by trusted tenant/project UUIDs.

    Offline experiments keep their existing fixture layout. They must not
    share a writable cache root with production application processes.
    """
    from uuid import UUID

    root = Path(root)
    tenant = info.get("tenant_id")
    project = info.get("selected_project_id")
    if project is None:
        projects = tuple(info.get("selected_project_ids") or info.get("project_ids") or ())
        project = projects[0] if len(projects) == 1 else None
    if tenant is None:
        if production:
            raise PermissionError("Cache tenant context required")
        return root
    if project is None:
        raise PermissionError("Cache project context required")
    return root / "tenants" / str(UUID(str(tenant))) / "projects" / str(UUID(str(project)))


def scoped_cache_config(db, config):
    root = scoped_cache_root(config.web_cache_dir, db.info,
                             production=config.environment == "production")
    return config.model_copy(update={"web_cache_dir": str(root)})
