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
import time
import tempfile

logger = logging.getLogger("document_processor")
from langchain_community.document_loaders import (
    TextLoader,
    PyPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredExcelLoader,
    UnstructuredPowerPointLoader,
    UnstructuredMarkdownLoader,
    CSVLoader,
    JSONLoader,
    UnstructuredHTMLLoader,
    UnstructuredEPubLoader,
)
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.config import settings, DATA_DIR

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_document_process
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

# 文件类型与加载器映射
# 键: 文件扩展名（小写），值: LangChain加载器类
FILE_TYPE_MAPPING = {
    ".txt": TextLoader,
    ".pdf": PyPDFLoader,
    ".docx": UnstructuredWordDocumentLoader,
    ".doc": UnstructuredWordDocumentLoader,
    ".xlsx": UnstructuredExcelLoader,
    ".xls": UnstructuredExcelLoader,
    ".pptx": UnstructuredPowerPointLoader,
    ".ppt": UnstructuredPowerPointLoader,
    ".md": UnstructuredMarkdownLoader,
    ".markdown": UnstructuredMarkdownLoader,
    ".csv": CSVLoader,
    ".json": JSONLoader,
    ".html": UnstructuredHTMLLoader,
    ".htm": UnstructuredHTMLLoader,
    ".epub": UnstructuredEPubLoader,
}

# 支持的文件扩展名列表
SUPPORTED_EXTENSIONS = list(FILE_TYPE_MAPPING.keys())


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
        
        minio_service = MinioService()
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
        
        if ext == ".txt":
            loader = loader_class(local_path, encoding="utf-8")
        elif ext == ".csv":
            loader = loader_class(local_path, encoding="utf-8")
        elif ext == ".json":
            loader = loader_class(local_path, text_content=True)
        else:
            loader = loader_class(local_path)
        
        documents = loader.load()
        return documents
    finally:
        if is_temp and os.path.exists(local_path):
            os.remove(local_path)


def split_documents(documents, chunk_size=None, chunk_overlap=None):
    """
    将文档切分为文本块
    
    使用递归字符文本分割器，根据配置的chunk_size和chunk_overlap进行分割
    分割策略：优先按段落、换行、空格进行分割
    
    Args:
        documents: LangChain Document对象列表
        chunk_size: 文本块大小（可选，默认为配置文件中的CHUNK_SIZE）
        chunk_overlap: 文本块重叠大小（可选，默认为配置文件中的CHUNK_OVERLAP）
        
    Returns:
        list: 分割后的Document对象列表，每个chunk包含来源元信息
    """
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
        filename = os.path.basename(source) if source else 'unknown'
        
        # 添加来源信息到metadata
        chunk.metadata.update({
            'filename': filename,
            'chunk_index': i,
            'total_chunks': len(chunks),
            'source': source,
            'chunk_size': actual_chunk_size,
            'chunk_overlap': actual_chunk_overlap
        })
    
    return chunks


def process_document(file_path, chunk_size=None, chunk_overlap=None):
    """
    处理单个文档：加载并分割
    
    Args:
        file_path: 文件路径
        chunk_size: 文本块大小（可选）
        chunk_overlap: 文本块重叠大小（可选）
        
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
        chunks = split_documents(documents, chunk_size, chunk_overlap)
        return chunks
    finally:
        # 记录文档处理时间（Prometheus）
        if PROMETHEUS_AVAILABLE:
            record_document_process(file_type, time.time() - process_start)


def process_folder(folder_path):
    """
    批量处理文件夹中的所有文档
    
    遍历文件夹，对每个支持的文件类型进行处理
    
    Args:
        folder_path: 文件夹路径
        
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
                    chunks = process_document(file_path)
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
    
    minio_service = MinioService()
    
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
        minio_service = MinioService()
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


def get_document_chunks(file_path):
    """
    获取文档的所有文本块
    
    Args:
        file_path: 文件路径（本地路径或 MinIO 路径）
        
    Returns:
        list: 文本块列表，每个元素包含index和content
        
    Raises:
        ValueError: 文件不存在或不支持的文件类型
    """
    if file_path.startswith("minio://"):
        from src.services.minio_service import MinioService
        minio_service = MinioService()
        if not minio_service.file_exists(file_path):
            raise ValueError("文件不存在")
    elif not os.path.exists(file_path):
        raise ValueError("文件不存在")
    
    _, ext = os.path.splitext(file_path)
    ext = ext.lower()
    
    if ext not in FILE_TYPE_MAPPING:
        raise ValueError(f"不支持的文件类型: {ext}")
    
    chunks = process_document(file_path)
    return [
        {
            "index": chunk.metadata.get('chunk_index', i),
            "content": chunk.page_content,
            "filename": chunk.metadata.get('filename', ''),
            "total_chunks": chunk.metadata.get('total_chunks', len(chunks))
        }
        for i, chunk in enumerate(chunks)
    ]