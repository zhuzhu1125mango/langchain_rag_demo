"""多源数值交叉验证器。

从多个来源文本中提取数值型事实，进行单位归一化与冲突检测，
为答案生成提供数据一致性评估与警告依据。
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class NumericalFact:
    """单个数值事实。"""

    value: float
    unit: str
    metric: str
    original_text: str
    source_index: Optional[int] = None
    source_title: str = ""


@dataclass
class NumericalConflict:
    """数值冲突条目。"""

    metric: str
    unit: str
    severity: str  # high | medium | low
    facts: List[NumericalFact]
    max_relative_diff: float
    message: str


@dataclass
class NumericalValidationResult:
    """多源数值验证结果。"""

    is_consistent: bool
    confidence: float
    facts: List[NumericalFact] = field(default_factory=list)
    conflicts: List[NumericalConflict] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class NumericalValidator:
    """数值事实提取与交叉验证器。

    流程：
    1. 从每段来源文本中提取数值 + 单位 + 指标。
    2. 将单位归一化为标准单位（如万、亿、千、K、M、B 转为基本数值）。
    3. 按 (metric, unit) 分组，计算组内数值的最大相对差异。
    4. 根据差异阈值判定冲突严重级别。
    """

    # 数值匹配：支持 1,234.56、12345.6、3.14 等
    _NUMBER_RE = re.compile(r"(?:(?:\d{1,3},)+\d{3}|\d+)(?:\.\d+)?")

    # 常见指标关键词（用于从上下文推断指标）
    _METRIC_KEYWORDS = {
        "价格": {"价格", "售价", "单价", "报价", "市价", "行情"},
        "温度": {"温度", "气温", "水温", "高温", "低温"},
        "人口": {"人口", "人数", "常住人口", "总人口"},
        "GDP": {"gdp", "国内生产总值", "生产总值"},
        "面积": {"面积", "占地面积", "建筑面积", "平方公里"},
        "长度": {"长度", "高度", "宽度", "深度", "距离", "公里", "千米", "米"},
        "重量": {"重量", "质量", "吨", "千克", "公斤", "克"},
        "汇率": {"汇率", "兑换", "换算"},
        "股价": {"股价", "股票", "股价", "收盘价", "开盘价"},
        "利率": {"利率", "收益率", "年化收益"},
        "速度": {"速度", "时速", "速率"},
        "时间": {"时间", "日期", "年份", "月份"},
    }

    # 冲突阈值
    _SEVERITY_THRESHOLDS = {
        "high": 0.50,
        "medium": 0.20,
        "low": 0.05,
    }

    def __init__(
        self,
        low_threshold: float = 0.05,
        medium_threshold: float = 0.20,
        high_threshold: float = 0.50,
    ):
        self._SEVERITY_THRESHOLDS = {
            "low": low_threshold,
            "medium": medium_threshold,
            "high": high_threshold,
        }

    def _extract_numbers(self, text: str) -> List[Tuple[str, int]]:
        """从文本中提取所有数字字符串及其起始位置。"""
        return [(m.group(), m.start()) for m in self._NUMBER_RE.finditer(text)]

    def _normalize_unit(self, text: str) -> Tuple[float, str]:
        """将单位归一化为基本倍数和标准单位名。

        返回 (multiplier, normalized_unit)。
        例如："5 万" -> (50000, "元")，"3.2 亿美元" -> (320000000, "美元")。
        """
        text = text.lower().strip()
        multiplier = 1.0
        unit = ""

        # 中文单位
        if "万亿" in text:
            multiplier = 1e12
            unit = text.replace("万亿", "").strip()
        elif "亿" in text:
            multiplier = 1e8
            unit = text.replace("亿", "").strip()
        elif "万" in text:
            multiplier = 1e4
            unit = text.replace("万", "").strip()
        elif "千" in text:
            multiplier = 1e3
            unit = text.replace("千", "").strip()
        elif "百" in text:
            multiplier = 1e2
            unit = text.replace("百", "").strip()
        elif "十" in text:
            multiplier = 1e1
            unit = text.replace("十", "").strip()
        else:
            # 英文单位
            lowered = text
            if "trillion" in lowered:
                multiplier = 1e12
                unit = lowered.replace("trillion", "").strip()
            elif "billion" in lowered:
                multiplier = 1e9
                unit = lowered.replace("billion", "").strip()
            elif "million" in lowered:
                multiplier = 1e6
                unit = lowered.replace("million", "").strip()
            elif "thousand" in lowered or " k" in lowered or lowered.endswith("k"):
                multiplier = 1e3
                unit = re.sub(r"(thousand|k)\b", "", lowered).strip()
            elif " m" in lowered or lowered.endswith("m"):
                multiplier = 1e6
                unit = re.sub(r"m\b", "", lowered).strip()
            elif " b" in lowered or lowered.endswith("b"):
                multiplier = 1e9
                unit = re.sub(r"b\b", "", lowered).strip()
            else:
                unit = text

        # 清理单位中的数字和多余空格
        unit = re.sub(r"[\d\.\s,]+", "", unit).strip()
        return multiplier, unit or ""

    def _infer_metric(self, text: str, window: str) -> str:
        """根据上下文窗口推断指标名。"""
        combined = (text + " " + window).lower()
        for metric, keywords in self._METRIC_KEYWORDS.items():
            if any(kw.lower() in combined for kw in keywords):
                return metric
        return "数值"

    def _parse_value(self, number_str: str) -> float:
        """将数字字符串转为浮点数。"""
        return float(number_str.replace(",", ""))

    def _extract_window(self, text: str, start: int, end: int, window_size: int = 12) -> str:
        """提取数字附近的上下文窗口。"""
        s = max(0, start - window_size)
        e = min(len(text), end + window_size)
        return text[s:e]

    def _extract_unit_window(self, text: str, end: int, window_size: int = 8) -> str:
        """提取数字右侧的单位上下文窗口，避免左侧噪音污染单位。"""
        e = min(len(text), end + window_size)
        return text[end:e]

    def extract_facts(
        self,
        content: str,
        source_index: Optional[int] = None,
        source_title: str = "",
    ) -> List[NumericalFact]:
        """从单段文本中提取数值事实列表。"""
        facts = []
        if not content:
            return facts

        numbers = self._extract_numbers(content)
        for number_str, start in numbers:
            end = start + len(number_str)
            window = self._extract_window(content, start, end)
            unit_window = self._extract_unit_window(content, end)
            try:
                base_value = self._parse_value(number_str)
            except ValueError:
                continue

            multiplier, unit = self._normalize_unit(unit_window)
            value = base_value * multiplier
            metric = self._infer_metric(content, window)

            facts.append(
                NumericalFact(
                    value=value,
                    unit=unit,
                    metric=metric,
                    original_text=number_str,
                    source_index=source_index,
                    source_title=source_title,
                )
            )

        return facts

    def validate(
        self,
        sources: List[Dict[str, Any]],
    ) -> NumericalValidationResult:
        """对多个来源进行数值交叉验证。

        Args:
            sources: 来源列表，每项至少包含 content，可选 source_index/title。

        Returns:
            NumericalValidationResult: 验证结果。
        """
        all_facts: List[NumericalFact] = []
        for i, src in enumerate(sources or []):
            content = src.get("content") or src.get("page_content", "")
            title = src.get("title", "")
            idx = src.get("source_index", i + 1)
            facts = self.extract_facts(content, source_index=idx, source_title=title)
            all_facts.extend(facts)

        if not all_facts:
            return NumericalValidationResult(
                is_consistent=True,
                confidence=1.0,
                facts=[],
                conflicts=[],
                warnings=[],
            )

        # 按 (metric, unit) 分组
        groups: Dict[Tuple[str, str], List[NumericalFact]] = {}
        for fact in all_facts:
            key = (fact.metric, fact.unit)
            groups.setdefault(key, []).append(fact)

        conflicts: List[NumericalConflict] = []
        warnings: List[str] = []
        max_severity_score = 0.0  # 0=无冲突, low=1, medium=2, high=3

        for (metric, unit), facts in groups.items():
            if len(facts) < 2:
                continue

            values = [f.value for f in facts]
            max_rel_diff = self._max_relative_difference(values)

            severity = None
            if max_rel_diff >= self._SEVERITY_THRESHOLDS["high"]:
                severity = "high"
            elif max_rel_diff >= self._SEVERITY_THRESHOLDS["medium"]:
                severity = "medium"
            elif max_rel_diff >= self._SEVERITY_THRESHOLDS["low"]:
                severity = "low"

            if severity:
                severity_score = {"low": 1, "medium": 2, "high": 3}[severity]
                max_severity_score = max(max_severity_score, severity_score)
                message = (
                    f"{metric}（{unit or '无量纲'}）在多个来源中存在 {severity} 不一致："
                    f"最大相对差异 {max_rel_diff:.1%}"
                )
                conflicts.append(
                    NumericalConflict(
                        metric=metric,
                        unit=unit,
                        severity=severity,
                        facts=facts,
                        max_relative_diff=max_rel_diff,
                        message=message,
                    )
                )
                warnings.append(message)

        is_consistent = len(conflicts) == 0
        confidence = self._severity_score_to_confidence(max_severity_score)

        return NumericalValidationResult(
            is_consistent=is_consistent,
            confidence=confidence,
            facts=all_facts,
            conflicts=conflicts,
            warnings=warnings,
        )

    @staticmethod
    def _max_relative_difference(values: List[float]) -> float:
        """计算一组数值中的最大相对差异。"""
        if len(values) < 2:
            return 0.0
        max_diff = 0.0
        for i in range(len(values)):
            for j in range(i + 1, len(values)):
                a, b = values[i], values[j]
                denominator = max(abs(a), abs(b), 1e-9)
                diff = abs(a - b) / denominator
                if diff > max_diff:
                    max_diff = diff
        return max_diff

    @staticmethod
    def _severity_score_to_confidence(score: int) -> float:
        """将冲突严重级别分数映射为置信度。"""
        mapping = {0: 1.0, 1: 0.8, 2: 0.5, 3: 0.2}
        return mapping.get(score, 0.5)

    def format_warning(self, result: NumericalValidationResult) -> str:
        """将验证结果格式化为人类可读警告文本。"""
        if result.is_consistent:
            return ""
        lines = ["\n\n【数据一致性提示】"]
        for conflict in result.conflicts:
            lines.append(f"- {conflict.message}")
            for fact in conflict.facts:
                src = f"[来源{fact.source_index}]" if fact.source_index else "[来源未知]"
                display_value = self._format_display_value(fact)
                lines.append(f"  {src} {display_value}")
        return "\n".join(lines)

    @staticmethod
    def _format_display_value(fact: NumericalFact) -> str:
        """格式化数值用于显示，尽量还原原始量级。"""
        value = fact.value
        if value >= 1e12:
            return f"{value/1e12:.2f} 万亿{fact.unit}"
        if value >= 1e8:
            return f"{value/1e8:.2f} 亿{fact.unit}"
        if value >= 1e4:
            return f"{value/1e4:.2f} 万{fact.unit}"
        if value >= 1e3:
            return f"{value/1e3:.2f} 千{fact.unit}"
        return f"{value:.2f} {fact.unit}".strip()
