import ast
import copy
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

MarkdownRenderer = Callable[[list[dict[str, object]], str, str], str]


class _FakeVLM:
    calls: list[object] = []

    @classmethod
    def image2base64(cls, image: object) -> str:
        cls.calls.append(image)
        return "data:image/png;base64,encoded"


def _load_pdf_markdown_renderer() -> MarkdownRenderer:
    """Isolate the production PDF markdown loop for unit testing."""
    repo_root = Path(__file__).resolve().parents[5]
    module_path = repo_root / "rag" / "flow" / "parser" / "parser.py"
    module_ast = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    parser_class = next(node for node in module_ast.body if isinstance(node, ast.ClassDef) and node.name == "Parser")
    pdf_method = next(node for node in parser_class.body if isinstance(node, ast.FunctionDef) and node.name == "_pdf")
    markdown_loop = next(
        node
        for node in ast.walk(pdf_method)
        if isinstance(node, ast.For)
        and isinstance(node.iter, ast.Name)
        and node.iter.id == "bboxes"
        and any(isinstance(child, ast.Constant) and isinstance(child.value, str) and "![Image]" in child.value for child in ast.walk(node))
    )

    renderer = ast.FunctionDef(
        name="render",
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="bboxes"), ast.arg(arg="name"), ast.arg(arg="parse_method")],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[
            ast.Assign(targets=[ast.Name(id="mkdn", ctx=ast.Store())], value=ast.Constant(value="")),
            copy.deepcopy(markdown_loop),
            ast.Return(value=ast.Name(id="mkdn", ctx=ast.Load())),
        ],
        decorator_list=[],
        type_params=[],
    )
    function_module = ast.Module(body=[renderer], type_ignores=[])
    ast.fix_missing_locations(function_module)

    namespace: dict[str, object] = {"VLM": _FakeVLM, "logging": logging}
    exec(compile(function_module, str(module_path), "exec"), namespace)
    return cast(MarkdownRenderer, namespace["render"])


def test_pdf_markdown_skips_figures_without_images_and_logs(caplog) -> None:
    render = _load_pdf_markdown_renderer()
    _FakeVLM.calls = []
    bboxes: list[dict[str, object]] = [
        {"layout_type": "figure", "text": "missing image key"},
        {"layout_type": "figure", "image": None, "text": "null image"},
        {"layout_type": "text", "text": "Document body"},
    ]

    with caplog.at_level(logging.WARNING):
        markdown = render(bboxes, "document.pdf", "MinerU")

    warnings = [record.getMessage() for record in caplog.records if "Skipping figure in markdown output" in record.getMessage()]
    assert markdown == "Document body\n"
    assert _FakeVLM.calls == []
    assert len(warnings) == 2
    assert all("document.pdf" in message and "parse_method=MinerU" in message for message in warnings)


def test_pdf_markdown_renders_available_figure_images() -> None:
    render = _load_pdf_markdown_renderer()
    image = object()
    _FakeVLM.calls = []

    markdown = render([{"layout_type": "figure", "image": image}], "document.pdf", "MinerU")

    assert markdown == "\n![Image](data:image/png;base64,encoded)"
    assert _FakeVLM.calls == [image]
