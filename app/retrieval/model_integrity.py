"""Fail-closed verification of administrator-provisioned model artifacts."""
import hashlib
import json
import re
from pathlib import Path

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_ALLOWED_SUFFIXES = {".json", ".safetensors", ".model", ".txt"}

def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def verified_model_path(config, kind):
    """Production trusts a separately configured manifest digest, not the model itself.

    The administrator must mount the verified bundle read-only. This check is
    not a sandbox and cannot protect against concurrent privileged filesystem writes.
    """
    model = getattr(config, kind + "_model")
    if config.environment != "production":
        return model
    revision = getattr(config, kind + "_revision")
    digest = getattr(config, kind + "_manifest_sha256")
    root = Path(model)
    if (not getattr(config, kind + "_local_files_only")
            or not _REVISION.fullmatch(revision)
            or not _SHA256.fullmatch(digest)
            or not root.is_absolute() or not root.is_dir() or root.is_symlink()):
        raise ValueError("Production requires an absolute local model bundle, pinned revision and trusted manifest digest")
    manifest = root / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file() or _digest(manifest) != digest:
        raise ValueError("Model manifest integrity check failed")
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("revision") != revision or not isinstance(metadata.get("files"), dict):
        raise ValueError("Model manifest revision or file list is invalid")
    files = metadata["files"]
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Model bundle contains a symbolic link")
        if path.is_dir():
            continue
        if path == manifest:
            continue
        name = path.relative_to(root).as_posix()
        if not path.is_file() or path.suffix not in _ALLOWED_SUFFIXES:
            raise ValueError("Model bundle contains an unsupported file type")
        expected = files.get(name)
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected) or _digest(path) != expected:
            raise ValueError("Model file integrity check failed")
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("auto_map"):
                raise ValueError("Custom model code is forbidden")
        actual.add(name)
    if actual != set(files) or "config.json" not in actual or not any(n.endswith(".safetensors") for n in actual):
        raise ValueError("Model bundle is incomplete")
    return str(root)
