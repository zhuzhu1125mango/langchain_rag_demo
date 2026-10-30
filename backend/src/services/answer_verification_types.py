"""答案校验的共享类型与正则常量（CODE-1 拆分自 output_sanitizer）。"""

import logging
import re
from dataclasses import dataclass, field
from typing import List


logger = logging.getLogger(__name__)

_ANSWER_CITATION_RE = re.compile(r"\[(\d+|\?)\]")
# 日期模式：2026年、2026-06-27、今天/昨日/本周/上周
_DATE_PATTERNS = [
    re.compile(r"\d{4}\s*年"),
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}/\d{1,2}/\d{1,2}"),
    re.compile(r"今天|今日|昨天|昨日|前天|本周|上周|本月|上月|今年|去年"),
]
# 句末标点（用于统计事实句）
_FACT_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
# 格式行（标题、分隔线、列表符号）—— 不计入事实句
_FORMAT_LINE_RE = re.compile(
    r"^\s*(?:[-=*]{3,}|#{1,6}\s|```|>\s*|\s*[-*+]\s+|\s*\d+\.\s+)|^\s*$"
)


@dataclass
class VerificationResult:
    """答案校验结果。

    Attributes:
        is_consistent: 答案是否与来源一致（无严重冲突）。
        confidence: 综合置信度 0~1。
        warnings: 警告信息列表。
        unsupported_claims: 未在 sources 中找到支撑的声明。
        conflicting_claims: 与 sources 冲突的声明。
        citation_issues: 引用编号问题（如 [n] 超出有效来源范围）。单列一份，
            便于调用方在不解析 warnings 文案的前提下判断「引用是否对得上」。
        citation_coverage: 引用覆盖率（有 [n] 的句子 / 总事实句，[?] 不计入分子）。
        cross_source_consistency: 多源交叉一致的比例。
        source_authority: 来源平均权威度。
        freshness_score: 来源平均新鲜度。
    """

    is_consistent: bool
    confidence: float
    warnings: List[str] = field(default_factory=list)
    unsupported_claims: List[str] = field(default_factory=list)
    conflicting_claims: List[str] = field(default_factory=list)
    citation_issues: List[str] = field(default_factory=list)
    citation_coverage: float = 0.0
    cross_source_consistency: float = 0.0
    source_authority: float = 0.0
    freshness_score: float = 0.0
