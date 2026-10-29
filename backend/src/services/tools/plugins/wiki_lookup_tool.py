"""Wiki 编译页查询工具插件（Agent 工具）——导航式（阶段一 D1）。

两种用法（Karpathy Pattern 的 query 工作流，渐进式披露）：
1. query 入口检索：定位编译页切片（含 synthesis 页），结果末尾附 Related
   链接（来自命中页的 links 字段），供 Agent 决定是否继续行走；
2. page_title 按页取读：精确获取某一编译页全文（截断保护）+ Related 链接，
   Agent 可沿链接继续调用本工具实现跨文档导航。

与原始 chunk 检索互补：编译页按概念重组、含跨文档综合与矛盾标注。
"""

import logging
from typing import Any

from src.services.tools.tool_manager import BaseTool, ToolResult
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")


class WikiLookupTool(BaseTool):
    """Wiki 编译页查询工具，供 Agent 查询与导航知识库的结构化编译页面。"""

    name = "wiki_lookup"
    # 声明接受编排层注入的 _owner_id（服务端注入，模型不可见），
    # 用于工具侧对象级授权（kb 归属校验，fail-closed）。
    supports_owner_scope = True
    description = (
        "查询并导航知识库的 Wiki 编译页（由 LLM 将多篇文档编译成的结构化主题页，"
        "含跨文档综合与矛盾标注）。当问题涉及概念解释、多篇文档的综合对比、"
        "主题梳理时优先使用。导航策略：入口检索可能先给出【索引页】目录（列出全部"
        "页面），结果末尾的 Related: [[页面标题]] 是相关页面链接——需要展开某一页时，"
        "把其标题作为 page_title 再次调用本工具，沿【索引页】目录或 Related 逐页取读；"
        "跨文档问题应多走几步，而非一次性检索。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询文本，应为要查找的主题、概念或问题（按页取读时可省略）",
            },
            "page_title": {
                "type": "string",
                "description": "（可选）精确取读某一编译页的全文，值为入口结果 Related 中的页面标题",
            },
        },
        "required": [],
    }

    def __init__(self, kb_retrieval_service=None, kb_ids=None, navigator=None):
        """
        Args:
            kb_retrieval_service: KBRetrievalService 实例（共用管线检索口径）；
                                  缺省时懒加载。
            kb_ids: 默认检索的知识库 ID 列表。
            navigator: WikiNavigator 实例（缺省时懒加载，单测可注入 fake）。
        """
        self._kb_retrieval_service = kb_retrieval_service
        self._kb_ids = kb_ids
        self._navigator = navigator

    async def _ensure_navigator(self):
        if self._navigator is None:
            from src.services.wiki_navigator import WikiNavigator

            self._navigator = WikiNavigator(
                kb_retrieval_service=self._kb_retrieval_service
            )
        return self._navigator

    def _resolve_kb_ids(self, kwargs: dict) -> list:
        return kwargs.get("kb_ids") or self._kb_ids or []

    def _no_kb_result(self, kwargs: dict) -> ToolResult:
        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output="当前未选择知识库，无法查询 Wiki 编译页。请先选择知识库后再提问。",
        )

    @staticmethod
    def _record_nav(mode: str, target: str, index_used: bool = False) -> None:
        """E-1 W3 导航埋点：记录一次 wiki_lookup 调用事件（fail-soft）。

        mode: "entry"（入口检索）| "page"（按页取读）；index_used 表示入口经索引页目录。
        """
        try:
            from src.services.pipeline.state import record_wiki_nav

            record_wiki_nav({"mode": mode, "target": target, "index_used": index_used})
        except Exception:
            pass  # 导航埋点：纯指标计数，失败不影响导航结果

    async def execute(self, **kwargs) -> ToolResult:
        # _owner_id 由编排层服务端注入（非模型参数），用于工具侧归属校验
        owner_id = kwargs.pop("_owner_id", None)
        query = (kwargs.get("query") or "").strip()
        page_title = (kwargs.get("page_title") or "").strip()

        if not query and not page_title:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error="query 与 page_title 至少提供一个",
            )

        kb_ids = self._resolve_kb_ids(kwargs)
        if not kb_ids:
            return self._no_kb_result(kwargs)

        # 工具侧对象级授权（fail-closed），与 kb_search 同一口径
        if owner_id:
            from src.utils.validators import kb_ids_owned

            if not await kb_ids_owned(list(kb_ids), owner_id):
                logger.warning(f"wiki_lookup 拒绝越权检索: owner={owner_id}, kb_ids={kb_ids}")
                return ToolResult(
                    tool_name=self.name,
                    input_arguments=kwargs,
                    success=False,
                    error="无权访问所选知识库，已拒绝查询。",
                )

        try:
            navigator = await self._ensure_navigator()
            if page_title:
                return await self._fetch_page(navigator, kb_ids, page_title, kwargs)
            return await self._entry_search(navigator, kb_ids, query, kwargs)
        except Exception as e:
            # 工具失败会以 success=False 回给 Agent，属用户可见的能力降级
            log_exception(
                logger, e,
                action="wiki_lookup.execute",
                message="wiki_lookup 工具执行失败",
                kb_count=len(kb_ids),
            )
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"Wiki 编译页查询失败: {e}",
            )

    async def _fetch_page(self, navigator, kb_ids: list, page_title: str, kwargs: dict) -> ToolResult:
        """按页取读：全文（截断）+ Related 链接。"""
        self._record_nav("page", page_title)
        page = await navigator.get_page_by_title(kb_ids, page_title)
        if page is None:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output=f"未找到标题为「{page_title}」的 Wiki 编译页。可先用 query 入口检索定位相近页面。",
            )

        from src.services.wiki_navigator import format_related

        related = format_related(page.get("links") or [], exclude_titles=[page["title"]])
        type_label = {"entity": "实体页", "topic": "主题页", "synthesis": "综合页", "index": "索引页"}.get(
            page.get("page_type", ""), "编译页"
        )
        output = f"[{type_label}] {page['title']}\n\n{page['content']}"
        if related:
            output = f"{output}\n\n{related}"

        sources = [{
            "url": "",
            "source": f"wiki://{page['title']}",
            "title": page["title"],
            "page_content": page["content"][:800],
            "document_id": page["page_id"],
            "filename": f"wiki://{page['title']}",
            "chunk_index": 0,
            "total_chunks": 1,
            "source_type": "kb",
            "source_kind": "wiki",
            "score": 0.0,
        }]
        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=output,
            sources=sources,
        )

    async def _entry_search(self, navigator, kb_ids: list, query: str, kwargs: dict) -> ToolResult:
        """入口检索：索引页目录（E-1 W1，开关控制）→ 语义命中切片 + Related。"""
        from src.config import settings

        docs = await navigator.search_entries(query, kb_ids)

        # E-1 W1 第一跳：索引页目录（PG/MinIO 直读，不进向量库）——供 Agent 按目录选页。
        # 属工具内部导航，不混入主检索，灰度开关控制（默认关）。
        index_page = None
        if settings.wiki_compile.WIKI_NAV_INDEX_FIRST_ENABLED:
            try:
                index_page = await navigator.get_index_page(kb_ids)
            except Exception as e:
                log_exception(
                    logger, e,
                    action="wiki_lookup.index_page",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="索引页读取失败，回退语义检索入口",
                    kb_count=len(kb_ids),
                )

        # E-1 W3 导航埋点：入口类型（经索引页目录 / 语义检索）
        self._record_nav(
            "entry", query, index_used=bool(index_page and index_page.get("content"))
        )

        if not docs and index_page is None:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output="该知识库暂无相关 Wiki 编译页（可能未开启 Wiki 编译或无匹配主题）。",
            )

        from src.services.wiki_navigator import format_related

        lines = []
        sources = []
        hit_titles = []
        links_acc: list = []

        # 索引页作为第一跳先行透出（目录内容含 [[标题]]，Agent 可据此取读）
        if index_page and index_page.get("content"):
            hit_titles.append(index_page["title"])
            lines.append(f"[索引页] {index_page['title']}\n\n{index_page['content']}")
            sources.append({
                "url": "",
                "source": f"wiki://{index_page['title']}",
                "title": index_page["title"],
                "page_content": index_page["content"][:800],
                "document_id": index_page["page_id"],
                "filename": f"wiki://{index_page['title']}",
                "chunk_index": 0,
                "total_chunks": 1,
                "source_type": "kb",
                "source_kind": "wiki",
                "score": 0.0,
            })

        for i, doc in enumerate(docs, 1):
            meta = doc.metadata or {}
            title = meta.get("heading_path") or meta.get("source", "未知")
            hit_titles.append(title)
            content = doc.page_content[:800] if hasattr(doc, "page_content") else str(doc)[:800]
            lines.append(
                f"[{i}] Wiki 页: {meta.get('source', '未知')}\n"
                f"标题路径: {meta.get('heading_path', '')}\n"
                f"内容: {content}"
            )
            sources.append({
                "url": "",
                "source": meta.get("source", ""),
                "title": meta.get("heading_path", ""),
                "page_content": content,
                "document_id": meta.get("document_id", ""),
                "filename": meta.get("source", ""),
                "chunk_index": meta.get("chunk_index", 0),
                "total_chunks": 1,
                "source_type": "kb",
                "source_kind": "wiki",
                "score": meta.get("score", 0.0),
            })

        # Related 聚合：切片 metadata 携带的 links（由检索层透传），去重后统一展示
        for doc in docs:
            for t in (doc.metadata or {}).get("page_links") or []:
                if t not in links_acc:
                    links_acc.append(t)
        related = format_related(links_acc, exclude_titles=hit_titles)
        output = "\n\n".join(lines)
        if related:
            output = f"{output}\n\n{related}\n（需要展开某篇时，将其标题作为 page_title 再次调用本工具）"
        elif index_page and index_page.get("content"):
            output = f"{output}\n（可按索引页目录，把任一 [[标题]] 作为 page_title 取读）"

        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=output,
            sources=sources,
        )
