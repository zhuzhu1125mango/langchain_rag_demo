"""贵金属价格查询工具插件。

集成多个免费/可选 Key 的贵金属数据源，优先使用无需 API Key 的 xaus.com，
可选使用 GoldAPI.io 支持更多金属（银、铂、钯）。
对多源返回的数值进行交叉验证与置信度评分，避免模型直接使用低质量网页数据。
"""

import os
import re
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

import httpx
import logging

from src.services.tools.price_data_models import (
    PriceDataPoint,
    ValidatedPriceResult,
    authority_score,
    compute_overall_confidence,
    cross_validate_numeric_values,
    format_price_answer,
    freshness_score,
)
from src.services.tools.tool_manager import BaseTool, ToolResult

logger = logging.getLogger("rag_system")


# 金属代码映射
_METAL_CODES = {
    "gold": "XAU",
    "silver": "XAG",
    "platinum": "XPT",
    "palladium": "XPD",
    "金": "XAU",
    "黄金": "XAU",
    "白银": "XAG",
    "银": "XAG",
    "铂金": "XPT",
    "钯金": "XPD",
}

# 单位映射
_UNIT_MAP = {
    "oz": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "盎司": "oz",
    "gram": "gram",
    "grams": "gram",
    "克": "gram",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "千克": "kg",
}

# 货币映射
_CURRENCY_MAP = {
    "usd": "USD",
    "美元": "USD",
    "$": "USD",
    "cny": "CNY",
    "人民币": "CNY",
    "元": "CNY",
    "￥": "CNY",
    "eur": "EUR",
    "欧元": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "英镑": "GBP",
    "£": "GBP",
    "jpy": "JPY",
    "日元": "JPY",
    "¥": "JPY",
}

# 金衡盎司转克常量
_TROY_OUNCE_TO_GRAM = Decimal("31.1034768")


class GoldPriceTool(BaseTool):
    """查询黄金、白银等贵金属实时价格。"""

    name = "gold_price"
    description = (
        "查询黄金、白银、铂金、钯金等贵金属的实时价格。"
        "当用户询问金价、银价、黄金价格、白银价格、贵金属价格等问题时使用此工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "metal": {
                "type": "string",
                "enum": ["gold", "silver", "platinum", "palladium"],
                "default": "gold",
                "description": "贵金属类型",
            },
            "currency": {
                "type": "string",
                "default": "CNY",
                "description": "货币代码，如 CNY、USD、EUR",
            },
            "unit": {
                "type": "string",
                "enum": ["oz", "gram", "kg"],
                "default": "gram",
                "description": "重量单位：oz（盎司）、gram（克）、kg（千克）",
            },
            "question": {
                "type": "string",
                "description": "用户原始问题（用于自动提取 metal/currency/unit）",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> ToolResult:
        """执行贵金属价格查询。"""
        question = kwargs.get("question", "")
        metal = kwargs.get("metal", "")
        currency = kwargs.get("currency", "")
        unit = kwargs.get("unit", "")

        # 如果未传入参数，尝试从问题中解析
        if not metal and question:
            metal = _extract_metal(question) or "gold"
        if not currency:
            currency = _extract_currency(question) or "CNY"
        if not unit:
            unit = _extract_unit(question) or "gram"

        metal = (metal or "gold").lower()
        currency = (currency or "CNY").upper()
        unit = _UNIT_MAP.get((unit or "gram").lower(), "gram")

        metal_code = _METAL_CODES.get(metal, "XAU")

        # 1. 优先使用免费数据源 xaus.com（仅支持黄金）
        data_points: List[PriceDataPoint] = []
        fallback_used = False

        if metal_code == "XAU":
            xaus_point = await _fetch_xaus(currency, unit)
            if xaus_point:
                data_points.append(xaus_point)

        # 2. 使用 GoldAPI.io（可选 Key，支持多种贵金属）
        api_key = os.getenv("FREE_GOLD_API_KEY", "").strip()
        if api_key:
            goldapi_point = await _fetch_goldapi(metal_code, currency, unit, api_key)
            if goldapi_point:
                data_points.append(goldapi_point)
        else:
            fallback_used = True
            logger.debug("未配置 FREE_GOLD_API_KEY，跳过 GoldAPI.io 数据源")

        # 3. 交叉验证与置信度计算
        validated = _validate_price_points(data_points, currency, unit, fallback_used)

        item_name = _metal_name(metal_code)
        answer = format_price_answer(validated, item_name=item_name)

        sources_metadata = []
        for dp in validated.sources:
            sources_metadata.append(
                {
                    "url": dp.source_url,
                    "source": dp.source,
                    "title": f"{item_name}实时价格",
                    "page_content": f"{dp.value} {dp.currency}/{dp.unit}",
                    "confidence": validated.confidence,
                    "timestamp": dp.timestamp.isoformat() if dp.timestamp else "",
                    "data_type": "price",
                    "unit": f"{dp.currency}/{dp.unit}",
                    "document_id": "",
                    "filename": self.name,
                    "chunk_index": 0,
                    "total_chunks": 1,
                }
            )

        return ToolResult(
            tool_name=self.name,
            input_arguments={
                "metal": metal,
                "currency": currency,
                "unit": unit,
                "question": question,
            },
            output=answer,
            success=validated.confidence >= 0.4,
            sources=sources_metadata,
        )


def _extract_metal(question: str) -> Optional[str]:
    """从问题中提取贵金属类型。"""
    if not question:
        return None
    q = question.lower()
    # 优先匹配 silver/platinum/palladium，避免 gold 误匹配
    for key in ("白银", "银价", "银", "silver", "铂金", "platinum", "钯金", "palladium"):
        if key in q:
            return {
                "白银": "silver", "银价": "silver", "银": "silver", "silver": "silver",
                "铂金": "platinum", "platinum": "platinum",
                "钯金": "palladium", "palladium": "palladium",
            }[key]
    for key in ("金价", "黄金价格", "黄金", "gold"):
        if key in q:
            return "gold"
    return None


def _extract_currency(question: str) -> Optional[str]:
    """从问题中提取货币代码。"""
    if not question:
        return None
    q = question
    # 优先匹配完整词
    for key, code in _CURRENCY_MAP.items():
        if key in q:
            return code
    return None


def _extract_unit(question: str) -> Optional[str]:
    """从问题中提取重量单位。"""
    if not question:
        return None
    q = question.lower()
    for key, unit in _UNIT_MAP.items():
        if key in q:
            return unit
    return None


def _metal_name(metal_code: str) -> str:
    """金属代码转中文名称。"""
    mapping = {
        "XAU": "黄金",
        "XAG": "白银",
        "XPT": "铂金",
        "XPD": "钯金",
    }
    return mapping.get(metal_code.upper(), "贵金属")


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """解析 ISO 8601 时间戳。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _to_decimal(value: Any) -> Optional[Decimal]:
    """安全转换为 Decimal。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _round_value(value: Decimal, currency: str = "CNY") -> Decimal:
    """根据货币习惯精度四舍五入。"""
    if currency == "JPY":
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _normalize_to_unit(value: Decimal, from_unit: str, to_unit: str) -> Decimal:
    """将贵金属价格按重量单位归一化。

    假设输入 value 为 per-from-unit 价格。
    """
    from_unit = _UNIT_MAP.get(from_unit.lower(), from_unit.lower())
    to_unit = _UNIT_MAP.get(to_unit.lower(), to_unit.lower())

    if from_unit == to_unit:
        return value

    # 先统一到 gram
    if from_unit == "oz":
        per_gram = value / _TROY_OUNCE_TO_GRAM
    elif from_unit == "kg":
        per_gram = value / Decimal("1000")
    elif from_unit == "gram":
        per_gram = value
    else:
        return value

    if to_unit == "gram":
        return per_gram
    if to_unit == "oz":
        return per_gram * _TROY_OUNCE_TO_GRAM
    if to_unit == "kg":
        return per_gram * Decimal("1000")
    return per_gram


async def _fetch_xaus(currency: str, unit: str) -> Optional[PriceDataPoint]:
    """从 xaus.com 获取金价数据。

    xaus.com 免费、无需 Key，支持 XAU/USD 及 30+ 货币、盎司/克/千克单位转换。
    """
    url = "https://xaus.com/api/v1/spot"
    params = {"currency": currency, "unit": unit}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        xau = data.get("xau", {})
        price = _to_decimal(xau.get("price"))
        if price is None:
            return None

        timestamp = _parse_iso_timestamp(data.get("updated_at"))
        return PriceDataPoint(
            value=price,
            currency=(xau.get("currency") or currency).upper(),
            unit=_UNIT_MAP.get((xau.get("unit") or unit).lower(), unit),
            timestamp=timestamp,
            source="xaus.com",
            source_url="https://xaus.com/api/v1/spot",
            raw_data=data,
        )
    except Exception as e:
        logger.warning(f"xaus.com 金价获取失败: {e}")
        return None


async def _fetch_goldapi(
    metal_code: str, currency: str, unit: str, api_key: str
) -> Optional[PriceDataPoint]:
    """从 GoldAPI.io 获取贵金属数据。

    GoldAPI.io 免费层每月 50 次请求，需要 API Key，支持 XAU/XAG/XPT/XPD。
    """
    url = f"https://www.goldapi.io/api/{metal_code}/{currency}"
    headers = {"x-access-token": api_key}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # GoldAPI.io 默认返回每盎司价格，字段名为 price
        price = _to_decimal(data.get("price"))
        if price is None:
            return None

        # 归一化到请求单位
        price = _normalize_to_unit(price, "oz", unit)

        timestamp = _parse_iso_timestamp(data.get("timestamp"))
        return PriceDataPoint(
            value=price,
            currency=currency.upper(),
            unit=unit,
            timestamp=timestamp,
            source="goldapi.io",
            source_url=f"https://www.goldapi.io/api/{metal_code}/{currency}",
            raw_data=data,
        )
    except Exception as e:
        logger.warning(f"GoldAPI.io 贵金属获取失败 [{metal_code}/{currency}]: {e}")
        return None


def _validate_price_points(
    data_points: List[PriceDataPoint],
    currency: str,
    unit: str,
    fallback_used: bool,
) -> ValidatedPriceResult:
    """对多数据源价格点进行交叉验证与置信度计算。"""
    if not data_points:
        return ValidatedPriceResult(
            value=Decimal("0"),
            currency=currency,
            unit=unit,
            confidence=0.0,
            freshness_score=0.0,
            authority_score=0.0,
            cross_validation_score=0.0,
            sources=[],
            fallback_used=fallback_used,
            warnings=["所有数据源均不可用"],
        )

    values = [dp.value for dp in data_points]
    consensus, consistency_score, warnings = cross_validate_numeric_values(values)

    # 时间新鲜度取平均值
    fresh_scores = [freshness_score(dp.timestamp) for dp in data_points]
    avg_freshness = sum(fresh_scores) / len(fresh_scores) if fresh_scores else 0.0

    # 权威性取平均值
    auth_scores = [authority_score(dp.source) for dp in data_points]
    avg_authority = sum(auth_scores) / len(auth_scores) if auth_scores else 0.0

    overall = compute_overall_confidence(
        consistency_score=consistency_score,
        freshness_score=avg_freshness,
        authority_score=avg_authority,
        source_count=len(data_points),
        fallback_used=fallback_used,
    )

    # 对共识值按目标货币精度四舍五入
    consensus = _round_value(consensus, currency)

    return ValidatedPriceResult(
        value=consensus,
        currency=currency,
        unit=unit,
        confidence=overall,
        freshness_score=avg_freshness,
        authority_score=avg_authority,
        cross_validation_score=consistency_score,
        sources=data_points,
        fallback_used=fallback_used,
        warnings=warnings,
    )
