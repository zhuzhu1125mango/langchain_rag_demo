"""价格类实时数据的通用模型与置信度计算工具。

为金价、汇率、股价等数值型实时查询提供统一的数据结构、交叉验证和置信度评分，
帮助上层工具与答案生成器判断数据可信度并给出合适的用户提示。
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PriceDataPoint:
    """单个价格数据点。

    Attributes:
        value: 数值。
        currency: 货币代码（如 CNY、USD）。
        unit: 单位（如 gram、oz、kg、share）。
        timestamp: 数据时间戳（UTC）。
        source: 来源名称。
        source_url: 来源 URL。
        raw_data: 原始响应数据（用于调试和追踪）。
    """

    value: Decimal
    currency: str
    unit: str
    timestamp: Optional[datetime]
    source: str
    source_url: str
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConfidenceReport:
    """价格数据置信度报告。

    Attributes:
        overall: 综合置信度 0.0 ~ 1.0。
        freshness: 时间新鲜度得分。
        authority: 来源权威性得分。
        cross_validation: 数值交叉验证一致性得分。
        source_count: 可用数据源数量。
        fallback_used: 是否使用了降级数据源。
        warnings: 警告信息列表。
    """

    overall: float = 0.0
    freshness: float = 0.0
    authority: float = 0.0
    cross_validation: float = 0.0
    source_count: int = 0
    fallback_used: bool = False
    warnings: List[str] = field(default_factory=list)

    def is_low_confidence(self, threshold: float = 0.6) -> bool:
        """是否低于给定置信度阈值。"""
        return self.overall < threshold

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "overall": self.overall,
            "freshness": self.freshness,
            "authority": self.authority,
            "cross_validation": self.cross_validation,
            "source_count": self.source_count,
            "fallback_used": self.fallback_used,
            "warnings": self.warnings,
        }


@dataclass
class ValidatedPriceResult:
    """经交叉验证后的价格结果。

    Attributes:
        value: 共识数值。
        currency: 货币代码。
        unit: 单位。
        confidence: 综合置信度。
        freshness_score: 时间新鲜度得分。
        authority_score: 来源权威性得分。
        cross_validation_score: 数值交叉验证一致性得分。
        sources: 所有数据源列表。
        fallback_used: 是否使用了降级数据源。
        warnings: 警告信息列表。
    """

    value: Decimal
    currency: str
    unit: str
    confidence: float
    freshness_score: float
    authority_score: float
    cross_validation_score: float
    sources: List[PriceDataPoint]
    fallback_used: bool = False
    warnings: List[str] = field(default_factory=list)

    def format_summary(self) -> str:
        """生成供 LLM 阅读的摘要文本。"""
        lines = [
            f"价格: {self.value} {self.currency}/{self.unit}",
            f"置信度: {self.confidence:.0%}",
        ]
        if self.warnings:
            lines.append("警告: " + "; ".join(self.warnings))
        lines.append("数据来源:")
        for s in self.sources:
            ts = s.timestamp.strftime("%Y-%m-%d %H:%M UTC") if s.timestamp else "未知时间"
            lines.append(f"- {s.source}: {s.value} {s.currency}/{s.unit} ({ts})")
        return "\n".join(lines)


def freshness_score(timestamp: Optional[datetime], now: Optional[datetime] = None) -> float:
    """根据数据时间戳计算新鲜度得分。

    Args:
        timestamp: 数据时间戳。
        now: 当前时间，默认使用 UTC 现在时间。

    Returns:
        0.0 ~ 1.0 的新鲜度得分。
    """
    if timestamp is None:
        return 0.3
    now = now or datetime.now(timezone.utc)
    # 统一为 offset-naive，避免时区混用相减报错
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.replace(tzinfo=None)
    age_seconds = (now - timestamp).total_seconds()

    if age_seconds <= 60:
        return 1.0
    if age_seconds <= 300:
        return 0.9
    if age_seconds <= 900:
        return 0.8
    if age_seconds <= 3600:
        return 0.7
    if age_seconds <= 21600:
        return 0.5
    if age_seconds <= 86400:
        return 0.3
    return 0.1


def authority_score(source_name: str) -> float:
    """根据来源名称计算权威性得分。

    Args:
        source_name: 来源名称或域名。

    Returns:
        0.0 ~ 1.0 的权威性得分。
    """
    source_lower = (source_name or "").lower()
    # 官方交易所 / 央行
    if any(k in source_lower for k in ("sge.com.cn", "lbma.org.uk", "gov.cn", "pbc.gov.cn")):
        return 0.95
    # 知名金融数据 API
    if any(
        k in source_lower
        for k in (
            "gold-api.com",
            "xaus.com",
            "frankfurter.app",
            "exchangerate-api.com",
            "open-er-api.com",
            "alpha vantage",
            "yfinance",
            "yahoo finance",
        )
    ):
        return 0.85
    # 知名财经媒体
    if any(
        k in source_lower
        for k in (
            "investing.com",
            "gold.org",
            "bloomberg",
            "reuters",
            "wsj",
            "xetra-gold",
            "新浪财经",
            "东方财富",
        )
    ):
        return 0.7
    # 通用搜索结果
    if any(k in source_lower for k in ("web_search", "searxng", "google", "bing")):
        return 0.5
    # 未知来源
    return 0.3


def cross_validate_numeric_values(
    values: List[Decimal],
    tolerance: float = 0.05,
) -> Tuple[Decimal, float, List[str]]:
    """对多个数据源返回的数值进行交叉验证。

    使用 consensus value（中位数）作为基准，计算各数据源与基准的最大相对偏差。

    Args:
        values: 多个数据源的数值。
        tolerance: 相对偏差容忍度，默认 5%。

    Returns:
        (consensus_value, consistency_score, warnings)
    """
    if not values:
        return Decimal("0"), 0.0, ["无可用数值"]

    if len(values) == 1:
        return values[0], 0.7, ["仅单一数据源"]

    sorted_values = sorted(values)
    n = len(sorted_values)
    if n % 2 == 1:
        median = sorted_values[n // 2]
    else:
        median = (sorted_values[n // 2 - 1] + sorted_values[n // 2]) / Decimal("2")

    if median == 0:
        return median, 0.0, ["共识值为 0，无法计算偏差"]

    deviations = [abs(v - median) / median for v in values]
    max_deviation = max(deviations)

    max_deviation_float = float(max_deviation)
    if max_deviation_float <= tolerance:
        consistency_score = 1.0 - max_deviation_float / tolerance * 0.3
    else:
        consistency_score = max(0.0, 0.5 - (max_deviation_float - tolerance))

    warnings = []
    if max_deviation > tolerance:
        warnings.append(f"数据源偏差过大，最大偏差 {max_deviation:.2%}")

    return median, consistency_score, warnings


def compute_overall_confidence(
    consistency_score: float,
    freshness_score: float,
    authority_score: float,
    source_count: int,
    fallback_used: bool,
) -> float:
    """计算综合置信度。

    权重：交叉验证 0.4 + 新鲜度 0.3 + 权威性 0.2 + 数据源数量 0.1。
    若使用了降级数据源，最终得分乘以 0.7。

    Args:
        consistency_score: 数值交叉验证一致性得分。
        freshness_score: 时间新鲜度得分。
        authority_score: 来源权威性得分。
        source_count: 可用数据源数量。
        fallback_used: 是否使用了降级数据源。

    Returns:
        0.0 ~ 1.0 的综合置信度。
    """
    count_score = min(1.0, source_count / 2.0)
    base = (
        consistency_score * 0.4
        + freshness_score * 0.3
        + authority_score * 0.2
        + count_score * 0.1
    )
    if fallback_used:
        base *= 0.7
    return round(min(1.0, max(0.0, base)), 2)


def format_price_answer(
    result: ValidatedPriceResult,
    item_name: str = "",
    low_confidence_threshold: float = 0.6,
    very_low_confidence_threshold: float = 0.4,
) -> str:
    """根据验证结果和价格名称生成标准化答案文本。

    高置信度：直接给出数值并标注来源和时间。
    中置信度：给出数值但附加风险提示。
    低置信度：不给出具体数值，明确告知用户无法获取可靠数据。

    Args:
        result: 验证后的价格结果。
        item_name: 价格项名称（如"黄金"、"美元兑人民币汇率"）。
        low_confidence_threshold: 低置信度阈值。
        very_low_confidence_threshold: 极低置信度阈值。

    Returns:
        标准化答案文本。
    """
    item_name = item_name or "价格"
    if result.confidence < very_low_confidence_threshold:
        return (
            f"当前无法获取可靠的实时{item_name}数据（置信度 {result.confidence:.0%}）。\n"
            "建议您通过官方渠道或专业金融平台核实最新数据。"
        )

    primary = result.sources[0] if result.sources else None
    timestamp_str = ""
    if primary and primary.timestamp:
        timestamp_str = primary.timestamp.strftime("%Y-%m-%d %H:%M UTC")
    elif result.sources:
        timestamp_str = "未知时间"

    source_names = "、".join(sorted({s.source for s in result.sources})) or "未知来源"

    if result.confidence < low_confidence_threshold:
        answer = (
            f"截至 {timestamp_str}，{item_name}约为 {result.value} {result.currency}/{result.unit}。\n"
            f"数据来源：{source_names}。\n"
            "注意：当前数据可信度较低，可能存在偏差，建议通过官方渠道进一步核实。"
        )
    else:
        answer = (
            f"截至 {timestamp_str}，{item_name}为 {result.value} {result.currency}/{result.unit}。\n"
            f"数据来源：{source_names}，综合置信度 {result.confidence:.0%}。"
        )

    if result.warnings:
        answer += "\n提示：" + "；".join(result.warnings)

    return answer
