"""联网搜索公共类型：WebContent、WebSearchError 与关键词常量（CODE-1 拆分）。"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Set



logger = logging.getLogger(__name__)

# 常见低质量/噪音域名，抓取正文时过滤
_LOW_QUALITY_DOMAINS: Set[str] = {
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "weibo.com",
    "zhihu.com/question",
    "reddit.com",
    "pinterest.com",
    "taobao.com",
    "tmall.com",
    "jd.com",
    "amazon.com",
}

# 简单中文/英文停用词，用于基础 Query 改写
_STOPWORDS: Set[str] = {
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "哪", "什么", "怎么", "为什么", "如何", "请", "一下",
    "能否", "可以", "告诉", "帮忙", "一下", "呢", "吗", "吧", "啊",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall",
}

_CODE_KEYWORDS: Set[str] = {
    "代码", "code", "编程", "programming", "github", "stackoverflow",
    "python", "java", "javascript", "typescript", "go", "rust", "c++", "c#",
    "报错", "error", "exception", "bug", "调试", "debug", "函数", "库", "package",
    "怎么写", "如何实现", "示例", "demo", "snippet",
}

_RECENT_KEYWORDS: Set[str] = {
    "最新", "最近", "今年", "去年", "新闻", "实时", "刚刚", "近日",
    "2024", "2025", "2026", "newest", "latest", "recent", "news",
}

# 时效性关键词分级，用于 SearXNG time_range 参数（增强1：时效性感知搜索）
_TIME_RANGE_DAY_KEYWORDS: Set[str] = {
    "今天", "今日", "现在", "当前", "实时", "刚刚", "刚才", "此刻",
    "几点", "多少钱", "价格", "today", "now", "current",
}
_TIME_RANGE_WEEK_KEYWORDS: Set[str] = {
    "最新", "最近", "本周", "这周", "近日", "这几天", "latest", "recent", "this week",
}
_TIME_RANGE_MONTH_KEYWORDS: Set[str] = {
    "本月", "上月", "这个月", "今年", "去年", "2024", "2025", "2026",
    "this month", "this year",
}


def _detect_time_range(query: str) -> Optional[str]:
    """根据查询词的时效性关键词，返回 SearXNG time_range 参数值。

    返回值：'day' / 'week' / 'month' / None（不限）。
    优先级 day > week > month，命中更精细的时间范围即返回，
    让"今天比特币价格""现在几点"等实时性问题优先拿到最新网页。
    """
    if not query:
        return None
    q = query.lower()
    if any(kw in q for kw in _TIME_RANGE_DAY_KEYWORDS):
        return "day"
    if any(kw in q for kw in _TIME_RANGE_WEEK_KEYWORDS):
        return "week"
    if any(kw in q for kw in _TIME_RANGE_MONTH_KEYWORDS):
        return "month"
    return None


@dataclass
class WebContent:
    url: str
    title: str
    content: str
    snippet: str = ""
    source_index: int = 0
    score: float = 0.0
    metadata: Dict = field(default_factory=dict)


class WebSearchError(Exception):
    """联网搜索异常：包含搜索引擎类型与原始错误信息"""

    def __init__(self, message: str, engine: str = "", cause: Optional[Exception] = None):
        super().__init__(message)
        self.engine = engine
        self.cause = cause

    def __str__(self) -> str:
        parts = [f"[{self.engine}] {super().__str__()}" if self.engine else super().__str__()]
        if self.cause:
            parts.append(f"原始错误: {self.cause}")
        return " | ".join(parts)
