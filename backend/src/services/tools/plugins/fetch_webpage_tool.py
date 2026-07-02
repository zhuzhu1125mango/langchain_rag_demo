"""网页抓取工具插件。"""

from typing import Any, Optional

from src.services.tools.tool_manager import BaseTool, ToolResult
from src.utils.security import validate_url_safe, UnsafeUrlError


class FetchWebpageTool(BaseTool):
    """抓取指定网页的完整正文内容，用于深入查看某个搜索结果。"""

    name = "fetch_webpage"
    description = "抓取指定网页的完整正文内容，用于深入查看某个搜索结果"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标网页 URL"},
            "title": {"type": "string", "description": "网页标题（可选）"},
            "snippet": {"type": "string", "description": "网页摘要（可选）"},
        },
        "required": ["url"],
    }

    def __init__(self, web_search_service=None):
        self.web_search_service = web_search_service

    async def execute(self, **kwargs) -> ToolResult:
        # 延迟导入，避免工具发现阶段因环境缺少依赖而失败
        from src.services.web_search_service import WebSearchService

        url = kwargs.get("url", "")
        if not url:
            return ToolResult(tool_name=self.name, output="URL 为空，无法抓取。")

        try:
            validate_url_safe(url)
        except UnsafeUrlError as e:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"URL 不安全: {e}",
            )

        service = self.web_search_service or WebSearchService()
        try:
            content = await service.fetch_content(
                url,
                title=kwargs.get("title", ""),
                snippet=kwargs.get("snippet", ""),
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"抓取失败: {e}",
            )

        if not content:
            return ToolResult(tool_name=self.name, output="网页内容为空或无法解析。")

        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=content[:3000],
            sources=[{
                "url": url,
                "source": "webpage",
                "title": kwargs.get("title", ""),
                "page_content": content[:500],
                "document_id": "",
                "filename": "fetch_webpage",
                "chunk_index": 0,
                "total_chunks": 1,
            }],
        )
