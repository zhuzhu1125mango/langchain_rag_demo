"""
文档处理器 - 多格式文档加载与文本分片

本模块负责：
1. 支持多种格式文档的加载（TXT、PDF、Word、Excel、PPT、Markdown等）
2. 文本分片处理（将长文档切分为小片段）
3. 文件保存功能
4. 集成 Prometheus 指标记录

支持的文件格式：
- 文本文档: TXT, PDF, EPUB
- Office文档: DOCX, DOC, XLSX, XLS, PPTX, PPT
- Markdown: MD, MARKDOWN
- 数据文件: CSV, JSON
- 网页: HTML, HTM
"""

import logging
import os
import re
import time
import tempfile

logger = logging.getLogger("document_processor")
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredExcelLoader,
    UnstructuredPowerPointLoader,
    CSVLoader,
    JSONLoader,
    UnstructuredHTMLLoader,
    UnstructuredEPubLoader,
)
from enum import Enum
from typing import List, Optional

from langchain_core.documents import Document
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
    HTMLHeaderTextSplitter,
    Language,
)
from src.config import settings, DATA_DIR
from src.services.ocr_parser import is_scanned_pdf, parse_pdf_ocr

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_document_process
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

# 文件类型与加载器映射
# 键: 文件扩展名（小写），值: LangChain加载器类
# 注意: .md/.markdown 使用 TextLoader 保留原始 markdown 语法（# 标记），
# 供 MarkdownHeaderTextSplitter 识别标题层级；Unstructured 加载器会把
# 标记剥离为纯文本导致标题切分失效。
FILE_TYPE_MAPPING = {
    ".txt": TextLoader,
    ".pdf": PyPDFLoader,
    ".docx": UnstructuredWordDocumentLoader,
    ".doc": UnstructuredWordDocumentLoader,
    ".xlsx": UnstructuredExcelLoader,
    ".xls": UnstructuredExcelLoader,
    ".pptx": UnstructuredPowerPointLoader,
    ".ppt": UnstructuredPowerPointLoader,
    ".md": TextLoader,
    ".markdown": TextLoader,
    ".csv": CSVLoader,
    ".json": JSONLoader,
    ".html": UnstructuredHTMLLoader,
    ".htm": UnstructuredHTMLLoader,
    ".epub": UnstructuredEPubLoader,
}

# 支持的文件扩展名列表
SUPPORTED_EXTENSIONS = list(FILE_TYPE_MAPPING.keys())


class ChunkingStrategy(str, Enum):
    """分块策略枚举。"""

    AUTO = "auto"
    RECURSIVE = "recursive"
    MARKDOWN = "markdown"
    HTML = "html"
    CODE = "code"
    WORD = "word"
    SEMANTIC = "semantic"


# 文件扩展名到默认分块策略的映射
_EXTENSION_STRATEGY_MAP = {
    ".md": ChunkingStrategy.MARKDOWN,
    ".markdown": ChunkingStrategy.MARKDOWN,
    ".html": ChunkingStrategy.HTML,
    ".htm": ChunkingStrategy.HTML,
    ".docx": ChunkingStrategy.WORD,
    ".doc": ChunkingStrategy.WORD,
    ".py": ChunkingStrategy.CODE,
    ".js": ChunkingStrategy.CODE,
    ".ts": ChunkingStrategy.CODE,
    ".java": ChunkingStrategy.CODE,
    ".go": ChunkingStrategy.CODE,
    ".rs": ChunkingStrategy.CODE,
    ".cpp": ChunkingStrategy.CODE,
    ".c": ChunkingStrategy.CODE,
    ".cs": ChunkingStrategy.CODE,
}

# 编程语言到 Language 枚举的映射
_CODE_LANGUAGE_MAP = {
    ".py": Language.PYTHON,
    ".js": Language.JS,
    ".ts": Language.TS,
    ".java": Language.JAVA,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".cpp": Language.CPP,
    ".c": Language.C,
    ".cs": Language.CSHARP,
}


# =============================================================================
# 结构化分块（P0-2a）：标题层级路径 / 表格行级分块 / 代码块不切分
# =============================================================================

_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_DELIM_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_HTML_TABLE_RE = re.compile(r"<table[\s\S]*?</table>", re.IGNORECASE)
_HTML_TR_RE = re.compile(r"<tr[\s\S]*?</tr>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


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


def get_local_file_path(file_path):
    """
    获取本地文件路径（处理 MinIO 路径）
    
    Args:
        file_path: 文件路径（可能是本地路径或 MinIO 路径）
        
    Returns:
        str: 本地文件路径（可能是临时文件）
        bool: 是否为临时文件，需要清理
    """
    if file_path.startswith("minio://"):
        from src.services.minio_service import MinioService
        
        minio_service = MinioService.get_instance_sync()
        _, ext = os.path.splitext(file_path)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
            temp_path = temp_file.name
        
        minio_service.download_file(file_path, temp_path)
        return temp_path, True
    
    return file_path, False


def load_document(file_path):
    """
    根据文件类型加载文档
    
    Args:
        file_path: 文件路径（本地路径或 MinIO 路径）
        
    Returns:
        list: LangChain Document对象列表
        
    Raises:
        ValueError: 不支持的文件类型
    """
    local_path, is_temp = get_local_file_path(file_path)
    
    try:
        _, ext = os.path.splitext(local_path)
        ext = ext.lower()
        
        if ext not in FILE_TYPE_MAPPING:
            raise ValueError(f"不支持的文件类型: {ext}。支持的格式: {', '.join(SUPPORTED_EXTENSIONS)}")
        
        loader_class = FILE_TYPE_MAPPING[ext]
        
        if ext in (".txt", ".md", ".markdown"):
            # 显式 utf-8：Windows 默认 GBK 会导致中文文档解码失败
            loader = loader_class(local_path, encoding="utf-8")
        elif ext == ".pdf":
            # P0-2b：扫描版 PDF 分流 OCR 深度解析，输出 Markdown 后走
            # Markdown 结构化分块；未安装后端/解析失败时回退内置解析，不阻断上传
            if settings.ocr.DEEP_PARSING_ENABLED and is_scanned_pdf(local_path):
                try:
                    md_text, backend_name = parse_pdf_ocr(local_path)
                    return [
                        Document(
                            page_content=md_text,
                            metadata={"source": local_path, "ocr_backend": backend_name},
                        )
                    ]
                except Exception as e:
                    logger.warning(f"OCR 解析失败，回退内置 PDF 解析器: {local_path}, 错误: {e}")
            loader = PyPDFLoader(local_path)
        elif ext == ".csv":
            loader = loader_class(local_path, encoding="utf-8")
        elif ext == ".json":
            loader = loader_class(local_path, text_content=True)
        elif ext in (".docx", ".doc"):
            # elements 模式：输出 Title/NarrativeText/Table 等结构化元素，
            # 供 Word 结构化分块识别章节（single 模式会丢失标题结构）
            loader = loader_class(local_path, mode="elements")
        else:
            loader = loader_class(local_path)
        
        documents = loader.load()
        return documents
    finally:
        if is_temp and os.path.exists(local_path):
            os.remove(local_path)


def split_documents(documents, chunk_size=None, chunk_overlap=None, chunk_strategy=ChunkingStrategy.RECURSIVE, file_path=""):
    """
    将文档切分为文本块。

    默认保留原有 RecursiveCharacterTextSplitter 行为；
    传入 chunk_strategy 时，使用 ChunkingFactory 选择更合适的分块策略。

    Args:
        documents: LangChain Document对象列表
        chunk_size: 文本块大小（可选，默认为配置文件中的CHUNK_SIZE）
        chunk_overlap: 文本块重叠大小（可选，默认为配置文件中的CHUNK_OVERLAP）
        chunk_strategy: 分块策略（可选，默认 RECURSIVE）
        file_path: 文件路径（可选，用于 auto 推断与代码语言识别）

    Returns:
        list: 分割后的Document对象列表，每个chunk包含来源元信息
    """
    if chunk_strategy != ChunkingStrategy.RECURSIVE:
        return ChunkingFactory.split(
            documents,
            file_path=file_path,
            chunk_strategy=chunk_strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )

    # 使用传入的参数或默认配置
    actual_chunk_size = chunk_size if chunk_size is not None else settings.processing.CHUNK_SIZE
    actual_chunk_overlap = chunk_overlap if chunk_overlap is not None else settings.processing.CHUNK_OVERLAP

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=actual_chunk_size,
        chunk_overlap=actual_chunk_overlap,
        length_function=len,
        separators=["\n\n", "\n", " ", ""]
    )
    chunks = text_splitter.split_documents(documents)

    # 为每个chunk添加来源元信息
    for i, chunk in enumerate(chunks):
        # 从原始文档获取文件名
        source = chunk.metadata.get('source', '')
        filename = os.path.basename(source) if source else os.path.basename(file_path) or 'unknown'

        # 添加来源信息到metadata
        chunk.metadata.update({
            'filename': filename,
            'chunk_index': i,
            'total_chunks': len(chunks),
            'source': source,
            'doc_type': ChunkingStrategy.RECURSIVE.value,
            'chunk_size': actual_chunk_size,
            'chunk_overlap': actual_chunk_overlap,
            'chunk_strategy': ChunkingStrategy.RECURSIVE.value,
        })

    return chunks


def process_document(file_path, chunk_size=None, chunk_overlap=None, chunk_strategy=ChunkingStrategy.AUTO):
    """
    处理单个文档：加载并按策略分割。

    Args:
        file_path: 文件路径
        chunk_size: 文本块大小（可选）
        chunk_overlap: 文本块重叠大小（可选）
        chunk_strategy: 分块策略（可选，默认 auto 按扩展名推断）

    Returns:
        list: 分割后的Document对象列表
    """
    # 记录文档处理开始时间
    process_start = time.time()

    # 获取文件类型
    _, ext = os.path.splitext(file_path)
    file_type = ext.lower().lstrip('.') if ext else 'unknown'

    try:
        documents = load_document(file_path)
        # P0-2b：OCR 解析出的 Markdown 文档自动切换 Markdown 结构化分块
        ocr_backend = next(
            (d.metadata.get("ocr_backend") for d in documents if d.metadata.get("ocr_backend")),
            None,
        )
        if ocr_backend and chunk_strategy == ChunkingStrategy.AUTO:
            chunk_strategy = ChunkingStrategy.MARKDOWN
        chunks = split_documents(
            documents,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            chunk_strategy=chunk_strategy,
            file_path=file_path,
        )
        if ocr_backend:
            for chunk in chunks:
                chunk.metadata["ocr_backend"] = ocr_backend
        return chunks
    finally:
        # 记录文档处理时间（Prometheus）
        if PROMETHEUS_AVAILABLE:
            record_document_process(file_type, time.time() - process_start)


def process_folder(folder_path, chunk_strategy=ChunkingStrategy.AUTO):
    """
    批量处理文件夹中的所有文档

    遍历文件夹，对每个支持的文件类型进行处理

    Args:
        folder_path: 文件夹路径
        chunk_strategy: 分块策略（可选，默认 auto 按扩展名推断）

    Returns:
        list: 所有分割后的Document对象列表
    """
    all_chunks = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            file_path = os.path.join(root, file)
            _, ext = os.path.splitext(file_path)
            ext = ext.lower()

            if ext in SUPPORTED_EXTENSIONS:
                try:
                    chunks = process_document(file_path, chunk_strategy=chunk_strategy)
                    all_chunks.extend(chunks)
                    logger.info(f"已处理: {file_path}")
                except Exception as e:
                    logger.error(f"处理失败: {file_path}, 错误: {e}")
            else:
                logger.debug(f"跳过不支持的格式: {file_path}")

    return all_chunks


async def save_uploaded_file(uploaded_file):
    """
    保存上传的文件到 MinIO
    
    Args:
        uploaded_file: FastAPI UploadFile对象或文件路径字符串
        
    Returns:
        str: 保存后的文件路径（MinIO路径）
    """
    from src.services.minio_service import MinioService
    
    minio_service = await MinioService.get_instance()
    
    if isinstance(uploaded_file, str):
        if uploaded_file.startswith("minio://"):
            return uploaded_file
        
        with open(uploaded_file, "rb") as f:
            import io
            from fastapi import UploadFile
            
            file_like = io.BytesIO(f.read())
            mock_file = UploadFile(
                filename=os.path.basename(uploaded_file),
                file=file_like,
                content_type="application/octet-stream"
            )
            return minio_service.upload_file(mock_file)
    else:
        return minio_service.upload_file(uploaded_file)


def get_file_type_info():
    """
    获取支持的文件类型信息
    
    Returns:
        str: 格式化的文件类型列表字符串
    """
    info = []
    for ext, loader_class in FILE_TYPE_MAPPING.items():
        info.append(f"{ext}: {loader_class.__name__.replace('Loader', '')}")
    return "\n".join(info)


def preview_document(file_path, max_length=1000, page=1, page_size=500):
    """
    预览文档内容
    
    Args:
        file_path: 文件路径（本地路径或 MinIO 路径）
        max_length: 最大返回字符数（默认1000）
        page: 页码（用于分页预览，默认1）
        page_size: 每页字符数（默认500）
        
    Returns:
        dict: {"content": 预览内容, "total_length": 总长度, "page": 当前页, "total_pages": 总页数}
        
    Raises:
        ValueError: 文件不存在或不支持的文件类型
    """
    if file_path.startswith("minio://"):
        from src.services.minio_service import MinioService
        minio_service = MinioService.get_instance_sync()
        if not minio_service.file_exists(file_path):
            raise ValueError("文件不存在")
    elif not os.path.exists(file_path):
        raise ValueError("文件不存在")
    
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    
    if ext not in FILE_TYPE_MAPPING:
        raise ValueError(f"不支持的文件类型: {ext}")
    
    try:
        documents = load_document(file_path)
        full_content = "\n\n".join(doc.page_content for doc in documents)
        total_length = len(full_content)
        
        # 计算分页
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        preview_content = full_content[start_idx:end_idx]
        
        total_pages = max(1, (total_length + page_size - 1) // page_size)
        
        return {
            "content": preview_content,
            "total_length": total_length,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "truncated": end_idx < total_length
        }
    except Exception as e:
        raise ValueError(f"预览文档失败: {str(e)}")


def get_document_chunks(file_path, chunk_strategy=ChunkingStrategy.AUTO):
    """
    获取文档的所有文本块

    Args:
        file_path: 文件路径（本地路径或 MinIO 路径）
        chunk_strategy: 分块策略（可选，默认 auto 按扩展名推断）

    Returns:
        list: 文本块列表，每个元素包含index和content

    Raises:
        ValueError: 文件不存在或不支持的文件类型
    """
    if file_path.startswith("minio://"):
        from src.services.minio_service import MinioService
        minio_service = MinioService.get_instance_sync()
        if not minio_service.file_exists(file_path):
            raise ValueError("文件不存在")
    elif not os.path.exists(file_path):
        raise ValueError("文件不存在")

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    if ext not in FILE_TYPE_MAPPING:
        raise ValueError(f"不支持的文件类型: {ext}")

    chunks = process_document(file_path, chunk_strategy=chunk_strategy)
    return [
        {
            "index": chunk.metadata.get('chunk_index', i),
            "content": chunk.page_content,
            "filename": chunk.metadata.get('filename', ''),
            "total_chunks": chunk.metadata.get('total_chunks', len(chunks)),
            "doc_type": chunk.metadata.get('doc_type', ''),
            "chunk_strategy": chunk.metadata.get('chunk_strategy', ''),
        }
        for i, chunk in enumerate(chunks)
    ]