"""Token-limited chunks with page and section provenance."""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import groupby

import tiktoken


DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 80


@dataclass(frozen=True)
class DocumentBlock:
    text: str
    page: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class Chunk:
    text: str
    document_id: str
    page: int | None
    section: str | None
    source: str
    version: str
    chunk_index: int
    token_count: int


@lru_cache(maxsize=1)
def get_tokenizer() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    # Special-looking strings in source documents are ordinary text.
    return len(get_tokenizer().encode(text, disallowed_special=()))


def _token_prefix(text: str, limit: int) -> str:
    encoding = get_tokenizer()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= limit:
        return text
    # Token boundaries may fall inside a Chinese character or emoji.
    prefix = encoding.decode_bytes(tokens[:limit]).decode("utf-8", errors="ignore")
    while prefix and count_tokens(prefix) > limit:
        prefix = prefix[:-1]
    if not prefix:
        raise ValueError("chunk_size is too small to hold a complete character")
    return prefix


def _token_suffix(text: str, limit: int) -> str:
    if not limit:
        return ""
    encoding = get_tokenizer()
    tokens = encoding.encode(text, disallowed_special=())
    suffix = encoding.decode_bytes(tokens[-limit:]).decode("utf-8", errors="ignore")
    while suffix and count_tokens(suffix) > limit:
        suffix = suffix[1:]
    return suffix


def _split_text(text: str, chunk_size: int, overlap: int) -> Iterable[str]:
    start, previous_end = 0, 0
    while start < len(text):
        prefix = _token_prefix(text[start:], chunk_size)
        end = start + len(prefix)
        # Near-total overlap can leave insufficient room for the next Unicode
        # character. Shorten that overlap rather than emitting duplicate chunks.
        while end <= previous_end:
            start += 1
            prefix = _token_prefix(text[start:], chunk_size)
            end = start + len(prefix)
        if end < len(text):
            # Prefer a complete paragraph while ensuring forward progress.
            boundary = text.rfind("\n\n", start, end + 1)
            if boundary > previous_end and count_tokens(text[start:boundary]) > overlap:
                end = boundary
        content = text[start:end]
        if content.strip():
            yield content
        if end == len(text):
            break
        suffix = _token_suffix(content, overlap)
        next_start = end - len(suffix)
        start = next_start if next_start > start else start + 1
        previous_end = end


def chunk_blocks(
    blocks: Iterable[DocumentBlock],
    *,
    document_id: str,
    source: str,
    version: str = "1",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError("Require chunk_size > 0 and 0 <= chunk_overlap < chunk_size")
    if not document_id.strip() or not source.strip() or not version.strip():
        raise ValueError("document_id, source and version must be non-empty")

    chunks = []
    nonempty = (block for block in blocks if block.text.strip())
    # Never mix provenance or carry overlap across page/section boundaries.
    for (page, section), group in groupby(nonempty, key=lambda b: (b.page, b.section)):
        text = "\n\n".join(block.text.strip() for block in group)
        for content in _split_text(text, chunk_size, chunk_overlap):
            chunks.append(Chunk(
                text=content,
                document_id=document_id,
                page=page,
                section=section,
                source=source,
                version=version,
                chunk_index=len(chunks),
                token_count=count_tokens(content),
            ))
    return chunks
