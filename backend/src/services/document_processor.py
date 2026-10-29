"""文档处理入口：process_document / process_folder / 上传落盘 / 预览与切片读取（CODE-1 拆分后为入口层）。"""

import logging
import os
import time

logger = logging.getLogger("document_processor")
# txt/md/csv/json/pdf 走本地直读（复用已装 pypdf，去 langchain-community 依赖，见
# document_loaders.py）；unstructured 依赖的 Office/HTML/EPUB 仍走 community（warning 待迁）。


# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_document_process
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

from src.services.document_loader import load_document, split_documents
from src.services.chunking import (  # noqa: F401  兼容历史导入路径
    ChunkingFactory,
    _make_chunk,
    _normalize_heading_path,
    _split_html_structured,
    _split_long_text,
    _split_markdown_structured,
    _split_word_structured,
)
from src.services.document_types import ChunkingStrategy, FILE_TYPE_MAPPING, SUPPORTED_EXTENSIONS
from src.utils.error_handling import log_exception

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
                    # 批量导入里单篇失败只跳过该文件，但「某类文档一直没进库」必须能追到具体文件
                    log_exception(
                        logger,
                        e,
                        action="document.process_folder.file",
                        message="批量处理单个文档失败，已跳过",
                        file=os.path.basename(file_path),
                        ext=ext,
                    )
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
    
    # 注意：这里**不再**把异常统一包成 ValueError。
    # 路由层以 ValueError 判 400，包装会让「MinIO/OCR/解析器故障」伪装成「请求有问题」：
    # 既不触发 5xx 告警，也看不到原始异常类型，只剩一句被吞掉的文案。
    # 文件不存在、类型不支持、超限等调用方可纠正的问题已在上面直接抛 ValueError。
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

__all__ = [
    "process_document",
    "process_folder",
    "save_uploaded_file",
    "get_file_type_info",
    "preview_document",
    "get_document_chunks",
    "load_document",
    "split_documents",
    "ChunkingFactory",
    "ChunkingStrategy",
    "FILE_TYPE_MAPPING",
    "SUPPORTED_EXTENSIONS",
    "_make_chunk",
    "_normalize_heading_path",
    "_split_long_text",
    "_split_markdown_structured",
    "_split_html_structured",
    "_split_word_structured",
]
