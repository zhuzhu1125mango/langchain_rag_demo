"""
系统时间工具 - DateTime Tool

提供获取当前日期、时间的工具函数，用于回答"今天几号"、"现在几点"等
时效性问题，避免完全依赖搜索引擎。
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Optional


# 常见时间/日期类问题的关键词模式（覆盖各类自然表达）
_DATETIME_QUESTION_PATTERNS = [
    # —— 日期类 ——
    r"今天\s*是?\s*几号",
    r"今天\s*是?\s*几月\s*几[号日]",
    r"今天\s*几号了",
    r"今天\s*是?\s*几号了",
    r"今天\s*日期",
    r"今天\s*是?\s*星期几",
    r"今天\s*是?\s*周几",
    r"今天\s*是?\s*星期[一二三四五六日天]",
    r"今天\s*是?\s*周[一二三四五六日天]",
    r"星期几了",
    r"周几了",
    r"几月几[号日]",
    r"日期是多少",
    r"今天\s*多少号",
    r"今天\s*多少号了",
    # —— 时间类 ——
    r"现在\s*是?\s*几点",
    r"现在\s*几点了",
    r"现在\s*几点钟",
    r"几点钟了",
    r"当前\s*时间",
    r"当前\s*日期",
    r"现在\s*时间",
    r"现在\s*日期",
    # —— 英文 ——
    r"what\s+time",
    r"what\s+date",
    r"current\s+time",
    r"current\s+date",
    r"what\s+day\s+is\s+it",
]
_DATETIME_QUESTION_RE = re.compile("|".join(_DATETIME_QUESTION_PATTERNS), re.IGNORECASE)

# 短问题兜底关键词：当问题较短且仅由这些词 + 语气词/标点构成时，判定为时间问题
_DATETIME_SHORT_KEYWORDS = {
    "几点", "几点了", "几号", "几号了", "星期几", "周几",
    "日期", "时间", "几点钟", "几点钟了",
}

# 标点与常见语气词，用于短问题归一化
_SHORT_QUESTION_NOISE_RE = re.compile(r"[？?！!。.,，、的了吗呢吧啊呀哦哟呢呗]+")


def is_datetime_question(question: str) -> bool:
    """判断用户问题是否为询问当前日期/时间。

    采用两层判断：
    1. 精准正则匹配：覆盖"今天几号""现在几点""几点了"等常见表达；
    2. 短问题兜底：问题长度 <=8 字时，剥离标点与语气词后，
       若剩余主体命中时间关键词集合，则判定为时间问题。
       这样"几点了？""几号？"能命中，而"你几点下班""开会时间定了吗"不会误伤。
    """
    if not question:
        return False
    q = question.strip()

    # 1. 精准正则匹配
    if _DATETIME_QUESTION_RE.search(q):
        return True

    # 2. 短问题兜底
    if len(q) <= 8:
        cleaned = _SHORT_QUESTION_NOISE_RE.sub("", q)
        if cleaned in _DATETIME_SHORT_KEYWORDS:
            return True

    return False


def get_current_datetime(timezone: str = "Asia/Shanghai") -> Dict[str, str]:
    """
    获取指定时区的当前日期和时间。

    Args:
        timezone: 时区名称，默认 Asia/Shanghai。

    Returns:
        dict: 包含 date、time、weekday、iso 等字段。
    """
    # 优先使用 ZoneInfo 精确时区；Windows 无 tzdata 时降级为系统本地时区
    try:
        tz = ZoneInfo(timezone)
        now = datetime.now(tz)
        tz_name = timezone
    except Exception:
        now = datetime.now().astimezone()
        tz_name = str(now.tzinfo) or "local"

    weekdays_zh = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekday_zh = weekdays_zh[now.weekday()]

    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": weekday_zh,
        "iso": now.isoformat(),
        "timezone": tz_name,
    }


def build_datetime_answer(question: str, timezone: str = "Asia/Shanghai") -> Optional[str]:
    """
    根据问题生成时间/日期回答。

    Args:
        question: 用户问题。
        timezone: 时区名称。

    Returns:
        str or None: 如果能回答则返回答案，否则返回 None。
    """
    if not is_datetime_question(question):
        return None

    dt = get_current_datetime(timezone)
    q = question.lower()

    if "几点" in q or "时间" in q:
        return f"现在是 {dt['date']} {dt['weekday']}，时间 {dt['time']}（时区：{dt['timezone']}）。"

    return f"今天是 {dt['date']}，{dt['weekday']}。"
