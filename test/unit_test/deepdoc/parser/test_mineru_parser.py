import importlib.util
import json
import logging
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType

import pytest


def _load_mineru_parser(monkeypatch):
    repo_root = Path(__file__).resolve().parents[4]

    deepdoc_mod = ModuleType("deepdoc")
    deepdoc_mod.__path__ = [str(repo_root / "deepdoc")]
    monkeypatch.setitem(sys.modules, "deepdoc", deepdoc_mod)

    parser_mod = ModuleType("deepdoc.parser")
    parser_mod.__path__ = [str(repo_root / "deepdoc" / "parser")]
    monkeypatch.setitem(sys.modules, "deepdoc.parser", parser_mod)

    pdf_parser_mod = ModuleType("deepdoc.parser.pdf_parser")

    class _RAGFlowPdfParser:
        pass

    setattr(pdf_parser_mod, "RAGFlowPdfParser", _RAGFlowPdfParser)
    monkeypatch.setitem(sys.modules, "deepdoc.parser.pdf_parser", pdf_parser_mod)

    utils_mod = ModuleType("deepdoc.parser.utils")
    setattr(utils_mod, "extract_pdf_outlines", lambda *_args, **_kwargs: [])
    monkeypatch.setitem(sys.modules, "deepdoc.parser.utils", utils_mod)

    module_name = "test_mineru_parser_unit_module"
    module_path = repo_root / "deepdoc" / "parser" / "mineru_parser.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load MinerU parser from {module_path}")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.p1
@pytest.mark.parametrize(
    ("language", "expected_language"),
    [
        ("Japanese", "JAPAN"),
        ("", "EN"),
        (None, "EN"),
    ],
)
def test_parse_pdf_normalizes_dataset_language(monkeypatch, tmp_path, language, expected_language):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    output_dir = tmp_path / "output"
    captured = {}

    def capture_options(_input_path, _output_dir, options, **_kwargs):
        captured["language"] = options.lang
        return output_dir

    monkeypatch.setattr(parser, "__images__", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(parser, "_run_mineru", capture_options)
    monkeypatch.setattr(parser, "_read_output", lambda *_args, **_kwargs: [])

    language_kwargs = {} if language is None else {"lang": language}
    parser.parse_pdf(
        filepath=tmp_path / "document.pdf",
        binary=b"%PDF-1.4 fake",
        output_dir=str(output_dir),
        delete_output=False,
        **language_kwargs,
    )

    assert captured["language"] == getattr(module.MinerULanguage, expected_language)


def test_sanitize_section_text_removes_escaped_html_tags(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    text = "&lt;table&gt;&lt;tr&gt;&lt;td&gt;Alpha&lt;/td&gt;&lt;td&gt;Beta&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;"

    sanitized = module.MinerUParser._sanitize_section_text(text)

    assert sanitized == "AlphaBeta"
    assert "<td>" not in sanitized
    assert "</td>" not in sanitized


def test_transfer_to_sections_logs_tables_dropped_after_sanitization(monkeypatch, caplog):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": "&lt;td&gt;&lt;/td&gt;",
            "table_caption": [],
            "table_footnote": [],
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        }
    ]

    with caplog.at_level(logging.DEBUG, logger=parser.logger.name):
        sections = parser._transfer_to_sections(outputs, parse_method="pipeline")

    assert sections == []
    assert "Skip empty section after normalization" in caplog.text
    assert f"type={module.MinerUContentType.TABLE}" in caplog.text


def test_transfer_to_sections_preserves_non_table_angle_brackets_and_entities(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Use List<String> and a<b. 5 &lt; 6",
        },
        {
            "type": module.MinerUContentType.CODE,
            "code_body": "template<typename T> void f();",
            "code_caption": [],
        },
        {
            "type": module.MinerUContentType.EQUATION,
            "text": "x<T and 5 &lt; 6",
        },
        {
            "type": module.MinerUContentType.LIST,
            "list_items": ["List<String>", "5 &lt; 6"],
        },
    ]
    expected_texts = [
        "Use List<String> and a<b. 5 &lt; 6",
        "template<typename T> void f();",
        "x<T and 5 &lt; 6",
        "List<String>\n5 &lt; 6",
    ]

    for table_enable in (False, True):
        sections = parser._transfer_to_sections(outputs, parse_method="raw", table_enable=table_enable)
        assert [section[0] for section in sections] == expected_texts


def test_transfer_to_sections_skips_page_chrome_without_duplicating_text(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    fixture_path = Path(__file__).resolve().parents[3] / "fixtures" / "mineru" / "bmw_page_chrome_content_list.json"
    outputs = __import__("json").loads(fixture_path.read_text(encoding="utf-8"))

    sections = parser._transfer_to_sections(outputs, parse_method="raw")
    texts = [section[0] for section in sections]

    assert texts == ["打开和关闭", "车辆装备", "车辆钥匙", "概述", "安全提示"]
    assert texts.count("打开和关闭") == 1
    assert texts.count("概述") == 1
    assert "77" not in texts
    assert "Online Edition for Part no." not in " ".join(texts)


def test_paper_routes_tables_and_images_only_to_media_blocks(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    media_image = object()
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: media_image)
    table_html = "<table><tr><td>Table cell</td></tr></table>"
    outputs = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Document text",
        },
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": table_html,
            "table_caption": [],
            "table_footnote": [],
        },
        {
            "type": module.MinerUContentType.IMAGE,
            "image_caption": ["Figure caption"],
            "image_footnote": [],
        },
    ]

    sections = parser._transfer_to_sections(outputs, parse_method="paper", table_enable=True)
    media_blocks = parser._transfer_to_media_blocks(outputs, table_enable=True)

    assert sections == [("Document text", module.MinerUContentType.TEXT.value)]
    assert media_blocks == [
        ((media_image, table_html), []),
        ((media_image, ["Figure caption"]), []),
    ]


def test_raw_and_manual_route_tables_and_images_only_to_media_blocks(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {"type": module.MinerUContentType.TEXT, "text": "Before media"},
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": "<table><tr><td>Table cell</td></tr></table>",
            "table_caption": [],
            "table_footnote": [],
        },
        {
            "type": module.MinerUContentType.IMAGE,
            "image_caption": ["Figure caption"],
            "image_footnote": [],
        },
        {"type": module.MinerUContentType.TEXT, "text": "After media"},
    ]

    for parse_method in ("raw", "manual"):
        sections = parser._transfer_to_sections(outputs, parse_method=parse_method, table_enable=True)
        assert [section[0] for section in sections] == ["Before media", "After media"]


def test_pipeline_preserves_text_table_image_text_order(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_images = [module.Image.new("RGB", (100, 200), "white")]
    table_html = "<table><tr><td>Table cell</td></tr></table>"
    outputs = [
        {"type": module.MinerUContentType.TEXT, "text": "Before media"},
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": table_html,
            "table_caption": [],
            "table_footnote": [],
        },
        {
            "type": module.MinerUContentType.IMAGE,
            "image_caption": [],
            "image_footnote": [],
            "page_idx": 0,
            "bbox": [100, 100, 900, 900],
        },
        {"type": module.MinerUContentType.TEXT, "text": "After media"},
    ]

    sections = parser._transfer_to_sections(outputs, parse_method="pipeline", table_enable=True)

    assert [section[0] for section in sections] == ["Before media", table_html, "", "After media"]
    assert [section[1] for section in sections] == ["text", "table", "image", "text"]


def test_pipeline_drops_empty_image_without_crop_position(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    sections = parser._transfer_to_sections(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "image_caption": [],
                "image_footnote": [],
            }
        ],
        parse_method="pipeline",
        table_enable=True,
    )

    assert sections == []


def test_pipeline_keeps_empty_table_with_renderable_position(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_images = [_FakePageImage(200, 400)]

    sections = parser._transfer_to_sections(
        [
            {
                "type": module.MinerUContentType.TABLE,
                "table_body": "",
                "table_caption": [],
                "table_footnote": [],
                "page_idx": 0,
                "bbox": [100, 100, 900, 900],
            }
        ],
        parse_method="pipeline",
        table_enable=True,
    )

    assert sections == [("", "table", "@@1\t20.0\t180.0\t40.0\t360.0##")]


def test_transfer_to_sections_keeps_tables_when_media_is_disabled_by_default(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": "<table><tr><td>Table cell</td></tr></table>",
            "table_caption": [],
            "table_footnote": [],
        }
    ]

    assert parser._transfer_to_sections(outputs, parse_method="raw") == [("Table cell", "")]


def test_transfer_to_sections_skips_unknown_types_without_duplicating_text(monkeypatch, caplog):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outputs = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Primary content",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        },
        {
            "type": "sidebar",
            "text": "Should not repeat previous section",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        },
        {
            "type": module.MinerUContentType.TEXT,
            "text": "Next content",
            "page_idx": 0,
            "bbox": (0, 0, 1, 1),
        },
    ]

    with caplog.at_level(logging.DEBUG, logger=parser.logger.name):
        sections = parser._transfer_to_sections(outputs, parse_method="raw")

    assert [section[0] for section in sections] == ["Primary content", "Next content"]
    assert "Skip unsupported section type=sidebar" in caplog.text


def test_build_image_texts_uses_only_mineru_caption_and_footnote(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    image_texts = parser._build_image_texts(
        {
            "type": module.MinerUContentType.IMAGE,
            "image_caption": ["Figure caption"],
            "image_footnote": ["Figure footnote"],
            "vlm_description": "Description supplied by a caller",
        }
    )

    assert image_texts == ["Figure caption", "Figure footnote"]
    assert not hasattr(parser, "_enhance_images_with_vlm")


def test_build_table_text_returns_empty_without_real_content(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    assert parser._build_table_text({"table_body": "", "table_caption": [], "table_footnote": []}) == ""


def test_media_blocks_drop_empty_image_without_resource(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: None)

    media_blocks = parser._transfer_to_media_blocks(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "image_caption": [],
                "image_footnote": [],
            }
        ]
    )

    assert media_blocks == []


def test_media_blocks_keep_caption_only_image_classification(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: None)

    media_blocks = parser._transfer_to_media_blocks(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "image_caption": ["Figure caption"],
                "image_footnote": [],
            }
        ]
    )

    assert media_blocks == [((None, ["Figure caption"]), [])]


def test_media_blocks_keep_image_without_caption_placeholder(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    image = object()
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: image)

    media_blocks = parser._transfer_to_media_blocks(
        [
            {
                "type": module.MinerUContentType.IMAGE,
                "image_caption": [],
                "image_footnote": [],
            }
        ]
    )

    assert media_blocks == [((image, [""]), [])]


def test_media_blocks_drop_empty_table_without_resource(monkeypatch, caplog):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: None)

    with caplog.at_level(logging.WARNING, logger=parser.logger.name):
        media_blocks = parser._transfer_to_media_blocks(
            [
                {
                    "type": module.MinerUContentType.TABLE,
                    "table_body": "",
                    "table_caption": [],
                    "table_footnote": [],
                }
            ]
        )

    assert media_blocks == []
    assert "Skip empty table without text or renderable image" in caplog.text
    assert "FAILED TO PARSE TABLE" not in caplog.text


def test_media_blocks_keep_empty_table_with_image(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    image = object()
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: image)

    media_blocks = parser._transfer_to_media_blocks(
        [
            {
                "type": module.MinerUContentType.TABLE,
                "table_body": "",
                "table_caption": [],
                "table_footnote": [],
            }
        ]
    )

    assert media_blocks == [((image, [""]), [])]


class _FakeHeadResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, True),
        (404, True),
        (405, True),
        (499, True),
        (500, False),
        (503, False),
    ],
)
def test_http_endpoint_reachability_uses_status_class(monkeypatch, status_code, expected):
    module = _load_mineru_parser(monkeypatch)
    monkeypatch.setattr(module.requests, "head", lambda *_args, **_kwargs: _FakeHeadResponse(status_code))

    assert module.MinerUParser._is_http_endpoint_valid("http://service.local") is expected


def test_http_endpoint_network_error_is_unavailable(monkeypatch):
    module = _load_mineru_parser(monkeypatch)

    def raise_connection_error(*_args, **_kwargs):
        raise module.requests.ConnectionError("connection refused")

    monkeypatch.setattr(module.requests, "head", raise_connection_error)

    assert module.MinerUParser._is_http_endpoint_valid("http://service.local") is False


@pytest.mark.parametrize(("server_status", "expected_available"), [(404, True), (405, True), (500, False)])
def test_check_installation_validates_vlm_http_server(monkeypatch, server_status, expected_available):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.local", mineru_server_url="http://vlm.local")

    def fake_head(url, **_kwargs):
        return _FakeHeadResponse(200 if url.endswith("/openapi.json") else server_status)

    monkeypatch.setattr(module.requests, "head", fake_head)

    available, reason = parser.check_installation(backend="vlm-http-client")

    assert available is expected_available
    if expected_available:
        assert reason == ""
    else:
        assert "vlm-http-client server not accessible" in reason


class _FakeZipResponse:
    """Stand-in for the streaming response returned by requests.post.

    Provides the minimum surface that _run_mineru_api touches: status code,
    headers (Content-Type), and a `.raw` stream that copyfileobj can drain.
    """

    def __init__(self, body: bytes = b"zip-bytes"):
        self._body = body
        self.headers = {"Content-Type": "application/zip"}
        self.raw = BytesIO(body)

    def raise_for_status(self):
        return None


class _FakePostContext:
    def __init__(self, response: _FakeZipResponse, captured: dict):
        self._response = response
        self._captured = captured

    def __enter__(self):
        return self._response

    def __exit__(self, exc_type, exc, tb):
        return False


def _capture_run_mineru_api(monkeypatch, module, *, pdf_path: Path, extracted_dir: Path):
    """Stub everything around requests.post so _run_mineru_api runs end-to-end
    against an in-memory response. Returns the captured kwargs dict.
    """
    captured: dict = {}

    def fake_post(url, files, data, headers, timeout, stream):
        captured["url"] = url
        captured["data"] = data
        captured["files"] = files
        return _FakePostContext(_FakeZipResponse(), captured)

    monkeypatch.setattr(module.requests, "post", fake_post)
    monkeypatch.setattr(module.os.path, "exists", lambda _p: True)
    monkeypatch.setattr(
        module.MinerUParser,
        "_extract_zip_no_root",
        lambda self, *_a, **_kw: None,
    )
    monkeypatch.setattr(
        module.shutil,
        "copyfileobj",
        lambda _src, _dst: None,
    )
    import tempfile

    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix="", dir=None: str(extracted_dir))
    return captured


def test_run_mineru_api_threads_page_range_into_request_payload(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.local")
    parser.mineru_server_url = ""

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    extracted_dir = tmp_path / "out"
    extracted_dir.mkdir()

    captured = _capture_run_mineru_api(monkeypatch, module, pdf_path=pdf_path, extracted_dir=extracted_dir)
    options = module.MinerUParseOptions()

    # Mid-document range: pages 0..12 inclusive in RAGFlow slice terms.
    parser._run_mineru_api(
        pdf_path,
        extracted_dir,
        options,
        callback=None,
        page_from=0,
        page_to=13,
    )

    assert captured["data"]["start_page_id"] == 0
    assert captured["data"]["end_page_id"] == 12

    # End-of-document range: still need the full doc to come back.
    captured.clear()
    parser._run_mineru_api(
        pdf_path,
        extracted_dir,
        options,
        callback=None,
        page_from=5,
        page_to=20,
    )

    assert captured["data"]["start_page_id"] == 5
    assert captured["data"]["end_page_id"] == 19


def test_run_mineru_api_uses_full_document_when_no_range_given(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.local")
    parser.mineru_server_url = ""

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    extracted_dir = tmp_path / "out"
    extracted_dir.mkdir()

    captured = _capture_run_mineru_api(monkeypatch, module, pdf_path=pdf_path, extracted_dir=extracted_dir)
    options = module.MinerUParseOptions()

    # No page_from/page_to: defaults should keep the prior behavior (0 / 99999).
    parser._run_mineru_api(pdf_path, extracted_dir, options, callback=None)

    assert captured["data"]["start_page_id"] == 0
    assert captured["data"]["end_page_id"] == 99999


def test_end_page_minus_one_normalizes_for_mineru_api(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser(mineru_api="http://mineru.local")
    parser.mineru_server_url = ""

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    extracted_dir = tmp_path / "out"
    extracted_dir.mkdir()

    captured = _capture_run_mineru_api(monkeypatch, module, pdf_path=pdf_path, extracted_dir=extracted_dir)
    options = module.MinerUParseOptions()

    # RAGFlow to_page is exclusive (Python slice stop); MinerU end_page_id is
    # 0-based inclusive, so to_page - 1 is the correct translation.
    parser._run_mineru_api(
        pdf_path,
        extracted_dir,
        options,
        callback=None,
        page_from=0,
        page_to=13,
    )

    assert captured["data"]["end_page_id"] == 12


class _FakePageImage:
    def __init__(self, width: int, height: int):
        self.size = (width, height)


class _FakePdfPage:
    def __init__(self, width: int, height: int, *, render_error: bool = False):
        self.width = width
        self.height = height
        self._render_error = render_error

    def to_image(self, **_kwargs):
        if self._render_error:
            raise RuntimeError("PDFium: Data format error")
        return type("RenderedPage", (), {"original": _FakePageImage(self.width, self.height)})()


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_images_renders_only_requested_page_range(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    pages = [_FakePdfPage(612, 792, render_error=True) for _ in range(13)] + [_FakePdfPage(612, 792)]
    monkeypatch.setattr(module.pdfplumber, "open", lambda *_args, **_kwargs: _FakePdf(pages))

    parser.__images__("sample.pdf", page_from=13, page_to=14)

    assert parser.page_images is not None
    assert len(parser.page_images) == 1
    assert parser.page_sizes == {0: (612.0, 792.0)}


def test_media_bbox_uses_local_page_metadata_when_rendering_fails(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    pages = [_FakePdfPage(612, 792) for _ in range(13)] + [_FakePdfPage(612, 792, render_error=True)]
    monkeypatch.setattr(module.pdfplumber, "open", lambda *_args, **_kwargs: _FakePdf(pages))

    parser.__images__("sample.pdf", page_from=13, page_to=14)

    assert parser.page_images is None
    assert parser.page_sizes == {0: (612.0, 792.0)}
    output = {
        "type": module.MinerUContentType.IMAGE,
        "bbox": [196, 210, 823, 763],
        "page_idx": 0,
    }
    assert parser._line_tag(output) == "@@1\t120.0\t503.7\t166.3\t604.3##"

    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: object())
    media_blocks = parser._transfer_to_media_blocks([output])
    assert media_blocks[0][1] == [(13, 120.0, 503.7, 166.3, 604.3)]


def test_crop_uses_local_page_tag_and_returns_global_page_position(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_from = 13
    parser.page_images = [module.Image.new("RGB", (100, 200), "white")]

    image, positions = parser.crop("@@1\t10\t40\t50\t80##", need_position=True)

    assert image is not None
    assert positions == [(13, 10, 40, 50, 80)]


def test_parse_pdf_threads_page_range_into_renderer(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    captured = {}

    def capture_images(_pdf, zoomin=1, page_from=0, page_to=module.MAXIMUM_PAGE_NUMBER, callback=None):
        captured.update(zoomin=zoomin, page_from=page_from, page_to=page_to, callback=callback)

    monkeypatch.setattr(parser, "__images__", capture_images)
    monkeypatch.setattr(parser, "_run_mineru", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(parser, "_read_output", lambda *_args, **_kwargs: [])

    parser.parse_pdf(
        filepath=tmp_path / "sample.pdf",
        binary=b"%PDF-1.4 fake",
        output_dir=str(tmp_path / "output"),
        delete_output=False,
        page_from=13,
        page_to=14,
    )

    assert captured == {"zoomin": 1, "page_from": 13, "page_to": 14, "callback": None}


def test_parse_pdf_pipeline_returns_no_independent_media_blocks(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    monkeypatch.setattr(parser, "__images__", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(parser, "_run_mineru", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(
        parser,
        "_read_output",
        lambda *_args, **_kwargs: [
            {
                "type": module.MinerUContentType.IMAGE,
                "image_caption": ["Figure caption"],
                "image_footnote": [],
            }
        ],
    )
    monkeypatch.setattr(parser, "_transfer_to_media_blocks", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("pipeline must not load media blocks")))

    sections, media_blocks = parser.parse_pdf(
        filepath=tmp_path / "sample.pdf",
        binary=b"%PDF-1.4 fake",
        output_dir=str(tmp_path / "output"),
        delete_output=False,
        parse_method="pipeline",
    )

    assert sections == [("Figure caption", "image", "")]
    assert media_blocks == []


def _capture_parse_pdf_input(monkeypatch, module, parser, tmp_path, *, filepath, binary):
    captured = {}

    def capture_run(input_path, output_dir, _options, **_kwargs):
        input_path = Path(input_path)
        captured["path"] = input_path
        captured["bytes"] = input_path.read_bytes()
        return output_dir

    monkeypatch.setattr(parser, "__images__", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(parser, "_run_mineru", capture_run)
    monkeypatch.setattr(parser, "_read_output", lambda *_args, **_kwargs: [])

    result = parser.parse_pdf(
        filepath=filepath,
        binary=binary,
        output_dir=str(tmp_path / "output"),
        delete_output=False,
    )

    assert result == ([], [])
    return captured


def test_parse_pdf_reads_bytesio_and_cleans_temporary_input(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    binary = BytesIO(b"bytesio-pdf")
    binary.seek(len(b"bytesio-pdf"))

    captured = _capture_parse_pdf_input(
        monkeypatch,
        module,
        parser,
        tmp_path,
        filepath=tmp_path / "stream input.pdf",
        binary=binary,
    )

    assert captured["bytes"] == b"bytesio-pdf"
    assert " " not in captured["path"].name
    assert not captured["path"].parent.exists()


def test_parse_pdf_treats_empty_bytes_as_binary_input(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()

    captured = _capture_parse_pdf_input(
        monkeypatch,
        module,
        parser,
        tmp_path,
        filepath=tmp_path / "missing.pdf",
        binary=b"",
    )

    assert captured["bytes"] == b""
    assert not captured["path"].parent.exists()


def test_parse_pdf_none_uses_path_object_directly(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    source = tmp_path / "source.pdf"
    source.write_bytes(b"path-pdf")

    captured = _capture_parse_pdf_input(monkeypatch, module, parser, tmp_path, filepath=source, binary=None)

    assert captured == {"path": source, "bytes": b"path-pdf"}
    assert source.read_bytes() == b"path-pdf"


def test_parse_pdf_copies_spaced_path_without_modifying_original(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    source = tmp_path / "source document.pdf"
    source.write_bytes(b"path-pdf")
    original_mtime = source.stat().st_mtime_ns

    captured = _capture_parse_pdf_input(monkeypatch, module, parser, tmp_path, filepath=source, binary=None)

    assert captured["path"] != source
    assert captured["path"].name == "sourcedocument.pdf"
    assert captured["bytes"] == b"path-pdf"
    assert not captured["path"].parent.exists()
    assert source.read_bytes() == b"path-pdf"
    assert source.stat().st_mtime_ns == original_mtime


def test_pdf_open_and_render_use_shared_pdfplumber_lock(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    lock = module.sys.modules[module.LOCK_KEY_pdfplumber]

    class _LockCheckingPage(_FakePdfPage):
        def to_image(self, **kwargs):
            assert lock.locked()
            return super().to_image(**kwargs)

    pages = [_LockCheckingPage(612, 792)]

    def open_pdf(*_args, **_kwargs):
        assert lock.locked()
        return _FakePdf(pages)

    monkeypatch.setattr(module.pdfplumber, "open", open_pdf)

    parser.__images__("sample.pdf")

    assert parser.page_images is not None
    assert not lock.locked()


def test_media_bbox_is_omitted_when_page_size_is_unavailable(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_sizes = {0: (100.0, 200.0)}
    monkeypatch.setattr(module.pdfplumber, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cannot open PDF")))

    parser.__images__("sample.pdf")

    assert parser.page_sizes == {}
    output = {
        "type": module.MinerUContentType.IMAGE,
        "bbox": [100, 100, 900, 900],
        "page_idx": 0,
    }
    assert parser._line_tag(output) == ""
    monkeypatch.setattr(parser, "_resolve_output_image", lambda *_args, **_kwargs: object())
    assert parser._transfer_to_media_blocks([output])[0][1] == []
    assert (
        parser._middle_positions_for_output(
            {
                "type": module.MinerUContentType.TABLE,
                "table_body": "<table><tr><td>row</td></tr></table>",
                "table_caption": [],
                "table_footnote": [],
                "bbox": [100, 100, 900, 900],
                "page_idx": 0,
            },
            [{"type": "table", "page_idx": 0, "bbox": (10, 10, 90, 90), "text": "row"}],
        )
        == []
    )


def test_read_output_enriches_cross_page_table_positions_from_middle_json(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_images = [_FakePageImage(200, 400), _FakePageImage(200, 400)]

    content_list = [
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": "<table><tr><td>first page row</td></tr><tr><td>second page row</td></tr></table>",
            "table_caption": [],
            "table_footnote": [],
            "bbox": [100, 100, 900, 900],
            "page_idx": 0,
        }
    ]
    middle_json = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [200, 400],
                "para_blocks": [
                    {
                        "type": "table",
                        "bbox": [20, 40, 180, 360],
                        "blocks": [
                            {
                                "type": "table_body",
                                "lines": [
                                    {
                                        "spans": [
                                            {"type": "table", "content": "first page row", "bbox": [20, 40, 180, 360]},
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "page_idx": 1,
                "page_size": [200, 400],
                "para_blocks": [
                    {
                        "type": "table",
                        "bbox": [20, 0, 180, 80],
                        "blocks": [
                            {
                                "type": "table_body",
                                "lines": [
                                    {
                                        "spans": [
                                            {"type": "table", "content": "second page row", "bbox": [20, 0, 180, 80]},
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
        ],
    }
    (tmp_path / "sample_content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
    (tmp_path / "sample_middle.json").write_text(json.dumps(middle_json), encoding="utf-8")

    outputs = parser._read_output(tmp_path, "sample", method="auto", backend="pipeline")
    sections = parser._transfer_to_sections(outputs, parse_method="raw", table_enable=True)
    media_blocks = parser._transfer_to_media_blocks(outputs, table_enable=True)

    assert sections == []
    assert len(media_blocks) == 1
    assert media_blocks[0][1] == [
        (0, 20.0, 180.0, 40.0, 360.0),
        (1, 20.0, 180.0, 0.0, 80.0),
    ]


def test_read_output_resolves_media_inside_result_directory(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    image_path = tmp_path / "images" / "figure.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"image")
    (tmp_path / "sample_content_list.json").write_text(
        json.dumps([{"type": "image", "img_path": "images/figure.png"}]),
        encoding="utf-8",
    )

    outputs = parser._read_output(tmp_path, "sample")

    assert outputs[0]["img_path"] == str(image_path.resolve())


def test_read_output_rejects_unsafe_media_paths(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"outside")
    content_list = tmp_path / "sample_content_list.json"

    for unsafe_path in ("../outside.png", str(outside.resolve()), r"C:\outside.png"):
        content_list.write_text(json.dumps([{"type": "image", "img_path": unsafe_path}]), encoding="utf-8")
        with pytest.raises(RuntimeError, match="Unsafe media path"):
            parser._read_output(tmp_path, "sample")


def test_read_output_rejects_media_symlink_escape(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    outside = tmp_path.parent / "outside-symlink.png"
    outside.write_bytes(b"outside")
    images = tmp_path / "images"
    images.mkdir()
    (images / "figure.png").symlink_to(outside)
    (tmp_path / "sample_content_list.json").write_text(
        json.dumps([{"type": "image", "img_path": "images/figure.png"}]),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match=r"Unsafe media path \(escape\)"):
        parser._read_output(tmp_path, "sample")


def test_read_output_does_not_enrich_non_table_positions_from_middle_json(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_images = [_FakePageImage(200, 400), _FakePageImage(200, 400)]

    content_list = [
        {
            "type": module.MinerUContentType.TEXT,
            "text": "first page row second page row",
            "bbox": [100, 100, 900, 900],
            "page_idx": 0,
        }
    ]
    middle_json = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [200, 400],
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [20, 40, 180, 360],
                        "lines": [{"spans": [{"content": "first page row"}]}],
                    }
                ],
            },
            {
                "page_idx": 1,
                "page_size": [200, 400],
                "para_blocks": [
                    {
                        "type": "text",
                        "bbox": [20, 0, 180, 80],
                        "lines": [{"spans": [{"content": "second page row"}]}],
                    }
                ],
            },
        ],
    }
    (tmp_path / "sample_content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
    (tmp_path / "sample_middle.json").write_text(json.dumps(middle_json), encoding="utf-8")

    outputs = parser._read_output(tmp_path, "sample", method="auto", backend="pipeline")
    sections = parser._transfer_to_sections(outputs, parse_method="raw", table_enable=True)

    assert len(sections) == 1
    _, line_tag = sections[0]
    assert module.MinerUParser.extract_positions(line_tag) == [
        ([0], 20.0, 180.0, 40.0, 360.0),
    ]


def test_middle_positions_ignore_malformed_output_bbox(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_images = [_FakePageImage(200, 400)]

    positions = parser._middle_positions_for_output(
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": "<table><tr><td>row</td></tr></table>",
            "table_caption": [],
            "table_footnote": [],
            "bbox": [100, 100, 900],
            "page_idx": 0,
        },
        [
            {
                "type": "table",
                "page_idx": 0,
                "bbox": (20, 40, 180, 360),
                "text": "row",
            }
        ],
    )

    assert positions == []


def test_middle_positions_choose_highest_overlap_anchor(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_sizes = {0: (200, 400), 1: (200, 400)}
    output = {
        "type": module.MinerUContentType.TABLE,
        "table_body": "anchor continuation",
        "table_caption": [],
        "table_footnote": [],
        "bbox": [100, 100, 900, 900],
        "page_idx": 0,
    }
    middle_blocks = [
        {"type": "table", "page_idx": 0, "bbox": (20, 40, 180, 360), "text": "anchor"},
        {"type": "table", "page_idx": 0, "bbox": (25, 45, 175, 350), "text": "wrong"},
        {"type": "table", "page_idx": 1, "bbox": (20, 0, 180, 80), "text": "continuation"},
    ]

    assert parser._middle_positions_for_output(output, middle_blocks) == [
        {"page_idx": 0, "bbox": (20, 40, 180, 360)},
        {"page_idx": 1, "bbox": (20, 0, 180, 80)},
    ]


def test_middle_positions_ignore_same_page_unrelated_table(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_sizes = {0: (200, 400), 1: (200, 400)}
    output = {
        "type": module.MinerUContentType.TABLE,
        "table_body": "anchor continuation",
        "table_caption": [],
        "table_footnote": [],
        "bbox": [100, 100, 900, 900],
        "page_idx": 0,
    }
    middle_blocks = [
        {"type": "table", "page_idx": 0, "bbox": (20, 40, 180, 360), "text": "anchor"},
        {"type": "table", "page_idx": 0, "bbox": (0, 0, 10, 10), "text": "continuation"},
    ]

    assert parser._middle_positions_for_output(output, middle_blocks) == [
        {"page_idx": 0, "bbox": (20, 40, 180, 360)},
    ]


def test_middle_positions_stop_at_page_gap(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_sizes = {0: (200, 400), 5: (200, 400)}
    output = {
        "type": module.MinerUContentType.TABLE,
        "table_body": "anchor repeated text",
        "table_caption": [],
        "table_footnote": [],
        "bbox": [100, 100, 900, 900],
        "page_idx": 0,
    }
    middle_blocks = [
        {"type": "table", "page_idx": 0, "bbox": (20, 40, 180, 360), "text": "anchor"},
        {"type": "table", "page_idx": 5, "bbox": (20, 0, 180, 80), "text": "repeatedtext"},
    ]

    assert parser._middle_positions_for_output(output, middle_blocks) == [
        {"page_idx": 0, "bbox": (20, 40, 180, 360)},
    ]


def test_middle_positions_require_monotonic_target_text(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_sizes = {0: (200, 400), 1: (200, 400), 2: (200, 400)}
    output = {
        "type": module.MinerUContentType.TABLE,
        "table_body": "anchor first second",
        "table_caption": [],
        "table_footnote": [],
        "bbox": [100, 100, 900, 900],
        "page_idx": 0,
    }
    middle_blocks = [
        {"type": "table", "page_idx": 0, "bbox": (20, 40, 180, 360), "text": "anchor"},
        {"type": "table", "page_idx": 1, "bbox": (20, 0, 180, 80), "text": "second"},
        {"type": "table", "page_idx": 2, "bbox": (20, 0, 180, 80), "text": "first"},
    ]

    assert parser._middle_positions_for_output(output, middle_blocks) == [
        {"page_idx": 0, "bbox": (20, 40, 180, 360)},
        {"page_idx": 1, "bbox": (20, 0, 180, 80)},
    ]


def test_middle_positions_do_not_reuse_short_repeated_text(monkeypatch):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_sizes = {0: (200, 400), 1: (200, 400)}
    output = {
        "type": module.MinerUContentType.TABLE,
        "table_body": "total",
        "table_caption": [],
        "table_footnote": [],
        "bbox": [100, 100, 900, 900],
        "page_idx": 0,
    }
    middle_blocks = [
        {"type": "table", "page_idx": 0, "bbox": (20, 40, 180, 360), "text": "total"},
        {"type": "table", "page_idx": 1, "bbox": (20, 0, 180, 80), "text": "total"},
    ]

    assert parser._middle_positions_for_output(output, middle_blocks) == [
        {"page_idx": 0, "bbox": (20, 40, 180, 360)},
    ]


def test_read_output_keeps_original_tag_when_middle_json_has_single_table_position(monkeypatch, tmp_path):
    module = _load_mineru_parser(monkeypatch)
    parser = module.MinerUParser()
    parser.page_images = [_FakePageImage(200, 400)]

    content_list = [
        {
            "type": module.MinerUContentType.TABLE,
            "table_body": "<table><tr><td>only row</td></tr></table>",
            "table_caption": [],
            "table_footnote": [],
            "bbox": [100, 100, 850, 850],
            "page_idx": 0,
        }
    ]
    middle_json = {
        "pdf_info": [
            {
                "page_idx": 0,
                "page_size": [200, 400],
                "para_blocks": [
                    {
                        "type": "table",
                        "bbox": [20, 40, 180, 360],
                        "blocks": [
                            {
                                "type": "table_body",
                                "lines": [{"spans": [{"type": "table", "content": "only row"}]}],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    (tmp_path / "sample_content_list.json").write_text(json.dumps(content_list), encoding="utf-8")
    (tmp_path / "sample_middle.json").write_text(json.dumps(middle_json), encoding="utf-8")

    outputs = parser._read_output(tmp_path, "sample", method="auto", backend="pipeline")
    sections = parser._transfer_to_sections(outputs, parse_method="raw", table_enable=True)
    media_blocks = parser._transfer_to_media_blocks(outputs, table_enable=True)

    assert "_mineru_positions" not in outputs[0]
    assert sections == []
    assert len(media_blocks) == 1
    assert media_blocks[0][1] == [(0, 20.0, 170.0, 40.0, 340.0)]
