"""本地文档直读加载器（替代 langchain-community 的 TextLoader/PyPDFLoader/CSVLoader/JSONLoader）。

背景：langchain-community 正被官方 sunset（DeprecationWarning）。对不依赖
unstructured 的轻量格式（txt/md/csv/json/pdf），本项目底层解析库（pypdf 等）
已是直接依赖，故改为本地直读，彻底去掉对 community 的依赖，行为契约与原
community loader 对齐（返回 List[langchain_core.Document]）。

unstructured 依赖的 Office/HTML/EPUB 加载器暂保留走 community（另见
document_processor.py 顶部导入），待 community 真正移除前再迁。

每个加载器提供 community 一致的构造签名与 .load() -> List[Document]。
"""

import csv
import json
import logging
from typing import List

from langchain_core.documents import Document

logger = logging.getLogger(__name__)


class TextFileLoader:
    """txt / markdown 等纯文本文件直读（显式 utf-8，等价原 TextLoader）。"""

    def __init__(self, file_path: str, encoding: str = "utf-8", autodetect_encoding: bool = False):
        self.file_path = file_path
        self.encoding = encoding
        self.autodetect_encoding = autodetect_encoding

    def load(self) -> List[Document]:
        with open(self.file_path, encoding=self.encoding) as f:
            text = f.read()
        return [Document(page_content=text, metadata={"source": self.file_path})]


class CsvFileLoader:
    """CSV 直读：每行一条 Document，内容为「列名: 值」逐行拼接（等价原 CSVLoader）。"""

    def __init__(self, file_path: str, encoding: str = "utf-8", **kwargs):
        self.file_path = file_path
        self.encoding = encoding

    @staticmethod
    def _row_to_content(row: List[str], headers: List[str]) -> str:
        return "\n".join(f"{headers[i]}: {row[i]}" for i in range(min(len(headers), len(row))))

    def load(self) -> List[Document]:
        documents: List[Document] = []
        with open(self.file_path, newline="", encoding=self.encoding) as f:
            reader = csv.reader(f)
            rows = list(reader)
        if not rows:
            return documents
        headers = rows[0]
        for idx, row in enumerate(rows[1:], start=1):
            if not row:
                continue
            documents.append(
                Document(
                    page_content=self._row_to_content(row, headers),
                    metadata={"source": self.file_path, "row": idx},
                )
            )
        return documents


class JsonFileLoader:
    """JSON 直读（text_content=True：递归抽取字符串值拼接，等价原 JSONLoader）。"""

    def __init__(self, file_path: str, text_content: bool = True, **kwargs):
        self.file_path = file_path
        self.text_content = text_content

    def _extract(self, data) -> List[str]:
        texts: List[str] = []
        if isinstance(data, str):
            texts.append(data)
        elif isinstance(data, dict):
            for v in data.values():
                texts.extend(self._extract(v))
        elif isinstance(data, list):
            for v in data:
                texts.extend(self._extract(v))
        return texts

    def load(self) -> List[Document]:
        with open(self.file_path, encoding="utf-8") as f:
            data = json.load(f)
        if self.text_content:
            content = "\n".join(self._extract(data))
        else:
            content = json.dumps(data, ensure_ascii=False, indent=2)
        return [Document(page_content=content, metadata={"source": self.file_path})]


class PdfFileLoader:
    """PDF 直读（pypdf）：每页一条 Document，等价原 PyPDFLoader。"""

    def __init__(self, file_path: str, **kwargs):
        self.file_path = file_path

    def load(self) -> List[Document]:
        from pypdf import PdfReader

        documents: List[Document] = []
        with open(self.file_path, "rb") as f:
            reader = PdfReader(f)
            for idx, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                documents.append(
                    Document(
                        page_content=text,
                        metadata={"source": self.file_path, "page": idx},
                    )
                )
        return documents