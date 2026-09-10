"""KB 覆盖先验（P3 LLM-Wiki 编译层 Phase 3，§11.1）。

对每个 KB 取其 Wiki 索引页（page_type='index'，全 KB 目录，天然是「KB 覆盖面」
摘要），embedding 后与问题算余弦 → 0~1 先验分，供 kb_recommender 融合：

    final = chunk_avg × (1 − w) + index_sim × w

无 index 页的 KB 不做融合（保持纯 chunk 分，编译关闭的 KB 行为与现状完全一致）。
embedding 进程内 TTL 缓存，key 含 page_id + revision（索引页仅编译时变化，
revision 变更自动失效）；推荐为低频接口，无需 Redis。
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger("wiki_route_prior")

# 缓存条目存活时间（秒）；revision 变更已兜底失效，TTL 仅防长期驻留
_CACHE_TTL_SECONDS = 600


def _cosine(vec_a: List[float], vec_b: List[float]) -> float:
    """余弦相似度；非法输入返回 0（对齐 wiki_compiler 语义）。"""
    try:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = sum(a * a for a in vec_a) ** 0.5
        nb = sum(b * b for b in vec_b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
    except TypeError:
        return 0.0


class WikiRoutePrior:
    """索引页覆盖先验计算器（进程内单例）。"""

    def __init__(self):
        # key: (page_id, revision) -> (expires_at, embedding)
        self._cache: Dict[Tuple[str, int], Tuple[float, List[float]]] = {}

    async def get_priors(self, question: str, kb_ids: List[str]) -> Dict[str, float]:
        """计算各 KB 的覆盖先验分；异常仅记日志并返回空（回退纯 chunk 分）。"""
        if not kb_ids:
            return {}
        try:
            from src.database import async_session_maker

            async with async_session_maker() as db:
                return await self.compute_priors(db, question, kb_ids)
        except Exception as e:
            logger.warning(f"Wiki 覆盖先验计算失败，回退纯 chunk 分: {e}")
            return {}

    async def compute_priors(self, db, question: str, kb_ids: List[str]) -> Dict[str, float]:
        """给定会话计算先验分（独立方法便于单测注入假 db）。"""
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage

        rows = (
            await db.execute(
                select(WikiPage).filter(
                    WikiPage.kb_id.in_([str(k) for k in kb_ids]),
                    WikiPage.page_type == "index",
                    WikiPage.status == "active",
                )
            )
        ).scalars().all()
        if not rows:
            return {}

        # 载入索引页正文（MinIO 读取失败按空内容跳过）
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        pages = []
        for row in rows:
            content = await minio.download_text_async(row.content_path)
            if content and content.strip():
                pages.append((row, content))

        if not pages:
            return {}

        vectors = await self._embed_with_cache(
            [question] + [content for _, content in pages],
            page_keys=[(str(row.id), row.revision or 1) for row, _ in pages],
        )
        question_vec = vectors[0]

        priors: Dict[str, float] = {}
        for (row, _content), vec in zip(pages, vectors[1:]):
            sim = max(_cosine(question_vec, vec), 0.0)  # 余弦可为负，先验限 0~1
            # 同 KB 多 index 页理论不存在，防御性取最高分
            priors[str(row.kb_id)] = max(priors.get(str(row.kb_id), 0.0), sim)
        return priors

    async def _embed_with_cache(self, texts: List[str], page_keys: Optional[List[Tuple[str, int]]] = None) -> List[List[float]]:
        """批量 embedding：texts[0]（question）直接算，texts[1:] 按 (page_id, revision) TTL 缓存。

        page_keys 与 texts[1:] 一一对应；缺省时退化为全部直接算。
        """
        from src.services.model_manager import model_manager

        if not page_keys:
            return await model_manager.get_embeddings().aembed_documents(texts)

        now = time.monotonic()
        results: List[Optional[List[float]]] = [None] * len(texts)
        # question（index 0）不入缓存，始终待算
        pending_idx = [0]
        for i, key in enumerate(page_keys, start=1):
            cached = self._cache.get(key)
            if cached and cached[0] > now:
                results[i] = cached[1]
            else:
                pending_idx.append(i)

        # 过期条目清理（惰性）
        if self._cache:
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}

        if pending_idx:
            vectors = await model_manager.get_embeddings().aembed_documents(
                [texts[i] for i in pending_idx]
            )
            for i, vec in zip(pending_idx, vectors):
                results[i] = vec
                if i > 0:
                    self._cache[page_keys[i - 1]] = (now + _CACHE_TTL_SECONDS, vec)
        return results


wiki_route_prior = WikiRoutePrior()
