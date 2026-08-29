"""天气查询实现（供插件与意图路由共用）。

集成 Open-Meteo 免费天气 API，提供比 SearXNG 网页抓取更可靠、实时的天气数据。
Open-Meteo 无需 API key，支持城市名地理编码，适合离线/低成本部署场景。

本模块只含实现逻辑，不含 BaseTool 插件类；插件定义见同包 weather_tool.py。
"""
import asyncio
import re
from datetime import datetime
from typing import Optional, Dict, Any

import httpx
import logging

logger = logging.getLogger("rag_system")


# Open-Meteo 地理编码 API：将城市名解析为经纬度
OPEN_METEO_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
# Open-Meteo 预报 API：获取当前天气和预报
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def _extract_city_name(question: str) -> Optional[str]:
    """从用户问题中提取中国城市名。

    支持常见问法：
    - "北京天气"、"北京今天天气"、"北京明天天气"
    - "上海市天气怎么样"
    - "今天上海天气"

    Args:
        question: 用户原始问题。

    Returns:
        str | None: 提取到的城市名，未识别则返回 None。
    """
    if not question:
        return None

    q = question.strip()

    # 常见模式：{城市}天气 / 天气{城市} / {城市}今天天气 / 今天{城市}天气
    # 同时支持气温、温度、下雪、下雨等天气相关词
    patterns = [
        r"(.{2,10}?)(?:市|县|区)?(?:今天|明天|后天|未来一周|一周|7天|15天)?(?:天气|气温|温度|下雪|下雨|降雨|降雪)",
        r"(?:今天|明天|后天|现在|当前)?(.{2,10}?)(?:市|县|区)?(?:天气|气温|温度|下雪|下雨|降雨|降雪)",
        r"(?:天气|气温|温度|下雪|下雨|降雨|降雪)(?:怎么样|如何)?(.{2,10}?)(?:市|县|区)?",
    ]
    for pat in patterns:
        match = re.search(pat, q)
        if match:
            city = match.group(1).strip()
            # 过滤掉常见前缀词，并去除连接词/助词后缀
            if city and city not in ("今天", "明天", "后天", "现在", "当前", "未来"):
                city = re.sub(r"^(的|是|在|有|为)", "", city).strip()
                city = re.sub(r"(的|是|在|有|为)$", "", city).strip()
                if city:
                    return city

    return None


async def _geocode_city(city: str) -> Optional[Dict[str, Any]]:
    """通过 Open-Meteo 地理编码 API 将城市名解析为经纬度和时区。

    Args:
        city: 城市名（如"吕梁"、"北京"）。

    Returns:
        dict | None: 包含 latitude, longitude, name, country, timezone 的字典。
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                OPEN_METEO_GEO_URL,
                params={
                    "name": city,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            if not results:
                logger.warning(f"Open-Meteo 未找到城市: {city}")
                return None
            return results[0]
    except Exception as e:
        logger.warning(f"Open-Meteo 地理编码失败 [{city}]: {e}")
        return None


async def get_weather_by_city(city: str) -> Optional[Dict[str, Any]]:
    """获取指定城市的当前天气和今日预报。

    Args:
        city: 城市名（如"吕梁"）。

    Returns:
        dict | None: 天气数据字典，失败返回 None。
    """
    geo = await _geocode_city(city)
    if not geo:
        return None

    lat = geo.get("latitude")
    lon = geo.get("longitude")
    timezone = geo.get("timezone", "auto")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": [
                        "temperature_2m",
                        "relative_humidity_2m",
                        "apparent_temperature",
                        "weather_code",
                        "wind_speed_10m",
                        "wind_direction_10m",
                        "pressure_msl",
                    ],
                    "daily": [
                        "weather_code",
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "sunrise",
                        "sunset",
                        "precipitation_sum",
                    ],
                    "timezone": timezone,
                    "forecast_days": 3,
                    "language": "zh",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "city": city,
                "resolved_name": geo.get("name"),
                "country": geo.get("country"),
                "latitude": lat,
                "longitude": lon,
                "timezone": timezone,
                "current": data.get("current", {}),
                "daily": data.get("daily", {}),
                "units": data.get("current_units", {}),
                "fetched_at": datetime.now().isoformat(),
            }
    except Exception as e:
        logger.warning(f"Open-Meteo 天气查询失败 [{city}]: {e}")
        return None


# WMO 天气代码转中文描述
_WMO_WEATHER_CODES: Dict[int, str] = {
    0: "晴",
    1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "中雨", 55: "大雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨伴冰雹", 99: "强雷雨伴冰雹",
}


def _weather_code_to_text(code: Optional[int]) -> str:
    if code is None:
        return "未知"
    # 统一英文描述为中文
    mapping = {
        0: "晴",
        1: "晴间多云",
        2: "多云",
        3: "阴",
        45: "雾",
        48: "雾凇",
        51: "毛毛雨",
        53: "中雨",
        55: "大雨",
        56: "冻毛毛雨",
        57: "强冻毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "冻雨",
        67: "强冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "雪粒",
        80: "阵雨",
        81: "强阵雨",
        82: "暴雨",
        85: "阵雪",
        86: "强阵雪",
        95: "雷雨",
        96: "雷雨伴冰雹",
        99: "强雷雨伴冰雹",
    }
    return mapping.get(code, f"天气代码 {code}")


def format_weather(data: Optional[Dict[str, Any]]) -> str:
    """将 Open-Meteo 天气数据格式化为易读文本。

    Args:
        data: get_weather_by_city 返回的天气数据。

    Returns:
        str: 格式化后的天气文本，失败返回空字符串。
    """
    if not data:
        return ""

    city = data.get("resolved_name") or data.get("city", "未知城市")
    current = data.get("current", {})
    daily = data.get("daily", {})

    temp = current.get("temperature_2m")
    apparent = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind_speed = current.get("wind_speed_10m")
    pressure = current.get("pressure_msl")
    weather_text = _weather_code_to_text(current.get("weather_code"))
    unit_temp = data.get("units", {}).get("temperature_2m", "°C")
    unit_wind = data.get("units", {}).get("wind_speed_10m", "km/h")
    unit_pressure = data.get("units", {}).get("pressure_msl", "hPa")

    lines = [f"{city}当前天气：{weather_text}"]
    if temp is not None:
        lines.append(f"当前气温：{temp}{unit_temp}")
    if apparent is not None:
        lines.append(f"体感温度：{apparent}{unit_temp}")
    if humidity is not None:
        lines.append(f"相对湿度：{humidity}%")
    if wind_speed is not None:
        lines.append(f"风速：{wind_speed}{unit_wind}")
    if pressure is not None:
        lines.append(f"气压：{pressure}{unit_pressure}")

    # 今日最高/最低气温
    daily_dates = daily.get("time", [])
    daily_max = daily.get("temperature_2m_max", [])
    daily_min = daily.get("temperature_2m_min", [])
    daily_code = daily.get("weather_code", [])
    if daily_dates and daily_max and daily_min:
        lines.append(
            f"今日（{daily_dates[0]}）预报：{daily_max[0]}{unit_temp} / {daily_min[0]}{unit_temp}，"
            f"{_weather_code_to_text(daily_code[0] if daily_code else None)}"
        )

    return "\n".join(lines)


async def fetch_weather_for_question(question: str) -> Optional[str]:
    """根据用户问题自动提取城市并返回天气文本。

    Args:
        question: 用户问题（如"吕梁今天天气"）。

    Returns:
        str | None: 天气文本，未识别城市或查询失败返回 None。
    """
    city = _extract_city_name(question)
    if not city:
        return None
    data = await get_weather_by_city(city)
    if not data:
        return None
    return format_weather(data)
