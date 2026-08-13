import importlib
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

from rag import nlp


def _load_manual_module(monkeypatch):
    """Load the real Manual chunker with parser dependencies isolated."""
    parser_module = ModuleType("deepdoc.parser")
    setattr(parser_module, "PdfParser", type("PdfParser", (), {}))
    setattr(parser_module, "DocxParser", type("DocxParser", (), {}))
    monkeypatch.setitem(sys.modules, "deepdoc.parser", parser_module)

    parser_utils_module = ModuleType("deepdoc.parser.utils")
    setattr(parser_utils_module, "extract_pdf_outlines", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "deepdoc.parser.utils", parser_utils_module)

    figure_parser_module = ModuleType("deepdoc.parser.figure_parser")
    setattr(figure_parser_module, "vision_figure_parser_pdf_wrapper", lambda tbls, **_kwargs: tbls)
    setattr(figure_parser_module, "vision_figure_parser_docx_wrapper", lambda sections, tbls, **_kwargs: (sections, tbls))
    monkeypatch.setitem(sys.modules, "deepdoc.parser.figure_parser", figure_parser_module)

    naive_module = ModuleType("rag.app.naive")
    setattr(naive_module, "by_plaintext", lambda **_kwargs: ([], [], object()))
    setattr(naive_module, "PARSERS", {})
    monkeypatch.setitem(sys.modules, "rag.app.naive", naive_module)

    module_path = Path(__file__).resolve().parents[2] / "rag" / "app" / "manual.py"
    spec = importlib.util.spec_from_file_location("test_manual_media_module", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Manual chunker from {module_path}")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _extract_positions(text):
    positions = []
    for tag in re.findall(r"@@[0-9-]+\t[0-9.\t]+##", text):
        page, left, right, top, bottom = tag.strip("#").strip("@").split("\t")
        positions.append(([int(value) - 1 for value in page.split("-")], float(left), float(right), float(top), float(bottom)))
    return positions


class _FakeMinerUParser:
    def __init__(self, page_from: int = 0):
        self.page_from = page_from

    extract_positions = staticmethod(_extract_positions)

    @staticmethod
    def remove_tag(text):
        return re.sub(r"@@[\t0-9.-]+?##", "", text)

    def crop(self, text, need_position=False, **_kwargs):
        positions = []
        for pages, left, right, top, bottom in self.extract_positions(text):
            if not pages or any(page < 0 for page in pages):
                return (None, None) if need_position else None
            positions.append((pages[0] + self.page_from, left, right, top, bottom))
        image = object() if positions else None
        return (image, positions) if need_position else image


@pytest.fixture
def lightweight_tokenizer(monkeypatch):
    rag_tokenizer = importlib.import_module("rag.nlp.rag_tokenizer")
    monkeypatch.setattr(rag_tokenizer.tokenizer, "set_language", lambda _language: None)
    monkeypatch.setattr(rag_tokenizer, "tokenize", lambda text: text.split())
    monkeypatch.setattr(rag_tokenizer, "fine_grained_tokenize", lambda tokens: tokens)


def test_tokenize_table_classifies_caption_only_figure_as_image(lightweight_tokenizer):
    chunks = nlp.tokenize_table([((None, ["Figure caption"]), [])], {"docnm_kwd": "document.pdf"}, eng=True)

    assert len(chunks) == 1
    assert chunks[0]["doc_type_kwd"] == "image"
    assert chunks[0]["content_with_weight"] == "Figure caption"
    assert "image" not in chunks[0]


def test_tokenize_table_drops_empty_media_without_an_image(lightweight_tokenizer):
    chunks = nlp.tokenize_table([((None, [""]), [])], {"docnm_kwd": "document.pdf"}, eng=True)

    assert chunks == []


def test_tokenize_table_keeps_image_without_caption(lightweight_tokenizer):
    image = object()

    chunks = nlp.tokenize_table([((image, []), [])], {"docnm_kwd": "document.pdf"}, eng=True)

    assert len(chunks) == 1
    assert chunks[0]["doc_type_kwd"] == "image"
    assert chunks[0]["content_with_weight"] == ""
    assert chunks[0]["image"] is image


def test_pdf_media_context_preserves_items_without_positions(monkeypatch):
    parser_module = ModuleType("deepdoc.parser")
    setattr(parser_module, "PdfParser", type("PdfParser", (), {"extract_positions": staticmethod(lambda _text: [])}))
    monkeypatch.setitem(sys.modules, "deepdoc.parser", parser_module)
    media_item = ((object(), ["description"]), [])

    assert nlp.append_context2table_image4pdf([], [media_item], table_context_size=32) == [media_item]
    assert nlp.append_context2table_image4pdf([], [media_item], table_context_size=32, return_context=True) == [("", "")]


@pytest.mark.parametrize(
    "sections",
    [
        [("Before", "@@1\t0.0\t100.0\t0.0\t20.0##")],
        [("Before", 0, [(0, 0.0, 100.0, 0.0, 20.0)])],
        [("Before@@1\t0.0\t100.0\t0.0\t20.0##", "text")],
    ],
    ids=["raw", "manual", "paper"],
)
def test_pdf_media_context_offsets_mineru_section_pages(monkeypatch, sections):
    parser_module = ModuleType("deepdoc.parser")
    setattr(parser_module, "PdfParser", type("PdfParser", (), {"extract_positions": staticmethod(_extract_positions)}))
    monkeypatch.setitem(sys.modules, "deepdoc.parser", parser_module)
    media = [((None, "<table><tr><td>Cell</td></tr></table>"), [(13, 0.0, 100.0, 40.0, 80.0)])]

    contextualized = nlp.append_context2table_image4pdf(
        sections,
        media,
        table_context_size=32,
        section_page_offset=13,
    )

    assert contextualized[0][0][1].startswith("Before")


def test_manual_chunk_preserves_mineru_local_tag_and_global_page(monkeypatch, lightweight_tokenizer):
    manual = _load_manual_module(monkeypatch)
    parser = _FakeMinerUParser(page_from=13)
    sections = [("Document body", "text", "@@1\t10.0\t190.0\t50.0\t100.0##")]
    parsed = Mock(return_value=(sections, [], parser))
    wrapper_calls = []

    monkeypatch.setattr(manual, "PARSERS", {"mineru": parsed})
    monkeypatch.setattr(manual, "normalize_layout_recognizer", lambda value: (value, None))
    monkeypatch.setattr(manual, "extract_pdf_outlines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manual, "bullets_category", lambda texts: [0] * len(texts))
    monkeypatch.setattr(manual, "title_frequency", lambda _bullets, titled: (0, [0] * len(titled)))
    monkeypatch.setattr(manual, "num_tokens_from_string", lambda text: len(text.split()))

    def capture_wrapper(tbls, **kwargs):
        wrapper_calls.append(kwargs)
        return tbls

    monkeypatch.setattr(manual, "vision_figure_parser_pdf_wrapper", capture_wrapper)

    chunks = manual.chunk(
        "document.pdf",
        binary=b"%PDF-1.4 fake",
        from_page=13,
        to_page=14,
        lang="English",
        callback=lambda *_args, **_kwargs: None,
        parser_config={"layout_recognize": "MinerU", "chunk_token_num": 128, "delimiter": "\n"},
    )

    text_chunks = [chunk for chunk in chunks if chunk.get("doc_type_kwd") != "image"]
    assert len(text_chunks) == 1
    assert text_chunks[0]["page_num_int"] == [14]
    assert text_chunks[0]["position_int"][0][0] == 14
    assert wrapper_calls[0]["section_page_offset"] == 13


@pytest.mark.parametrize(("parser_name", "folds_media_into_text"), [("MinerU", False), ("Docling", True)])
def test_manual_chunk_only_avoids_media_refolding_for_mineru(monkeypatch, parser_name, folds_media_into_text):
    manual = _load_manual_module(monkeypatch)
    table_html = "<table><tr><td>Table cell</td></tr></table>"
    image = object()
    sections = [("Document body", "text", [(0, 0, 100, 0, 20)])]
    media = [
        ((None, table_html), [(0, 0, 100, 30, 60)]),
        ((image, ["Figure caption"]), [(0, 0, 100, 70, 90)]),
    ]
    parsed = Mock(return_value=(sections, media, object()))
    text_chunks = []

    monkeypatch.setattr(manual, "PARSERS", {parser_name.lower(): parsed})
    monkeypatch.setattr(manual, "normalize_layout_recognizer", lambda value: (value, None))
    monkeypatch.setattr(manual, "extract_pdf_outlines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(manual, "bullets_category", lambda texts: [0] * len(texts))
    monkeypatch.setattr(manual, "title_frequency", lambda _bullets, titled: (0, [0] * len(titled)))
    monkeypatch.setattr(manual, "num_tokens_from_string", lambda text: len(text.split()))
    monkeypatch.setattr(manual.rag_tokenizer, "tokenize", lambda text: text.split())
    monkeypatch.setattr(manual.rag_tokenizer, "fine_grained_tokenize", lambda tokens: tokens)
    monkeypatch.setattr(manual, "vision_figure_parser_pdf_wrapper", lambda tbls, **_kwargs: tbls)

    def capture_text_chunks(chunks, *_args, **_kwargs):
        text_chunks.extend(chunks)
        return [{"content_with_weight": chunk, "doc_type_kwd": "text"} for chunk in chunks]

    def capture_media_chunks(tbls, *_args, **_kwargs):
        result = []
        for (media_image, rows), _positions in tbls:
            text = rows if isinstance(rows, str) else "\n".join(rows)
            result.append(
                {
                    "content_with_weight": text,
                    "doc_type_kwd": "table" if isinstance(rows, str) else "image",
                    **({"image": media_image} if media_image is not None else {}),
                }
            )
        return result

    monkeypatch.setattr(manual, "tokenize_chunks", capture_text_chunks)
    monkeypatch.setattr(manual, "tokenize_table", capture_media_chunks)

    chunks = manual.chunk(
        "document.pdf",
        binary=b"%PDF-1.4 fake",
        lang="English",
        callback=lambda *_args, **_kwargs: None,
        parser_config={"layout_recognize": parser_name, "chunk_token_num": 128, "delimiter": "\n"},
    )

    text_payload = "\n".join(text_chunks)
    assert (table_html in text_payload) is folds_media_into_text
    assert ("Figure caption" in text_payload) is folds_media_into_text
    combined_payload = "\n".join(chunk["content_with_weight"] for chunk in chunks)
    if parser_name == "MinerU":
        assert combined_payload.count(table_html) == 1
        assert combined_payload.count("Figure caption") == 1
