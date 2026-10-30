"""网页抓取工具插件。"""

import logging
from typing import Any, Optional

from src.services.tools.tool_manager import BaseTool, ToolResult
from src.utils.error_handling import ErrorSeverity, log_exception
from src.utils.security import validate_url_safe, UnsafeUrlError

logger = logging.getLogger("rag_system")

# 「抓取成功但正文为空」按工具实例累计，前 3 次逐条记、之后每 50 次一条：
# 站点反爬时每次调用都会命中，逐条记录会把日志刷满。
_empty_content_hits = 0
_EMPTY_CONTENT_REPORT_EVERY = 50


class FetchWebpageTool(BaseTool):
    """抓取指定网页的完整正文内容，用于深入查看某个搜索结果。"""

    name = "fetch_webpage"
    # AM-2：访问外部网络（URL 安全另由 ToolGuard 统一校验）
    risk_level = "external"
    # SEC-19：URL 参数显式声明（ToolGuard 据此做静态 + DNS 校验）
    url_params = ("url",)
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
        # SEC-5：深度抓取默认关闭，避免成为提示注入后的数据外泄通道。
        from src.config import settings

        if not settings.search.AGENT_WEB_FETCH_ENABLED:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error="网页深度抓取已禁用（AGENT_WEB_FETCH_ENABLED=false）",
            )

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
            # 失败原因要能反查：此前这里直接返回「抓取失败」给模型，
            # 日志侧一个字都没有，事后只能看到「Agent 没用上网页内容」。
            log_exception(
                logger, e,
                action="tools.fetch_webpage.execute",
                severity=ErrorSeverity.RECOVERABLE,
                message="网页抓取失败，错误回传给模型",
                url=url,
            )
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"抓取失败: {e}",
            )

        # fetch_content 返回 WebContent 数据类，取其 content 字段（字符串）
        page_text = content.content if hasattr(content, "content") else str(content)
        if not page_text:
            # 抓取本身没报错、正文却是空：多半是站点改版或反爬拦住了提取器，
            # 属于「静默降级」——模型只会看到一句「内容为空」，运营侧看不出面。
            global _empty_content_hits
            _empty_content_hits += 1
            if _empty_content_hits <= 3 or _empty_content_hits % _EMPTY_CONTENT_REPORT_EVERY == 0:
                log_exception(
                    logger,
                    ValueError("抓取成功但正文为空"),
                    action="tools.fetch_webpage.empty_body",
                    severity=ErrorSeverity.REPORTABLE,
                    message="网页正文提取为空，可能是反爬或提取器失效",
                    url=url,
                    hits=_empty_content_hits,
                )
            return ToolResult(tool_name=self.name, output="网页内容为空或无法解析。")

        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=page_text[:3000],
            sources=[{
                "url": url,
                "source": "webpage",
                "title": kwargs.get("title", ""),
                "page_content": page_text[:500],
                "document_id": "",
                "filename": "fetch_webpage",
                "chunk_index": 0,
                "total_chunks": 1,
            }],
        )
