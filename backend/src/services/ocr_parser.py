"""扫描版 PDF OCR 深度解析（P0-2b）。

提供两类能力：
1. 扫描版检测：基于 pypdf 抽取文本的字符密度判定（平均每页字符数 < 阈值）
2. 双后端解析：MinerU（pipeline 后端）与 PaddleOCR（PP-StructureV3），
   统一输出 Markdown 文本，交由 Markdown 结构化分块管线处理（P0-2a）

设计约束：
- 两个后端均为可选依赖（uv group: ocr-mineru / ocr-paddle），懒导入，
  未安装时抛 OcrUnavailableError
- 任何失败由调用方（document_processor.load_document）回退内置 PyPDF
  解析，绝不阻断上传流程
- mineru 通过 CLI 子进程调用（隔离 torch 环境、可控超时）；
  paddle 通过 Python API 进程内调用（pipeline 实例全局复用，避免重复加载模型）
"""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from src.config import settings

logger = logging.getLogger("ocr_parser")


class OcrUnavailableError(RuntimeError):
    """OCR 后端未安装或不可用。"""


class OcrParseError(RuntimeError):
    """OCR 解析执行失败。"""


def is_scanned_pdf(pdf_path: str) -> bool:
    """判定 PDF 是否为扫描版（无可抽取文本层）。

    对前 N 页采样统计可抽取字符数，平均每页低于阈值即判定为扫描版。
    检测过程出现任何异常均按文本型处理（返回 False），交由常规解析器
    给出明确错误，避免检测失败掩盖真实问题。
    """
    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return False
        pages = list(reader.pages)
        if not pages:
            return False
        sample = pages[: settings.ocr.OCR_DETECT_MAX_PAGES]
        total_chars = sum(len((page.extract_text() or "").strip()) for page in sample)
        avg_chars = total_chars / len(sample)
        scanned = avg_chars < settings.ocr.OCR_SCANNED_CHAR_THRESHOLD
        logger.info(
            f"扫描版检测: {os.path.basename(pdf_path)}, "
            f"采样 {len(sample)} 页平均 {avg_chars:.0f} 字符/页 → {'扫描版' if scanned else '文本型'}"
        )
        return scanned
    except Exception as e:
        logger.warning(f"扫描版检测失败，按文本型 PDF 处理: {pdf_path}, 错误: {e}")
        return False


def get_available_backends() -> List[str]:
    """返回当前环境已安装可用的 OCR 后端列表（auto 模式的候选）。"""
    available: List[str] = []
    if shutil.which("mineru"):
        available.append("mineru")
    try:
        import paddleocr  # noqa: F401

        available.append("paddle")
    except ImportError:
        pass
    return available


def parse_pdf_ocr(pdf_path: str, backend: Optional[str] = None) -> Tuple[str, str]:
    """用指定（或自动选择）的 OCR 后端解析 PDF，返回 (Markdown 文本, 后端名)。

    Raises:
        OcrUnavailableError: 未安装任何后端 / 指定后端不可用
        OcrParseError: 后端执行失败
    """
    backend = (backend or settings.ocr.OCR_BACKEND).lower()
    if backend == "auto":
        available = get_available_backends()
        if not available:
            raise OcrUnavailableError(
                "未安装任何 OCR 后端（mineru / paddleocr），"
                "可执行: uv sync --group ocr-mineru 或 --group ocr-paddle"
            )
        backend = available[0]
    if backend == "mineru":
        return parse_pdf_with_mineru(pdf_path), "mineru"
    if backend == "paddle":
        return parse_pdf_with_paddle(pdf_path), "paddle"
    raise OcrParseError(f"未知 OCR 后端: {backend}（可选: auto / mineru / paddle）")


def parse_pdf_with_mineru(pdf_path: str) -> str:
    """MinerU pipeline 后端解析，返回 Markdown 文本。

    通过 CLI 子进程调用（隔离 torch 运行时、可控超时）；输出目录中
    可能包含多个 .md（分方法目录），取内容最大者。图片以相对路径引用，
    临时目录清理后链接失效，但正文文本不受影响。
    """
    exe = shutil.which("mineru")
    if not exe:
        raise OcrUnavailableError(
            "mineru 命令不可用，请先安装: uv sync --group ocr-mineru"
        )

    workdir = tempfile.mkdtemp(prefix="mineru_")
    env = os.environ.copy()
    if settings.ocr.MINERU_MODEL_SOURCE:
        # 不覆盖用户已显式设置的模型源（如 local）
        env.setdefault("MINERU_MODEL_SOURCE", settings.ocr.MINERU_MODEL_SOURCE)

    cmd = [exe, "-p", pdf_path, "-o", workdir, "-b", "pipeline"]
    device = settings.ocr.OCR_DEVICE
    if device and device != "auto":
        cmd += ["-d", device]

    logger.info(f"mineru 开始解析: {os.path.basename(pdf_path)}, 设备: {device}")
    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=settings.ocr.MINERU_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise OcrParseError(
            f"mineru 解析超时（>{settings.ocr.MINERU_TIMEOUT_SECONDS}s）: {pdf_path}"
        ) from e
    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-500:]
        raise OcrParseError(f"mineru 退出码 {result.returncode}: {stderr_tail}")

    md_files = sorted(
        Path(workdir).rglob("*.md"), key=lambda p: p.stat().st_size, reverse=True
    )
    if not md_files:
        raise OcrParseError(f"mineru 未产出 Markdown 文件: {workdir}")
    text = md_files[0].read_text(encoding="utf-8", errors="replace")
    logger.info(
        f"mineru 解析完成: {os.path.basename(pdf_path)}, "
        f"用时 {time.time() - started:.1f}s, 输出 {len(text)} 字符"
    )
    return text


# PaddleOCR pipeline 实例全局复用（模型加载耗时数十秒，进程内只加载一次）
_paddle_pipeline = None
_paddle_lock = threading.Lock()


def _get_paddle_pipeline():
    """懒加载 PP-StructureV3 pipeline 实例（线程安全）。"""
    global _paddle_pipeline
    if _paddle_pipeline is None:
        with _paddle_lock:
            if _paddle_pipeline is None:
                try:
                    from paddleocr import PPStructureV3
                except ImportError as e:
                    raise OcrUnavailableError(
                        "paddleocr 未安装，请先安装: uv sync --group ocr-paddle"
                    ) from e
                if settings.ocr.PADDLE_MODEL_SOURCE:
                    os.environ.setdefault(
                        "PADDLE_PDX_MODEL_SOURCE", settings.ocr.PADDLE_MODEL_SOURCE
                    )
                kwargs = {}
                device = settings.ocr.OCR_DEVICE
                if device and device != "auto":
                    kwargs["device"] = "gpu:0" if device in ("cuda", "gpu") else device
                # MKLDNN 在 paddle 3.x Windows CPU 下触发
                # NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support
                kwargs["enable_mkldnn"] = False
                _paddle_pipeline = PPStructureV3(**kwargs)
    return _paddle_pipeline


def parse_pdf_with_paddle(pdf_path: str) -> str:
    """PaddleOCR PP-StructureV3 解析，返回逐页拼接的 Markdown 文本。"""
    pipeline = _get_paddle_pipeline()
    logger.info(f"PP-StructureV3 开始解析: {os.path.basename(pdf_path)}")
    started = time.time()
    texts: List[str] = []
    try:
        for res in pipeline.predict(pdf_path):
            md = getattr(res, "markdown", None)
            if isinstance(md, dict):
                md = md.get("markdown_texts") or ""
            texts.append((md or "").strip())
    except OcrUnavailableError:
        raise
    except Exception as e:
        raise OcrParseError(f"PP-StructureV3 解析失败: {e}") from e
    text = "\n\n".join(t for t in texts if t)
    if not text.strip():
        raise OcrParseError("PP-StructureV3 未产出任何 Markdown 内容")
    logger.info(
        f"PP-StructureV3 解析完成: {os.path.basename(pdf_path)}, "
        f"用时 {time.time() - started:.1f}s, 输出 {len(text)} 字符"
    )
    return text
