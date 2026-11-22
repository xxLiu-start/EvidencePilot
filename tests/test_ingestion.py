from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
import pytest

from app.retrieval.ingestion import chunk_document, parse_document, parse_document_blocks


def test_markdown_nested_headings_and_code_fences(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text(
        "# Guide\n\nIntro.\n\n## Setup\n\nFirst paragraph.\n\n"
        "```python\n# Not a heading\n\nprint('example')\n```\n\n"
        "## Usage\n\nLast paragraph.", encoding="utf-8",
    )
    chunks = chunk_document(str(path), document_id="doc")
    assert [c.section for c in chunks] == ["Guide", "Guide > Setup", "Guide > Usage"]
    assert "# Not a heading" in chunks[1].text
    assert "print('example')" in chunks[1].text
    assert all(c.page is None and c.source == str(path) and c.version == "1" for c in chunks)
    assert parse_document(str(path)) == path.read_text(encoding="utf-8")


def test_setext_headings_and_bom(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("\ufeffGuide\n=====\n\nIntro\n\nDetails\n-------\n\nBody", encoding="utf-8")
    chunks = chunk_document(str(path), document_id="doc")
    assert [c.section for c in chunks] == ["Guide", "Guide > Details"]
    assert not chunks[0].text.startswith("\ufeff")


def test_hash_in_heading_title_is_preserved(tmp_path):
    path = tmp_path / "languages.md"
    path.write_text("# C#\n\nIntro\n\n## Examples ###\n\nBody", encoding="utf-8")
    assert [c.section for c in chunk_document(str(path), document_id="doc")] == [
        "C#", "C# > Examples"
    ]


def test_docx_heading_styles(tmp_path):
    path = tmp_path / "guide.docx"
    document = Document()
    document.add_heading("Guide", level=1)
    document.add_paragraph("Intro")
    document.add_heading("Setup", level=2)
    document.add_paragraph("安装说明。")
    document.save(path)
    chunks = chunk_document(str(path), document_id="doc")
    assert [c.section for c in chunks] == ["Guide", "Guide > Setup"]
    assert all(c.page is None for c in chunks)
    assert "安装说明。" in chunks[1].text


def test_pdf_preserves_physical_page_numbers_and_section_across_blank_page(tmp_path):
    path = tmp_path / "guide.pdf"
    writer = PdfWriter()
    for text in ["1 Introduction", "", "continued on page three"]:
        page = writer.add_blank_page(width=612, height=792)
        if text:
            font = DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            })
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
            })
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = stream
    writer.write(path)
    chunks = chunk_document(str(path), document_id="pdf")
    assert [c.page for c in chunks] == [1, 3]
    assert [c.section for c in chunks] == ["1 Introduction", "1 Introduction"]
    assert chunks[1].text == "continued on page three"


def test_plain_text_and_unsupported_format(tmp_path):
    path = tmp_path / "guide.txt"
    path.write_text("第一章 概述\n\n正文。\n\n第二章 使用\n\n更多正文。", encoding="utf-8")
    assert [c.section for c in chunk_document(str(path), document_id="doc")] == [
        "第一章 概述", "第二章 使用"
    ]
    with pytest.raises(ValueError, match="Unsupported file type"):
        parse_document_blocks(str(tmp_path / "file.html"))
