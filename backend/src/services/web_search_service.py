"""联网搜索服务（CODE-1 拆分后为组合类：引擎 + 正文 + 缓存 + 上下文编排）。"""

import asyncio
import hashlib
import json
import logging
import os
from typing import List, Dict, Optional

import httpx

from src.config import settings
from src.utils.safe_http import create_safe_async_client
from .search_postprocessor import SearchPostprocessor
from .search_types import SearchResult

logger = logging.getLogger(__name__)

from src.services.web_search_content import ContentMixin
from src.services.web_search_reranker import WebReranker
from src.services.web_search_rewriter import SearchQueryRewriter
from src.services.web_search_types import WebContent, WebSearchError
from src.services.web_search_engines import EngineMixin

class WebSearchService(EngineMixin, ContentMixin):
    """联网搜索服务统一入口，集成多搜索引擎、Query 改写、重排与缓存。"""

    def __init__(self, llm=None, cache_service=None):
        self.search_provider = settings.search.SEARCH_PROVIDER.lower()
        self.api_key = settings.search.SEARCH_API_KEY or ""
        self.searxng_base_url = settings.search.SEARXNG_BASE_URL
        self.searxng_timeout = settings.search.SEARXNG_TIMEOUT
        self.fetch_timeout = settings.search.SEARCH_FETCH_TIMEOUT
        self.max_fetch = settings.search.SEARCH_MAX_FETCH
        self.min_content_length = settings.search.SEARCH_MIN_CONTENT_LENGTH
        self.max_results = settings.search.SEARCH_MAX_RESULTS
        # 增强3：注入 LLM 上下文的最终结果条数（经重排后）
        self.context_results = settings.search.SEARCH_CONTEXT_RESULTS
        self.cache_ttl = settings.search.SEARCH_CACHE_TTL
        self.content_cache_ttl = settings.search.SEARCH_CONTENT_CACHE_TTL

        self.llm = llm
        self.cache_service = cache_service
        self.query_rewriter = SearchQueryRewriter(llm=llm, num_queries=settings.search.SEARCH_NUM_QUERIES)
        # 新版 QueryRewriter：支持多轮上下文补全，规则优先 + LLM fallback
        self.query_rewriter_v2 = None
        try:
            from .query_rewriter import QueryRewriter
            # 未显式传入 LLM 时使用配置中的 FAST_LLM_MODEL_NAME 默认模型
            self.query_rewriter_v2 = QueryRewriter(llm=llm) if llm else QueryRewriter()
        except Exception as e:
            logger.warning("QueryRewriter v2 加载失败，回退到旧版改写器：%s", e)
        self.reranker = WebReranker()
        self.postprocessor = SearchPostprocessor()

        self._ddgs_client = None
        self._tavily_client = None
        # Persistent HTTP 客户端：复用 TCP/TLS 连接，避免每次请求新建+关闭的握手开销。
        # follow_redirects=False（安全 W1-5）：重定向目标可能是内网/元数据地址，
        # 自动跟随会绕过入口 SSRF 校验；正文抓取改用 _fetch_html 逐跳校验。
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(self.fetch_timeout),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RAG-Bot/1.0)"},
        )
        # SEC-9：外部网页抓取使用 IP 固定 transport，避免 DNS rebinding；
        # SearXNG 等内部/受信服务继续走普通客户端。
        self._safe_http = create_safe_async_client(
            timeout=httpx.Timeout(self.fetch_timeout),
            follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RAG-Bot/1.0)"},
        )
        # 重定向逐跳校验的跳数上限
        self.max_redirect_hops = max(1, int(settings.search.SEARCH_FETCH_MAX_REDIRECTS or 5))
        self.max_response_bytes = max(1024, int(settings.search.SEARCH_FETCH_MAX_BYTES or 2_000_000))
        self.allowed_content_types = {
            item.strip().lower()
            for item in (settings.search.SEARCH_FETCH_ALLOWED_CONTENT_TYPES or "").split(",")
            if item.strip()
        }

    def _cache_key(self, prefix: str, text: str) -> str:
        hash_value = hashlib.md5(text.encode("utf-8")).hexdigest()
        return f"{prefix}:{hash_value}"

    async def _get_cache(self, key: str):
        if not self.cache_service:
            return None
        try:
            return await self.cache_service.get(key)
        except Exception as e:
            logger.debug(f"读取缓存失败 {key}: {e}")
            return None

    async def _set_cache(self, key: str, value, expire: int):
        if not self.cache_service:
            return
        try:
            await self.cache_service.set(key, value, expire=expire)
        except Exception as e:
            logger.debug(f"写入缓存失败 {key}: {e}")

    async def search(self, query: str, max_results: Optional[int] = None) -> List[SearchResult]:
        """执行单 Query 搜索，按配置路由到对应搜索引擎。"""
        max_results = max_results or self.max_results

        # 受控 web 仿真：设 AB_WEB_MOCK_FILE 时从预置文件返回匹配片段，完全绕开网络，
        # 用于 plan on/off 对拍排除 SearXNG 反爬噪声；不设则走真实搜索（生产行为不变）。
        mock_file = os.environ.get("AB_WEB_MOCK_FILE")
        if mock_file:
            return self._search_mock(query, max_results, mock_file)

        if self.search_provider == "searxng":
            return await self._search_searxng(query, max_results)
        if self.search_provider == "tavily":
            return await self._search_tavily(query, max_results)
        return await self._search_duckduckgo(query, max_results)

    async def search_multi(
        self, query: str, conversation_context: Optional[List[Dict]] = None
    ) -> List[SearchResult]:
        """多 Query 并行搜索，结果按 URL 去重；失败时聚合错误信息。

        天气等强实时数据查询应通过独立的 weather_query 工具获取，
        不再在搜索服务内部硬编码注入，避免单一职责混乱。

        Args:
            query: 搜索查询（已通过 enhance_context 做过指代消解）。
            conversation_context: 可选的多轮对话上下文，供新版 QueryRewriter
                                  做代词/省略实体补全。为 None 时回退到旧版改写器。
        """
        # 优先使用新版 QueryRewriter（支持多轮上下文补全）
        if self.query_rewriter_v2 is not None and conversation_context:
            queries = await self.query_rewriter_v2.rewrite(
                query, conversation_context=conversation_context
            )
        else:
            queries = await self.query_rewriter.rewrite(query)
        logger.info(f"多角度搜索 queries: {queries}")

        if not queries:
            queries = [query]

        tasks = [self.search(q, max_results=self.max_results) for q in queries]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        seen = set()
        merged = []
        errors = []

        for q, result in zip(queries, results_list):
            if isinstance(result, Exception):
                errors.append(f"query='{q}': {result}")
                continue
            for r in result:
                if r.url in seen:
                    continue
                seen.add(r.url)
                merged.append(r)

        if not merged and errors:
            raise WebSearchError(
                "所有搜索查询均失败: " + "; ".join(errors),
                engine=self.search_provider,
            )
        if errors:
            logger.warning(f"部分搜索查询失败: {'; '.join(errors)}")

        # 阶段二：搜索后处理（去重、评分、重排序）
        processed = self.postprocessor.process(merged, top_k=self.context_results)
        return processed

    async def build_search_context_with_sources(
        self, query: str, max_results: Optional[int] = None
    ) -> tuple[str, List[Dict]]:
        """
        构建联网搜索上下文，并返回原始来源列表

        Args:
            query: 搜索查询
            max_results: 注入上下文的最终结果条数；None 时使用配置 SEARCH_CONTEXT_RESULTS

        Returns:
            tuple: (context_string, sources)
                - context_string: 供 LLM 使用的文本上下文
                - sources: 来源列表，每项包含 title、url、content
        """
        # 增强3：默认使用配置的注入条数，而非硬编码 3
        if max_results is None:
            max_results = self.context_results

        # 最终上下文缓存（仅缓存文本，来源随请求返回）
        context_cache_key = self._cache_key("web_search:context", query)
        cached_context = await self._get_cache(context_cache_key)
        if cached_context:
            logger.debug("命中搜索上下文缓存")
            return cached_context, []

        # 多 Query 搜索
        try:
            results = await self.search_multi(query)
        except WebSearchError as e:
            logger.warning(f"联网搜索失败，本次问答将不使用网络上下文: {e}")
            raise
        if not results:
            return "", []

        # 并行抓取正文
        fetch_tasks = []
        for result in results[: self.max_fetch]:
            fetch_tasks.append(self.fetch_content(result.url, result.title, result.content))

        contents: List[Optional[WebContent]] = await asyncio.gather(*fetch_tasks)
        valid_contents = [c for c in contents if c is not None]

        if not valid_contents:
            valid_contents = [
                WebContent(url=r.url, title=r.title, content=r.content, snippet=r.content)
                for r in results[:max_results]
            ]

        # 语义重排
        ranked_contents = await self.reranker.rerank(query, valid_contents, top_k=max_results)

        # 构建上下文与来源
        context_parts = []
        sources = []
        for i, content in enumerate(ranked_contents, 1):
            chunks = self._chunk_content(content.content)
            # 增强2：关键段落提取——取前 2 块并确保包含含数据特征的块，
            # 避免价格/数据被截断在第一块之外
            display_text = self._select_relevant_chunks(query, chunks) if chunks else content.content

            context_parts.append(
                f"[{i}] 标题: {content.title}\n"
                f"来源: {content.url}\n"
                f"内容: {display_text}"
            )
            sources.append({
                "title": content.title,
                "url": content.url,
                "page_content": display_text,
                "source": "web_search",
                "source_index": i,
            })

        context = "\n\n".join(context_parts)

        # 缓存最终上下文
        await self._set_cache(context_cache_key, context, self.cache_ttl)
        return context, sources

    async def build_search_context_enhanced(
        self,
        query: str,
        conversation_context: Optional[List[Dict]] = None,
        max_results: Optional[int] = None,
    ) -> tuple[str, List[Dict], Dict]:
        """增强版搜索上下文构建，集成多轮上下文改写与多源交叉验证。

        相比 build_search_context_with_sources：
        - 透传 conversation_context 给新版 QueryRewriter 做多轮上下文补全。
        - sources 增加 content 字段（= page_content），兼容 CitationBackfiller/AnswerVerifier。
        - 返回 cross_source_data，供 AnswerVerifier 计算多源交叉一致性。

        Args:
            query: 搜索查询。
            conversation_context: 多轮对话上下文，供 QueryRewriter 补全。
            max_results: 注入上下文的最终结果条数。

        Returns:
            (context, sources, cross_source_data):
                - context: 供 LLM 使用的文本上下文。
                - sources: 来源列表，含 title/url/content/source/source_index。
                - cross_source_data: 多源交叉验证结果，
                  {"validated_values": [], "single_source_values": [], "conflicting_values": []}。
        """
        if max_results is None:
            max_results = self.context_results

        # 结果级缓存：键含 query 与会话上下文散列（避免相同 query 复用
        # 了不同 context 改写的结果），命中则跳过 LLM 改写 + N 路搜索 + N 次抓取。
        context_cache_key = self._cache_key("web_search:context_enhanced", query)
        if conversation_context:
            ctx_hash = hashlib.md5(
                json.dumps(conversation_context, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:8]
            context_cache_key += f":{ctx_hash}"
        cached = await self._get_cache(context_cache_key)
        if isinstance(cached, dict):
            logger.debug("命中增强搜索上下文缓存")
            return (
                cached.get("context", ""),
                cached.get("sources", []),
                cached.get("cross_source_data", {
                    "validated_values": [],
                    "single_source_values": [],
                    "conflicting_values": [],
                }),
            )

        # 多 Query 搜索（带多轮上下文）
        try:
            results = await self.search_multi(query, conversation_context=conversation_context)
        except WebSearchError as e:
            logger.warning(f"联网搜索失败，本次问答将不使用网络上下文: {e}")
            raise
        if not results:
            return "", [], {
                "validated_values": [],
                "single_source_values": [],
                "conflicting_values": [],
            }

        # 多源交叉验证（基于搜索结果中的数值）
        cross_source_data = self.postprocessor.cross_source_validate(results)

        # 并行抓取正文
        fetch_tasks = []
        for result in results[: self.max_fetch]:
            fetch_tasks.append(self.fetch_content(result.url, result.title, result.content))

        contents: List[Optional[WebContent]] = await asyncio.gather(*fetch_tasks)
        valid_contents = [c for c in contents if c is not None]

        if not valid_contents:
            valid_contents = [
                WebContent(url=r.url, title=r.title, content=r.content, snippet=r.content)
                for r in results[:max_results]
            ]

        # 语义重排
        ranked_contents = await self.reranker.rerank(query, valid_contents, top_k=max_results)

        # 构建上下文与来源（sources 补充 content 字段以兼容 CitationBackfiller）
        context_parts = []
        sources = []
        for i, content in enumerate(ranked_contents, 1):
            chunks = self._chunk_content(content.content)
            display_text = self._select_relevant_chunks(query, chunks) if chunks else content.content

            context_parts.append(
                f"[{i}] 标题: {content.title}\n"
                f"来源: {content.url}\n"
                f"内容: {display_text}"
            )
            sources.append({
                "title": content.title,
                "url": content.url,
                "page_content": display_text,
                "content": display_text,  # 新增：兼容 CitationBackfiller/AnswerVerifier
                "source": "web_search",
                "source_index": i,
            })

        context = "\n\n".join(context_parts)

        # 缓存最终结果（context + sources + 交叉验证），需与来源随请求返回
        await self._set_cache(
            context_cache_key,
            {"context": context, "sources": sources, "cross_source_data": cross_source_data},
            self.cache_ttl,
        )
        return context, sources, cross_source_data

    async def build_search_context(self, query: str, max_results: int = 3) -> str:
        """仅返回联网搜索上下文字符串（兼容旧接口）"""
        context, _ = await self.build_search_context_with_sources(query, max_results)
        return context
