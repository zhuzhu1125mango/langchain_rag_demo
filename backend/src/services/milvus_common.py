"""Milvus 服务公共依赖：BM25 可选后端、A/B rerank 参数（CODE-1 拆分）。"""

import logging

from src.config import settings

logger = logging.getLogger("milvus_service")

# 可选的 BM25 稀疏嵌入模型，失败时优雅降级为纯向量检索
try:
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

    _BM25_AVAILABLE = True
except Exception as _bm25_import_err:  # pragma: no cover - 依赖未安装时降级
    BM25EmbeddingFunction = None
    build_default_analyzer = None
    _BM25_AVAILABLE = False
    logger.warning(
        f"pymilvus-model 未安装，知识库混合检索将降级为纯向量检索: {_bm25_import_err}"
    )


def _ab_rerank_top_k() -> int:
    """LEARN W-C：请求级 A/B 覆盖的 rerank 候选数；无覆盖时回落 settings 默认。"""
    try:
        from src.services.pipeline.state import get_ab_config

        override = (get_ab_config() or {}).get("kb_hybrid_rerank_top_k")
        if override is not None:
            return int(override)
    except Exception:
        pass
    return settings.processing.KB_HYBRID_RERANK_TOP_K
