"""Prepare trusted, safetensors-only BGE-M3 and BGE Reranker bundles.

Run in a disposable, network-enabled model-preparation environment. Production
inference remains offline and verifies the generated manifest digest separately.
The BGE-M3 backbone checkpoint is converted with weights_only=True; no .pt
sparse/ColBERT heads are imported because this service exposes Dense only.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from huggingface_hub import snapshot_download
from safetensors.torch import load_file, save_file
import torch

BGE_REPO = "BAAI/bge-m3"
BGE_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_SOURCE_SHA256 = "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38"
RERANKER_REPO = "BAAI/bge-reranker-v2-m3"
RERANKER_REVISION = "b9a8f459d786a86f171264d4b075572506495226"
RERANKER_SHA256 = "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"
COMMON_FILES = [
    "config.json", "sentencepiece.bpe.model", "special_tokens_map.json",
    "tokenizer.json", "tokenizer_config.json", "vocab.txt", "vocab.json",
    "merges.txt", "added_tokens.json", "sentence_bert_config.json",
]

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def download(repo: str, revision: str, out: Path, patterns: list[str]) -> Path:
    snapshot = Path(snapshot_download(
        repo_id=repo, revision=revision, allow_patterns=patterns,
    ))
    out.mkdir(parents=True, exist_ok=True)
    for name in patterns:
        source = snapshot / name
        if source.is_file():
            shutil.copyfile(source, out / name)
    return out

def convert_bge_dense(out: Path) -> None:
    source = out / "pytorch_model.bin"
    if not source.is_file() or sha256(source) != BGE_SOURCE_SHA256:
        raise RuntimeError("Pinned official BGE-M3 source checkpoint SHA-256 mismatch")
    state = torch.load(source, map_location="cpu", weights_only=True)
    if not isinstance(state, dict) or not state:
        raise RuntimeError("Unexpected checkpoint structure")
    if any(not isinstance(k, str) or not isinstance(v, torch.Tensor) for k, v in state.items()):
        raise RuntimeError("Checkpoint contains non-tensor state; refusing conversion")
    config = json.loads((out / "config.json").read_text())
    if config.get("model_type") != "xlm-roberta" or config.get("hidden_size") != 1024:
        raise RuntimeError("Unexpected BGE-M3 backbone configuration")
    target = out / "model.safetensors"
    save_file({k: v.contiguous() for k, v in state.items()}, str(target),
              metadata={"format": "pt", "source_repo": BGE_REPO,
                        "source_revision": BGE_REVISION,
                        "source_sha256": BGE_SOURCE_SHA256})
    converted = load_file(str(target), device="cpu")
    if set(converted) != set(state):
        raise RuntimeError("Safetensors key set differs from source checkpoint")
    for key, tensor in state.items():
        if not torch.equal(tensor.cpu(), converted[key]):
            raise RuntimeError("Safetensors tensor mismatch: " + key)
    del converted, state
    source.unlink()

def write_manifest(out: Path, revision: str) -> str:
    files = {}
    for path in sorted(out.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Unexpected non-regular model artifact")
        if path.name == "manifest.json":
            continue
        if path.suffix not in {".json", ".safetensors", ".model", ".txt"}:
            raise RuntimeError("Unsupported artifact in production bundle: " + path.name)
        files[path.name] = sha256(path)
    if "config.json" not in files or "model.safetensors" not in files:
        raise RuntimeError("Model bundle is missing config or safetensors weights")
    manifest = {"revision": revision, "files": files}
    path = out / "manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    return sha256(path)

def prepare_bge(root: Path) -> str:
    out = download(BGE_REPO, BGE_REVISION, root / "embedding",
                   COMMON_FILES + ["pytorch_model.bin"])
    convert_bge_dense(out)
    return write_manifest(out, BGE_REVISION)

def prepare_reranker(root: Path) -> str:
    out = download(RERANKER_REPO, RERANKER_REVISION, root / "reranker",
                   COMMON_FILES + ["model.safetensors"])
    if sha256(out / "model.safetensors") != RERANKER_SHA256:
        raise RuntimeError("Pinned official reranker safetensors SHA-256 mismatch")
    config = json.loads((out / "config.json").read_text())
    if "ForSequenceClassification" not in " ".join(config.get("architectures", [])):
        raise RuntimeError("Reranker checkpoint is not a sequence-classification model")
    if config.get("num_labels", 1) != 1:
        raise RuntimeError("Expected a single-logit reranker")
    return write_manifest(out, RERANKER_REVISION)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    bge_digest = prepare_bge(root)
    reranker_digest = prepare_reranker(root)
    print(json.dumps({
        "embedding_path": str(root / "embedding"),
        "embedding_revision": BGE_REVISION,
        "embedding_manifest_sha256": bge_digest,
        "reranker_path": str(root / "reranker"),
        "reranker_revision": RERANKER_REVISION,
        "reranker_manifest_sha256": reranker_digest,
    }, indent=2))

if __name__ == "__main__":
    main()
