"""知识库检索工具插件（Agent 工具）。

供 Agent 循环调用，与 RAG 管线共用 KBRetrievalService 同一检索口径，
确保"工具检索"与"管线检索"行为一致，不出现两套检索结果。
"""

import logging
from typing import Any

from src.services.tools.tool_manager import BaseTool, ToolResult

logger = logging.getLogger("rag_system")


class KBSearchTool(BaseTool):
    """知识库语义检索工具，供 Agent 在循环中按需检索本地知识库。"""

    name = "kb_search"
    description = (
        "在用户已选择的知识库中进行语义检索（混合 dense+BM25+rerank）。"
        "当问题涉及用户上传的文档、知识库中的专业知识时使用。"
        "返回最相关的文档片段（含来源元数据）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索查询文本，应为完整的问题或关键词",
            },
        },
        "required": ["query"],
    }

    def __init__(self, kb_retrieval_service=None, kb_ids=None):
        """
        Args:
            kb_retrieval_service: KBRetrievalService 实例（共用管线检索口径）；
                                  缺省时懒加载。
            kb_ids: 默认检索的知识库 ID 列表（通常为会话当前已选 KB）。
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
                output="当前未选择知识库，无法检索。请先选择知识库后再提问。",
            )

        try:
            service = await self._ensure_service()
            docs = await service.retrieve(query, kb_ids=kb_ids)
            if not docs:
                return ToolResult(
                    tool_name=self.name,
                    input_arguments=kwargs,
                    output="未检索到相关文档。",
                )

            lines = []
            sources = []
            for i, doc in enumerate(docs, 1):
                meta = doc.metadata or {}
                content = doc.page_content[:500] if hasattr(doc, "page_content") else str(doc)[:500]
                lines.append(
                    f"[{i}] 来源: {meta.get('source', '未知')}\n"
                    f"标题路径: {meta.get('heading_path', '')}\n"
                    f"类型: {meta.get('source_kind', 'raw')}\n"
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
                    "source_kind": meta.get("source_kind", "raw"),
                    "score": meta.get("score", 0.0),
                })

            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output="\n\n".join(lines),
                sources=sources,
            )
        except Exception as e:
            logger.warning(f"kb_search 工具执行失败: {e}")
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"知识库检索失败: {e}",
            )
