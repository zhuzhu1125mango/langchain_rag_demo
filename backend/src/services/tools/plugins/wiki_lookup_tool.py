"""Wiki 编译页查询工具插件（Agent 工具）。

检索 LLM-Wiki 编译层产出的结构化页面（source_kind="wiki"），
与原始 chunk 检索互补：编译页按概念重组、含跨文档综合与矛盾标注。
"""

import logging
from typing import Any

from src.services.tools.tool_manager import BaseTool, ToolResult

logger = logging.getLogger("rag_system")


class WikiLookupTool(BaseTool):
    """Wiki 编译页查询工具，供 Agent 查询知识库的知识图谱式编译页面。"""

    name = "wiki_lookup"
    description = (
        "查询知识库的 Wiki 编译页（由 LLM 将多篇文档编译成的结构化主题页，"
        "含跨文档综合与矛盾标注）。当问题涉及概念解释、多篇文档的综合对比、"
        "主题梳理时优先使用，效果优于直接检索原始片段。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "查询文本，应为要查找的主题、概念或问题",
            },
        },
        "required": ["query"],
    }

    def __init__(self, kb_retrieval_service=None, kb_ids=None):
        """
        Args:
            kb_retrieval_service: KBRetrievalService 实例（共用管线检索口径）；
                                  缺省时懒加载。
            kb_ids: 默认检索的知识库 ID 列表。
        """
        self._kb_retrieval_service = kb_retrieval_service
        self._kb_ids = kb_ids

    async def _ensure_service(self):
        if self._kb_retrieval_service is None:
            from src.services.kb_retrieval_service import KBRetrievalService
            self._kb_retrieval_service = KBRetrievalService()
        return self._kb_retrieval_service

    async def execute(self, **kwargs) -> ToolResult:
        query = kwargs.get("query", "")
        if not query:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error="检索查询为空",
            )

        kb_ids = kwargs.get("kb_ids") or self._kb_ids
        if not kb_ids:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output="当前未选择知识库，无法查询 Wiki 编译页。请先选择知识库后再提问。",
            )

        try:
            service = await self._ensure_service()
            # 仅检索 source_kind="wiki" 的编译页切片（Milvus 标量过滤）
            docs = await service.retrieve(query, kb_ids=kb_ids, source_kind="wiki")
            if not docs:
                return ToolResult(
                    tool_name=self.name,
                    input_arguments=kwargs,
                    output="该知识库暂无相关 Wiki 编译页（可能未开启 Wiki 编译或无匹配主题）。",
                )

            lines = []
            sources = []
            for i, doc in enumerate(docs, 1):
                meta = doc.metadata or {}
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
                    "source_kind": "wiki",
                    "score": meta.get("score", 0.0),
                })

            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output="\n\n".join(lines),
                sources=sources,
            )
        except Exception as e:
            logger.warning(f"wiki_lookup 工具执行失败: {e}")
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"Wiki 编译页查询失败: {e}",
            )
