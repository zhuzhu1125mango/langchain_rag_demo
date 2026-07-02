"""系统时间工具插件。"""

from typing import Any

from src.services.tools.datetime_tool import (
    build_datetime_answer,
    get_current_datetime,
    is_datetime_question,
)
from src.services.tools.tool_manager import BaseTool, ToolResult


class DateTimeTool(BaseTool):
    """获取系统当前日期与时间，用于回答时间/日期类问题。"""

    name = "get_current_time"
    description = (
        "获取系统当前日期与时间（含星期、时区）。"
        "用于回答『几点了』『今天几号』『今天星期几』等时间/日期类问题，"
        "无需联网搜索即可给出精确答案。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "时区名称，默认 Asia/Shanghai",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> ToolResult:
        timezone = kwargs.get("timezone") or "Asia/Shanghai"
        try:
            dt = get_current_datetime(timezone)
            output = (
                f"当前日期: {dt['date']}\n"
                f"星期: {dt['weekday']}\n"
                f"时间: {dt['time']}\n"
                f"ISO: {dt['iso']}\n"
                f"时区: {dt['timezone']}"
            )
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                output=output,
            )
        except Exception as e:
            return ToolResult(
                tool_name=self.name,
                input_arguments=kwargs,
                success=False,
                error=f"获取时间失败: {e}",
            )
