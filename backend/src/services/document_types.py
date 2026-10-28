"""文档处理公共类型与格式映射（CODE-1 拆分自 document_processor）。"""

import logging
import re

logger = logging.getLogger("document_processor")
# txt/md/csv/json/pdf 走本地直读（复用已装 pypdf，去 langchain-community 依赖，见
# document_loaders.py）；unstructured 依赖的 Office/HTML/EPUB 仍走 community（warning 待迁）。
from langchain_community.document_loaders import (
    UnstructuredWordDocumentLoader,
    UnstructuredExcelLoader,
    UnstructuredPowerPointLoader,
    UnstructuredHTMLLoader,
    UnstructuredEPubLoader,
)
from src.services.document_loaders import (
    TextFileLoader,
    CsvFileLoader,
    JsonFileLoader,
    PdfFileLoader,
)
from enum import Enum

from langchain_text_splitters import (
    Language,
)



FILE_TYPE_MAPPING = {
    ".txt": TextFileLoader,
    ".pdf": PdfFileLoader,
    ".docx": UnstructuredWordDocumentLoader,
    ".doc": UnstructuredWordDocumentLoader,
    ".xlsx": UnstructuredExcelLoader,
    ".xls": UnstructuredExcelLoader,
    ".pptx": UnstructuredPowerPointLoader,
    ".ppt": UnstructuredPowerPointLoader,
    ".md": TextFileLoader,
    ".markdown": TextFileLoader,
    ".csv": CsvFileLoader,
    ".json": JsonFileLoader,
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
