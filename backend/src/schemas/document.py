"""
文档相关 Pydantic 模型统一定义

将 document.py 和 knowledge_base.py 中重复定义的请求/响应模型统一到这里，
非共有字段统一标记为 Optional，以兼容两个 API 的不同返回结构。
"""

from pydantic import BaseModel
from typing import List, Optional


class DuplicateDetectionRequest(BaseModel):
    """重复检测请求模型（统一）

    threshold 默认为 None，由各端点按自身算法解析为合适默认值：
    - document.py 的词频余弦相似度默认 0.7
    - knowledge_base.py 的语义向量相似度默认 0.85
    """
    doc_id: Optional[str] = None
    content: Optional[str] = None
    kb_id: Optional[str] = None
    threshold: Optional[float] = None


class DocumentQualityResponse(BaseModel):
    """文档质量评估响应模型（统一）

    为 document.py 与 knowledge_base.py 两个端点返回字段的并集：
    - document.py 端点返回 overall_grade/completeness_comment/readability 等
    - knowledge_base.py 端点返回 accuracy/language_quality/summary 等
    非共有字段标记为 Optional，未提供时序列化为 null。
    """
    overall_score: float
    overall_grade: Optional[str] = None
    completeness: float
    completeness_comment: Optional[str] = None
    readability: Optional[float] = None
    readability_comment: Optional[str] = None
    structure: float
    structure_comment: Optional[str] = None
    relevance: float
    relevance_comment: Optional[str] = None
    accuracy: Optional[float] = None
    language_quality: Optional[float] = None
    summary: Optional[str] = None
    suggestions: List[str]


class DocumentClassificationResponse(BaseModel):
    """文档分类响应模型（统一）

    为 document.py 与 knowledge_base.py 两个端点返回字段的并集：
    - document.py 端点返回 document_type_label/domain_label/confidence
    - knowledge_base.py 端点仅返回基础字段
    非共有字段标记为 Optional，未提供时序列化为 null。
    """
    document_type: str
    document_type_label: Optional[str] = None
    topics: List[str]
    domain: str
    domain_label: Optional[str] = None
    confidence: Optional[float] = None
    summary: str
