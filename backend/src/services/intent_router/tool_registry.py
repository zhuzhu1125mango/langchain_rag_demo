"""工具元数据注册表。

为意图路由层维护工具的语义描述、触发模式、所需实体与冲突优先级，
支持更精细的工具选择与冲突裁决。
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from src.services.tools.weather_tool import _extract_city_name


@dataclass
class ToolMeta:
    """工具元数据。

    Attributes:
        name: 工具唯一标识。
        description: 工具功能描述。
        trigger_patterns: 触发该工具的关键词/正则模式列表。
        required_entities: 工具执行所需的关键实体类型。
        examples: 典型问题示例。
        conflict_priority: 冲突优先级，数值越大优先级越高。
    """

    name: str
    description: str
    trigger_patterns: List[str] = field(default_factory=list)
    required_entities: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)
    conflict_priority: int = 0


class ToolMetaRegistry:
    """工具元数据注册表。"""

    def __init__(self):
        self._tools: Dict[str, ToolMeta] = {}
        self._register_defaults()

    def register(self, meta: ToolMeta) -> None:
        """注册一个工具元数据。"""
        self._tools[meta.name] = meta

    def get(self, name: str) -> Optional[ToolMeta]:
        """根据名称获取工具元数据。"""
        return self._tools.get(name)

    def list_tools(self) -> List[ToolMeta]:
        """获取所有已注册工具元数据。"""
        return list(self._tools.values())

    def names(self) -> Set[str]:
        """获取所有已注册工具名称。"""
        return set(self._tools.keys())

    def select_tools(
        self,
        question: str,
        candidates: Optional[List[str]] = None,
    ) -> List[str]:
        """根据问题从候选工具中选择匹配的工具。

        匹配规则：
        1. 若未提供候选，则遍历全部已注册工具。
        2. 工具 trigger_patterns 中任一关键词出现在问题中即视为匹配。
        3. 按 conflict_priority 降序返回。

        Args:
            question: 用户问题。
            candidates: 候选工具名称列表，None 表示全部。

        Returns:
            匹配且按优先级排序的工具名称列表。
        """
        question = (question or "").lower()
        candidates = candidates or list(self._tools.keys())
        matched = []

        for name in candidates:
            meta = self._tools.get(name)
            if not meta:
                continue
            if self._match_patterns(question, meta.trigger_patterns):
                matched.append(meta)

        matched.sort(key=lambda m: m.conflict_priority, reverse=True)
        return [m.name for m in matched]

    def resolve_conflict(
        self,
        question: str,
        candidates: List[str],
    ) -> List[str]:
        """解决工具冲突，返回按优先级排序的工具列表。

        策略：
        1. 先按触发模式匹配过滤。
        2. 按 conflict_priority 降序排列。
        3. 若最高优先级工具 required_entities 在问题中体现较完整，直接返回该工具；
           否则返回前两名，由调用方决定（如 ConfidenceGate）。

        Args:
            question: 用户问题。
            candidates: 冲突的工具候选。

        Returns:
            排序后的工具名称列表。
        """
        matched = self.select_tools(question, candidates)
        if not matched:
            return candidates

        # 若最高优先级工具实体完整，直接采用
        top = self._tools.get(matched[0])
        if top and self._has_required_entities(question, top.required_entities):
            return [top.name]

        # 否则返回前两名，供上层进一步裁决
        return matched[:2]

    def entity_completeness(
        self,
        question: str,
        tool_name: str,
    ) -> float:
        """计算问题对某工具所需实体的完整度（0~1）。"""
        meta = self._tools.get(tool_name)
        if not meta or not meta.required_entities:
            return 1.0

        hits = sum(1 for e in meta.required_entities if self._entity_present(question, e))
        return hits / len(meta.required_entities)

    def _register_defaults(self) -> None:
        """注册系统内置工具的元数据。"""
        defaults = [
            ToolMeta(
                name="get_current_time",
                description="获取当前系统时间、日期、星期几等时间信息。",
                trigger_patterns=[
                    "时间", "几点", "几号", "星期几", "日期", "今天", "现在",
                    "time", "date", "what time", "what day",
                ],
                required_entities=[],
                examples=["现在几点", "今天几号", "2026年6月28日是星期几"],
                conflict_priority=10,
            ),
            ToolMeta(
                name="weather_query",
                description="查询指定城市的天气、气温、降雨、空气质量等信息。",
                trigger_patterns=[
                    "天气", "气温", "温度", "下雨", "下雪", "空气质量", "预报",
                    "weather", "temperature", "forecast", "rain", "snow",
                ],
                required_entities=["城市"],
                examples=["北京天气怎么样", "今天上海会下雨吗"],
                conflict_priority=9,
            ),
            ToolMeta(
                name="gold_price",
                description="查询黄金、白银、铂金、钯金等贵金属的实时价格。",
                trigger_patterns=[
                    "金价", "黄金价格", "银价", "白银价格", "铂金价格", "钯金价格",
                    "贵金属", "gold price", "silver price",
                ],
                required_entities=[],
                examples=["今天金价多少", "白银价格走势"],
                conflict_priority=9,
            ),
            ToolMeta(
                name="exchange_rate",
                description="查询两种货币之间的汇率或进行货币兑换计算。",
                trigger_patterns=[
                    "汇率", "兑换", "换算", "美元兑人民币", "人民币兑美元",
                    "exchange rate", "convert", "currency",
                ],
                required_entities=["货币对"],
                examples=["美元兑人民币汇率", "100美元等于多少人民币"],
                conflict_priority=9,
            ),
            ToolMeta(
                name="calculator",
                description="执行数学表达式计算。",
                trigger_patterns=[
                    "计算", "等于", "+", "-", "*", "×", "÷", "/", "%", "平方", "开方",
                    "calculate", "compute", "sum", "sqrt",
                ],
                required_entities=["表达式"],
                examples=["计算 12 * 34", "sqrt(16) 等于多少"],
                conflict_priority=8,
            ),
            ToolMeta(
                name="web_search",
                description="通过搜索引擎获取实时网页信息。",
                trigger_patterns=[
                    "搜索", "查一下", "最新", "新闻", "资料", "信息",
                    "search", "google", "bing", "lookup",
                ],
                required_entities=["查询词"],
                examples=["搜索最新科技新闻", "查一下 Python 3.12 新特性"],
                conflict_priority=5,
            ),
            ToolMeta(
                name="fetch_webpage",
                description="抓取指定 URL 的网页内容。",
                trigger_patterns=[
                    "网页", "链接", "url", "打开", "抓取",
                    "fetch", "webpage", "url content",
                ],
                required_entities=["url"],
                examples=["抓取这个链接的内容 https://example.com"],
                conflict_priority=4,
            ),
        ]
        for meta in defaults:
            self.register(meta)

    @staticmethod
    def _match_patterns(question: str, patterns: List[str]) -> bool:
        """判断问题是否命中任一触发模式。"""
        for pattern in patterns:
            if pattern.lower() in question:
                return True
            # 支持简单的正则模式（包含特殊字符时）
            if any(c in pattern for c in ".*?+[](){}|\\"):
                try:
                    if re.search(pattern, question, re.IGNORECASE):
                        return True
                except re.error:
                    continue
        return False

    @staticmethod
    def _has_required_entities(question: str, entities: List[str]) -> bool:
        """判断问题是否包含某工具所需的大部分实体。"""
        if not entities:
            return True
        hits = sum(1 for e in entities if ToolMetaRegistry._entity_present(question, e))
        return hits >= len(entities) / 2

    @staticmethod
    def _entity_present(question: str, entity: str) -> bool:
        """判断某实体是否在问题中体现。"""
        entity = entity.lower()
        if entity in question:
            return True

        # 城市：复用 weather_tool 的城市提取逻辑
        if entity == "城市":
            return _extract_city_name(question) is not None

        # 货币对：出现货币代码或常见货币名
        if entity == "货币对":
            return bool(re.search(r"\b[A-Z]{3}\b", question)) or \
                   bool(re.search(r"(?:美元|人民币|欧元|日元|港币|英镑|韩元)", question))

        # 表达式：包含数字和运算符
        if entity == "表达式":
            return bool(re.search(r"\d+\s*[+\-*/×÷%]|sqrt|sin|cos|tan|\^", question))

        # URL：包含 http 链接
        if entity == "url":
            return bool(re.search(r"https?://\S+", question))

        # 查询词：只要有非停用词内容即可
        if entity == "查询词":
            return len(question.strip()) >= 3

        return False


# 全局默认注册表
_default_registry: Optional[ToolMetaRegistry] = None


def get_tool_meta_registry() -> ToolMetaRegistry:
    """获取全局默认工具元数据注册表。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolMetaRegistry()
    return _default_registry
