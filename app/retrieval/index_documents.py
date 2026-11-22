"""CLI: python -m app.retrieval.index_documents FILE [FILE ...] or --backfill."""

import argparse
import json
from pathlib import Path

from app.db.repository import backfill_embeddings, ingest_document
from app.db.schema import initialize_database
from app.db.session import SessionLocal, engine
from app.retrieval.chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from app.retrieval.ingestion import SUPPORTED_SUFFIXES


def main() -> None:
    parser = argparse.ArgumentParser(description="BGE-M3 document embedding and pgvector ingestion")
    parser.add_argument("paths", nargs="*", type=Path, help="Files or document directories")
    parser.add_argument("--recursive", action="store_true", help="Include nested directories")
    parser.add_argument("--backfill", action="store_true", help="Embed existing chunks missing vectors")
    parser.add_argument("--batch-size", type=int, default=16, help="Backfill transaction size")
    parser.add_argument("--source", help="Source metadata for uploaded files")
    parser.add_argument("--version", default="1")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    args = parser.parse_args()
    if bool(args.paths) == args.backfill:
        parser.error("Specify document paths or --backfill")
    if args.batch_size <= 0 or args.chunk_size <= 0 or not 0 <= args.chunk_overlap < args.chunk_size:
        parser.error("Require positive batch/chunk sizes and 0 <= chunk-overlap < chunk-size")
    if not args.version.strip() or (args.source is not None and not args.source.strip()):
        parser.error("Source and version must be non-empty")

    paths: list[Path] = []
    for path in args.paths:
        if not path.exists():
            parser.error(f"Path does not exist: {path}")
        if path.is_dir():
            paths.extend(sorted(
                p for p in (path.rglob("*") if args.recursive else path.iterdir())
                if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
            ))
        elif path.suffix.lower() in SUPPORTED_SUFFIXES:
            paths.append(path)
        else:
            parser.error(f"Unsupported document format: {path}")
    paths = list(dict.fromkeys(p.resolve() for p in paths))
    if not args.backfill and not paths:
        parser.error("No supported documents found")

    initialize_database(engine)
    with SessionLocal() as db:
        if args.backfill:
            count = backfill_embeddings(db, batch_size=args.batch_size)
            print(json.dumps({"embedded_chunks": count}))
        else:
            for path in paths:
                document = ingest_document(
                    db, str(path), source=args.source, version=args.version,
                    chunk_size=args.chunk_size, chunk_overlap=args.chunk_overlap,
                )
                print(json.dumps({
                    "document_id": str(document.id), "source": document.source,
                    "chunk_count": document.chunk_count,
                }, ensure_ascii=False))


if __name__ == "__main__":
    main()
