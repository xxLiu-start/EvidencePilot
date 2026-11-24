import hashlib
import json

import pytest

from app.config import Settings
from app.retrieval.model_integrity import verified_model_path

def bundle(tmp_path, kind="embedding"):
    revision = "a" * 40
    files = {"config.json": b"{}", "model.safetensors": b"test bytes"}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    manifest = json.dumps({"revision": revision, "files": {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }}).encode()
    (tmp_path / "manifest.json").write_bytes(manifest)
    return Settings(database_url="sqlite://", environment="production", **{
        kind + "_model": str(tmp_path), kind + "_revision": revision,
        kind + "_local_files_only": True,
        kind + "_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
    })

@pytest.mark.parametrize("kind", ["embedding", "reranker"])
def test_verified_bundle(tmp_path, kind):
    config = bundle(tmp_path, kind)
    assert verified_model_path(config, kind) == str(tmp_path)

@pytest.mark.parametrize("change", ["tamper", "pickle", "symlink", "missing", "manifest", "revision", "online"])
def test_reject_untrusted_bundle(tmp_path, change):
    config = bundle(tmp_path)
    if change == "tamper":
        (tmp_path / "model.safetensors").write_bytes(b"changed")
    elif change == "pickle":
        (tmp_path / "pytorch_model.bin").write_bytes(b"unsafe")
    elif change == "symlink":
        (tmp_path / "alias.json").symlink_to(tmp_path / "config.json")
    elif change == "missing":
        (tmp_path / "config.json").unlink()
    elif change == "manifest":
        (tmp_path / "manifest.json").write_text("{}")
    elif change == "revision":
        config.embedding_revision = "main"
    elif change == "online":
        config.embedding_local_files_only = False
    with pytest.raises(ValueError):
        verified_model_path(config, "embedding")

def test_development_preserves_existing_model_resolution():
    config = Settings(database_url="sqlite://", environment="development")
    assert verified_model_path(config, "embedding") == config.embedding_model
