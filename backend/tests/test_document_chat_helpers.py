"""
专项 D4 补测：api/document.py 与 api/chat.py 轻依赖单元行为测试。

定位：巨型文件拆分（special-projects §A）前的行为守卫。不追求端到端覆盖，
聚焦可确定性断言的真实行为——文档序列化映射、状态归一、重复检测相似度、
分块配置回显、LLM 分析兜底分支、消息请求模型校验。
_parse_session_id / _ensure_upload_size 已由 test_security_hardening.py 覆盖，不重复。
"""

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.chat import MessageRequest, _parse_session_id  # noqa: F401 (_parse_session_id 供 fixture 复用断言)
from src.api.document import (
    analyze_document_with_llm,
    calculate_similarity,
    get_chunk_config,
    get_status_display,
    serialize_document,
)
from src.config import settings


# ---------------------------------------------------------------------------
# get_status_display：内部状态 → 对外展示状态归一
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        ("published", "active"),
        ("draft", "inactive"),
        ("archived", "inactive"),
        ("unknown_value", "inactive"),  # 未知状态兜底 inactive
    ],
)
def test_get_status_display_mapping(status, expected):
    assert get_status_display(status) == expected


# ---------------------------------------------------------------------------
# serialize_document：ORM → 响应模型的字段映射与空值分支
# ---------------------------------------------------------------------------

class _FakeRelated:
    def __init__(self, name):
        self.name = name


class _FakeDoc:
    """serialize_document 输入桩：仅提供其读取的属性。"""

    def __init__(self, **overrides):
        self.id = "doc-uuid-1"
        self.filename = "report.pdf"
        self.file_type = "pdf"
        self.size = 1024
        self.kb_id = "kb-uuid-1"
        self.knowledge_base = _FakeRelated("测试知识库")
        self.category = _FakeRelated("手册")
        self.status = "published"
        self.processing_status = "completed"
        self.processing_message = "解析完成"
        self.processing_progress = 100
        self.chunks_count = 42
        self.created_at = datetime(2026, 9, 30, 8, 0, 0, tzinfo=timezone.utc)
        self.tags = ["guide", "v2"]
        self.document_type = "manual"
        self.document_type_label = "操作手册"
        self.domain = "ops"
        self.domain_label = "运维"
        self.topics = ["部署"]
        self.summary = "一段摘要"
        self.quality_score = 88
        self.quality_grade = "良好"
        for key, value in overrides.items():
            setattr(self, key, value)


def test_serialize_document_full_mapping():
    resp = serialize_document(_FakeDoc())

    assert resp.id == "doc-uuid-1"
    assert resp.filename == "report.pdf"
    assert resp.kb_id == "kb-uuid-1"
    assert resp.kb_name == "测试知识库"
    assert resp.status == "active"  # published → active 归一
    assert resp.chunks_count == 42
    assert resp.created_at == "2026-09-30T08:00:00+00:00"
    assert resp.category_name == "手册"
    assert resp.tags == ["guide", "v2"]
    assert resp.document_type_label == "操作手册"
    assert resp.domain_label == "运维"
    assert resp.topics == ["部署"]
    assert resp.quality_score == 88
    assert resp.quality_grade == "良好"


def test_serialize_document_none_branches():
    """关联对象缺失/空值时走 None 与默认值分支（列表字段空值兜底为 []）。"""
    resp = serialize_document(_FakeDoc(
        kb_id=None, knowledge_base=None, category=None,
        tags=None, document_type=None, document_type_label=None,
        domain=None, domain_label=None, topics=None, summary=None,
        quality_score=None, quality_grade=None,
    ))

    assert resp.kb_id is None
    assert resp.kb_name is None
    assert resp.category_name is None
    assert resp.tags == []
    assert resp.document_type is None
    assert resp.domain_label is None
    assert resp.topics == []
    assert resp.summary is None
    assert resp.quality_score is None
    assert resp.quality_grade is None


def test_serialize_document_falsy_values_are_masked():
    """现状行为锁定：falsy 值（0 分、空串）经 `or None` 归一为 None。"""
    resp = serialize_document(_FakeDoc(quality_score=0, summary="", document_type=""))
    assert resp.quality_score is None
    assert resp.summary is None
    assert resp.document_type is None


# ---------------------------------------------------------------------------
# calculate_similarity：词频余弦 + 字符 Jaccard 的轻量重复检测
# ---------------------------------------------------------------------------

def test_calculate_similarity_identical_text_is_one():
    text = "FastAPI 使用 uvicorn 启动服务"
    assert calculate_similarity(text, text) == pytest.approx(1.0)


def test_calculate_similarity_case_insensitive():
    assert calculate_similarity("Hello World", "hello world") == pytest.approx(1.0)


def test_calculate_similarity_disjoint_text_is_low():
    score = calculate_similarity("apple banana orange", "汽车 发动机 轮胎")
    assert 0.0 <= score < 0.3


def test_calculate_similarity_empty_text_is_zero():
    assert calculate_similarity("", "anything") == 0.0
    assert calculate_similarity("", "") == 0.0


def test_calculate_similarity_partial_overlap_is_between():
    score = calculate_similarity(
        "RAG 知识库检索与问答", "RAG 知识库检索与流式问答系统"
    )
    assert 0.3 < score < 1.0


# ---------------------------------------------------------------------------
# get_chunk_config：配置回显与推荐区间结构
# ---------------------------------------------------------------------------

def test_get_chunk_config_reflects_settings():
    """端点为普通 async 函数（无依赖注入），直接 await 验证。"""
    import asyncio

    config = asyncio.run(get_chunk_config())

    assert config["chunk_size"] == settings.processing.CHUNK_SIZE
    assert config["chunk_overlap"] == settings.processing.CHUNK_OVERLAP
    assert config["supported_range"]["chunk_size"]["default"] == settings.processing.CHUNK_SIZE
    assert config["supported_range"]["chunk_overlap"]["max"] == 500
    assert set(config["recommendation"].keys()) == {"small_documents", "medium_documents", "large_documents"}


# ---------------------------------------------------------------------------
# analyze_document_with_llm：LLM 结果兜底分支（失败哨兵 → None 保留规则分析）
# ---------------------------------------------------------------------------

class _FakeVectorStoreManager:
    """替换模块内 VectorStoreManager 符号，避免真实连接 Milvus。"""

    @classmethod
    async def get_instance(cls):
        return object()


class _FakeRAGChain:
    """替换模块内 RAGChain 符号：get_instance 直接返回自身实例。"""

    result = {}

    @classmethod
    async def get_instance(cls, vector_store):
        return cls()

    async def analyze_document(self, content):
        assert content == "文档内容"
        return type(self).result


def test_analyze_document_with_llm_failure_sentinel_returns_none(monkeypatch):
    _FakeRAGChain.result = {"summary": "评估失败", "overall_score": 0}
    monkeypatch.setattr("src.api.document.VectorStoreManager", _FakeVectorStoreManager)
    monkeypatch.setattr("src.api.document.RAGChain", _FakeRAGChain)

    import asyncio

    assert asyncio.run(analyze_document_with_llm("文档内容")) is None


def test_analyze_document_with_llm_normal_result_passthrough(monkeypatch):
    _FakeRAGChain.result = {"summary": "正常摘要", "overall_score": 92, "grade": "优秀"}
    monkeypatch.setattr("src.api.document.VectorStoreManager", _FakeVectorStoreManager)
    monkeypatch.setattr("src.api.document.RAGChain", _FakeRAGChain)

    import asyncio

    result = asyncio.run(analyze_document_with_llm("文档内容"))
    assert result == {"summary": "正常摘要", "overall_score": 92, "grade": "优秀"}


# ---------------------------------------------------------------------------
# MessageRequest：deep_thinking 校验器
# ---------------------------------------------------------------------------

def test_message_request_deep_thinking_default_off():
    assert MessageRequest(question="什么是RAG").deep_thinking == "off"
    assert MessageRequest(question="什么是RAG", deep_thinking=None).deep_thinking == "off"


def test_message_request_deep_thinking_accepts_on_off():
    assert MessageRequest(question="q", deep_thinking="on").deep_thinking == "on"
    assert MessageRequest(question="q", deep_thinking="off").deep_thinking == "off"


def test_message_request_deep_thinking_rejects_invalid():
    with pytest.raises(ValidationError) as exc_info:
        MessageRequest(question="q", deep_thinking="always")
    assert "deep_thinking" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _parse_session_id：补充空字符串/非法类型边界（安全测试只覆盖了部分场景）
# ---------------------------------------------------------------------------

def test_parse_session_id_invalid_uuid_raises_400():
    with pytest.raises(HTTPException) as exc_info:
        _parse_session_id("not-a-uuid")
    assert exc_info.value.status_code == 400
