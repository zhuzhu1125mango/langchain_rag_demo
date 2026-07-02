"""工具管理平台。

提供统一的工具抽象、自动发现、注册和执行能力。
所有工具（天气、时间、搜索、计算等）都通过 BaseTool 子类实现，
并在 ToolRegistry 中注册，供 Agent 动态调用。
"""

import asyncio
import importlib
import inspect
import pkgutil
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

import logging

logger = logging.getLogger("rag_system")


@dataclass
class ToolResult:
    """工具执行结果。

    Attributes:
        tool_name: 工具名称。
        input_arguments: 调用时传入的参数。
        output: 工具返回的字符串结果。
        success: 是否执行成功。
        error: 失败时的错误信息。
        latency_ms: 执行耗时（毫秒）。
        sources: 工具返回的来源信息（如 URL、文档等）。
    """

    tool_name: str
    input_arguments: Dict[str, Any] = field(default_factory=dict)
    output: str = ""
    success: bool = True
    error: Optional[str] = None
    latency_ms: int = 0
    sources: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """将结果序列化为字典，便于日志和持久化。"""
        return {
            "tool_name": self.tool_name,
            "input_arguments": self.input_arguments,
            "output": self.output,
            "success": self.success,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "sources": self.sources,
        }


class BaseTool(ABC):
    """工具抽象基类。

    所有具体工具都应继承此类，并通过 @register_tool 装饰器注册。
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """执行工具逻辑，返回 ToolResult。"""
        ...

    def schema(self) -> Dict[str, Any]:
        """生成 OpenAI function-calling 风格的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def legacy_schema(self) -> Dict[str, Any]:
        """生成旧版 SearchToolkit 兼容的 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """工具注册表，管理所有已注册工具。"""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册一个工具实例。"""
        if not tool.name:
            raise ValueError("Tool name cannot be empty")
        if tool.name in self._tools:
            logger.warning(f"工具 {tool.name} 已被注册，将被覆盖")
        self._tools[tool.name] = tool
        logger.debug(f"已注册工具: {tool.name}")

    def get(self, name: str) -> Optional[BaseTool]:
        """根据名称获取工具。"""
        return self._tools.get(name)

    def list_tools(self) -> List[BaseTool]:
        """获取所有已注册工具列表。"""
        return list(self._tools.values())

    def tools_schema(self) -> List[Dict[str, Any]]:
        """获取所有工具的 OpenAI function-calling schema。"""
        return [tool.schema() for tool in self._tools.values()]

    def legacy_tools_schema(self) -> List[Dict[str, Any]]:
        """获取所有工具的旧版兼容 schema。"""
        return [tool.legacy_schema() for tool in self._tools.values()]


class ToolManager:
    """工具管理器，提供工具发现、统一执行和并行调用能力。"""

    def __init__(self, registry: Optional[ToolRegistry] = None):
        self.registry = registry or ToolRegistry()

    def register_tool(self, tool: BaseTool) -> None:
        """注册单个工具。"""
        self.registry.register(tool)

    def discover_tools(self, package_name: str = "src.services.tools.plugins") -> None:
        """自动扫描指定包下的所有工具模块并注册。"""
        try:
            package = importlib.import_module(package_name)
        except ImportError as e:
            logger.warning(f"工具包 {package_name} 导入失败: {e}")
            return

        prefix = package.__name__ + "."
        for _, module_name, _ in pkgutil.iter_modules(package.__path__, prefix):
            try:
                module = importlib.import_module(module_name)
                self._register_tools_from_module(module)
            except Exception as e:
                logger.warning(f"加载工具模块 {module_name} 失败: {e}")

    def _register_tools_from_module(self, module: Any) -> None:
        """从模块中查找并注册 BaseTool 子类实例。"""
        for _, obj in inspect.getmembers(module):
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseTool)
                and obj is not BaseTool
                and not inspect.isabstract(obj)
                and getattr(obj, "name", None)
            ):
                try:
                    instance = obj()
                    self.registry.register(instance)
                except Exception as e:
                    logger.warning(f"实例化工具 {obj.__name__} 失败: {e}")

    async def execute(self, tool_name: str, **kwargs) -> ToolResult:
        """执行指定工具，带超时和异常捕获。"""
        tool = self.registry.get(tool_name)
        if not tool:
            return ToolResult(
                tool_name=tool_name,
                input_arguments=kwargs,
                success=False,
                error=f"工具 '{tool_name}' 未注册",
            )

        start = time.time()
        try:
            result = await asyncio.wait_for(tool.execute(**kwargs), timeout=30.0)
            result.latency_ms = int((time.time() - start) * 1000)
            return result
        except asyncio.TimeoutError:
            return ToolResult(
                tool_name=tool_name,
                input_arguments=kwargs,
                success=False,
                error="工具执行超时",
                latency_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            logger.warning(f"工具 {tool_name} 执行失败: {e}")
            return ToolResult(
                tool_name=tool_name,
                input_arguments=kwargs,
                success=False,
                error=f"执行失败: {e}",
                latency_ms=int((time.time() - start) * 1000),
            )

    async def execute_parallel(
        self, calls: List[Dict[str, Any]]
    ) -> List[ToolResult]:
        """并行执行多个工具调用。

        Args:
            calls: 每个元素为 {"tool_name": str, "arguments": dict}。

        Returns:
            工具执行结果列表，顺序与调用一致。
        """
        if not calls:
            return []

        tasks = [
            self.execute(call["tool_name"], **call.get("arguments", {}))
            for call in calls
        ]
        return await asyncio.gather(*tasks, return_exceptions=False)

    def tools_prompt(self) -> str:
        """生成供 LLM 使用的工具描述 JSON 字符串。"""
        import json

        return json.dumps(self.registry.legacy_tools_schema(), ensure_ascii=False, indent=2)


# 全局默认工具管理器实例
default_tool_manager: Optional[ToolManager] = None


def get_tool_manager() -> ToolManager:
    """获取全局默认工具管理器，首次调用时自动发现所有工具。"""
    global default_tool_manager
    if default_tool_manager is None:
        default_tool_manager = ToolManager()
        default_tool_manager.discover_tools()
    return default_tool_manager
