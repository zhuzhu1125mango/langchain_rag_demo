"""价格类工具（金价 / 汇率）单元与端到端测试。

单元测试通过 mock httpx 避免依赖外部网络；
端到端测试默认跳过，需要配置 FREE_GOLD_API_KEY / FREE_EXCHANGE_RATE_API_KEY
或网络可访问免费 API 时手动运行。
"""

import os
import sys
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.tools.plugins.exchange_rate_tool import (
    ExchangeRateTool,
    _fetch_frankfurter,
    _normalize_currency,
    _parse_currency_pair,
    _validate_rate_points,
)
from src.services.tools.plugins.gold_price_tool import (
    GoldPriceTool,
    _extract_currency,
    _extract_metal,
    _extract_unit,
    _fetch_goldapi,
    _fetch_xaus,
    _normalize_to_unit,
    _parse_iso_timestamp,
    _round_value,
    _to_decimal,
    _validate_price_points,
)
from src.services.tools.tool_manager import ToolManager


class MockResponse:
    """模拟 httpx Response。"""

    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self
            )

    def json(self):
        return self._json


def _mock_async_client(responses):
    """生成按顺序返回responses的 AsyncClient mock。"""
    class _MockClient:
        def __init__(self, *args, **kwargs):
            self._responses = list(responses)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args, **kwargs):
            pass

        async def get(self, *args, **kwargs):
            return self._responses.pop(0)

    return _MockClient


# =============================================================================
# 1. 金价工具解析函数
# =============================================================================
class TestGoldPriceParsing:
    """金价工具参数解析测试。"""

    def test_extract_metal(self):
        """应正确提取贵金属类型。"""
        assert _extract_metal("今日金价") == "gold"
        assert _extract_metal("白银价格") == "silver"
        assert _extract_metal("铂金多少钱") == "platinum"
        assert _extract_metal("钯金行情") == "palladium"
        assert _extract_metal("天气如何") is None

    def test_extract_currency(self):
        """应正确提取货币代码。"""
        assert _extract_currency("今日金价 美元") == "USD"
        assert _extract_currency("黄金价格 人民币") == "CNY"
        assert _extract_currency("金价") is None

    def test_extract_unit(self):
        """应正确提取重量单位。"""
        assert _extract_unit("金价 每盎司") == "oz"
        assert _extract_unit("黄金价格 每克") == "gram"
        assert _extract_unit("金价") is None

    def test_normalize_to_unit(self):
        """应按重量单位归一化价格（每单位价格）。"""
        # 1 oz = 31.1034768 g，每盎司 31.1034768 元 = 每克 1 元
        per_gram = _normalize_to_unit(Decimal("31.1034768"), "oz", "gram")
        assert per_gram == Decimal("1")

        # 每克 1 元 = 每千克 1000 元
        per_kg = _normalize_to_unit(Decimal("1"), "gram", "kg")
        assert per_kg == Decimal("1000")

    def test_round_value(self):
        """应按货币精度四舍五入。"""
        assert _round_value(Decimal("780.555")) == Decimal("780.56")

    def test_parse_iso_timestamp(self):
        """应解析 ISO 时间戳。"""
        dt = _parse_iso_timestamp("2026-06-27T14:32:00Z")
        assert dt is not None
        assert dt.year == 2026

    def test_to_decimal(self):
        """应安全转换 Decimal。"""
        assert _to_decimal("123.45") == Decimal("123.45")
        assert _to_decimal(None) is None
        assert _to_decimal("invalid") is None


# =============================================================================
# 2. 金价工具数据获取与验证
# =============================================================================
class TestGoldPriceFetching:
    """金价工具数据获取与结果验证测试。"""

    @pytest.mark.asyncio
    async def test_fetch_xaus_success(self):
        """mock xaus.com 成功返回金价数据。"""
        mock_resp = MockResponse(
            {
                "xau": {"price": 780.5, "currency": "CNY", "unit": "gram"},
                "updated_at": "2026-06-27T14:32:00Z",
            }
        )
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            point = await _fetch_xaus("CNY", "gram")
        assert point is not None
        assert point.value == Decimal("780.5")
        assert point.currency == "CNY"
        assert point.source == "xaus.com"

    @pytest.mark.asyncio
    async def test_fetch_xaus_failure(self):
        """xaus.com 返回异常时应返回 None。"""
        mock_resp = MockResponse({}, status_code=500)
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            point = await _fetch_xaus("CNY", "gram")
        assert point is None

    @pytest.mark.asyncio
    async def test_fetch_goldapi_success(self):
        """mock GoldAPI.io 成功返回金价数据。"""
        mock_resp = MockResponse(
            {
                "price": 2420.0,
                "currency": "USD",
                "timestamp": 1719496320,
            }
        )
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            point = await _fetch_goldapi("XAU", "USD", "gram", "fake-key")
        assert point is not None
        assert point.currency == "USD"
        assert point.source == "goldapi.io"

    def test_validate_price_points_empty(self):
        """无数据源时置信度应为 0。"""
        result = _validate_price_points([], "CNY", "gram", fallback_used=True)
        assert result.confidence == 0.0
        assert "所有数据源均不可用" in result.warnings

    def test_validate_price_points_single(self):
        """单一数据源应能计算出非零置信度。"""
        from src.services.tools.price_data_models import PriceDataPoint

        point = PriceDataPoint(
            value=Decimal("780"),
            currency="CNY",
            unit="gram",
            timestamp=None,
            source="xaus.com",
            source_url="https://xaus.com/api/v1/spot",
        )
        result = _validate_price_points([point], "CNY", "gram", fallback_used=True)
        assert result.value == Decimal("780.00")
        assert result.confidence > 0


# =============================================================================
# 3. 金价工具 execute 入口
# =============================================================================
class TestGoldPriceToolExecute:
    """金价工具 execute 方法测试。"""

    @pytest.mark.asyncio
    @pytest.mark.integration  # execute 成功路径会写价格历史到 PostgreSQL
    async def test_execute_gold_cny_gram(self):
        """mock 数据源后 execute 应返回成功结果。"""
        mock_resp = MockResponse(
            {
                "xau": {"price": 780.5, "currency": "CNY", "unit": "gram"},
                "updated_at": "2026-06-27T14:32:00Z",
            }
        )
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            tool = GoldPriceTool()
            result = await tool.execute(question="今日金价")
        assert result.success is True
        assert "780" in result.output
        assert result.sources
        assert result.sources[0].get("data_type") == "price"

    @pytest.mark.asyncio
    async def test_execute_all_sources_fail(self):
        """所有数据源失败时 success 应为 False。"""
        mock_resp = MockResponse({}, status_code=500)
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            tool = GoldPriceTool()
            result = await tool.execute(question="今日银价")
        assert result.success is False
        assert "无法获取" in result.output


# =============================================================================
# 4. 汇率工具解析函数
# =============================================================================
class TestExchangeRateParsing:
    """汇率工具参数解析测试。"""

    def test_normalize_currency(self):
        """应正确归一化货币代码。"""
        assert _normalize_currency("美元") == "USD"
        assert _normalize_currency("人民币") == "CNY"
        assert _normalize_currency("usd") == "USD"
        assert _normalize_currency("EUR") == "EUR"
        assert _normalize_currency("") is None

    def test_parse_currency_pair_explicit(self):
        """应解析显式货币对。"""
        parsed = _parse_currency_pair("100美元兑换人民币")
        assert parsed["from"] == "USD"
        assert parsed["to"] == "CNY"
        assert parsed["amount"] == Decimal("100")

    def test_parse_currency_pair_fallback(self):
        """兜底提取问题中出现的货币。"""
        parsed = _parse_currency_pair("美元和欧元")
        assert parsed["from"] == "USD"
        assert parsed["to"] == "EUR"


# =============================================================================
# 5. 汇率工具数据获取与验证
# =============================================================================
class TestExchangeRateFetching:
    """汇率工具数据获取与结果验证测试。"""

    @pytest.mark.asyncio
    async def test_fetch_frankfurter_success(self):
        """mock frankfurter.app 成功返回汇率数据。"""
        mock_resp = MockResponse(
            {
                "rates": {"CNY": Decimal("7.25")},
                "date": "2026-06-27",
            }
        )
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            point = await _fetch_frankfurter("USD", "CNY")
        assert point is not None
        assert point.value == Decimal("7.25")
        assert point.source == "frankfurter.app"

    def test_validate_rate_points_empty(self):
        """无数据源时置信度应为 0。"""
        result = _validate_rate_points([], "USD", "CNY", fallback_used=True)
        assert result.confidence == 0.0


# =============================================================================
# 6. 汇率工具 execute 入口
# =============================================================================
class TestExchangeRateToolExecute:
    """汇率工具 execute 方法测试。"""

    @pytest.mark.asyncio
    @pytest.mark.integration  # execute 成功路径会写价格历史到 PostgreSQL
    async def test_execute_usd_to_cny(self):
        """mock 数据源后 execute 应返回成功结果。"""
        mock_resp = MockResponse(
            {
                "rates": {"CNY": Decimal("7.25")},
                "date": "2026-06-27",
            }
        )
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            tool = ExchangeRateTool()
            result = await tool.execute(question="美元兑人民币汇率")
        assert result.success is True
        assert "7.25" in result.output
        assert result.sources
        assert result.sources[0].get("data_type") == "exchange_rate"

    @pytest.mark.asyncio
    @pytest.mark.integration  # execute 成功路径会写价格历史到 PostgreSQL
    async def test_execute_with_amount(self):
        """mock 数据源后 execute 应正确换算金额。"""
        mock_resp = MockResponse(
            {
                "rates": {"CNY": Decimal("7.25")},
                "date": "2026-06-27",
            }
        )
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            tool = ExchangeRateTool()
            result = await tool.execute(question="100美元等于多少人民币")
        assert result.success is True
        assert "725" in result.output

    @pytest.mark.asyncio
    async def test_execute_all_sources_fail(self):
        """所有数据源失败时 success 应为 False。"""
        mock_resp = MockResponse({}, status_code=500)
        with patch("httpx.AsyncClient", _mock_async_client([mock_resp])):
            tool = ExchangeRateTool()
            result = await tool.execute(question="美元兑人民币汇率")
        assert result.success is False


# =============================================================================
# 7. 工具管理器自动发现
# =============================================================================
class TestPriceToolDiscovery:
    """价格/汇率工具自动发现测试。"""

    def test_tool_manager_discovers_price_tools(self):
        """ToolManager 应能自动发现金价和汇率工具。"""
        manager = ToolManager()
        manager.discover_tools()
        names = [t.name for t in manager.registry.list_tools()]
        assert "gold_price" in names
        assert "exchange_rate" in names


# =============================================================================
# 8. 端到端场景（需要外部服务 / API Key）
# =============================================================================
@pytest.mark.e2e
class TestPriceEndToEndScenarios:
    """价格/汇率端到端测试，默认跳过，传入 --run-e2e 时运行。

    依赖：xaus.com / GoldAPI.io / frankfurter.app 等网络服务。
    """

    @pytest.mark.asyncio
    async def test_gold_price_e2e(self):
        """金价查询端到端（需 xaus.com 或 GoldAPI.io 网络访问）。"""
        tool = GoldPriceTool()
        result = await tool.execute(question="今日金价")
        assert result.success is True
        assert "元/克" in result.output or "CNY/gram" in result.output

    @pytest.mark.asyncio
    async def test_exchange_rate_e2e(self):
        """汇率查询端到端（需 frankfurter.app 网络访问）。"""
        tool = ExchangeRateTool()
        result = await tool.execute(question="美元兑人民币汇率")
        assert result.success is True
        assert "USD" in result.output
        assert "CNY" in result.output
