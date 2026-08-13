import importlib
import importlib.util
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
