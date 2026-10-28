"""搜索 Agent 公共类型与判定工具（CODE-1 拆分自 search_agent）。"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List


logger = logging.getLogger(__name__)

def _extract_domain(url: str) -> str:
    """从 URL 中提取域名（与 web_search_service.WebSearchService._extract_domain 逻辑一致）"""
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


# 强时效性关键词：命中后若 Agent 未调用工具，自动降级到 Phase 2 联网搜索。
# 这些关键词代表的问题必须依赖实时数据才能准确回答，LLM 自身知识无法覆盖。
_REALTIME_KEYWORDS: set = {
    # 天气/环境
    "天气", "气温", "温度", "预报", "下雨", "下雪", "降雨", "降雪", "台风",
    "空气质量", "pm2.5", "aqi",
    "weather", "forecast", "temperature",
    # 新闻/资讯
    "新闻", "最新消息", "近日", "今天", "刚刚", "刚才", "热点",
    "news", "latest news",
    # 价格/行情
    "价格", "多少钱", "股价", "股票", "基金", "汇率", "油价", "金价",
    "比特币", "加密货币",
    "price", "stock price", "exchange rate",
    # 实时状态
    "实时", "现在", "当前", "目前", "latest", "today", "now", "current",
    # 赛事/排名
    "比分", "赛果", "排名", "排行榜",
    "score", "ranking",
}


def is_realtime_question(question: str) -> bool:
    """判断问题是否为强时效性问题（必须联网搜索才能准确回答）。

    用于 Function Calling Agent 决策失败时的降级判断：当 LLM 未调用 web_search
    工具但问题命中时效性关键词时，自动降级到 Phase 2 直接搜索，避免 LLM
    笼统回复"无法获取实时信息"。

    Args:
        question: 用户问题。

    Returns:
        bool: True 表示是强时效性问题，需要联网搜索。
    """
    if not question:
        return False
    q = question.lower()
    return any(kw in q for kw in _REALTIME_KEYWORDS)


# 检测 LLM 输出是否被工具调用 JSON 污染（未按预期输出自然语言回答）
_TOOL_CALL_POLLUTION_PATTERNS = [
    re.compile(r'\[\s*\{\s*"name"\s*:\s*"web_search"', re.IGNORECASE),
    re.compile(r'\{\s*"name"\s*:\s*"web_search"', re.IGNORECASE),
    re.compile(r'"tool"\s*:\s*"web_search"', re.IGNORECASE),
    re.compile(r'"arguments"\s*:\s*\{\s*"query"', re.IGNORECASE),
    re.compile(r'<tool_call>\s*\[', re.IGNORECASE),
]


def looks_like_tool_call(text: str) -> bool:
    """判断文本是否包含工具调用 JSON，而非自然语言回答。

    deepseek-r1 等推理模型容易把工具调用 JSON 直接输出为回答。
    当检测到这种污染时，应视为 LLM 未正确回答，触发降级到 Phase 2 搜索。

    Args:
        text: LLM 输出文本。

    Returns:
        bool: True 表示文本被工具调用 JSON 污染。
    """
    if not text:
        return False
    return any(p.search(text) for p in _TOOL_CALL_POLLUTION_PATTERNS)


@dataclass
class ToolCall:
    """工具调用描述"""

    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    """工具执行结果"""

    tool_call: ToolCall
    result: str
    success: bool = True


@dataclass
class AgentStep:
    """ReAct 单步记录"""

    step: int
    thought: str
    action: Dict[str, Any]
    observation: str


@dataclass
class SearchAgentResult:
    """Agent 搜索结果"""

    answer: str = ""
    context: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    steps: List[AgentStep] = field(default_factory=list)


class Tool:
    """工具定义"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Awaitable[str]],
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
