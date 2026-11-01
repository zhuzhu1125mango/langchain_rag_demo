"""三方一致性巡检（只读，不修改任何数据）。

回答一个运维问题：**「元数据说删/说没有的东西，实际还在吗？」**
巡检三类存储与 DB（唯一事实来源）的差异：

1. Milvus ↔ DB：向量切片是否指向已删除的文档/页面、文档下架后是否还有向量、
   文档已发布却检索不到向量、切片的 kb_id 是否为空/指向已删知识库；
2. MinIO ↔ DB：对象存储里是否躺着无 DB 引用的孤儿文件、DB 引用的文件是否已丢失；
3. Redis ↔ DB：语义缓存条目是否引用了已删除/未发布的文档、所属知识库是否已删、
   是否已过 TTL 仍未被清理。

设计约束：
- **只报告，不自动删**。孤儿向量/对象删除是不可逆操作，先让人看到再决定；
- 每一侧独立采集，单侧失败不影响其它侧（报告里标注 error，整体退出码 2）；
- 结果结构化（``kind`` 稳定），便于 nightly 任务做阈值告警与趋势对比。

判定口径与业务一致：向量只对 ``published`` 文档有效（下架即删向量），
Agent 记忆切片（``source_kind="memory"`` / ``kb_id="memory"``）不属于任何文档，跳过。
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import select

from src.config import settings
from src.database import async_session_maker
from src.models import Document, KnowledgeBase
from src.models.wiki_page import WikiPage
from src.services.semantic_cache_service import (
    INDEX_PREFIX,
    KEY_PREFIX,
    _decode_entry,
)

logger = logging.getLogger("consistency_audit")

MEMORY_KB_ID = "memory"
MINIO_PREFIX = "minio://"

# 问题类型 → 人类可读说明（CLI 直接打印，nightly 用 kind 做告警规则）
KIND_LABELS: Dict[str, str] = {
    "orphan_vector_doc": "向量指向已删除的文档/页面（元数据没了、向量还在）",
    "vector_for_unpublished_doc": "文档未发布却仍有向量（下架未删干净，仍可能被检索命中）",
    "vector_kb_mismatch": "向量 kb_id 与文档当前所属知识库不一致（移库后未重建索引）",
    "vector_kb_blank": "向量 kb_id 为空（按知识库删除会漏删，历史数据未回填）",
    "vector_kb_missing": "向量 kb_id 指向不存在的知识库",
    "missing_vector_doc": "文档已发布且有分片，但向量库中没有它的切片（检索不到）",
    "orphan_object": "对象存储中存在但 DB 无引用（无主文件）",
    "missing_object": "DB 引用但对象存储中不存在（下载会 404）",
    "stale_cache_entry": "缓存条目引用了已删除/未发布的文档",
    "stale_cache_kb": "缓存条目所属知识库已删除",
    "expired_cache_entry": "缓存条目已过 TTL 但仍未被清理",
    "dangling_cache_index": "缓存 scope 索引指向不存在的 key",
}


@dataclass
class Finding:
    """一条差异记录。``subject`` 是可直接拿去排查的主体 id。"""

    kind: str
    subject: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SectionResult:
    """单侧巡检结果。"""

    name: str
    scanned: int = 0
    total: Optional[int] = None
    truncated: bool = False
    findings: List[Finding] = field(default_factory=list)
    error: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.findings if f.kind == kind)

    def summary(self, sample: int = 5) -> Dict[str, Any]:
        kinds: Dict[str, Dict[str, Any]] = {}
        for kind in KIND_LABELS:
            hits = [f for f in self.findings if f.kind == kind]
            if not hits:
                continue
            kinds[kind] = {
                "label": KIND_LABELS[kind],
                "count": len(hits),
                "samples": [
                    {"subject": f.subject, **f.detail} for f in hits[:sample]
                ],
            }
        return {
            "scanned": self.scanned,
            "total": self.total,
            "truncated": self.truncated,
            "error": self.error,
            "issues": sum(v["count"] for v in kinds.values()),
            "kinds": kinds,
            "extra": self.extra,
        }


# ---------------------------------------------------------------------------
# DB 快照：唯一事实来源
# ---------------------------------------------------------------------------
@dataclass
class DbSnapshot:
    doc_status: Dict[str, str] = field(default_factory=dict)
    doc_kb: Dict[str, str] = field(default_factory=dict)
    doc_chunks: Dict[str, int] = field(default_factory=dict)
    wiki_ids: Set[str] = field(default_factory=set)
    kb_ids: Set[str] = field(default_factory=set)
    # 对象键 → 归属者（文档 / wiki 页），用于 MinIO 双向比对
    doc_object_keys: Dict[str, str] = field(default_factory=dict)
    wiki_object_keys: Dict[str, str] = field(default_factory=dict)

    @property
    def expected_object_keys(self) -> Set[str]:
        return set(self.doc_object_keys) | set(self.wiki_object_keys)


def to_object_key(file_path: str) -> Optional[str]:
    """``minio://bucket/documents/x/source.pdf`` → ``documents/x/source.pdf``。

    非 MinIO 路径（本地文件、空值、其他协议）返回 None。
    """
    if not file_path or not file_path.startswith(MINIO_PREFIX):
        return None
    rest = file_path[len(MINIO_PREFIX):]
    bucket, _, key = rest.partition("/")
    if bucket != settings.minio.MINIO_BUCKET_NAME or not key:
        # 桶名不一致说明记录指向别的桶：本地巡检无法判断，按「非本桶」跳过
        return None
    return key


async def load_db_snapshot(chunk: int = 1000) -> DbSnapshot:
    """一次性拉取判定所需的最小字段（id/status/kb/chunks/路径）。"""
    snapshot = DbSnapshot()
    async with async_session_maker() as db:
        result = await db.stream(
            select(
                Document.id, Document.status, Document.kb_id,
                Document.chunks_count, Document.file_path,
            )
        )
        async for partition in result.partitions(chunk):
            for doc_id, status, kb_id, chunks, file_path in partition:
                sid = str(doc_id)
                snapshot.doc_status[sid] = status or ""
                snapshot.doc_kb[sid] = str(kb_id) if kb_id else ""
                snapshot.doc_chunks[sid] = int(chunks or 0)
                key = to_object_key(file_path or "")
                if key:
                    snapshot.doc_object_keys[key] = sid

        result = await db.stream(select(WikiPage.id, WikiPage.content_path))
        async for partition in result.partitions(chunk):
            for page_id, content_path in partition:
                snapshot.wiki_ids.add(str(page_id))
                if content_path:
                    snapshot.wiki_object_keys[str(content_path)] = str(page_id)

        result = await db.stream(select(KnowledgeBase.id))
        async for partition in result.partitions(chunk):
            for (kb_id,) in partition:
                snapshot.kb_ids.add(str(kb_id))

    return snapshot


# ---------------------------------------------------------------------------
# Milvus
# ---------------------------------------------------------------------------
@dataclass
class VectorRef:
    document_id: str
    kb_id: str = ""
    source_kind: str = ""
    chunks: int = 0


def _is_memory(ref: VectorRef) -> bool:
    # source_kind 字段是后加的，老集合可能没有：kb_id="memory" 是可靠的兜底判据
    return ref.source_kind == "memory" or ref.kb_id == MEMORY_KB_ID


def classify_vectors(refs: Iterable[VectorRef], snapshot: DbSnapshot) -> List[Finding]:
    """向量 → DB 的判定（纯函数，便于单测）。"""
    findings: List[Finding] = []
    for ref in refs:
        if _is_memory(ref):
            continue  # 会话记忆不属于任何文档，DB 里本就没有对应行

        doc_id = ref.document_id
        if not doc_id:
            findings.append(Finding("orphan_vector_doc", "<empty>", {"reason": "document_id 为空"}))
            continue

        if doc_id in snapshot.doc_status:
            status = snapshot.doc_status[doc_id]
            if status != "published":
                findings.append(
                    Finding("vector_for_unpublished_doc", doc_id,
                            {"chunks": ref.chunks, "status": status})
                )
            doc_kb = snapshot.doc_kb.get(doc_id, "")
            if ref.kb_id and doc_kb and ref.kb_id != doc_kb:
                findings.append(
                    Finding("vector_kb_mismatch", doc_id,
                            {"vector_kb": ref.kb_id, "doc_kb": doc_kb, "chunks": ref.chunks})
                )
        elif doc_id not in snapshot.wiki_ids:
            findings.append(
                Finding("orphan_vector_doc", doc_id,
                        {"chunks": ref.chunks, "kb_id": ref.kb_id or "-",
                         "source_kind": ref.source_kind or "-"})
            )

        if not ref.kb_id:
            findings.append(
                Finding("vector_kb_blank", doc_id, {"chunks": ref.chunks})
            )
        elif ref.kb_id not in snapshot.kb_ids and ref.kb_id != MEMORY_KB_ID:
            findings.append(
                Finding("vector_kb_missing", doc_id,
                        {"kb_id": ref.kb_id, "chunks": ref.chunks})
            )
    return findings


def classify_missing_vectors(refs: Dict[str, VectorRef], snapshot: DbSnapshot) -> List[Finding]:
    """DB → 向量：已发布、有分片，却检索不到。"""
    findings: List[Finding] = []
    for doc_id, status in snapshot.doc_status.items():
        if status != "published":
            continue
        if snapshot.doc_chunks.get(doc_id, 0) <= 0:
            continue
        if doc_id in refs:
            continue
        findings.append(
            Finding("missing_vector_doc", doc_id,
                    {"chunks_count": snapshot.doc_chunks[doc_id],
                     "kb_id": snapshot.doc_kb.get(doc_id) or "-"})
        )
    return findings


async def scan_vectors(
    batch_size: int = 500,
    max_offset: int = 16000,
    progress: Optional[Any] = None,
) -> Tuple[SectionResult, Dict[str, VectorRef]]:
    """分页扫描 Milvus 全部切片，只取 document_id/kb_id/source_kind。

    Milvus 的 query 深分页有窗口上限，超过 ``max_offset`` 就不再继续并在报告里
    标记 truncated —— 只报「扫到的部分」，比假装扫全了更安全。
    """
    from src.services.milvus_service import MilvusService

    section = SectionResult(name="milvus")
    refs: Dict[str, VectorRef] = {}

    service = await MilvusService.get_instance()
    total = int(await service.count())
    section.total = total

    fields = ["document_id", "kb_id"]
    if getattr(service, "_has_source_kind", False):
        fields.append("source_kind")

    offset = 0
    while offset < total:
        if offset >= max_offset:
            section.truncated = True
            logger.warning(
                "Milvus 扫描达到 offset 上限 %d/%d，结果不完整；可调大 --max-milvus-offset "
                "或提高 Milvus 的 query 窗口", max_offset, total
            )
            break
        try:
            rows = await service.client.query(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                filter="",
                output_fields=fields,
                limit=batch_size,
                offset=offset,
            )
        except Exception as exc:
            # 深分页越界 / 连接故障：保留已扫到的部分，明确标注不完整
            section.error = f"{type(exc).__name__}: {exc}"
            section.truncated = True
            logger.warning("Milvus 分页扫描在第 %d 条处中断: %s", offset, exc)
            break
        if not rows:
            break

        for row in rows:
            doc_id = str(row.get("document_id") or "")
            ref = refs.get(doc_id)
            if ref is None:
                ref = VectorRef(
                    document_id=doc_id,
                    kb_id=str(row.get("kb_id") or ""),
                    source_kind=str(row.get("source_kind") or ""),
                )
                refs[doc_id] = ref
            ref.chunks += 1

        offset += len(rows)
        section.scanned = offset
        if progress:
            progress("milvus", offset, total)

    section.extra["documents"] = len(refs)
    return section, refs


# ---------------------------------------------------------------------------
# MinIO
# ---------------------------------------------------------------------------
def classify_objects(object_keys: Iterable[str], expected_keys: Set[str]) -> List[Finding]:
    """对象 ↔ DB 双向比对（纯函数）。"""
    keys = set(object_keys)
    findings: List[Finding] = []
    for key in sorted(keys - expected_keys):
        findings.append(Finding("orphan_object", key, {}))
    for key in sorted(expected_keys - keys):
        findings.append(Finding("missing_object", key, {}))
    return findings


async def scan_objects(max_objects: int = 200000, prefix: str = "") -> SectionResult:
    """流式列出桶内对象（list_objects 内部按 1000 条分页）。"""
    import asyncio

    from src.services.minio_service import MinioService

    section = SectionResult(name="minio")
    service = await MinioService.get_instance()

    def _collect() -> List[str]:
        return [key for key, _size in service.iter_objects(prefix=prefix, max_objects=max_objects)]

    keys = await asyncio.to_thread(_collect)
    section.scanned = len(keys)
    section.total = len(keys)
    section.extra["truncated_by_limit"] = bool(max_objects and len(keys) >= max_objects)
    section.extra["_keys"] = keys  # 由编排层消费后移除，不进入报告
    return section


# ---------------------------------------------------------------------------
# Redis（语义缓存）
# ---------------------------------------------------------------------------
@dataclass
class CacheEntryView:
    key: str
    field: str
    question: str = ""
    created_at: float = 0.0
    kb_ids: List[str] = field(default_factory=list)
    source_docs: List[Tuple[str, str]] = field(default_factory=list)


def classify_cache(
    entries: Iterable[CacheEntryView],
    snapshot: DbSnapshot,
    ttl_seconds: int,
    now: Optional[float] = None,
) -> List[Finding]:
    """缓存条目 → DB 判定（纯函数）。"""
    now = time.time() if now is None else now
    findings: List[Finding] = []
    for entry in entries:
        if ttl_seconds and entry.created_at and now - entry.created_at > ttl_seconds:
            findings.append(
                Finding("expired_cache_entry", f"{entry.key}#{entry.field}",
                        {"question": entry.question[:80],
                         "age_hours": round((now - entry.created_at) / 3600, 1)})
            )

        missing: List[str] = []
        for doc_id, kind in entry.source_docs:
            if not doc_id or kind == "memory":
                continue
            if kind == "wiki":
                if doc_id not in snapshot.wiki_ids:
                    missing.append(doc_id)
                continue
            status = snapshot.doc_status.get(doc_id)
            if status is None or status != "published":
                missing.append(doc_id)

        if missing:
            findings.append(
                Finding("stale_cache_entry", f"{entry.key}#{entry.field}",
                        {"question": entry.question[:80],
                         "missing_document_ids": ",".join(sorted(set(missing))[:10]),
                         "missing_count": len(set(missing))})
            )

        unknown_kb = [
            kb for kb in entry.kb_ids
            if kb and kb != "all" and kb not in snapshot.kb_ids
        ]
        if unknown_kb:
            findings.append(
                Finding("stale_cache_kb", f"{entry.key}#{entry.field}",
                        {"question": entry.question[:80],
                         "missing_kb_ids": ",".join(sorted(unknown_kb)[:5])})
            )
    return findings


def _entry_view(key: str, field: str, entry: Dict[str, Any]) -> CacheEntryView:
    metas = entry.get("source_metadata") or []
    docs: List[Tuple[str, str]] = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        if meta.get("source_type") not in (None, "kb"):
            continue
        doc_id = str(meta.get("document_id") or "").strip()
        if doc_id:
            docs.append((doc_id, str(meta.get("source_kind") or "raw")))
    return CacheEntryView(
        key=key,
        field=field,
        question=str(entry.get("question") or ""),
        created_at=float(entry.get("created_at") or 0),
        kb_ids=[str(k) for k in (entry.get("kb_ids") or [])],
        source_docs=docs,
    )


async def scan_cache(max_keys: int = 0) -> Tuple[SectionResult, List[CacheEntryView]]:
    """扫描语义缓存 scope（跳过索引 key），解析出条目视图。"""
    from src.services.cache_service import CacheService

    section = SectionResult(name="redis")
    cache = await CacheService.get_instance()
    if not cache.available:
        raise RuntimeError("Redis 不可用")

    entries: List[CacheEntryView] = []
    scope_keys: List[str] = []
    index_keys: List[str] = []
    broken = 0

    async for key in cache.client.scan_iter(match=f"{KEY_PREFIX}:*", count=200):
        if max_keys and len(scope_keys) >= max_keys:
            section.truncated = True
            break
        if key.startswith(INDEX_PREFIX):
            index_keys.append(key)
            continue
        scope_keys.append(key)
        raw = await cache.client.hgetall(key)
        section.scanned += 1
        for field_id, value in (raw or {}).items():
            entry = _decode_entry(value)
            if entry is None:
                broken += 1
                continue
            entries.append(_entry_view(key, field_id, entry))

    # 索引指向已不存在的 scope：失效时白跑一次 HGETALL，属于可清理的残留
    dangling: List[Finding] = []
    for index_key in index_keys:
        members = await cache.client.smembers(index_key) or set()
        for member in members:
            if await cache.client.exists(member):
                continue
            dangling.append(Finding("dangling_cache_index", member, {"index": index_key}))

    section.extra.update(
        {
            "scopes": len(scope_keys),
            "index_keys": len(index_keys),
            "entries": len(entries),
            "undecodable": broken,
        }
    )
    section.findings.extend(dangling)
    return section, entries


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def decide_exit_code(report: Dict[str, Any]) -> int:
    """0=干净，1=发现差异，2=巡检本身出错（连不上/查询失败）。"""
    sections = report.get("sections") or {}
    if any((s or {}).get("error") for s in sections.values()):
        return 2
    return 1 if report.get("issue_count") else 0


def _total_issues(sections: Dict[str, SectionResult]) -> int:
    return sum(len(s.findings) for s in sections.values())


async def run_audit(
    *,
    skip_milvus: bool = False,
    skip_minio: bool = False,
    skip_redis: bool = False,
    milvus_batch: int = 500,
    max_milvus_offset: int = 16000,
    max_objects: int = 200000,
    max_cache_keys: int = 0,
    sample: int = 5,
) -> Dict[str, Any]:
    """执行三方巡检并返回可直接 JSON 序列化的报告。"""
    started = time.time()
    sections: Dict[str, SectionResult] = {}
    snapshot: Optional[DbSnapshot] = None

    try:
        snapshot = await load_db_snapshot()
    except Exception as exc:
        logger.error("读取 DB 快照失败: %s", exc)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.time() - started, 2),
            "fatal": f"读取 DB 快照失败: {type(exc).__name__}: {exc}",
            "sections": {},
            "issue_count": 0,
            "exit_code": 2,
        }

    if not skip_milvus:
        try:
            section, refs = await scan_vectors(milvus_batch, max_milvus_offset)
            section.findings.extend(classify_vectors(refs.values(), snapshot))
            section.findings.extend(classify_missing_vectors(refs, snapshot))
            sections["milvus"] = section
        except Exception as exc:
            logger.exception("Milvus 巡检失败")
            sections["milvus"] = SectionResult(
                name="milvus", error=f"{type(exc).__name__}: {exc}"
            )

    if not skip_minio:
        try:
            section = await scan_objects(max_objects)
            keys = section.extra.pop("_keys", [])
            section.findings.extend(classify_objects(keys, snapshot.expected_object_keys))
            sections["minio"] = section
        except Exception as exc:
            logger.exception("MinIO 巡检失败")
            sections["minio"] = SectionResult(name="minio", error=f"{type(exc).__name__}: {exc}")

    if not skip_redis:
        try:
            section, entries = await scan_cache(max_cache_keys)
            ttl_seconds = int(settings.semantic_cache.SEMANTIC_CACHE_TTL_HOURS * 3600)
            section.findings.extend(classify_cache(entries, snapshot, ttl_seconds))
            sections["redis"] = section
        except Exception as exc:
            logger.exception("Redis 巡检失败")
            sections["redis"] = SectionResult(name="redis", error=f"{type(exc).__name__}: {exc}")

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started, 2),
        "db": {
            "documents": len(snapshot.doc_status),
            "published": sum(1 for s in snapshot.doc_status.values() if s == "published"),
            "knowledge_bases": len(snapshot.kb_ids),
            "wiki_pages": len(snapshot.wiki_ids),
            "referenced_objects": len(snapshot.expected_object_keys),
        },
        "sections": {name: s.summary(sample) for name, s in sections.items()},
        "issue_count": _total_issues(sections),
    }
    report["exit_code"] = decide_exit_code(report)
    return report
