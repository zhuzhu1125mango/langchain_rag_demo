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
import jieba.posseg as pseg
import logging

logger = logging.getLogger("rag_system")


# 模块级共享 HTTP 客户端：懒加载复用连接，避免每次请求新建+关闭的 TCP/TLS 握手开销
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client


# Open-Meteo 地理编码 API：将城市名解析为经纬度
OPEN_METEO_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
# Open-Meteo 预报 API：获取当前天气和预报
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 句首时间前缀：剥除后再提取城市，避免被并入城市名（"今天上海" -> "今天上"）
_TIME_PREFIX_RE = re.compile(r"^(今天|明天|后天|昨天|现在|当前|今晚|明晚|今早|明早){1,2}")

# 诊断/故障类问题标记：含这些词时（如"服务器温度过高怎么办"）不当作天气问法
_DIAGNOSTIC_RE = re.compile(
    r"(过高|过低|偏高|偏低|怎么办|怎么处理|如何处理|如何解决|报警|告警|故障|异常|超标|太高|太低)"
)

# 技术语境温度：复合名词（工作温度/CPU 温度/机身温度等）永远不是天气问法，
# 直接排除（裸"温度/气温"不加守卫，避免误伤"上海温度范围"等真实问法，
# 由 _is_place_name 地名校验兜底拦截垃圾提取）
_TECH_TEMPERATURE_RE = re.compile(
    r"(?:工作|存储|运行|CPU|GPU|芯片|机身|核心|探头|传感器|设备|服务器|电脑|手机|电池|进水|回水|排风)温度"
)

# 常见中国城市白名单：兜住 jieba 词性错标的地名（如"吕梁"被标 nr 人名）。
# 覆盖直辖市/省会/计划单列市/热门城市；未收录的小城市仍可靠词性校验放行。
_KNOWN_CITIES = frozenset({
    "北京", "上海", "天津", "重庆",
    "石家庄", "太原", "呼和浩特", "沈阳", "长春", "哈尔滨", "南京", "杭州", "合肥",
    "福州", "南昌", "济南", "郑州", "武汉", "长沙", "广州", "南宁", "海口",
    "成都", "贵阳", "昆明", "拉萨", "西安", "兰州", "西宁", "银川", "乌鲁木齐",
    "台北", "高雄", "香港", "澳门",
    "深圳", "大连", "青岛", "宁波", "厦门", "苏州", "无锡", "常州", "佛山",
    "东莞", "珠海", "中山", "惠州", "湛江", "三亚", "桂林", "柳州", "丽江",
    "大理", "温州", "泉州", "烟台", "潍坊", "淄博", "济南", "洛阳", "南通",
    "徐州", "扬州", "泰州", "嘉兴", "绍兴", "金华", "台州", "保定", "唐山",
    "包头", "鄂尔多斯", "大同", "延吉", "秦皇岛", "承德", "张家界", "黄山",
    "九江", "赣州", "绵阳", "遵义",
    # 山西省地级市（吕梁曾被 jieba 错标为人名）
    "吕梁", "太原", "大同", "阳泉", "长治", "晋城", "朔州", "晋中", "运城",
    "忻州", "临汾",
})


def _is_place_name(text: str) -> bool:
    """判断提取结果是否为地名：白名单命中，或分词含地名词性（ns）。

    排除把任意名词短语当城市名（如"智星 X1 的工作"被惰性匹配提取出来，
    导致技术问法误路由到天气工具）。
    """
    if not text:
        return False
    if text in _KNOWN_CITIES:
        return True
    return any(flag.startswith("ns") for _, flag in pseg.lcut(text))


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

    # 排除诊断/故障类问题（"服务器温度过高怎么办"、"温度偏低"）：
    # 这类含"温度/气温"但非真实天气问法，不应路由到天气工具。
    # 同时排除技术语境复合温度词（"工作温度范围"、"CPU 温度"等设备问法）。
    if _DIAGNOSTIC_RE.search(q) or _TECH_TEMPERATURE_RE.search(q):
        return None

    # 先剥离句首时间前缀（今天上海天气 -> 上海天气），
    # 避免惰性匹配把时间并进城市（"今天上"）。
    q = _TIME_PREFIX_RE.sub("", q)

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
                # 地名校验：非地名短语（"智星 X1 的工作"等技术问法残片）
                # 不作为城市，继续尝试后续模式
                if city and _is_place_name(city):
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
        client = _get_http_client()
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
        client = _get_http_client()
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
