"""OCR 深度解析单元测试（P0-2b）。

全部基于 mock，不依赖真实 OCR 后端（mineru / paddleocr 未安装也可跑），
覆盖：扫描版检测、后端选择与调用、未安装报错、分流与回退、
Markdown 分块策略自动切换与 ocr_backend 元数据传播。
"""

import subprocess
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.config import settings
from src.services import document_processor
from src.services import ocr_parser


# =============================================================================
# 扫描版检测 is_scanned_pdf
# =============================================================================

def _fake_pdf_reader(pages_text):
    """构造 pypdf.PdfReader 的 mock 实例。"""
    pages = [SimpleNamespace(extract_text=lambda t=t: t) for t in pages_text]
    return SimpleNamespace(is_encrypted=False, pages=pages)


def test_is_scanned_pdf_true_when_text_empty(mocker):
    mocker.patch("pypdf.PdfReader", return_value=_fake_pdf_reader(["", "", ""]))
    assert ocr_parser.is_scanned_pdf("fake.pdf") is True


def test_is_scanned_pdf_false_when_text_rich(mocker):
    mocker.patch(
        "pypdf.PdfReader",
        return_value=_fake_pdf_reader(["这是一段很长的正文内容" * 10] * 3),
    )
    assert ocr_parser.is_scanned_pdf("fake.pdf") is False


def test_is_scanned_pdf_read_error_returns_false(mocker):
    mocker.patch("pypdf.PdfReader", side_effect=RuntimeError("bad pdf"))
    assert ocr_parser.is_scanned_pdf("fake.pdf") is False


def test_is_scanned_pdf_threshold_configurable(mocker):
    mocker.patch("pypdf.PdfReader", return_value=_fake_pdf_reader(["短文本"]))
    original = settings.ocr.OCR_SCANNED_CHAR_THRESHOLD
    try:
        settings.ocr.OCR_SCANNED_CHAR_THRESHOLD = 3  # 3 字符 < 3 为 False
        assert ocr_parser.is_scanned_pdf("fake.pdf") is False
        settings.ocr.OCR_SCANNED_CHAR_THRESHOLD = 5  # 3 字符 < 5 为 True
        assert ocr_parser.is_scanned_pdf("fake.pdf") is True
    finally:
        settings.ocr.OCR_SCANNED_CHAR_THRESHOLD = original


# =============================================================================
# 后端选择与调用
# =============================================================================

def test_parse_pdf_ocr_no_backend_raises(mocker):
    mocker.patch.object(ocr_parser, "get_available_backends", return_value=[])
    with pytest.raises(ocr_parser.OcrUnavailableError):
        ocr_parser.parse_pdf_ocr("fake.pdf", backend="auto")


def test_parse_pdf_ocr_auto_uses_first_available(mocker):
    mocker.patch.object(ocr_parser, "get_available_backends", return_value=["paddle", "mineru"])
    mock_parse = mocker.patch.object(
        ocr_parser, "parse_pdf_with_paddle", return_value="# 标题\n内容"
    )
    text, backend = ocr_parser.parse_pdf_ocr("fake.pdf", backend="auto")
    assert backend == "paddle"
    assert text == "# 标题\n内容"
    mock_parse.assert_called_once_with("fake.pdf")


def test_parse_pdf_ocr_unknown_backend_raises():
    with pytest.raises(ocr_parser.OcrParseError):
        ocr_parser.parse_pdf_ocr("fake.pdf", backend="tesseract")


def test_parse_pdf_with_mineru_success(mocker):
    mocker.patch.object(ocr_parser.shutil, "which", return_value="C:/fake/mineru.exe")

    def fake_run(cmd, **kwargs):
        # 从命令行参数中提取 -o 输出目录并写入 md 文件
        outdir = cmd[cmd.index("-o") + 1]
        md_path = ocr_parser.Path(outdir) / "result.md"
        md_path.write_text("# 标题\n正文内容", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_run = mocker.patch.object(ocr_parser.subprocess, "run", side_effect=fake_run)
    text = ocr_parser.parse_pdf_with_mineru("fake.pdf")
    assert "标题" in text and "正文内容" in text
    # 验证 pipeline 后端与 utf-8 子进程参数
    cmd = mock_run.call_args[0][0]
    assert "-b" in cmd and "pipeline" in cmd
    assert mock_run.call_args[1]["encoding"] == "utf-8"


def test_parse_pdf_with_mineru_nonzero_exit(mocker):
    mocker.patch.object(ocr_parser.shutil, "which", return_value="C:/fake/mineru.exe")
    mocker.patch.object(
        ocr_parser.subprocess, "run",
        return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="model missing"),
    )
    with pytest.raises(ocr_parser.OcrParseError, match="model missing"):
        ocr_parser.parse_pdf_with_mineru("fake.pdf")


def test_parse_pdf_with_mineru_timeout(mocker):
    mocker.patch.object(ocr_parser.shutil, "which", return_value="C:/fake/mineru.exe")
    mocker.patch.object(
        ocr_parser.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd=[], timeout=600),
    )
    with pytest.raises(ocr_parser.OcrParseError, match="超时"):
        ocr_parser.parse_pdf_with_mineru("fake.pdf")


def test_parse_pdf_with_mineru_not_installed(mocker):
    mocker.patch.object(ocr_parser.shutil, "which", return_value=None)
    with pytest.raises(ocr_parser.OcrUnavailableError):
        ocr_parser.parse_pdf_with_mineru("fake.pdf")


def test_parse_pdf_with_paddle_success(mocker):
    fake_res = SimpleNamespace(markdown={"markdown_texts": "# 章节\n表格内容"})
    fake_pipeline = SimpleNamespace(predict=lambda pdf: iter([fake_res, fake_res]))
    mocker.patch.object(ocr_parser, "_get_paddle_pipeline", return_value=fake_pipeline)
    text = ocr_parser.parse_pdf_with_paddle("fake.pdf")
    assert text.count("# 章节") == 2
    assert "\n\n" in text


def test_parse_pdf_with_paddle_empty_output_raises(mocker):
    fake_res = SimpleNamespace(markdown={"markdown_texts": ""})
    fake_pipeline = SimpleNamespace(predict=lambda pdf: iter([fake_res]))
    mocker.patch.object(ocr_parser, "_get_paddle_pipeline", return_value=fake_pipeline)
    with pytest.raises(ocr_parser.OcrParseError, match="未产出"):
        ocr_parser.parse_pdf_with_paddle("fake.pdf")


def test_get_paddle_pipeline_uninstalled(mocker):
    mocker.patch.dict("sys.modules", {"paddleocr": None})
    # 重置单例缓存，确保走到懒导入分支
    mocker.patch.object(ocr_parser, "_paddle_pipeline", None)
    with pytest.raises(ocr_parser.OcrUnavailableError):
        ocr_parser._get_paddle_pipeline()


# =============================================================================
# load_document 分流与回退
# =============================================================================

@pytest.fixture
def enable_deep_parsing(monkeypatch):
    monkeypatch.setattr(settings.ocr, "DEEP_PARSING_ENABLED", True)


def test_load_document_scanned_pdf_routes_to_ocr(mocker, enable_deep_parsing, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-fake")
    mocker.patch.object(document_processor, "is_scanned_pdf", return_value=True)
    mocker.patch.object(
        document_processor, "parse_pdf_ocr",
        return_value=("# 员工手册\n\n## 请假制度\n\n年假五天。", "mineru"),
    )
    docs = document_processor.load_document(str(pdf))
    assert len(docs) == 1
    assert docs[0].metadata["ocr_backend"] == "mineru"
    assert "员工手册" in docs[0].page_content


def test_load_document_text_pdf_skips_ocr(mocker, enable_deep_parsing, tmp_path):
    pdf = tmp_path / "text.pdf"
    pdf.write_bytes(b"%PDF-fake")
    mocker.patch.object(document_processor, "is_scanned_pdf", return_value=False)
    mock_parse = mocker.patch.object(document_processor, "parse_pdf_ocr")
    fake_loader = mocker.patch.object(document_processor, "PyPDFLoader")
    fake_loader.return_value.load.return_value = [
        Document(page_content="正常文本", metadata={"source": str(pdf)})
    ]
    docs = document_processor.load_document(str(pdf))
    mock_parse.assert_not_called()
    assert docs[0].page_content == "正常文本"
    assert "ocr_backend" not in docs[0].metadata


def test_load_document_disabled_skips_ocr(mocker, monkeypatch, tmp_path):
    monkeypatch.setattr(settings.ocr, "DEEP_PARSING_ENABLED", False)
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-fake")
    mock_detect = mocker.patch.object(document_processor, "is_scanned_pdf")
    mock_parse = mocker.patch.object(document_processor, "parse_pdf_ocr")
    fake_loader = mocker.patch.object(document_processor, "PyPDFLoader")
    fake_loader.return_value.load.return_value = [
        Document(page_content="fallback", metadata={"source": str(pdf)})
    ]
    docs = document_processor.load_document(str(pdf))
    mock_detect.assert_not_called()
    mock_parse.assert_not_called()
    assert docs[0].page_content == "fallback"


def test_load_document_ocr_failure_falls_back(mocker, enable_deep_parsing, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-fake")
    mocker.patch.object(document_processor, "is_scanned_pdf", return_value=True)
    mocker.patch.object(
        document_processor, "parse_pdf_ocr",
        side_effect=ocr_parser.OcrParseError("boom"),
    )
    fake_loader = mocker.patch.object(document_processor, "PyPDFLoader")
    fake_loader.return_value.load.return_value = [
        Document(page_content="fallback page", metadata={"source": str(pdf)})
    ]
    docs = document_processor.load_document(str(pdf))
    assert docs[0].page_content == "fallback page"
    assert "ocr_backend" not in docs[0].metadata


# =============================================================================
# process_document：Markdown 策略自动切换 + ocr_backend 传播
# =============================================================================

def test_process_document_ocr_forces_markdown_strategy(mocker, enable_deep_parsing, tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-fake")
    mocker.patch.object(
        document_processor, "load_document",
        return_value=[
            Document(
                page_content="# 员工手册\n\n## 请假制度\n\n年假天数为五天，需提前申请。",
                metadata={"source": str(pdf), "ocr_backend": "paddle"},
            )
        ],
    )
    chunks = document_processor.process_document(str(pdf))
    assert chunks, "应产出至少一个 chunk"
    for chunk in chunks:
        assert chunk.metadata["chunk_strategy"] == "markdown"
        assert chunk.metadata["ocr_backend"] == "paddle"
    assert any("请假制度" in c.metadata.get("heading_path", "") for c in chunks)


def test_process_document_normal_pdf_keeps_auto_strategy(mocker, tmp_path):
    pdf = tmp_path / "text.pdf"
    pdf.write_bytes(b"%PDF-fake")
    mocker.patch.object(
        document_processor, "load_document",
        return_value=[Document(page_content="普通文本内容", metadata={"source": str(pdf)})],
    )
    chunks = document_processor.process_document(str(pdf))
    for chunk in chunks:
        assert chunk.metadata["chunk_strategy"] == "recursive"
        assert "ocr_backend" not in chunk.metadata
