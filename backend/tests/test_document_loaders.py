"""本地直读加载器单测（document_loaders.py，替代 community Text/PyPDF/CSV/JSON Loader）。

用真实临时文件验证各加载器输出契约，确保部分安全迁移后行为不漂移。
"""

import json

from pypdf import PdfWriter

from src.services.document_loaders import (
    CsvFileLoader,
    JsonFileLoader,
    PdfFileLoader,
    TextFileLoader,
)


def test_text_loader_utf8(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("标题\n正文内容", encoding="utf-8")
    docs = TextFileLoader(str(p)).load()
    assert len(docs) == 1
    assert "标题" in docs[0].page_content
    assert docs[0].metadata.get("source") == str(p)


def test_markdown_preserves_heading(tmp_path):
    # .md 须保留原始 # 标记（供 Markdown 结构化分块识别标题）
    p = tmp_path / "a.md"
    p.write_text("# 标题\n正文", encoding="utf-8")
    docs = TextFileLoader(str(p)).load()
    assert docs[0].page_content.startswith("# 标题")


def test_csv_loader_per_row(tmp_path):
    p = tmp_path / "a.csv"
    p.write_text("姓名,部门\n张三,研发\n李四,财务", encoding="utf-8")
    docs = CsvFileLoader(str(p)).load()
    assert len(docs) == 2
    assert "张三" in docs[0].page_content and "研发" in docs[0].page_content
    assert docs[0].metadata.get("row") == 1


def test_json_loader_text_content(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"title": "标题", "body": ["第一段", "第二段"]}), encoding="utf-8")
    docs = JsonFileLoader(str(p)).load()
    assert len(docs) == 1
    assert "标题" in docs[0].page_content
    assert "第一段" in docs[0].page_content


def test_pdf_loader_pages(tmp_path):
    p = tmp_path / "a.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)  # 无文本页仅验证分页与不抛异常
    with open(p, "wb") as f:
        writer.write(f)
    docs = PdfFileLoader(str(p)).load()
    assert len(docs) == 1
    assert docs[0].metadata.get("page") == 1