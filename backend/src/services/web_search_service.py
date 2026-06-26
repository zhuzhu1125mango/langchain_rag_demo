"""
联网搜索服务 - Web Search Service

支持多种搜索引擎集成：
1. SearXNG (私有化聚合搜索，推荐)
2. Tavily Search (专为LLM设计)
3. DuckDuckGo Search (无需API Key)

Phase 2 增强：
- LLM 多角度 Query 改写 + 领域适配
- Cross-Encoder 语义重排
- Redis 搜索结果/正文缓存

Phase 3 增强：
- 为 Function Calling / ReAct Agent 提供工具化搜索能力
- search_agent.py 中 SearchToolkit 封装 web_search / fetch_webpage 工具
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import settings

logger = logging.getLogger(__name__)

# 常见低质量/噪音域名，抓取正文时过滤
_LOW_QUALITY_DOMAINS: Set[str] = {
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "weibo.com",
    "zhihu.com/question",
    "reddit.com",
    "pinterest.com",
    "taobao.com",
    "tmall.com",
    "jd.com",
    "amazon.com",
}

# 简单中文/英文停用词，用于基础 Query 改写
_STOPWORDS: Set[str] = {
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "哪", "什么", "怎么", "为什么", "如何", "请", "一下",
    "能否", "可以", "告诉", "帮忙", "一下", "呢", "吗", "吧", "啊",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall",
}

_CODE_KEYWORDS: Set[str] = {
    "代码", "code", "编程", "programming", "github", "stackoverflow",
    "python", "java", "javascript", "typescript", "go", "rust", "c++", "c#",
    "报错", "error", "exception", "bug", "调试", "debug", "函数", "库", "package",
    "怎么写", "如何实现", "示例", "demo", "snippet",
}

_RECENT_KEYWORDS: Set[str] = {
    "最新", "最近", "今年", "去年", "新闻", "实时", "刚刚", "近日",
    "2024", "2025", "2026", "newest", "latest", "recent", "news",
}


@dataclass
class SearchResult:
    title: str
    url: str
    content: str
    source: str = "web_search"
    engine: str = ""


@dataclass
class WebContent:
    url: str
    title: str
    content: str
    snippet: str = ""
    source_index: int = 0
    score: float = 0.0
    metadata: Dict = field(default_factory=dict)


class WebSearchError(Exception):
    """联网搜索异常：包含搜索引擎类型与原始错误信息"""

    def __init__(self, message: str, engine: str = "", cause: Optional[Exception] = None):
        super().__init__(message)
        self.engine = engine
        self.cause = cause

    def __str__(self) -> str:
        parts = [f"[{self.engine}] {super().__str__()}" if self.engine else super().__str__()]
        if self.cause:
            parts.append(f"原始错误: {self.cause}")
        return " | ".join(parts)


class SearchQueryRewriter:
    """
    Query 改写器：
    1. 基础改写：去除停用词、保留核心关键词
    2. LLM 多角度改写：生成多个适合搜索引擎的查询
    3. 领域适配：代码问题加 site:github.com，实时信息加时间限定
    """

    def __init__(self, llm=None, num_queries: int = 3):
        self.llm = llm
        self.num_queries = num_queries

    @staticmethod
    def _is_code_query(query: str) -> bool:
        """判断是否为代码/技术类查询，命中后保留 _ . + # 等关键符号"""
        if not query:
            return False
        q = query.lower()
        return any(kw in q for kw in _CODE_KEYWORDS) or bool(
            re.search(r"[a-zA-Z_][\w\-\.]*\.[a-zA-Z_][\w\-\.]*", query)
        )

    def basic_rewrite(self, query: str) -> str:
        """基础改写：去除停用词和冗余；代码类查询保留关键符号"""
        if not query:
            return query

        is_code = self._is_code_query(query)
        cleaned = re.sub(r"[\s]+", " ", query.strip())

        if is_code:
            # 代码查询：保留 _ . + # - / : < > ( ) 等符号，仅移除常见标点
            cleaned = re.sub(r"[^\w\s\u4e00-\u9fff_\-\.\+\#\/\\:\<\>\(\)\[\]\{\}]", " ", cleaned)
        else:
            cleaned = re.sub(r"[^\w\s\u4e00-\u9fff]", " ", cleaned)
        tokens = cleaned.split()
        filtered = [t for t in tokens if t.lower() not in _STOPWORDS and len(t) > 1]

        if not filtered:
            return query.strip()
        return " ".join(filtered)

    async def llm_rewrite(self, query: str) -> List[str]:
        """使用 LLM 生成多个搜索角度"""
        if not self.llm:
            return []

        template = """请将用户问题改写为 {num} 个适合搜索引擎的查询短语。
要求：
1. 每个短语聚焦不同角度，覆盖问题的不同方面
2. 去除语气词和冗余表述，保留核心关键词
3. 直接返回 JSON 数组，不要任何解释

用户问题：{question}

输出格式：["查询1", "查询2", "查询3"]"""

        prompt = template.format(question=query, num=self.num_queries)
        try:
            response = await self.llm.ainvoke(prompt)
            content = response.content.strip()
            # 尝试提取 JSON 数组
            match = re.search(r"\[[\s\S]*\]", content)
            if match:
                queries = json.loads(match.group())
                if isinstance(queries, list) and queries:
                    return [str(q).strip() for q in queries if str(q).strip()]
        except Exception as e:
            logger.warning(f"LLM Query 改写失败: {e}")
        return []

    def _detect_domain(self, query: str) -> str:
        """检测问题领域，返回适配后缀"""
        q = query.lower()
        if any(kw in q for kw in _CODE_KEYWORDS):
            return "site:github.com OR site:stackoverflow.com"
        if any(kw in q for kw in _RECENT_KEYWORDS):
            # SearXNG/DuckDuckGo 对时间过滤支持不一，这里加年份增强实时性
            return "2024 OR 2025"
        return ""

    def _normalize(self, query: str) -> str:
        """归一化查询，用于去重比较；代码类查询保留关键符号"""
        if self._is_code_query(query):
            return re.sub(r"[^\w\s\u4e00-\u9fff_\-\.\+\#\/\\:\<\>\(\)\[\]\{\}]", "", query).strip().lower()
        return re.sub(r"[^\w\s\u4e00-\u9fff]", "", query).strip().lower()

    async def rewrite(self, query: str) -> List[str]:
        """
        完整改写流程：
        1. LLM 生成多角度查询
        2. 加上基础改写和原始问题作为补充
        3. 领域适配
        """
        base_queries = []

        if settings.search.SEARCH_ENABLE_MULTI_QUERY and self.llm:
            base_queries = await self.llm_rewrite(query)

        # 加入基础改写和原始问题
        basic = self.basic_rewrite(query)
        if basic:
            base_queries.append(basic)
        base_queries.append(query)

        # 去重（按归一化 key）
        seen = set()
        unique = []
        for q in base_queries:
            key = self._normalize(q)
            if key and key not in seen:
                seen.add(key)
                unique.append(q)

        # 领域适配
        domain_hint = self._detect_domain(query)
        adapted = []
        for q in unique:
            adapted.append(q)
            if domain_hint and domain_hint not in q.lower():
                adapted.append(f"{q} {domain_hint}")

        # 再次去重并限制数量
        seen = set()
        final = []
        for q in adapted:
            key = self._normalize(q)
            if key not in seen:
                seen.add(key)
                final.append(q)
        return final[: self.num_queries * 2]


class WebReranker:
    """基于 Cross-Encoder 的语义重排器"""

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or settings.search.SEARCH_RERANK_MODEL
        self._model = None

    def _load_model(self):
        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder

                logger.info(f"正在加载重排模型: {self.model_name}")
                self._model = CrossEncoder(self.model_name)
                logger.info("重排模型加载完成")
            except ImportError:
                logger.warning("sentence-transformers 未安装，重排功能不可用")
                self._model = None

    async def rerank(self, query: str, contents: List[WebContent], top_k: int = 5) -> List[WebContent]:
        if not contents:
            return []

        if not settings.search.SEARCH_ENABLE_RERANK:
            return contents[:top_k]

        self._load_model()
        if self._model is None:
            return contents[:top_k]

        try:
            pairs = [(query, c.content) for c in contents]
            scores = await asyncio.to_thread(self._model.predict, pairs)

            for content, score in zip(contents, scores):
                content.score = float(score)

            ranked = sorted(contents, key=lambda x: x.score, reverse=True)
            return ranked[:top_k]
        except Exception as e:
            logger.warning(f"语义重排失败: {e}")
            return contents[:top_k]


class WebSearchService:
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
        self.cache_ttl = settings.search.SEARCH_CACHE_TTL
        self.content_cache_ttl = settings.search.SEARCH_CONTENT_CACHE_TTL

        self.llm = llm
        self.cache_service = cache_service
        self.query_rewriter = SearchQueryRewriter(llm=llm, num_queries=settings.search.SEARCH_NUM_QUERIES)
        self.reranker = WebReranker()

        self._ddgs_client = None
        self._tavily_client = None

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

        if self.search_provider == "searxng":
            return await self._search_searxng(query, max_results)
        if self.search_provider == "tavily":
            return await self._search_tavily(query, max_results)
        return await self._search_duckduckgo(query, max_results)

    async def search_multi(self, query: str) -> List[SearchResult]:
        """多 Query 并行搜索，结果按 URL 去重；失败时聚合错误信息"""
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
        return merged

    async def _search_searxng(self, query: str, max_results: int) -> List[SearchResult]:
        if not self.searxng_base_url:
            raise WebSearchError("SEARXNG_BASE_URL 未配置", engine="searxng")

        base_url = self.searxng_base_url.rstrip("/")
        params = {"q": query, "format": "json", "language": "zh-CN,en"}

        try:
            async with httpx.AsyncClient(timeout=self.searxng_timeout, follow_redirects=True) as client:
                response = await client.get(f"{base_url}/search", params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as e:
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
    async def fetch_content(self, url: str, title: str, snippet: str) -> Optional[WebContent]:
        # 优先读取缓存
        cache_key = self._cache_key("web_search:content", url)
        cached = await self._get_cache(cache_key)
        if cached and isinstance(cached, dict) and "content" in cached:
            return WebContent(
                url=url,
                title=cached.get("title", title),
                content=cached["content"],
                snippet=cached.get("snippet", snippet),
                metadata={"cached": True, "domain": self._extract_domain(url)},
            )

        try:
            async with httpx.AsyncClient(
                timeout=self.fetch_timeout,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; RAG-Bot/1.0)"},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                html = response.text
        except Exception as e:
            logger.debug(f"抓取页面失败 {url}: {e}")
            return None

        content = self._extract_text(html, url)
        if len(content) < self.min_content_length:
            content = snippet
        if len(content) < self.min_content_length:
            return None

        # 写入缓存
        await self._set_cache(
            cache_key,
            {"url": url, "title": title, "content": content, "snippet": snippet},
            self.content_cache_ttl,
        )

        return WebContent(
            url=url,
            title=title,
            content=content,
            snippet=snippet,
            metadata={"domain": self._extract_domain(url)},
        )

    def _extract_text(self, html: str, url: str) -> str:
        try:
            import trafilatura

            text = trafilatura.extract(html, include_comments=False, include_tables=False)
            if text and len(text.strip()) >= self.min_content_length:
                return text.strip()
        except ImportError:
            logger.debug("trafilatura 未安装，使用 BeautifulSoup 降级")
        except Exception as e:
            logger.debug(f"trafilatura 提取失败 {url}: {e}")

        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"BeautifulSoup 提取失败 {url}: {e}")
            return ""

    def _chunk_content(self, content: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except ImportError:
            return [content]

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", ". ", " ", ""],
        )
        return splitter.split_text(content)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _extract_domain(self, url: str) -> str:
        try:
            return urlparse(url).netloc.lower().lstrip("www.")
        except Exception:
            return ""

    def _is_low_quality_domain(self, url: str) -> bool:
        domain = self._extract_domain(url)
        return any(domain.endswith(bad) or bad in domain for bad in _LOW_QUALITY_DOMAINS)

    # ------------------------------------------------------------------
    # 搜索上下文构建
    # ------------------------------------------------------------------
    async def build_search_context_with_sources(
        self, query: str, max_results: int = 3
    ) -> tuple[str, List[Dict]]:
        """
        构建联网搜索上下文，并返回原始来源列表

        Returns:
            tuple: (context_string, sources)
                - context_string: 供 LLM 使用的文本上下文
                - sources: 来源列表，每项包含 title、url、content
        """
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
            return "", []
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
            display_text = chunks[0] if chunks else content.content

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

    async def build_search_context(self, query: str, max_results: int = 3) -> str:
        """仅返回联网搜索上下文字符串（兼容旧接口）"""
        context, _ = await self.build_search_context_with_sources(query, max_results)
        return context
