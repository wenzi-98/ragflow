import logging
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image

import deepdoc.parser.mineru_parser as mineru_parser_module
import rag.flow.parser.parser as parser_module


def _build_flow_parser(monkeypatch, pdf_parser):
    monkeypatch.setattr(parser_module.TenantModelService, "get_by_id", Mock(return_value=(False, None)))
    monkeypatch.setattr(parser_module, "resolve_model_config", Mock(return_value={"llm_name": "mineru-model"}))
    monkeypatch.setattr(parser_module, "LLMBundle", Mock(return_value=SimpleNamespace(mdl=pdf_parser)))
    monkeypatch.setattr(parser_module, "enhance_media_sections_with_vision", lambda *_args, **_kwargs: None)

    parser = object.__new__(parser_module.Parser)
    parser._param = SimpleNamespace(
        setups={
            "pdf": {
                "parse_method": "MinerU",
                "mineru_llm_name": "mineru-model",
                "lang": "English",
                "output_format": "markdown",
                "flatten_media_to_text": False,
                "remove_toc": False,
                "remove_header_footer": False,
            }
        },
        outputs={},
    )
    parser._canvas = SimpleNamespace(_tenant_id="tenant-id", _language="English")
    parser.callback = Mock()
    return parser


def test_pdf_mineru_markdown_preserves_source_order_and_empty_media_return(monkeypatch) -> None:
    mineru_parser = mineru_parser_module.MinerUParser()
    outputs = [
        {"type": mineru_parser_module.MinerUContentType.TEXT, "text": "Before", "page_idx": 0, "bbox": [5, 5, 45, 15]},
        {
            "type": mineru_parser_module.MinerUContentType.TABLE,
            "table_body": "<table><tr><td>Cell</td></tr></table>",
            "table_caption": [],
            "table_footnote": [],
            "page_idx": 0,
            "bbox": [5, 20, 45, 40],
        },
        {
            "type": mineru_parser_module.MinerUContentType.IMAGE,
            "image_caption": ["Figure caption"],
            "image_footnote": ["Figure footnote"],
            "page_idx": 0,
            "bbox": [5, 45, 45, 70],
        },
        {"type": mineru_parser_module.MinerUContentType.TEXT, "text": "After", "page_idx": 0, "bbox": [5, 75, 45, 85]},
    ]

    def render_pages(_pdf, zoomin=1, page_from=0, page_to=mineru_parser_module.MAXIMUM_PAGE_NUMBER, callback=None):
        mineru_parser.page_from = page_from
        mineru_parser.page_to = page_to
        mineru_parser.page_images = [Image.new("RGB", (100, 100), "white")]
        mineru_parser.page_sizes = {0: (100, 100)}

    monkeypatch.setattr(mineru_parser_module, "extract_pdf_outlines", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(mineru_parser, "__images__", render_pages)
    monkeypatch.setattr(mineru_parser, "_run_mineru", lambda _input_path, output_dir, _options, **_kwargs: output_dir)
    monkeypatch.setattr(mineru_parser, "_read_output", lambda *_args, **_kwargs: outputs)

    parse_results = []
    parse_pdf = mineru_parser.parse_pdf

    def record_parse_result(*args, **kwargs):
        result = parse_pdf(*args, **kwargs)
        parse_results.append(result)
        return result

    monkeypatch.setattr(mineru_parser, "parse_pdf", record_parse_result)
    monkeypatch.setattr(parser_module.VLM, "image2base64", staticmethod(lambda _image: "data:image/png;base64,figure"))
    parser = _build_flow_parser(monkeypatch, mineru_parser)

    parser._pdf("ordered.pdf", b"%PDF-1.4 fake", file={"id": "document-id"})

    assert parse_results and parse_results[0][1] == []
    assert parser.output("markdown") == ("Before\n<table><tr><td>Cell</td></tr></table>\n\n![Image](data:image/png;base64,figure)\nFigure caption\nFigure footnote\nAfter\n")


def test_pdf_markdown_preserves_text_when_figure_image_is_unavailable(monkeypatch, caplog) -> None:
    class TextOnlyMinerUParser:
        outlines = []

        @staticmethod
        def parse_pdf(**_kwargs):
            return [
                ("Caption without image", "image", ""),
                ("", "image", ""),
                ("Document body", "text", ""),
            ], []

        @staticmethod
        def extract_positions(_position_tag):
            return []

        @staticmethod
        def crop(_position_tag, _zoomin):
            return None

    image_calls = []
    monkeypatch.setattr(parser_module.VLM, "image2base64", staticmethod(lambda image: image_calls.append(image) or "unused"))
    parser = _build_flow_parser(monkeypatch, TextOnlyMinerUParser())

    with caplog.at_level(logging.WARNING):
        parser._pdf("document.pdf", b"%PDF-1.4 fake")

    assert parser.output("markdown") == "Caption without image\nDocument body\n"
    assert image_calls == []
    warnings = [record.getMessage() for record in caplog.records if "Skipping empty figure in markdown output" in record.getMessage()]
    assert warnings == ["Skipping empty figure in markdown output for document.pdf (parse_method=MinerU)."]
