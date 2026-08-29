"""天气查询工具插件。

集成 Open-Meteo 免费天气 API，提供比网页搜索更可靠、实时的天气数据。
"""

from typing import Any, Optional

from src.services.tools.tool_manager import BaseTool, ToolResult
from src.services.tools.plugins import _weather_impl as weather_tool_module


class WeatherTool(BaseTool):
    """查询指定城市的实时天气和短期预报。"""

    name = "weather_query"
    description = (
        "查询指定城市或地区的当前天气、气温、湿度、风速和今日预报。"
        "当用户询问某个城市天气、气温、是否下雨等问题时使用此工具。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名，例如：北京、上海、吕梁",
            },
            "question": {
                "type": "string",
                "description": "用户原始问题（可选，用于自动提取城市名）",
            },
        },
        "required": ["city"],
    }

    async def execute(self, **kwargs) -> ToolResult:
        city = kwargs.get("city", "")
        question = kwargs.get("question", "")

        # 如果未直接传入城市，尝试从问题中提取
        if not city and question:
            city = weather_tool_module._extract_city_name(question) or ""

        if not city:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error="未提供城市名，且无法从问题中识别城市。",
            )

        try:
            data = await weather_tool_module.get_weather_by_city(city)
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"天气查询失败: {e}",
            )

        if not data:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"未能获取 {city} 的天气信息。",
            )

        text = weather_tool_module.format_weather(data)
        return ToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=text,
            sources=[{
                "url": "https://open-meteo.com/",
                "source": "open-meteo",
                "title": "Open-Meteo 实时天气",
                "page_content": text,
                "document_id": "",
                "filename": "weather_query",
                "chunk_index": 0,
                "total_chunks": 1,
            }],
        )
