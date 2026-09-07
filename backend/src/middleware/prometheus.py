"""
Prometheus 指标暴露模块

本模块负责：
1. 定义各类监控指标（请求计数、响应时间、错误率等）
2. 提供指标记录方法
3. 生成 Prometheus 格式的指标输出
"""

from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    generate_latest,
    CollectorRegistry,
    CONTENT_TYPE_LATEST
)
from fastapi import Response

# 创建指标注册表
registry = CollectorRegistry()

# ==================== HTTP 请求指标 ====================

REQUEST_COUNT = Counter(
    "rag_request_total",
    "Total number of requests",
    ["method", "endpoint", "status_code"],
    registry=registry
)

REQUEST_DURATION = Histogram(
    "rag_request_duration_seconds",
    "Request duration in seconds",
    ["method", "endpoint"],
    registry=registry,
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)

ERROR_COUNT = Counter(
    "rag_request_errors_total",
    "Total number of errors",
    ["method", "endpoint", "error_type"],
    registry=registry
)

ACTIVE_REQUESTS = Gauge(
    "rag_active_requests",
    "Number of active requests",
    registry=registry
)

# ==================== LLM 调用指标 ====================

LLM_CALLS = Counter(
    "rag_llm_calls_total",
    "Total number of LLM calls",
    ["model_name"],
    registry=registry
)

LLM_CALL_DURATION = Histogram(
    "rag_llm_call_duration_seconds",
    "LLM call duration in seconds",
    ["model_name"],
    registry=registry
)

LLM_CALL_ERRORS = Counter(
    "rag_llm_call_errors_total",
    "Total number of failed LLM calls",
    ["model_name"],
    registry=registry
)

# ==================== 向量检索指标 ====================

VECTOR_SEARCH_TIME = Histogram(
    "rag_vector_search_duration_seconds",
    "Vector search duration in seconds",
    registry=registry
)

VECTOR_SEARCH_COUNT = Counter(
    "rag_vector_search_total",
    "Total number of vector searches",
    registry=registry
)

# ==================== 文档处理指标 ====================

DOCUMENTS_PROCESSED = Counter(
    "rag_documents_processed_total",
    "Total number of documents processed",
    ["file_type"],
    registry=registry
)

DOCUMENT_PROCESS_TIME = Histogram(
    "rag_document_process_duration_seconds",
    "Document processing duration in seconds",
    ["file_type"],
    registry=registry
)

# ==================== 知识库问答指标 ====================

KB_QUERIES = Counter(
    "rag_kb_queries_total",
    "Total number of knowledge base queries",
    ["answer_type"],
    registry=registry
)

# ==================== 策略决策指标 ====================

STRATEGY_DECISIONS = Counter(
    "rag_strategy_decisions_total",
    "Total number of strategy decisions",
    ["strategy_name", "result"],
    registry=registry
)

# ==================== 语义缓存指标 ====================

SEMANTIC_CACHE_HITS = Counter(
    "semantic_cache_hits_total",
    "Total number of semantic cache hits",
    ["match_type"],
    registry=registry
)

SEMANTIC_CACHE_MISSES = Counter(
    "semantic_cache_misses_total",
    "Total number of semantic cache lookups that missed",
    registry=registry
)

SEMANTIC_CACHE_STORES = Counter(
    "semantic_cache_stores_total",
    "Total number of semantic cache stores",
    ["result"],
    registry=registry
)

SEMANTIC_CACHE_LOOKUP_LATENCY = Histogram(
    "semantic_cache_lookup_seconds",
    "Semantic cache lookup latency in seconds (including embedding)",
    registry=registry
)


def get_prometheus_metrics():
    """
    生成 Prometheus 格式的指标数据
    
    Returns:
        Response: Prometheus 格式的响应
    """
    data = generate_latest(registry)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


def record_request(method, endpoint, status_code, duration):
    """
    记录请求指标
    
    Args:
        method: HTTP 方法
        endpoint: 端点路径
        status_code: 状态码
        duration: 耗时（秒）
    """
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status_code=str(status_code)).inc()
    REQUEST_DURATION.labels(method=method, endpoint=endpoint).observe(duration)
    
    if status_code >= 400:
        ERROR_COUNT.labels(method=method, endpoint=endpoint, error_type="http_error").inc()


def record_llm_call(model_name, duration):
    """
    记录 LLM 调用

    Args:
        model_name: 模型名称
        duration: 耗时（秒）
    """
    LLM_CALLS.labels(model_name=model_name).inc()
    LLM_CALL_DURATION.labels(model_name=model_name).observe(duration)


def record_llm_call_error(model_name):
    """
    记录 LLM 调用失败

    Args:
        model_name: 模型名称
    """
    LLM_CALL_ERRORS.labels(model_name=model_name).inc()


def record_vector_search(duration):
    """
    记录向量检索
    
    Args:
        duration: 耗时（秒）
    """
    VECTOR_SEARCH_COUNT.inc()
    VECTOR_SEARCH_TIME.observe(duration)


def record_document_process(file_type, duration):
    """
    记录文档处理
    
    Args:
        file_type: 文件类型
        duration: 耗时（秒）
    """
    DOCUMENTS_PROCESSED.labels(file_type=file_type).inc()
    DOCUMENT_PROCESS_TIME.labels(file_type=file_type).observe(duration)


def record_kb_query(answer_type):
    """
    记录知识库查询
    
    Args:
        answer_type: 回答类型（knowledge_base 或 llm_direct）
    """
    KB_QUERIES.labels(answer_type=answer_type).inc()


def record_strategy_decision(strategy_name, result):
    """
    记录策略决策

    Args:
        strategy_name: 策略名称
        result: 决策结果（use_kb 或 direct）
    """
    STRATEGY_DECISIONS.labels(strategy_name=strategy_name, result=result).inc()


def record_semantic_cache_hit(match_type, duration):
    """
    记录语义缓存命中

    Args:
        match_type: 命中类型（exact 或 semantic）
        duration: 查找耗时（秒）
    """
    SEMANTIC_CACHE_HITS.labels(match_type=match_type).inc()
    SEMANTIC_CACHE_LOOKUP_LATENCY.observe(duration)


def record_semantic_cache_miss(duration):
    """
    记录语义缓存未命中

    Args:
        duration: 查找耗时（秒）
    """
    SEMANTIC_CACHE_MISSES.inc()
    SEMANTIC_CACHE_LOOKUP_LATENCY.observe(duration)


def record_semantic_cache_store(result):
    """
    记录语义缓存写入

    Args:
        result: 写入结果（success 或 failed）
    """
    SEMANTIC_CACHE_STORES.labels(result=result).inc()