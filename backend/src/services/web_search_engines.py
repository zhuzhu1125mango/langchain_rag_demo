"""联网搜索各引擎实现（SearXNG / Tavily / DuckDuckGo / mock）（CODE-1 拆分）。"""

import asyncio
import json
import logging
from typing import List


from .search_types import SearchResult

logger = logging.getLogger(__name__)

from src.services.web_search_types import WebSearchError, _detect_time_range

class EngineMixin:
    """多引擎搜索实现，由 WebSearchService 组合。"""

    def _search_mock(self, query: str, max_results: int, mock_file: str) -> List[SearchResult]:
        """从预置 JSON 按关键词匹配返回仿真网页；无匹配返回空（模拟无结果）。

        该路径仅由 AB_WEB_MOCK_FILE 显式启用（受控 web 仿真对拍），一旦命中
        即在 search 层直接返回，不会触达任一真实搜索引擎。
        """
        data = getattr(self, "_web_mock_data", None)
        if data is None:
            data = {"matches": []}
            try:
                with open(mock_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and isinstance(loaded.get("matches"), list):
                    data = loaded
            except (OSError, ValueError) as e:
                logger.warning(f"web mock 读取失败 {mock_file}: {e}")
            self._web_mock_data = data

        q = (query or "").strip().lower()
        if not q:
            return []
        for entry in data.get("matches", []):
            if any(kw and kw.lower() in q for kw in entry.get("keywords", [])):
                results = []
                for r in entry.get("results", [])[:max_results]:
                    results.append(SearchResult(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        content=r.get("content", ""),
                        engine="mock",
                    ))
                return results
        return []

    async def _search_searxng(self, query: str, max_results: int) -> List[SearchResult]:
        if not self.searxng_base_url:
            raise WebSearchError("SEARXNG_BASE_URL 未配置", engine="searxng")

        base_url = self.searxng_base_url.rstrip("/")
        # SearXNG language 参数仅接受单语言代码，不支持逗号分隔的多语言；
        # 使用 zh-CN 优先返回中文结果（聚合搜索仍会包含部分英文来源）
        params = {"q": query, "format": "json", "language": "zh-CN"}

        # 增强1：时效性感知搜索——根据查询词自动限定时间范围
        time_range = _detect_time_range(query)
        if time_range:
            params["time_range"] = time_range
            logger.debug(f"SearXNG 时效过滤: query='{query}', time_range={time_range}")

        try:
            response = await self._http.get(
                f"{base_url}/search", params=params, follow_redirects=True
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            # 统一包装为 WebSearchError 向上抛：此处不另行记日志，避免与
            # search_multi 的分路失败记录重复；cause 保留原始异常供上层判断。
            raise WebSearchError(f"SearXNG 请求失败: {e}", engine="searxng", cause=e)

        results = []
        for item in data.get("results", [])[:max_results]:
            url = item.get("url", "")
            if self._is_low_quality_domain(url):
                continue
            results.append(SearchResult(
                title=item.get("title", ""),
                url=url,
                content=item.get("content", ""),
                engine="searxng",
            ))
        return results

    async def _search_tavily(self, query: str, max_results: int) -> List[SearchResult]:
        try:
            from tavily import TavilyClient
        except ImportError as e:
            raise WebSearchError("tavily 未安装", engine="tavily", cause=e)

        if self._tavily_client is None:
            self._tavily_client = TavilyClient(api_key=self.api_key)

        try:
            response = await asyncio.to_thread(
                self._tavily_client.search, query, max_results=max_results
            )
        except Exception as e:
            # 同 SearXNG：包装后上抛，由 search_multi 统一记录分路失败
            raise WebSearchError(f"Tavily 请求失败: {e}", engine="tavily", cause=e)

        results = []
        for item in response.get("results", []):
            url = item.get("url", "")
            if self._is_low_quality_domain(url):
                continue
            results.append(SearchResult(
                title=item.get("title", ""),
                url=url,
                content=item.get("content", ""),
                engine="tavily",
            ))
        return results

    async def _search_duckduckgo(self, query: str, max_results: int) -> List[SearchResult]:
        try:
            from duckduckgo_search import DDGS
        except ImportError as e:
            raise WebSearchError("duckduckgo-search 未安装", engine="duckduckgo", cause=e)

        if self._ddgs_client is None:
            self._ddgs_client = DDGS()

        try:
            items = await asyncio.to_thread(
                lambda: list(self._ddgs_client.text(query, max_results=max_results))
            )
        except Exception as e:
            # 同 SearXNG：包装后上抛，由 search_multi 统一记录分路失败
            raise WebSearchError(f"DuckDuckGo 请求失败: {e}", engine="duckduckgo", cause=e)

        results = []
        for item in items:
            url = item.get("href", "")
            if self._is_low_quality_domain(url):
                continue
            results.append(SearchResult(
                title=item.get("title", ""),
                url=url,
                content=item.get("body", ""),
                engine="duckduckgo",
            ))
        return results

    # ------------------------------------------------------------------
    # 网页正文提取
    # ------------------------------------------------------------------
