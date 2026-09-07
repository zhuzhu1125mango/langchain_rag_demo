"""OCR 双后端端到端对比测试（P0-2b 选型验证）。

需要本地同时安装 mineru 与 paddleocr 两个后端（uv sync --group ocr-mineru
--group ocr-paddle），并用 scripts/download_ocr_models.py 预热模型缓存。
CI 环境不安装 OCR 依赖组，自动跳过。

测试流程：
1. 用 Pillow 渲染一页含中文标题/正文/表格的图片型 PDF（无文本层）
2. 分别强制走 mineru / paddle 后端解析
3. 断言：两个后端均产出 Markdown 且包含关键内容
4. 输出双方指标对比（耗时/字符数/标题数/表行数/与基准文本相似度）
"""

import shutil
import time
from pathlib import Path

import pytest

from src.services import ocr_parser

mineru_available = shutil.which("mineru") is not None
paddle_available = False
try:
    import paddleocr  # noqa: F401

    paddle_available = True
except ImportError:
    pass

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (mineru_available and paddle_available),
        reason="需要 mineru 与 paddleocr 双后端（uv sync --group ocr-mineru --group ocr-paddle）",
    ),
]

SAMPLE_LINES = [
    "# 员工手册",
    "",
    "## 请假制度",
    "",
    "年假天数为五天，需提前三个工作日向部门主管申请。",
    "病假需要提供医院开具的证明材料。",
    "",
    "## 差旅标准",
    "",
    "| 城市 | 住宿上限 | 高铁座席 |",
    "| --- | --- | --- |",
    "| 北京 | 400 元 | 一等座 |",
    "| 成都 | 350 元 | 二等座 |",
]

# 双后端都必须识别出的关键词（标题 + 表格数值）
KEYWORDS = ["员工手册", "请假制度", "差旅标准", "400", "350"]


def _find_cjk_font() -> str:
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    return next((f for f in candidates if Path(f).exists()), "")


@pytest.fixture(scope="module")
def scanned_pdf(tmp_path_factory):
    """生成一页图片型 PDF（无文本层）。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = _find_cjk_font()
    font = ImageFont.truetype(font_path, 30) if font_path else ImageFont.load_default()

    img = Image.new("RGB", (1400, 1100), "white")
    draw = ImageDraw.Draw(img)
    y = 60
    for line in SAMPLE_LINES:
        draw.text((60, y), line, fill="black", font=font)
        y += 56
    pdf_path = tmp_path_factory.mktemp("ocr") / "ocr_e2e_sample.pdf"
    img.save(pdf_path, "PDF", resolution=150)
    return str(pdf_path)


@pytest.fixture(scope="module")
def parse_results(scanned_pdf):
    """两个后端各解析一次，缓存结果与耗时。"""
    results = {}
    for backend in ("mineru", "paddle"):
        started = time.time()
        text = ocr_parser.parse_pdf_ocr(scanned_pdf, backend=backend)[0]
        results[backend] = {
            "markdown": text,
            "elapsed": round(time.time() - started, 1),
            "chars": len(text),
            "headings": sum(1 for ln in text.split("\n") if ln.startswith("#")),
            "table_rows": sum(1 for ln in text.split("\n") if ln.strip().startswith("|")),
        }
    return results


def test_mineru_extracts_key_content(parse_results):
    md = parse_results["mineru"]["markdown"]
    for keyword in KEYWORDS:
        assert keyword in md, f"mineru 输出缺少关键词: {keyword}"


def test_paddle_extracts_key_content(parse_results):
    md = parse_results["paddle"]["markdown"]
    for keyword in KEYWORDS:
        assert keyword in md, f"paddle 输出缺少关键词: {keyword}"


def test_both_output_structured_markdown(parse_results):
    for backend, r in parse_results.items():
        assert r["headings"] >= 2, f"{backend} 标题数不足: {r['headings']}"
        assert r["table_rows"] >= 2, f"{backend} 表格行数不足: {r['table_rows']}"


def test_print_comparison_report(parse_results):
    """打印实测对比数据，供选型决策参考（不作为断言）。"""
    print("\nOCR 双后端实测对比：")
    for backend, r in parse_results.items():
        print(
            f"  [{backend}] 耗时 {r['elapsed']}s | {r['chars']} 字符 | "
            f"{r['headings']} 标题 | {r['table_rows']} 表行"
        )
