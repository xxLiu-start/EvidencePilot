import pytest

from app.retrieval.chunking import DocumentBlock, chunk_blocks, count_tokens, get_tokenizer


def split(blocks, **kwargs):
    return chunk_blocks(blocks, document_id="doc-1", source="manual.md", version="v2", **kwargs)


def test_default_size_overlap_and_metadata():
    text = "word " * 1400
    chunks = split([DocumentBlock(text, page=3, section="Guide > Setup")])
    assert len(chunks) > 2
    assert chunks[0].token_count == 500
    reconstructed = chunks[0].text
    encoding = get_tokenizer()
    for previous, current in zip(chunks, chunks[1:]):
        suffix = encoding.decode(encoding.encode(previous.text)[-80:])
        assert current.text.startswith(suffix)
        reconstructed += current.text[len(suffix):]
    assert reconstructed == text.strip()
    for index, chunk in enumerate(chunks):
        assert chunk.token_count == count_tokens(chunk.text) <= 500
        assert chunk.chunk_index == index
        assert (chunk.document_id, chunk.page, chunk.section, chunk.source, chunk.version) == (
            "doc-1", 3, "Guide > Setup", "manual.md", "v2"
        )


def test_prefer_paragraph_boundaries():
    first = ("alpha " * 200).strip()
    second = ("beta " * 400).strip()
    chunks = split([DocumentBlock(first), DocumentBlock(second)])
    assert chunks[0].text == first
    assert all(chunk.token_count <= 500 for chunk in chunks)
    assert chunks[-1].text.endswith(second[-100:])


def test_never_mix_pages_or_sections():
    chunks = split([
        DocumentBlock("first page", 1, "A"),
        DocumentBlock("second page", 2, "A"),
        DocumentBlock("new section", 2, "B"),
    ])
    assert [(c.text, c.page, c.section) for c in chunks] == [
        ("first page", 1, "A"), ("second page", 2, "A"), ("new section", 2, "B")
    ]


@pytest.mark.parametrize("overlap", [0, 8, 31])
def test_unicode_is_lossless_even_when_tokens_split_characters(overlap):
    text = "".join(f"{i}:证据检索与元数据🙂🚀 café <|endoftext|>。" for i in range(50))
    chunks = split([DocumentBlock(text)], chunk_size=32, chunk_overlap=overlap)
    reconstructed = chunks[0].text
    for previous, current in zip(chunks, chunks[1:]):
        # Find overlap in characters, so a split UTF-8 token cannot hide corruption.
        matching = [
            n for n in range(min(len(previous.text), len(current.text)) + 1)
            if previous.text.endswith(current.text[:n])
            and count_tokens(current.text[:n]) <= overlap
        ]
        reconstructed += current.text[max(matching):]
    assert reconstructed == text
    assert all("\ufffd" not in c.text and 0 < c.token_count <= 32 for c in chunks)


@pytest.mark.parametrize("size,overlap", [(0, 0), (-1, 0), (20, -1), (20, 20), (20, 21)])
def test_invalid_chunk_parameters(size, overlap):
    with pytest.raises(ValueError):
        split([DocumentBlock("text")], chunk_size=size, chunk_overlap=overlap)


def test_blank_blocks_produce_no_chunks():
    assert split([DocumentBlock(" \n "), DocumentBlock("")]) == []


@pytest.mark.parametrize("field", ["source", "version", "document_id"])
def test_provenance_cannot_be_blank(field):
    kwargs = dict(document_id="id", source="source", version="1")
    kwargs[field] = " "
    with pytest.raises(ValueError):
        chunk_blocks([DocumentBlock("text")], **kwargs)
