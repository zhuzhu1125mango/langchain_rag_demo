"""语义缓存服务（P1-3）。

相似问题命中缓存答案：精确匹配快路径 + embedding 余弦相似度匹配。
仅纯知识库问答参与读写（调用方负责条件判断），Redis 不可用时 fail-open。

存储结构：Redis HASH，每 scope（user + kb 范围）一个 key，
field 为条目 uuid，value 为条目 JSON（含 1024 维问题 embedding）。
"""

import asyncio
import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Dict, List, Optional

from src.config import settings

# 导入 Prometheus 指标模块（缺依赖时静默降级，与 rag_chain 导入方式一致）
try:
    from src.middleware.prometheus import (
        record_semantic_cache_hit,
        record_semantic_cache_miss,
        record_semantic_cache_store,
    )
    _METRICS_AVAILABLE = True
except ImportError:
    _METRICS_AVAILABLE = False

logger = logging.getLogger(__name__)

# 缓存 key 前缀：rag:semcache:{user_id}:{kb_scope}
KEY_PREFIX = "rag:semcache"

# Redis 操作兜底超时（秒），与 CacheService 的操作超时保持一致量级
REDIS_OPERATION_TIMEOUT = 3.0
# question embedding 计算超时（秒）。LLM 与 embedding 模型在显存互换时
# Ollama 需重新加载模型，远慢于常规 Redis 操作，需独立更长超时
EMBEDDING_TIMEOUT_SECONDS = 10.0


@dataclass
class SemanticCacheHit:
    """语义缓存命中结果。"""

    question: str            # 缓存的原始问题（resolved_question）
    answer: str              # 完整答案文本
    source_texts: List[str] = dataclass_field(default_factory=list)
    source_metadata: List[Dict[str, Any]] = dataclass_field(default_factory=list)
    score: float = 0.0       # 相似度（精确匹配为 1.0）
    match_type: str = "semantic"  # exact | semantic


class SemanticCacheService:
    """语义缓存读写与失效。无状态，方法级线程安全（Redis 操作原子性由 Redis 保证）。"""

    def __init__(self):
        self._embeddings = None

    # ------------------------------------------------------------------
    # 依赖获取
    # ------------------------------------------------------------------
    async def _get_cache(self):
        from src.services.cache_service import CacheService
        return await CacheService.get_instance()

    def _get_embeddings(self):
        """复用 model_manager 共享 OllamaEmbeddings 单例（B2，与检索链路同源连接），失败返回 None。

        此前此处自建 OllamaEmbeddings 且未显式配置连接地址，容器部署时依赖
        OLLAMA_HOST 环境变量兜底；改走共享单例以与管线保持完全一致。
        """
        if self._embeddings is None:
            try:
                from src.services.model_manager import model_manager

                self._embeddings = model_manager.get_embeddings()
            except Exception as e:
                logger.warning(f"语义缓存 Embedding 模型初始化失败: {e}")
                self._embeddings = None
        return self._embeddings

    async def _embed_question(self, question: str) -> Optional[List[float]]:
        embeddings = self._get_embeddings()
        if embeddings is None:
            return None
        try:
            # 独立于 Redis 操作超时：4GB 显存下 LLM 与 embedding 模型互换加载可达数秒，
            # 复用 3s 的 REDIS_OPERATION_TIMEOUT 会导致 embedding 永远超时、缓存永不生效
            return await asyncio.wait_for(
                embeddings.aembed_query(question), timeout=EMBEDDING_TIMEOUT_SECONDS
            )
        except Exception as e:
            logger.warning(f"语义缓存问题 embedding 计算失败: {e}")
            return None

    # ------------------------------------------------------------------
    # key 与工具
    # ------------------------------------------------------------------
    @staticmethod
    def scope_key(user_id: str, kb_ids: Optional[List[str]]) -> str:
        """缓存范围 key：用户 + 知识库范围（未选库为 all）。"""
        if kb_ids:
            digest = hashlib.md5(",".join(sorted(kb_ids)).encode("utf-8")).hexdigest()[:12]
            scope = digest
        else:
            scope = "all"
        return f"{KEY_PREFIX}:{user_id}:{scope}"

    @staticmethod
    def normalize_question(question: str) -> str:
        return (question or "").strip().lower()

    @staticmethod
    def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def _execute(self, cache, coro):
        """带超时执行 Redis 命令；失败抛异常由上层 fail-open 捕获。"""
        return await asyncio.wait_for(coro, timeout=REDIS_OPERATION_TIMEOUT)

    # ------------------------------------------------------------------
    # 查找
    # ------------------------------------------------------------------
    async def lookup(
        self,
        user_id: str,
        kb_ids: Optional[List[str]],
        question: str,
    ) -> tuple[Optional[SemanticCacheHit], Optional[List[float]]]:
        """查找语义缓存。

        Returns:
            (hit, query_embedding)：hit 为 None 表示未命中；
            query_embedding 为本次问题 embedding（未命中时供 store 复用，避免重复计算）。
            Redis/Embedding 异常一律返回 (None, None)。
        """
        if not settings.semantic_cache.SEMANTIC_CACHE_ENABLED:
            return None, None

        start = time.time()
        try:
            cache = await self._get_cache()
            if not cache.available:
                return None, None

            key = self.scope_key(user_id, kb_ids)
            raw = await self._execute(cache, cache.client.hgetall(key))
            if not raw:
                self._record_miss(start)
                return None, None

            entries: List[tuple[str, Dict[str, Any]]] = []
            now = time.time()
            ttl_seconds = settings.semantic_cache.SEMANTIC_CACHE_TTL_HOURS * 3600
            for field_id, value in raw.items():
                try:
                    entry = json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    continue
                if now - entry.get("created_at", 0) > ttl_seconds:
                    continue  # 惰性过期：读取时跳过，写入时清理
                entries.append((field_id, entry))

            if not entries:
                self._record_miss(start)
                return None, None

            # 精确匹配快路径
            q_norm = self.normalize_question(question)
            for field_id, entry in entries:
                if entry.get("question_norm") == q_norm:
                    self._record_hit(start, "exact")
                    return self._to_hit(entry, 1.0, "exact"), None

            # 语义匹配：条目过多时仅比较最近的 N 条
            entries.sort(key=lambda item: item[1].get("created_at", 0), reverse=True)
            entries = entries[: settings.semantic_cache.SEMANTIC_CACHE_MAX_ENTRIES]

            query_embedding = await self._embed_question(question)
            if not query_embedding:
                self._record_miss(start)
                return None, None

            threshold = settings.semantic_cache.SEMANTIC_CACHE_SIMILARITY_THRESHOLD
            best: Optional[tuple[str, Dict[str, Any], float]] = None
            for field_id, entry in entries:
                score = self.cosine_similarity(query_embedding, entry.get("embedding") or [])
                if score >= threshold and (best is None or score > best[2]):
                    best = (field_id, entry, score)

            if best is None:
                self._record_miss(start)
                return None, query_embedding

            self._record_hit(start, "semantic")
            return self._to_hit(best[1], best[2], "semantic"), query_embedding
        except Exception as exc:
            logger.warning(f"语义缓存查找失败（fail-open）: {exc}")
            return None, None

    def _to_hit(self, entry: Dict[str, Any], score: float, match_type: str) -> SemanticCacheHit:
        return SemanticCacheHit(
            question=entry.get("question", ""),
            answer=entry.get("answer", ""),
            source_texts=entry.get("source_texts") or [],
            source_metadata=entry.get("source_metadata") or [],
            score=score,
            match_type=match_type,
        )

    def _record_hit(self, start: float, match_type: str):
        if _METRICS_AVAILABLE:
            record_semantic_cache_hit(match_type, time.time() - start)

    def _record_miss(self, start: float):
        if _METRICS_AVAILABLE:
            record_semantic_cache_miss(time.time() - start)

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    async def store(
        self,
        user_id: str,
        kb_ids: Optional[List[str]],
        question: str,
        answer: str,
        source_texts: List[str],
        source_metadata: List[Dict[str, Any]],
        embedding: Optional[List[float]] = None,
    ) -> bool:
        """写入缓存条目。embedding 缺失时现场计算；任何失败静默返回 False。

        Args:
            embedding: 查找阶段已算好的问题 embedding，传入可避免重复计算。
        """
        if not settings.semantic_cache.SEMANTIC_CACHE_ENABLED:
            return False
        try:
            cache = await self._get_cache()
            if not cache.available:
                return False

            if not embedding:
                embedding = await self._embed_question(question)
            if not embedding:
                return False

            key = self.scope_key(user_id, kb_ids)
            entry = {
                "question": question,
                "question_norm": self.normalize_question(question),
                "embedding": embedding,
                "answer": answer,
                "source_texts": source_texts or [],
                "source_metadata": source_metadata or [],
                # 用于失效匹配：实际引用的知识库范围；未选库为 all（全域查询）
                "kb_ids": sorted(kb_ids) if kb_ids else ["all"],
                "answer_type": "knowledge_base",
                "created_at": time.time(),
            }
            field_id = uuid.uuid4().hex
            await self._execute(cache, cache.client.hset(key, field_id, json.dumps(entry, ensure_ascii=False)))
            await self._execute(cache, cache.client.expire(key, settings.semantic_cache.SEMANTIC_CACHE_TTL_HOURS * 3600 + 3600))
            await self._trim(cache, key)
            if _METRICS_AVAILABLE:
                record_semantic_cache_store("success")
            return True
        except Exception as exc:
            logger.warning(f"语义缓存写入失败（fail-open）: {exc}")
            if _METRICS_AVAILABLE:
                record_semantic_cache_store("failed")
            return False

    async def _trim(self, cache, key: str) -> None:
        """写时惰性清理：删除过期条目；容量超限时删除最旧条目。"""
        raw = await self._execute(cache, cache.client.hgetall(key))
        if not raw:
            return

        now = time.time()
        ttl_seconds = settings.semantic_cache.SEMANTIC_CACHE_TTL_HOURS * 3600
        parsed: List[tuple[str, float]] = []
        expired: List[str] = []
        for field_id, value in raw.items():
            try:
                entry = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                expired.append(field_id)
                continue
            created_at = entry.get("created_at", 0)
            if now - created_at > ttl_seconds:
                expired.append(field_id)
            else:
                parsed.append((field_id, created_at))

        max_entries = settings.semantic_cache.SEMANTIC_CACHE_MAX_ENTRIES
        if len(parsed) > max_entries:
            parsed.sort(key=lambda item: item[1])
            expired.extend(field_id for field_id, _ in parsed[: len(parsed) - max_entries])

        if expired:
            await self._execute(cache, cache.client.hdel(key, *expired))

    # ------------------------------------------------------------------
    # 失效
    # ------------------------------------------------------------------
    async def invalidate_kb(self, kb_id: str) -> int:
        """知识库内容变更后失效相关条目。

        匹配规则：条目 kb_ids 包含该 kb_id，或为 all（全域查询可能引用该库）。
        返回删除条目数；任何失败静默返回 0。
        """
        try:
            cache = await self._get_cache()
            if not cache.available:
                return 0

            deleted = 0
            # scan_iter 返回异步迭代器，不能用 wait_for 包裹（非协程）
            async for key in cache.client.scan_iter(match=f"{KEY_PREFIX}:*"):
                raw = await self._execute(cache, cache.client.hgetall(key))
                if not raw:
                    continue
                to_delete: List[str] = []
                for field_id, value in raw.items():
                    try:
                        entry = json.loads(value)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    entry_kb_ids = entry.get("kb_ids") or []
                    if kb_id in entry_kb_ids or "all" in entry_kb_ids:
                        to_delete.append(field_id)
                if to_delete:
                    await self._execute(cache, cache.client.hdel(key, *to_delete))
                    deleted += len(to_delete)
            if deleted:
                logger.info(f"语义缓存失效 [kb_id={kb_id}]: 删除 {deleted} 条")
            return deleted
        except Exception as exc:
            logger.warning(f"语义缓存失效失败 [kb_id={kb_id}]（fail-open）: {exc}")
            return 0


# 后台失效任务引用集，防止 fire-and-forget 任务被垃圾回收
_invalidation_tasks: set = set()


def schedule_invalidation(kb_id: str) -> None:
    """知识库内容变更后失效相关条目（fire-and-forget 后台任务，失败仅告警）。

    供文档上传/删除/重建、知识库删除等 API 在内容变更后调用，不阻塞响应。
    """
    async def _invalidate():
        try:
            await SemanticCacheService().invalidate_kb(kb_id)
        except Exception as exc:
            logger.warning(f"语义缓存失效失败 [kb_id={kb_id}]: {exc}")

    task = asyncio.create_task(_invalidate())
    _invalidation_tasks.add(task)
    task.add_done_callback(_invalidation_tasks.discard)
