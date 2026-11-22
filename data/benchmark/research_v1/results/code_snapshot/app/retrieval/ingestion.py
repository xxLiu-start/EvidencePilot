from pathlib import Path
import re

from pypdf import PdfReader
from docx import Document

from app.retrieval.chunking import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    Chunk,
    DocumentBlock,
    chunk_blocks,
)


SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)(.*)$")
_NUMBERED_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百千万零〇\d]+[章节篇部]\s*.+"
    r"|\d+(?:\.\d+)*[.、]?\s+\S.+)$"
)


def _section(headings: list[tuple[int, str]]) -> str | None:
    return " > ".join(title for _, title in headings) or None


def _push_heading(headings: list[tuple[int, str]], level: int, title: str) -> None:
    while headings and headings[-1][0] >= level:
        headings.pop()
    headings.append((level, title))


def _text_blocks(
    text: str,
    *,
    page: int | None = None,
    headings: list[tuple[int, str]] | None = None,
    markdown: bool = False,
) -> list[DocumentBlock]:
    headings = headings if headings is not None else []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[DocumentBlock] = []
    paragraph: list[str] = []
    fence: str | None = None

    def flush() -> None:
        content = "\n".join(paragraph).strip()
        if content:
            blocks.append(DocumentBlock(content, page, _section(headings)))
        paragraph.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line) if markdown else None
        if fence is not None:
            paragraph.append(line)
            if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
                fence = None
                flush()
            i += 1
            continue
        if fence_match:
            flush()
            fence = fence_match[1]
            paragraph.append(line)
            i += 1
            continue

        atx = _ATX_HEADING.match(line) if markdown else None
        setext = (
            markdown and stripped and not paragraph and i + 1 < len(lines)
            and re.fullmatch(r" {0,3}(=+|-+)\s*", lines[i + 1])
        )
        plain_heading = (
            not markdown and len(stripped) <= 100 and _NUMBERED_HEADING.match(stripped)
        )
        if atx or setext or plain_heading:
            flush()
            if atx:
                level = len(atx[1])
                title = re.sub(r"[ \t]+#+[ \t]*$", "", atx[2]).strip()
            elif setext:
                level = 1 if lines[i + 1].lstrip().startswith("=") else 2
                title = stripped
            else:
                number = re.match(r"^(\d+(?:\.\d+)*)", stripped)
                level = number[1].count(".") + 1 if number else 1
                title = stripped
            _push_heading(headings, level, title)
            heading_text = line + ("\n" + lines[i + 1] if setext else "")
            blocks.append(DocumentBlock(heading_text.strip(), page, _section(headings)))
            i += 2 if setext else 1
            continue

        if stripped:
            paragraph.append(line)
        else:
            flush()
        i += 1
    flush()
    return blocks


def parse_document_blocks(path: str) -> list[DocumentBlock]:
    """Preserve physical PDF pages and heading hierarchy before chunking."""
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        blocks = []
        headings: list[tuple[int, str]] = []
        for page_number, page in enumerate(PdfReader(path).pages, start=1):
            blocks.extend(_text_blocks(
                page.extract_text() or "", page=page_number, headings=headings
            ))
        return blocks
    if suffix == ".docx":
        blocks = []
        headings = []
        for paragraph in Document(path).paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = paragraph.style.name if paragraph.style else ""
            heading = re.fullmatch(r"Heading\s+([1-9])", style_name, re.IGNORECASE)
            if heading:
                _push_heading(headings, int(heading[1]), text)
            elif style_name == "Title":
                _push_heading(headings, 1, text)
            # Word page numbers depend on layout; do not invent them.
            blocks.append(DocumentBlock(text, section=_section(headings)))
        return blocks
    if suffix in {".md", ".txt"}:
        return _text_blocks(
            Path(path).read_text(encoding="utf-8-sig"), markdown=suffix == ".md"
        )
    raise ValueError(f"Unsupported file type: {suffix}")


def chunk_document(
    path: str,
    *,
    document_id: str,
    source: str | None = None,
    version: str = "1",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    return chunk_blocks(
        parse_document_blocks(path),
        document_id=document_id,
        source=source if source is not None else str(path),
        version=version,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


# Preserve the original plain-text parsing interface for existing callers.
def parse_pdf(path: str) -> str:
    reader = PdfReader(path)
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def parse_docx(path: str) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def parse_markdown(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def parse_document(path: str) -> str:
    suffix = Path(path).suffix.lower()

    if suffix == ".pdf":
        return parse_pdf(path)
    if suffix == ".docx":
        return parse_docx(path)
    if suffix in {".md", ".txt"}:
        return parse_markdown(path)

    raise ValueError(f"Unsupported file type: {suffix}")
