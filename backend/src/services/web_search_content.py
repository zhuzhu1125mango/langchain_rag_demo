"""联网搜索正文抓取与预处理：安全抓取、正文抽取、分块与相关性筛选（CODE-1 拆分）。"""

import logging
import re
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.utils.error_handling import ErrorSeverity, log_exception
from src.utils.security import UnsafeUrlError, is_public_ip, validate_url_safe

logger = logging.getLogger(__name__)

from src.services.web_search_types import WebContent, _LOW_QUALITY_DOMAINS

class ContentMixin:
    """抓取/解析/分块，由 WebSearchService 组合。"""

    async def _fetch_html(self, url: str) -> str:
        """GET 页面 HTML，重定向不自动跟随，逐跳重新做 SSRF 校验（安全 W1-5）。

        SEC-9 纵深：
        - 每一跳重新 validate_url_safe（DNS 失败即拒绝）；
        - 连接后复核 peer IP，防止校验与连接之间的 DNS rebinding；
        - 限制响应字节数、重定向跳数与 Content-Type。

        Raises:
            UnsafeUrlError: 任一跳不安全、重定向/响应超限或内容类型不允许。
            httpx.HTTPError: 网络请求失败。
        """
        current = url
        for _ in range(self.max_redirect_hops):
            validate_url_safe(current)
            next_url = None
            async with self._safe_http.stream("GET", current, follow_redirects=False) as response:
                self._assert_public_peer(response)
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        raise UnsafeUrlError("重定向响应缺少 Location")
                    next_url = str(response.url.join(location))
                else:
                    response.raise_for_status()
                    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if content_type and self.allowed_content_types and content_type not in self.allowed_content_types:
                        raise UnsafeUrlError(f"不允许的响应 Content-Type: {content_type}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > self.max_response_bytes:
                            raise UnsafeUrlError(
                                f"响应体超过上限 {self.max_response_bytes} 字节"
                            )
                    return body.decode(response.encoding or "utf-8", errors="replace")
            if next_url is None:
                break
            current = next_url
        raise UnsafeUrlError("重定向跳数超限或响应缺少 Location")

    @staticmethod
    def _assert_public_peer(response: httpx.Response) -> None:
        """连接建立后复核对端 IP，阻断 DNS rebinding 导致的私网访问（SEC-9）。"""
        stream = response.extensions.get("network_stream")
        if stream is None:
            return
        try:
            peer = stream.get_extra_info("server_addr")
        except Exception:
            # 非 socket 传输（测试替身、内存流）不实现 get_extra_info，属于预期控制流：
            # 此时没有对端 IP 可比，跳过复核即可，不是错误。
            return
        if not peer:
            return
        addr = peer[0] if isinstance(peer, (tuple, list)) else peer
        if not is_public_ip(str(addr)):
            raise UnsafeUrlError(f"连接落到受限地址: {addr}")

    async def close(self) -> None:
        """释放两个 HTTP 连接池（应用关闭或测试清理时调用）。"""
        await self._http.aclose()
        await self._safe_http.aclose()

    async def fetch_content(self, url: str, title: str, snippet: str) -> Optional[WebContent]:
        # SSRF 防护（与 tools/plugins/fetch_webpage_tool.py 一致）：拒绝抓取内网/回环/云元数据地址，
        # 防止搜索结果被污染为内网 URL（如 169.254.169.254）时打穿内网。校验失败直接跳过抓取。
        try:
            validate_url_safe(url)
        except UnsafeUrlError:
            logger.debug(f"拒绝抓取不安全 URL: {url}")
            return None

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
            html = await self._fetch_html(url)
        except UnsafeUrlError as e:
            # 重定向链中出现内网/元数据地址：拒绝抓取（与入口校验同一口径）
            logger.debug(f"拒绝抓取重定向至不安全地址的 URL {url}: {e}")
            return None
        except Exception as e:
            log_exception(
                logger, e,
                action="web_search.fetch_content.fetch_html",
                severity=ErrorSeverity.RECOVERABLE,
                message="抓取网页正文失败，跳过该结果",
                url=url,
            )
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
            # 提取器对畸形 HTML 报错很常见，可降级到 BeautifulSoup，按可恢复处理
            log_exception(
                logger, e,
                action="web_search.extract_text.trafilatura",
                severity=ErrorSeverity.RECOVERABLE,
                message="trafilatura 正文提取失败，降级 BeautifulSoup",
                url=url,
            )

        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return "\n".join(lines)
        except Exception as e:
            log_exception(
                logger, e,
                action="web_search.extract_text.beautifulsoup",
                severity=ErrorSeverity.RECOVERABLE,
                message="BeautifulSoup 正文提取失败，返回空正文",
                url=url,
            )
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

    def _select_relevant_chunks(
        self, query: str, chunks: List[str], max_blocks: int = 2
    ) -> str:
        """从分块中选取最相关片段组合为展示文本（增强2：关键段落提取）。

        策略：取前 max_blocks 块作为基础；若基础块不含数字/价格等数据特征，
        则在剩余块中寻找含数据特征的块追加，确保价格、日期等关键数据
        不被截断在第一块之外。

        Args:
            query: 原始查询（保留参数以便后续扩展基于 query 的相关性打分）。
            chunks: 网页正文分块列表。
            max_blocks: 基础块数。

        Returns:
            str: 拼接后的展示文本。
        """
        if not chunks:
            return ""
        if len(chunks) <= max_blocks:
            return "\n".join(chunks)

        base = chunks[:max_blocks]
        # 检测数字/价格/百分比等数据特征
        data_re = re.compile(
            r"[\d,]+\.?\d*\s*(元|美元|美金|￥|\$|€|£|%|万|亿|公斤|kg|吨|度|岁)"
        )
        base_has_data = any(data_re.search(b) for b in base)
        if not base_has_data:
            # 基础块无数据特征，在剩余块中找含数据的追加
            for c in chunks[max_blocks:]:
                if data_re.search(c):
                    base.append(c)
                    break
        return "\n".join(base)

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _extract_domain(self, url: str) -> str:
        try:
            return urlparse(url).netloc.lower().lstrip("www.")
        except Exception:
            # urlparse 仅在极端畸形输入上抛错，返回空域名即可，不影响主流程
            return ""

    def _is_low_quality_domain(self, url: str) -> bool:
        domain = self._extract_domain(url)
        return any(domain.endswith(bad) or bad in domain for bad in _LOW_QUALITY_DOMAINS)

    # ------------------------------------------------------------------
    # 搜索上下文构建
    # ------------------------------------------------------------------
