"""文档分片：结构化切分策略与 ChunkingFactory（CODE-1 拆分自 document_processor）。"""

import logging
import os
import re

logger = logging.getLogger("document_processor")
# txt/md/csv/json/pdf 走本地直读（复用已装 pypdf，去 langchain-community 依赖，见
# document_loaders.py）；unstructured 依赖的 Office/HTML/EPUB 仍走 community（warning 待迁）。
from typing import List, Optional

from langchain_core.documents import Document
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    HTMLHeaderTextSplitter,
)
from src.config import settings

from src.services.document_types import (
    ChunkingStrategy,
    _CODE_LANGUAGE_MAP,
    _EXTENSION_STRATEGY_MAP,
    _HTML_TABLE_RE,
    _HTML_TAG_RE,
    _HTML_TR_RE,
    _MD_HEADING_RE,
    _MD_TABLE_DELIM_RE,
    _MD_TABLE_ROW_RE,
)

def _normalize_heading_path(heading_levels: dict) -> str:
    """由 h1/h2/h3 构建「A > B > C」形式的标题路径。"""
    parts = [heading_levels.get(k, "") for k in ("h1", "h2", "h3")]
    return " > ".join(p for p in parts if p)


def _make_chunk(content: str, heading_levels: dict, **extra_meta) -> Document:
    """构建带标题路径的结构化 chunk。

    heading_path 前置到正文，使标题词进入 embedding 与 BM25 的索引范围，
    同时为生成环节提供章节上下文。
    """
    heading_path = _normalize_heading_path(heading_levels)
    text = f"{heading_path}\n{content}" if heading_path else content
    metadata = {
        "h1": heading_levels.get("h1", ""),
        "h2": heading_levels.get("h2", ""),
        "h3": heading_levels.get("h3", ""),
        "heading_path": heading_path,
    }
    metadata.update(extra_meta)
    return Document(page_content=text, metadata=metadata)


def _split_long_text(text: str, heading_levels: dict, chunk_size: int, chunk_overlap: int) -> List[Document]:
    """超长正文用 Recursive 二次切分，heading_path 传播到每个子 chunk。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", " ", ""],
    )
    return [_make_chunk(part, heading_levels) for part in splitter.split_text(text)]


def _split_markdown_structured(text: str, chunk_size: int, chunk_overlap: int) -> List[Document]:
    """Markdown 结构化分块。

    逐行扫描，维护标题栈：
    - 普通正文：超长时二次切分
    - fenced 代码块：原子 chunk，永不跨块切断（超长时按行分段并补全围栏）
    - 表格：行级分块，每行一条 chunk（内容 = 表头 + 该行），继承标题路径
    """
    lines = text.split("\n")
    heading_levels = {"h1": "", "h2": "", "h3": ""}
    chunks: List[Document] = []
    text_buffer: List[str] = []

    def flush_text():
        content = "\n".join(text_buffer).strip()
        if content:
            if len(content) <= chunk_size:
                chunks.append(_make_chunk(content, heading_levels))
            else:
                chunks.extend(_split_long_text(content, heading_levels, chunk_size, chunk_overlap))
        text_buffer.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        heading_match = _MD_HEADING_RE.match(line)
        if heading_match:
            flush_text()
            level, title = len(heading_match.group(1)), heading_match.group(2).strip()
            if level == 1:
                heading_levels = {"h1": title, "h2": "", "h3": ""}
            elif level == 2:
                heading_levels["h2"] = title
                heading_levels["h3"] = ""
            else:
                heading_levels["h3"] = title
        elif line.strip().startswith("```"):
            flush_text()
            code_lines = [line]
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                code_lines.append(lines[i])
            code = "\n".join(code_lines)
            if len(code) <= max(chunk_size, 2000):
                chunks.append(_make_chunk(code, heading_levels, is_code=True))
            else:
                # 超长代码块按行分段，每段补全围栏保持代码完整性
                fence_open, fence_close = code_lines[0], code_lines[-1]
                body = code_lines[1:-1] if code_lines[-1].strip().startswith("```") else code_lines[1:]
                seg: List[str] = []
                for ln in body:
                    seg.append(ln)
                    if len("\n".join(seg)) >= chunk_size:
                        chunks.append(_make_chunk(
                            f"{fence_open}\n" + "\n".join(seg) + f"\n{fence_close}",
                            heading_levels, is_code=True,
                        ))
                        seg = []
                if seg:
                    chunks.append(_make_chunk(
                        f"{fence_open}\n" + "\n".join(seg) + f"\n{fence_close}",
                        heading_levels, is_code=True,
                    ))
        elif _MD_TABLE_ROW_RE.match(line) and i + 1 < len(lines) and _MD_TABLE_DELIM_RE.match(lines[i + 1]):
            flush_text()
            header = line
            i += 2  # 跳过分隔行
            rows = []
            while i < len(lines) and _MD_TABLE_ROW_RE.match(lines[i]):
                rows.append(lines[i].strip())
                i += 1
            for row in rows:
                chunks.append(_make_chunk(f"{header}\n{row}", heading_levels, is_table_row=True))
            continue  # 已消费到下一非表格行，避免末尾再 i += 1
        else:
            text_buffer.append(line)
        i += 1
    flush_text()
    return chunks


def _html_table_row_chunks(table_html: str, heading_levels: dict) -> List[Document]:
    """HTML 表格行级分块：首行为表头，每行一条 chunk。"""
    trs = _HTML_TR_RE.findall(table_html)
    if len(trs) < 2:
        return [_make_chunk(_HTML_TAG_RE.sub(" ", table_html).strip(), heading_levels, is_table_row=True)]
    cells = lambda tr: [c.strip() for c in (_HTML_TAG_RE.sub("", td) for td in re.findall(r"<t[dh][\s\S]*?</t[dh]>", tr, re.IGNORECASE))]
    header = "| " + " | ".join(cells(trs[0])) + " |"
    return [
        _make_chunk(f"{header}\n| " + " | ".join(cells(tr)) + " |", heading_levels, is_table_row=True)
        for tr in trs[1:]
    ]


def _split_html_structured(text: str, chunk_size: int, chunk_overlap: int) -> List[Document]:
    """HTML 结构化分块：先提取表格（占位符）→ header 切分 → 表格行级还原 + 正文二次切分。

    HTMLHeaderTextSplitter 会把 <table> 展开为纯文本导致无法按行拆分，
    因此在切分前将表格替换为占位符，切分后按所在 section 的标题路径还原为行级 chunk。
    """
    tables = _HTML_TABLE_RE.findall(text)
    counter = {"i": 0}

    def _replace(m):
        # 用 <p> 包裹占位符：裸文本节点会被 splitter 放进无标题元数据的独立 section
        token = f"<p>__TABLE_{counter['i']}__</p>"
        counter["i"] += 1
        return token

    text_with_tokens = _HTML_TABLE_RE.sub(_replace, text)

    splitter = HTMLHeaderTextSplitter(headers_to_split_on=[("h1", "h1"), ("h2", "h2"), ("h3", "h3")])
    sections = splitter.split_text(text_with_tokens)
    chunks: List[Document] = []
    for section in sections:
        heading_levels = {k: section.metadata.get(k, "") for k in ("h1", "h2", "h3")}
        heading_path = _normalize_heading_path(heading_levels)
        content = section.page_content

        tokens = re.findall(r"__TABLE_(\d+)__", content)
        for idx in tokens:
            content = content.replace(f"__TABLE_{idx}__", "")
            table = tables[int(idx)]
            chunks.extend(_html_table_row_chunks(table, heading_levels))

        remainder = re.sub(r"\n+", "\n", content).strip()
        if not remainder:
            continue
        # 跳过纯标题 chunk（splitter 会把标题行保留为正文），避免"标题×2"噪声
        if heading_path and remainder.replace(" ", "") in heading_path.replace(" ", ""):
            continue
        if len(remainder) <= chunk_size:
            chunks.append(_make_chunk(remainder, heading_levels))
        else:
            chunks.extend(_split_long_text(remainder, heading_levels, chunk_size, chunk_overlap))
    return chunks


def _split_word_structured(documents: List[Document], chunk_size: int, chunk_overlap: int) -> List[Document]:
    """Word 结构化分块（依赖 elements 模式加载）。

    - Title 元素开启新章节；unstructured 不保留标题层级，用启发式标题栈
      构建路径：新标题前若已产生正文则视为平级（弹出栈顶），否则视为下级（入栈）
    - Table 元素整块保留（附标题路径），不做行级拆分
    - 其余正文累积，超长时二次切分
    """
    chunks: List[Document] = []
    title_stack: List[str] = []
    text_buffer: List[str] = []

    def _levels() -> dict:
        keys = ("h1", "h2", "h3")
        return {k: v for k, v in zip(keys, title_stack[:3])}

    def flush():
        content = "\n".join(text_buffer).strip()
        if content:
            if len(content) <= chunk_size:
                chunks.append(_make_chunk(content, _levels()))
            else:
                chunks.extend(_split_long_text(content, _levels(), chunk_size, chunk_overlap))
        text_buffer.clear()

    def heading_path() -> str:
        return " > ".join(title_stack)

    def has_body_content() -> bool:
        """当前叶子章节是否已产出正文/表格 chunk（用于标题栈平级判定）。"""
        return bool(chunks) and chunks[-1].metadata.get("heading_path") == heading_path()

    for doc in documents:
        category = doc.metadata.get("category", "NarrativeText")
        content = (doc.page_content or "").strip()
        if not content:
            continue
        if category == "Title":
            flush()
            if title_stack and has_body_content():
                title_stack.pop()  # 新标题为平级章节
            title_stack.append(content)
        elif category == "Table":
            flush()
            chunks.append(_make_chunk(content, _levels(), is_table=True))
        elif category in ("Header", "Footer"):
            continue  # 页眉页脚噪声
        else:
            text_buffer.append(content)
    flush()
    return chunks


class ChunkingFactory:
    """分块策略工厂，根据文档类型/扩展名选择分块策略。"""

    @classmethod
    def get_strategy(cls, file_path: str, strategy: ChunkingStrategy = ChunkingStrategy.AUTO) -> ChunkingStrategy:
        """解析实际使用的分块策略。

        Args:
            file_path: 文件路径，用于 auto 模式推断。
            strategy: 指定策略，auto 时按扩展名推断。

        Returns:
            实际使用的 ChunkingStrategy。
        """
        if isinstance(strategy, str):
            strategy = ChunkingStrategy(strategy.lower())
        if strategy != ChunkingStrategy.AUTO:
            return strategy
        _, ext = os.path.splitext(file_path)
        ext = ext.lower()
        return _EXTENSION_STRATEGY_MAP.get(ext, ChunkingStrategy.RECURSIVE)

    @classmethod
    def create_splitter(
        cls,
        strategy: ChunkingStrategy,
        chunk_size: int,
        chunk_overlap: int,
        file_path: str = "",
    ):
        """创建对应策略的文本分割器。

        Args:
            strategy: 分块策略。
            chunk_size: 块大小。
            chunk_overlap: 块重叠。
            file_path: 文件路径（代码策略推断语言）。

        Returns:
            文本分割器实例。
        """
        if strategy == ChunkingStrategy.CODE:
            _, ext = os.path.splitext(file_path)
            ext = ext.lower()
            language = _CODE_LANGUAGE_MAP.get(ext)
            if language is not None:
                return RecursiveCharacterTextSplitter.from_language(
                    language=language,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                )
            return RecursiveCharacterTextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                length_function=len,
                separators=["\n\n", "\n", " ", ""],
            )

        # 默认 recursive
        return RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", " ", ""],
        )

    @classmethod
    def split(
        cls,
        documents: List[Document],
        file_path: str = "",
        chunk_strategy: ChunkingStrategy = ChunkingStrategy.AUTO,
        chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None,
    ) -> List[Document]:
        """按指定策略对文档进行分块。

        Args:
            documents: 待分块的 Document 列表。
            file_path: 文件路径（用于 auto 推断与代码语言识别）。
            chunk_strategy: 分块策略，默认 auto。
            chunk_size: 块大小，None 时使用配置默认值。
            chunk_overlap: 块重叠，None 时使用配置默认值。

        Returns:
            分块后的 Document 列表，每个 chunk 包含来源元信息。
        """
        actual_chunk_size = chunk_size if chunk_size is not None else settings.processing.CHUNK_SIZE
        actual_chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.processing.CHUNK_OVERLAP

        strategy = cls.get_strategy(file_path, chunk_strategy)

        # 结构化策略：标题路径 / 表格行级 / 代码块保护（P0-2a）
        if strategy == ChunkingStrategy.MARKDOWN:
            all_text = "\n\n".join(doc.page_content for doc in documents)
            chunks = _split_markdown_structured(all_text, actual_chunk_size, actual_chunk_overlap)
        elif strategy == ChunkingStrategy.HTML:
            all_text = "\n\n".join(doc.page_content for doc in documents)
            chunks = _split_html_structured(all_text, actual_chunk_size, actual_chunk_overlap)
        elif strategy == ChunkingStrategy.WORD:
            chunks = _split_word_structured(documents, actual_chunk_size, actual_chunk_overlap)
        else:
            splitter = cls.create_splitter(strategy, actual_chunk_size, actual_chunk_overlap, file_path)
            chunks = splitter.split_documents(documents)

        # 为每个 chunk 添加来源元信息
        for i, chunk in enumerate(chunks):
            source = chunk.metadata.get("source", "")
            filename = os.path.basename(source) if source else os.path.basename(file_path) or "unknown"
            doc_type = strategy.value

            chunk.metadata.update({
                "filename": filename,
                "chunk_index": i,
                "total_chunks": len(chunks),
                "source": source,
                "doc_type": doc_type,
                "chunk_size": actual_chunk_size,
                "chunk_overlap": actual_chunk_overlap,
                "chunk_strategy": strategy.value,
            })

        return chunks
