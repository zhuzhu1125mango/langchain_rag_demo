"""LLM-Wiki 持久化：页面写库、链路补全、向量入库与索引页维护（CODE-1 拆分）。"""

import logging
import uuid
from typing import List, Optional, Sequence, Union

from src.config import settings
from src.utils.error_handling import ErrorSeverity, log_exception

from src.services.wiki_types import CompiledPage, WikiCompileResult
from src.services.wiki_text import (
    derive_links_from_titles,
    extract_links,
    normalize_doc_ids,
    normalize_title,
)

logger = logging.getLogger("wiki_compiler")

class PagePersistMixin:
    """页面持久化与索引维护，由 WikiCompiler 组合。"""

    async def persist_pages(
        self,
        db,
        kb_id: str,
        doc_ids: Union[str, Sequence[str]],
        compiled: List[CompiledPage],
    ) -> WikiCompileResult:
        """MinIO 正文 + wiki_pages upsert + 向量入库 + 索引页维护。

        doc_ids 支持单值 str（兼容）或多值（P5 去抖合并编译），source_doc_ids 归属全部。
        """
        doc_id_list = normalize_doc_ids(doc_ids)

        from src.models.wiki_page import WikiPage
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        result = WikiCompileResult()

        owner_id = await self._get_kb_owner(db, kb_id)
        upserted_rows: List[WikiPage] = []
        was_existing: List[bool] = []

        for page in compiled:
            # 既有页按 (kb_id, page_type, title) 幂等查找，未命中则新建
            from sqlalchemy import select

            row = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.page_type == page.page_type,
                        WikiPage.title == page.title,
                        WikiPage.status == "active",
                    )
                )
            ).scalars().first()

            if row is None:
                is_existing = False
                row = WikiPage(
                    id=str(uuid.uuid4()),
                    kb_id=str(kb_id),
                    page_type=page.page_type,
                    title=page.title,
                    content_path=f"wiki/{kb_id}/{str(uuid.uuid4())}.md",
                    # 显式初始化（Column default 仅在 INSERT 时生效，内存对象读不到）
                    source_doc_ids=list(doc_id_list),
                    revision=1,
                    owner_id=owner_id,
                )
                db.add(row)
                result.pages_created += 1
            else:
                is_existing = True
                row.revision = (row.revision or 1) + 1
                missing_docs = [d for d in doc_id_list if d not in (row.source_doc_ids or [])]
                if missing_docs:
                    # JSONB 原位变更不触发 UPDATE，须重新赋值
                    row.source_doc_ids = [*(row.source_doc_ids or []), *missing_docs]
                result.pages_updated += 1

            await minio.upload_text_async(row.content_path, page.content)
            upserted_rows.append(row)
            was_existing.append(is_existing)

        await db.commit()
        await self._fill_links(db, kb_id, upserted_rows, [p.content for p in compiled])

        # 页面正文分块入向量库（source_kind=wiki，参与混合检索）；
        # 既有页先删旧向量再入库，避免多 revision 重复累积
        n_indexed = 0
        for row, page, is_existing in zip(upserted_rows, compiled, was_existing):
            n_indexed += await self._index_page(kb_id, row, page, delete_old=is_existing)
        result.chunks_indexed = n_indexed

        await self._upsert_index_page(db, kb_id, minio, owner_id)
        return result

    async def _fill_links(self, db, kb_id: str, rows: List, contents: List[str]) -> None:
        """为页面写 links 列（校验目标页存在），两种建边口径：

        - `WIKI_LINK_METADATA_ENABLED=true`：E-1 L1 元数据建边——按「其他页标题
          出现在本页正文」生成图边（`derive_links_from_titles`），**正文零改动**；
        - 默认（false）：P3 正文 `[[Title]]` 语法提取（`extract_links`）。

        在页面 upsert 提交后执行，保证批次内互链可见；失败仅告警（links 缺失
        只影响导航/检索扩展，不影响编译产物）。
        """
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage

        try:
            active_titles = (
                await db.execute(
                    select(WikiPage.title).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.status == "active",
                    )
                )
            ).scalars().all()
            title_set = {normalize_title(t): t for t in active_titles}
            metadata_edges = settings.wiki_compile.WIKI_LINK_METADATA_ENABLED
            for row, content in zip(rows, contents):
                if metadata_edges:
                    row.links = derive_links_from_titles(content, row.title, title_set)
                else:
                    row.links = extract_links(content, row.title, title_set)
            await db.commit()
        except Exception as e:
            log_exception(
                logger, e,
                action="wiki.persist.links",
                severity=ErrorSeverity.RECOVERABLE,
                message="Wiki 页链接提取失败（不影响编译产物）",
                kb_id=str(kb_id), pages=len(rows),
            )

    async def _index_page(self, kb_id: str, row, page: CompiledPage, delete_old: bool = False) -> int:
        """将单个页面正文分块写入向量库；失败仅告警（raw 层兜底）。

        delete_old=True（既有页更新）时先删旧向量，避免多 revision 重复累积。
        """
        try:
            from langchain_core.documents import Document
            from langchain_text_splitters import RecursiveCharacterTextSplitter

            from src.services.vector_store import VectorStoreManager

            vector_store = await VectorStoreManager.get_instance()
            if delete_old:
                await vector_store.milvus_service.delete_by_document_id(row.id)
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                chunk_overlap=100,
                separators=["\n## ", "\n### ", "\n\n", "\n", "，", "。", ""],
            )
            pieces = splitter.split_text(page.content) or [page.content]
            # synthesis 页标记独立 source_kind（wiki_syn）：不参与主检索混入，
            # 仅经 wiki_lookup 导航入口触达（阶段一 D3，见 wiki-navigable-workspace.md）
            source_kind = "wiki_syn" if page.page_type == "synthesis" else "wiki"
            docs = [
                Document(
                    page_content=text,
                    metadata={
                        "document_id": row.id,
                        "source": f"wiki://{page.title}",
                        "chunk_index": i,
                        "heading_path": page.title,
                        "source_kind": source_kind,
                    },
                )
                for i, text in enumerate(pieces)
            ]
            # add_documents 无返回值（insert 语义），入库数即分块数
            await vector_store.add_documents(docs, str(kb_id))
            return len(docs)
        except Exception as e:
            log_exception(
                logger, e,
                action="wiki.persist.index_page",
                severity=ErrorSeverity.RECOVERABLE,
                message="Wiki 页向量入库失败（raw 层兜底不受影响）",
                kb_id=str(kb_id), page=page.title,
            )
            return 0

    async def rebuild_index_page(self, db, kb_id: str) -> None:
        """公开封装：按当前 active 页重建索引页（级联删除后由 wiki_cascade 调用）。"""
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        await self._upsert_index_page(db, kb_id, minio, await self._get_kb_owner(db, kb_id))

    async def _upsert_index_page(self, db, kb_id: str, minio, owner_id: Optional[str]) -> None:
        """维护索引页（目录，不进向量库）：每次编译后按全部 active 页重建。"""
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage

        try:
            # 先取/建索引行，再查全量页面构建目录（调用序稳定，便于测试对齐）
            index_row = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.page_type == "index",
                    )
                )
            ).scalars().first()
            if index_row is None:
                index_row = WikiPage(
                    id=str(uuid.uuid4()),
                    kb_id=str(kb_id),
                    page_type="index",
                    title="索引",
                    content_path=f"wiki/{kb_id}/{str(uuid.uuid4())}.md",
                    source_doc_ids=[],
                    revision=1,
                    owner_id=owner_id,
                )
                db.add(index_row)
            else:
                index_row.revision = (index_row.revision or 1) + 1

            rows = (
                await db.execute(
                    select(WikiPage)
                    .filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.status == "active",
                        WikiPage.page_type.in_(("entity", "topic", "synthesis")),
                    )
                    .order_by(WikiPage.page_type, WikiPage.title)
                )
            ).scalars().all()

            lines = ["# 知识库索引", ""]
            current_type = None
            type_labels = {"entity": "实体", "topic": "主题", "synthesis": "综合"}
            for row in rows:
                if row.page_type != current_type:
                    label = type_labels.get(row.page_type, "主题")
                    lines += [f"## {label}页", ""]
                    current_type = row.page_type
                lines.append(f"- [[{row.title}]]（revision {row.revision}）")
            lines += ["", f"共 {len(rows)} 个页面。"]
            content = "\n".join(lines)

            await minio.upload_text_async(index_row.content_path, content)
            await db.commit()
        except Exception as e:
            log_exception(
                logger, e,
                action="wiki.persist.index_page_rebuild",
                severity=ErrorSeverity.RECOVERABLE,
                message="Wiki 索引页维护失败（目录可能滞后于页面集合）",
                kb_id=str(kb_id),
            )

    async def _get_kb_owner(self, db, kb_id: str) -> Optional[str]:
        from sqlalchemy import select

        from src.models.knowledge_base import KnowledgeBase

        try:
            kb_uuid = uuid.UUID(str(kb_id))  # KB 主键为 UUID 列，字符串主键场景直接跳过
        except ValueError:
            return None
        try:
            row = (
                await db.execute(
                    select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid)
                )
            ).scalars().first()
            return row.owner_id if row else None
        except Exception as e:
            # owner 查不到会让页面归属为空，后续权限过滤按「无主」处理
            log_exception(
                logger, e,
                action="wiki.persist.kb_owner",
                message="查询知识库归属失败，页面归属降级为空",
                kb_id=str(kb_id),
            )
            return None
