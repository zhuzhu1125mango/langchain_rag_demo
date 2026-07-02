"""联网搜索工具插件。"""

from typing import Any, Dict, Optional

from src.services.tools.tool_manager import BaseTool, ToolResult


class WebSearchTool(BaseTool):
    """执行联网搜索，返回相关网页标题、URL 和摘要。"""

    name = "web_search"
    description = (
        "执行联网搜索，返回相关网页的标题、URL 和摘要，"
        "用于获取实时信息或外部知识。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问题，建议简洁明确",
            },
            "reason": {
                "type": "string",
                "description": "进行本次搜索的原因",
            },
        },
        "required": ["query"],
    }

    def __init__(self, web_search_service=None):
        self.web_search_service = web_search_service

    async def execute(self, **kwargs) -> ToolResult:
        # 延迟导入，避免工具发现阶段因环境缺少依赖而失败
        from src.services.web_search_service import WebSearchService

        query = kwargs.get("query", "")
        if not query:
            return ToolResult(tool_name=self.name, output="搜索关键词为空，未执行搜索。")

        service = self.web_search_service or WebSearchService()
        try:
            results = await service.search_multi(query)
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"搜索失败: {e}",
            )

        if not results:
            return ToolResult(tool_name=self.name, output="未找到相关搜索结果。")

        lines = []
        sources = []
        for i, r in enumerate(results, 1):
            lines.append(
                f"[{i}] 标题: {r.title}\nURL: {r.url}\n摘要: {r.content[:500]}"
            )
            sources.append({
                "url": r.url,
                "source": r.source,
                "title": r.title,
                "page_content": r.content[:500],
                "document_id": "",
                "filename": "web_search",
                "chunk_index": 0,
                "total_chunks": 1,
            })

        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output="\n\n".join(lines),
            sources=sources,
        )
