"""端到端验证脚本。

验证天气、时间、联网搜索等工具在真实环境下的可用性。
运行方式：
    cd backend && uv run python tests/evaluation/verify_e2e.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.tools.plugins.datetime_tool import DateTimeTool
from src.services.tools.plugins.weather_tool import WeatherTool
from src.services.web_search_service import WebSearchService


async def verify_datetime():
    """验证时间工具。"""
    print("=" * 60)
    print("验证时间工具")
    result = await DateTimeTool().execute()
    print(f"success={result.success}")
    print(f"output={result.output}")
    assert result.success, "时间工具执行失败"
    assert "202" in result.output, "输出不包含年份"
    print("时间工具验证通过")


async def verify_weather():
    """验证天气工具。"""
    print("=" * 60)
    print("验证天气工具")
    result = await WeatherTool().execute(question="吕梁今天天气")
    print(f"success={result.success}")
    print(f"output={result.output}")
    assert result.success, "天气工具执行失败"
    assert "°C" in result.output or "温度" in result.output, "输出不包含温度"
    print("天气工具验证通过")


async def verify_web_search():
    """验证联网搜索服务。"""
    print("=" * 60)
    print("验证联网搜索服务")
    service = WebSearchService()
    results = await service.search("2026 年新闻", max_results=3)
    print(f"返回结果数: {len(results)}")
    for r in results:
        print(f"- {r.title[:40]} | {r.url[:60]}")
    assert len(results) > 0, "联网搜索未返回结果"
    print("联网搜索服务验证通过")


async def main():
    await verify_datetime()
    await verify_weather()
    try:
        await verify_web_search()
    except Exception as e:
        print(f"联网搜索验证失败（可能 SearXNG 未启动）: {e}")


if __name__ == "__main__":
    asyncio.run(main())
