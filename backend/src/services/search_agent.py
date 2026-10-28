"""联网搜索 Agent（CODE-1 拆分后为聚合入口）。

Phase 3 的两种搜索模式实现已按职责拆到同目录模块，本文件只做再导出，
保持 `from src.services.search_agent import ...` 的历史导入不变：

- `search_agent_types`      ToolCall / ToolResult / AgentStep / SearchAgentResult / Tool
                            与实时性、工具调用污染判定
- `search_toolkit`          SearchToolkit：工具注册、schema 与 handler 绑定
- `search_agent_base`       BaseSearchAgent：JSON 抽取、工具执行、来源归并
- `function_calling_agent`  FunctionCallingHandler：单轮决策 + 工具执行 + 最终回答
- `react_agent`             ReActAgent：多轮推理-行动循环与流式输出

设计原则（保持不变）：
- 兼容 Ollama 本地模型，不依赖原生 function calling，prompt-based 实现；
- 全异步：LLM 调用、网页抓取、搜索执行；
- 可降级：模型未按格式输出时回退到直接回答或 Phase 2 搜索上下文。
"""

from src.config import settings  # noqa: F401  兼容历史打桩路径 settings.search.*

from src.services.function_calling_agent import FunctionCallingHandler
from src.services.react_agent import ReActAgent
from src.services.search_agent_base import BaseSearchAgent
from src.services.search_agent_types import (  # noqa: F401  兼容历史导入路径
    _TOOL_CALL_POLLUTION_PATTERNS,
    AgentStep,
    SearchAgentResult,
    Tool,
    ToolCall,
    ToolResult,
    _extract_domain,
    is_realtime_question,
    looks_like_tool_call,
)
from src.services.search_toolkit import SearchToolkit

__all__ = [
    "FunctionCallingHandler",
    "ReActAgent",
    "BaseSearchAgent",
    "SearchToolkit",
    "SearchAgentResult",
    "AgentStep",
    "Tool",
    "ToolCall",
    "ToolResult",
    "is_realtime_question",
    "looks_like_tool_call",
    "_extract_domain",
    "_TOOL_CALL_POLLUTION_PATTERNS",
]
