"""文档加载与切分入口：路径解析、多格式加载、split_documents（CODE-1 拆分）。"""

import logging
import os
import tempfile

logger = logging.getLogger("document_processor")
# txt/md/csv/json/pdf 走本地直读（复用已装 pypdf，去 langchain-community 依赖，见
# document_loaders.py）；unstructured 依赖的 Office/HTML/EPUB 仍走 community（warning 待迁）。
from src.services.document_loaders import (
    PdfFileLoader,
)

from langchain_core.documents import Document
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
)
from src.config import settings
from src.services.ocr_parser import is_scanned_pdf, parse_pdf_ocr

from src.services.document_types import FILE_TYPE_MAPPING, SUPPORTED_EXTENSIONS
from src.services.chunking import ChunkingFactory
from src.services.document_types import ChunkingStrategy
from src.utils.error_handling import log_exception

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
        
        try:
            minio_service.download_file(file_path, temp_path)
        except Exception:
            # 下载失败时清理刚创建的临时文件，避免脏文件残留在临时目录
            # （load_document 的 finally 清理逻辑在异常传播前不会执行）
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    logger.warning(f"清理下载失败的临时文件失败: {temp_path}")
            raise
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
        ValueError: 不支持的文件类型，或文档超出大小/字符数上限
    """
    local_path, is_temp = get_local_file_path(file_path)

    try:
        _, ext = os.path.splitext(local_path)
        ext = ext.lower()

        if ext not in FILE_TYPE_MAPPING:
            raise ValueError(f"不支持的文件类型: {ext}。支持的格式: {', '.join(SUPPORTED_EXTENSIONS)}")

        # W6 #19：加载前兜底文件大小校验——API 层对上传文件已有 MAX_UPLOAD_SIZE_MB
        # 限制，此处覆盖绕过 API 直接以本地路径调用的场景（如重新处理/批处理）
        max_bytes = settings.processing.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        try:
            file_size = os.path.getsize(local_path)
        except OSError:
            file_size = 0
        if file_size > max_bytes:
            raise ValueError(
                f"文档大小 {file_size / 1024 / 1024:.1f}MB 超过上限 "
                f"{settings.processing.MAX_UPLOAD_SIZE_MB}MB: {os.path.basename(local_path)}"
            )

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
                    # 回退内置解析器是设计好的降级，但扫描件长期走不到 OCR 会直接体现为
                    # 「表格/图片内容全丢」，所以要留下后端名与文件，便于判断是哪类失败
                    log_exception(
                        logger,
                        e,
                        action="document.load.ocr_fallback",
                        message="OCR 解析失败，回退内置 PDF 解析器",
                        file=os.path.basename(local_path),
                    )
            loader = PdfFileLoader(local_path)
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

        # W6 #19：加载后总字符数上限（loader 均为全量加载，用上限拒绝超大文档，
        # 防止内存膨胀；上限可经 MAX_DOCUMENT_CHARS 调整）
        total_chars = sum(len(doc.page_content or "") for doc in documents)
        if total_chars > settings.processing.MAX_DOCUMENT_CHARS:
            raise ValueError(
                f"文档解析后共 {total_chars} 字符，超过上限 "
                f"{settings.processing.MAX_DOCUMENT_CHARS}（MAX_DOCUMENT_CHARS），"
                f"请拆分后上传: {os.path.basename(local_path)}"
            )

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
