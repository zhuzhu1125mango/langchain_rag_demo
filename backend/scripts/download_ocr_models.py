"""OCR 模型缓存预热脚本（P0-2b，离线部署需在有网环境执行一次）。

原理：生成一页含中文文字的图片型 PDF（无文本层），分别调用 mineru /
paddle 后端解析一次。两个后端首次运行时会自动下载全部所需模型到本地
缓存，同时完成端到端冒烟验证——后续离线环境直接可用。

用法（backend 目录下）：
    uv run python scripts/download_ocr_models.py --backend mineru
    uv run python scripts/download_ocr_models.py --backend paddle
    uv run python scripts/download_ocr_models.py --backend all

依赖安装：
    uv sync --group ocr-mineru
    uv sync --group ocr-paddle

模型源由 .env 的 MINERU_MODEL_SOURCE / PADDLE_MODEL_SOURCE 控制
（modelscope | huggingface），脚本执行失败时以非零码退出。
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings  # noqa: E402

# 常见中文字体候选（按优先级），找不到时回退默认字体（中文可能显示为方块，
# 不影响模型下载，只影响冒烟文本质量）
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

SAMPLE_TEXT_LINES = [
    "# 员工手册",
    "## 请假制度",
    "年假天数为五天，需提前三个工作日申请。",
    "## 差旅标准",
    "| 城市 | 住宿上限 |",
    "| --- | --- |",
    "| 北京 | 400 元 |",
    "| 成都 | 350 元 |",
]


def make_sample_pdf(out_path: Path) -> None:
    """渲染一页含中文文字的图片型 PDF（无文本层，触发 OCR 路径）。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)
    font = ImageFont.truetype(font_path, 32) if font_path else ImageFont.load_default()

    img = Image.new("RGB", (1240, 800), "white")
    draw = ImageDraw.Draw(img)
    y = 60
    for line in SAMPLE_TEXT_LINES:
        draw.text((60, y), line, fill="black", font=font)
        y += 64
    img.save(out_path, "PDF", resolution=150)


def warm_mineru(pdf_path: Path) -> None:
    """通过 CLI 解析样例 PDF 触发模型下载与冒烟验证。"""
    exe = None
    for candidate in ("mineru", "mineru.exe"):
        exe = shutil_which(candidate)
        if exe:
            break
    if not exe:
        print("[mineru] 未找到 mineru 命令，请先执行: uv sync --group ocr-mineru")
        sys.exit(1)

    outdir = tempfile.mkdtemp(prefix="mineru_warmup_")
    env = os.environ.copy()
    if settings.ocr.MINERU_MODEL_SOURCE:
        env.setdefault("MINERU_MODEL_SOURCE", settings.ocr.MINERU_MODEL_SOURCE)
    cmd = [exe, "-p", str(pdf_path), "-o", outdir, "-b", "pipeline"]
    if settings.ocr.OCR_DEVICE and settings.ocr.OCR_DEVICE != "auto":
        cmd += ["-d", settings.ocr.OCR_DEVICE]
    print(f"[mineru] 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"[mineru] 冒烟验证失败，退出码 {result.returncode}")
        sys.exit(result.returncode)
    print("[mineru] 模型下载与冒烟验证成功")


def warm_paddle(pdf_path: Path) -> None:
    """进程内调用 PP-StructureV3 触发模型下载与冒烟验证。"""
    try:
        if settings.ocr.PADDLE_MODEL_SOURCE:
            os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", settings.ocr.PADDLE_MODEL_SOURCE)
        from paddleocr import PPStructureV3
    except ImportError:
        print("[paddle] 未安装 paddleocr，请先执行: uv sync --group ocr-paddle")
        sys.exit(1)

    kwargs = {}
    device = settings.ocr.OCR_DEVICE
    if device and device != "auto":
        kwargs["device"] = "gpu:0" if device in ("cuda", "gpu") else device
    # MKLDNN 在 paddle 3.x Windows CPU 下触发 NotImplementedError（与
    # src/services/ocr_parser.py 保持一致），需禁用
    kwargs["enable_mkldnn"] = False
    print(f"[paddle] 初始化 PP-StructureV3（首次运行下载模型，device={device}）")
    pipeline = PPStructureV3(**kwargs)
    results = list(pipeline.predict(str(pdf_path)))
    if not results:
        print("[paddle] 冒烟验证失败：未产出解析结果")
        sys.exit(1)
    print(f"[paddle] 模型下载与冒烟验证成功（{len(results)} 页）")


def shutil_which(name: str):
    import shutil

    return shutil.which(name)


def main():
    parser = argparse.ArgumentParser(description="OCR 模型缓存预热")
    parser.add_argument(
        "--backend", choices=["mineru", "paddle", "all"], default="all",
        help="要预热的后端（默认 all）",
    )
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="ocr_warmup_"))
    pdf_path = tmpdir / "ocr_warmup_sample.pdf"
    print(f"生成样例扫描 PDF: {pdf_path}")
    make_sample_pdf(pdf_path)

    if args.backend in ("mineru", "all"):
        warm_mineru(pdf_path)
    if args.backend in ("paddle", "all"):
        warm_paddle(pdf_path)


if __name__ == "__main__":
    main()
