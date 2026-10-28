"""搜索工具集：Tool 注册、schema 与 handler 绑定（CODE-1 拆分）。"""

import logging
from typing import Awaitable, Callable, List, Optional

from src.config import settings
from .tools.plugins.web_search_tool import WebSearchTool
from .tools.plugins.fetch_webpage_tool import FetchWebpageTool

logger = logging.getLogger(__name__)

from src.services.search_agent_types import Tool

class SearchToolkit:
    """搜索工具集兼容层。

    基于全局 ToolManager（get_tool_manager，自动发现全部插件）的白名单视图，
    保持对旧版 SearchAgent 的接口兼容：get_tool()/tools 仅暴露白名单内的工具，
    避免旧 Agent（4B 模型）面对过大的工具面。
    """

    # 默认白名单：与旧版行为一致（仅搜索相关核心工具）
    DEFAULT_TOOL_NAMES = ("web_search", "fetch_webpage", "get_current_time")

    def __init__(self, web_search_service, tool_names=None):
        from .tools.tool_manager import get_tool_manager

        self.web_search_service = web_search_service
        # 统一注册表：全局 ToolManager（含全部插件：web/时间/天气/计算/KB 检索/Wiki 等）
        self.tool_manager = get_tool_manager()
        # 用共享 WebSearchService 实例覆盖自动发现的无参实例，保持搜索缓存/上下文共享
        self.tool_manager.register_tool(WebSearchTool(web_search_service))
        self.tool_manager.register_tool(FetchWebpageTool(web_search_service))
        # 白名单视图（旧 Agent 只见搜索核心工具；P2 Agent 循环用 AGENT_TOOLS_ENABLED 扩展）
        names = tuple(tool_names) if tool_names else self.DEFAULT_TOOL_NAMES
        if not settings.search.AGENT_WEB_FETCH_ENABLED:
            names = tuple(n for n in names if n != "fetch_webpage")
        self._tool_names = names
        self._tools = self._build_tools()

    def _build_tools(self) -> List[Tool]:
        """基于 ToolManager 注册的白名单工具，生成旧版 Tool 对象列表。"""
        tools = []
        for base_tool in self.tool_manager.registry.list_tools():
            if base_tool.name not in self._tool_names:
                continue
            # 包装 BaseTool 的 execute 为旧版 handler 签名
            handler = self._make_handler(base_tool.name)
            tools.append(
                Tool(
                    name=base_tool.name,
                    description=base_tool.description,
                    parameters=base_tool.parameters,
                    handler=handler,
                )
            )
        return tools

    def _make_handler(self, tool_name: str) -> Callable[..., Awaitable[str]]:
        """生成旧版 handler，内部调用 ToolManager.execute 并返回字符串。"""

        async def handler(**kwargs) -> str:
            result = await self.tool_manager.execute(tool_name, **kwargs)
            if not result.success:
                return f"工具执行失败: {result.error}"
            return result.output

        return handler

    @property
    def tools(self) -> List[Tool]:
        return self._tools

    def get_tool(self, name: str) -> Optional[Tool]:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def tools_prompt(self) -> str:
        return self.tool_manager.tools_prompt()
