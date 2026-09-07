"""P0-2a 结构化分块测试。

覆盖：
- Markdown：标题路径构建与传播、超长章节二次切分、代码块不切分、表格行级分块
- HTML：标题路径、表格行级分块
- Word：elements 模式章节识别（python-docx 构造真实 docx）
- ChunkingFactory：策略路由
"""

import os
import tempfile

import pytest

from src.services.document_processor import (
    ChunkingFactory,
    ChunkingStrategy,
    _make_chunk,
    _normalize_heading_path,
    _split_html_structured,
    _split_markdown_structured,
    _split_word_structured,
    load_document,
)

SAMPLE_MD = """# 员工手册

公司总则说明。

## 请假制度

事假需提前申请。

### 年假规定

入职满一年享受 5 天带薪年假。

```python
def greet():
    name = "world"

    print(f"hello {name}")

    return 0
```

## 差旅标准

| 城市 | 住宿上限 | 高铁座席 |
| --- | --- | --- |
| 北京 | 500 | 二等座 |
| 上海 | 500 | 二等座 |
| 成都 | 350 | 二等座 |
"""


def test_normalize_heading_path():
    """标题路径应按 h1 > h2 > h3 拼接，空层级跳过。"""
    assert _normalize_heading_path({"h1": "A", "h2": "B", "h3": "C"}) == "A > B > C"
    assert _normalize_heading_path({"h1": "A", "h2": "", "h3": "C"}) == "A > C"
    assert _normalize_heading_path({"h1": "", "h2": "", "h3": ""}) == ""


def test_make_chunk_prefixes_heading_path():
    """heading_path 应前置到正文（进入 embedding/BM25 索引范围）并写入 metadata。"""
    chunk = _make_chunk("正文内容", {"h1": "手册", "h2": "考勤"})
    assert chunk.page_content.startswith("手册 > 考勤\n")
    assert chunk.metadata["heading_path"] == "手册 > 考勤"
    assert "正文内容" in chunk.page_content


def test_markdown_heading_path_propagation():
    """各章节 chunk 应携带正确的标题路径。"""
    chunks = _split_markdown_structured(SAMPLE_MD, chunk_size=500, chunk_overlap=50)
    paths = [c.metadata["heading_path"] for c in chunks]

    assert "员工手册" in paths
    assert "员工手册 > 请假制度" in paths
    assert "员工手册 > 请假制度 > 年假规定" in paths
    assert "员工手册 > 差旅标准" in paths


def test_markdown_code_block_intact():
    """代码块应作为完整 chunk 保留，内部空行不导致切断。"""
    chunks = _split_markdown_structured(SAMPLE_MD, chunk_size=500, chunk_overlap=50)
    code_chunks = [c for c in chunks if c.metadata.get("is_code")]

    assert len(code_chunks) == 1
    code = code_chunks[0].page_content
    assert code.count("```") == 2
    assert "def greet():" in code
    assert 'print(f"hello {name}")' in code


def test_markdown_table_row_chunks():
    """表格应行级分块：每行一条 chunk，内容含表头。"""
    chunks = _split_markdown_structured(SAMPLE_MD, chunk_size=500, chunk_overlap=50)
    table_chunks = [c for c in chunks if c.metadata.get("is_table_row")]

    assert len(table_chunks) == 3  # 三行数据
    header = "| 城市 | 住宿上限 | 高铁座席 |"
    for c in table_chunks:
        assert c.metadata["heading_path"] == "员工手册 > 差旅标准"
        assert header in c.page_content
    assert any("成都" in c.page_content for c in table_chunks)
    assert any("350" in c.page_content for c in table_chunks)


def test_markdown_oversized_section_split():
    """超长章节应二次切分且所有子 chunk 保留标题路径。"""
    long_body = "这是一段很长的制度说明。" * 100  # ~1200 字
    md = f"# 制度\n\n{long_body}\n"
    chunks = _split_markdown_structured(md, chunk_size=300, chunk_overlap=30)

    assert len(chunks) > 1
    for c in chunks:
        assert c.metadata["heading_path"] == "制度"
        assert len(c.page_content) <= 300 + len("制度\n") + 5  # 允许标题前缀长度


def test_html_heading_and_table():
    """HTML 应产出标题路径，表格拆为行级 chunk。"""
    html = """<html><body>
<h1>产品说明</h1>
<p>产品概述内容。</p>
<table>
  <tr><th>型号</th><th>价格</th></tr>
  <tr><td>A1</td><td>100</td></tr>
  <tr><td>B2</td><td>200</td></tr>
</table>
</body></html>"""
    chunks = _split_html_structured(html, chunk_size=500, chunk_overlap=50)
    table_chunks = [c for c in chunks if c.metadata.get("is_table_row")]

    assert len(table_chunks) == 2
    for c in table_chunks:
        assert c.metadata["heading_path"] == "产品说明"
        assert "| 型号 | 价格 |" in c.page_content
    assert any("A1" in c.page_content for c in table_chunks)


def _build_sample_docx(path: str):
    """用 python-docx 构造含标题/正文/表格的样例 docx。"""
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_heading("人事制度", level=1)
    doc.add_heading("考勤管理", level=2)
    doc.add_paragraph("员工每日需打卡两次。")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "项目"
    table.rows[0].cells[1].text = "标准"
    table.rows[1].cells[0].text = "迟到"
    table.rows[1].cells[1].text = "扣款50元"
    doc.add_heading("休假管理", level=2)
    doc.add_paragraph("年假需提前一周申请。")
    doc.save(path)


def test_word_elements_loading_and_split(tmp_path):
    """Word 走 elements 模式加载后，应按 Title 建章节并保留 heading_path。"""
    docx_path = str(tmp_path / "sample.docx")
    _build_sample_docx(docx_path)

    elements = load_document(docx_path)
    categories = [e.metadata.get("category") for e in elements]
    assert "Title" in categories, f"elements 模式未识别到标题，categories={categories}"

    chunks = _split_word_structured(elements, chunk_size=500, chunk_overlap=50)
    paths = [c.metadata["heading_path"] for c in chunks]

    assert "人事制度 > 考勤管理" in paths
    assert "人事制度 > 休假管理" in paths
    # 标题词前置进入正文，可供检索
    assert any("年假需提前一周申请" in c.page_content for c in chunks)


def test_factory_strategy_routing():
    """auto 策略应按扩展名路由到结构化策略。"""
    assert ChunkingFactory.get_strategy("a.md") == ChunkingStrategy.MARKDOWN
    assert ChunkingFactory.get_strategy("a.html") == ChunkingStrategy.HTML
    assert ChunkingFactory.get_strategy("a.docx") == ChunkingStrategy.WORD
    assert ChunkingFactory.get_strategy("a.txt") == ChunkingStrategy.RECURSIVE
    assert ChunkingFactory.get_strategy("a.py") == ChunkingStrategy.CODE


def test_factory_split_md_end_to_end(tmp_path):
    """工厂入口端到端：md 文件 → TextLoader → 结构化分块 + 来源元信息。"""
    md_path = tmp_path / "e2e.md"
    md_path.write_text(SAMPLE_MD, encoding="utf-8")

    from src.services.document_processor import process_document

    chunks = process_document(str(md_path))

    assert chunks, "端到端分块结果为空"
    assert all(c.metadata["chunk_strategy"] == "markdown" for c in chunks)
    assert any(c.metadata.get("is_table_row") for c in chunks)
    assert any(c.metadata["heading_path"] == "员工手册 > 请假制度 > 年假规定" for c in chunks)
    # chunk_index 连续且 total 一致
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c.metadata["total_chunks"] == len(chunks) for c in chunks)


def test_word_strategy_via_factory(tmp_path):
    """docx 通过 process_document 端到端分块。"""
    docx_path = str(tmp_path / "sample.docx")
    _build_sample_docx(docx_path)

    from src.services.document_processor import process_document

    chunks = process_document(docx_path)

    assert chunks
    assert all(c.metadata["chunk_strategy"] == "word" for c in chunks)
    assert any(c.metadata["heading_path"] == "人事制度 > 考勤管理" for c in chunks)
