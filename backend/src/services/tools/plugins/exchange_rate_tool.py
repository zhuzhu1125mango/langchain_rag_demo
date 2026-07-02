"""汇率查询工具插件。

集成免费无需 Key 的 frankfurter.app 与可选 Key 的 exchangerate-api.com，
提供常见货币对的最新汇率查询，返回带来源、时间和置信度的结果。
"""

import os
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

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


# 货币名称到 ISO 代码映射
_CURRENCY_MAP = {
    "usd": "USD", "美元": "USD", "$": "USD",
    "cny": "CNY", "人民币": "CNY", "元": "CNY", "￥": "CNY",
    "eur": "EUR", "欧元": "EUR", "€": "EUR",
    "gbp": "GBP", "英镑": "GBP", "£": "GBP",
    "jpy": "JPY", "日元": "JPY", "¥": "JPY",
    "hkd": "HKD", "港币": "HKD", "港元": "HKD",
    "aud": "AUD", "澳元": "AUD",
    "cad": "CAD", "加元": "CAD",
    "chf": "CHF", "瑞士法郎": "CHF",
    "krw": "KRW", "韩元": "KRW",
    "inr": "INR", "印度卢比": "INR",
    "rub": "RUB", "卢布": "RUB",
    "sgd": "SGD", "新加坡元": "SGD",
}


class ExchangeRateTool(BaseTool):
    """查询两种货币之间的最新汇率。"""

    name = "exchange_rate"
    description = (
        "查询两种货币之间的最新汇率，支持常见货币对。"
        "当用户询问汇率、兑换、换算、X 美元兑多少人民币等问题时使用此工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "from_currency": {
                "type": "string",
                "default": "USD",
                "description": "源货币代码，如 USD、CNY、EUR",
            },
            "to_currency": {
                "type": "string",
                "default": "CNY",
                "description": "目标货币代码，如 CNY、USD、EUR",
            },
            "amount": {
                "type": "number",
                "default": 1,
                "description": "兑换金额，默认 1",
            },
            "question": {
                "type": "string",
                "description": "用户原始问题（用于自动提取货币对）",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> ToolResult:
        """执行汇率查询。"""
        question = kwargs.get("question", "")
        from_currency = kwargs.get("from_currency", "")
        to_currency = kwargs.get("to_currency", "")
        amount = kwargs.get("amount", 1)

        if not from_currency and not to_currency and question:
            parsed = _parse_currency_pair(question)
            from_currency = parsed.get("from") or "USD"
            to_currency = parsed.get("to") or "CNY"
            amount = parsed.get("amount") or amount

        from_currency = _normalize_currency(from_currency) or "USD"
        to_currency = _normalize_currency(to_currency) or "CNY"

        try:
            amount_decimal = Decimal(str(amount))
        except Exception:
            amount_decimal = Decimal("1")

        # 1. 免费数据源 frankfurter.app
        data_points: List[PriceDataPoint] = []
        frankfurt_point = await _fetch_frankfurter(from_currency, to_currency)
        if frankfurt_point:
            data_points.append(frankfurt_point)

        # 2. 可选 Key 数据源 exchangerate-api.com
        fallback_used = False
        api_key = os.getenv("FREE_EXCHANGE_RATE_API_KEY", "").strip()
        if api_key:
            er_point = await _fetch_exchangerate_api(from_currency, to_currency, api_key)
            if er_point:
                data_points.append(er_point)
        else:
            fallback_used = True
            logger.debug("未配置 FREE_EXCHANGE_RATE_API_KEY，跳过 exchangerate-api.com")

        # 3. 交叉验证与置信度计算
        validated = _validate_rate_points(
            data_points, from_currency, to_currency, fallback_used
        )

        # 4. 按金额换算最终数值
        final_value = (validated.value * amount_decimal).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )

        item_name = f"{from_currency}兑{to_currency}汇率"
        if amount_decimal != Decimal("1"):
            item_name = f"{amount_decimal} {from_currency} 兑换 {to_currency}"

        # 将 validated.value 替换为按 amount 换算后的值用于展示
        display_result = ValidatedPriceResult(
            value=final_value,
            currency=to_currency,
            unit=from_currency,
            confidence=validated.confidence,
            freshness_score=validated.freshness_score,
            authority_score=validated.authority_score,
            cross_validation_score=validated.cross_validation_score,
            sources=validated.sources,
            fallback_used=validated.fallback_used,
            warnings=validated.warnings,
        )
        answer = format_price_answer(display_result, item_name=item_name)

        sources_metadata = []
        for dp in validated.sources:
            sources_metadata.append(
                {
                    "url": dp.source_url,
                    "source": dp.source,
                    "title": f"{from_currency}/{to_currency} 汇率",
                    "page_content": f"1 {from_currency} = {dp.value} {to_currency}",
                    "confidence": validated.confidence,
                    "timestamp": dp.timestamp.isoformat() if dp.timestamp else "",
                    "data_type": "exchange_rate",
                    "unit": f"{to_currency}/{from_currency}",
                    "document_id": "",
                    "filename": self.name,
                    "chunk_index": 0,
                    "total_chunks": 1,
                }
            )

        return ToolResult(
            tool_name=self.name,
            input_arguments={
                "from_currency": from_currency,
                "to_currency": to_currency,
                "amount": str(amount_decimal),
                "question": question,
            },
            output=answer,
            success=validated.confidence >= 0.4,
            sources=sources_metadata,
        )


def _normalize_currency(value: Any) -> Optional[str]:
    """将货币名称或代码归一化为 ISO 代码。"""
    if not value:
        return None
    key = str(value).strip().lower()
    return _CURRENCY_MAP.get(key, key.upper())


def _parse_currency_pair(question: str) -> Dict[str, Any]:
    """从问题中解析源货币、目标货币和金额。"""
    result: Dict[str, Any] = {}
    if not question:
        return result

    q = question.strip()

    # 尝试匹配 "100 USD to CNY" / "100美元等于多少人民币" / "美元兑人民币"
    pattern = re.compile(
        r"(?:(\d+(?:\.\d+)?)\s*)?"
        r"([a-zA-Z]{3}|美元|人民币|欧元|日元|英镑|港币|澳元|加元|韩元|印度卢比|新加坡元|卢布|瑞士法郎|港元)"
        r"\s*(?:to|兑|兑换|换算成|等于|换|转)\s*(?:多少|几)?\s*"
        r"([a-zA-Z]{3}|美元|人民币|欧元|日元|英镑|港币|澳元|加元|韩元|印度卢比|新加坡元|卢布|瑞士法郎|港元)",
        re.IGNORECASE,
    )
    m = pattern.search(q)
    if m:
        if m.group(1):
            result["amount"] = Decimal(m.group(1))
        result["from"] = _normalize_currency(m.group(2))
        result["to"] = _normalize_currency(m.group(3))
        return result

    # 兜底：提取问题中出现的任意两种货币
    currencies = []
    # 按 key 长度降序，避免"元"误匹配"美元"/"欧元"等包含"元"的货币
    for key, code in sorted(_CURRENCY_MAP.items(), key=lambda x: -len(x[0])):
        if key in q and code not in currencies:
            currencies.append(code)
    if len(currencies) >= 1:
        result["from"] = currencies[0]
    if len(currencies) >= 2:
        result["to"] = currencies[1]
    else:
        result["to"] = "CNY"

    return result


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    """解析 ISO 8601 时间戳。"""
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(value)
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


async def _fetch_frankfurter(from_currency: str, to_currency: str) -> Optional[PriceDataPoint]:
    """从 frankfurter.app 获取汇率。

    frankfurter.app 免费、无需 Key，基于欧洲央行日度参考价，支持 200+ 货币。
    """
    url = "https://api.frankfurter.dev/v1/latest"
    params = {"from": from_currency, "to": to_currency}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        rates = data.get("rates", {})
        rate = _to_decimal(rates.get(to_currency))
        if rate is None:
            return None

        timestamp = _parse_iso_timestamp(data.get("date"))
        return PriceDataPoint(
            value=rate,
            currency=to_currency,
            unit=from_currency,
            timestamp=timestamp,
            source="frankfurter.app",
            source_url=f"https://api.frankfurter.dev/v1/latest?from={from_currency}&to={to_currency}",
            raw_data=data,
        )
    except Exception as e:
        logger.warning(f"frankfurter.app 汇率获取失败 [{from_currency}/{to_currency}]: {e}")
        return None


async def _fetch_exchangerate_api(
    from_currency: str, to_currency: str, api_key: str
) -> Optional[PriceDataPoint]:
    """从 exchangerate-api.com 获取汇率。

    免费层每月 1,500 次请求，需要 API Key，汇率通常每 24 小时更新。
    """
    url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/{from_currency}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        rates = data.get("conversion_rates", {})
        rate = _to_decimal(rates.get(to_currency))
        if rate is None:
            return None

        timestamp = _parse_iso_timestamp(data.get("time_last_update_unix"))
        return PriceDataPoint(
            value=rate,
            currency=to_currency,
            unit=from_currency,
            timestamp=timestamp,
            source="exchangerate-api.com",
            source_url=f"https://v6.exchangerate-api.com/v6/{api_key}/latest/{from_currency}",
            raw_data=data,
        )
    except Exception as e:
        logger.warning(f"exchangerate-api.com 汇率获取失败 [{from_currency}/{to_currency}]: {e}")
        return None


def _validate_rate_points(
    data_points: List[PriceDataPoint],
    from_currency: str,
    to_currency: str,
    fallback_used: bool,
) -> ValidatedPriceResult:
    """对多数据源汇率点进行交叉验证与置信度计算。"""
    if not data_points:
        return ValidatedPriceResult(
            value=Decimal("0"),
            currency=to_currency,
            unit=from_currency,
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

    fresh_scores = [freshness_score(dp.timestamp) for dp in data_points]
    avg_freshness = sum(fresh_scores) / len(fresh_scores) if fresh_scores else 0.0

    auth_scores = [authority_score(dp.source) for dp in data_points]
    avg_authority = sum(auth_scores) / len(auth_scores) if auth_scores else 0.0

    overall = compute_overall_confidence(
        consistency_score=consistency_score,
        freshness_score=avg_freshness,
        authority_score=avg_authority,
        source_count=len(data_points),
        fallback_used=fallback_used,
    )

    consensus = consensus.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    return ValidatedPriceResult(
        value=consensus,
        currency=to_currency,
        unit=from_currency,
        confidence=overall,
        freshness_score=avg_freshness,
        authority_score=avg_authority,
        cross_validation_score=consistency_score,
        sources=data_points,
        fallback_used=fallback_used,
        warnings=warnings,
    )
