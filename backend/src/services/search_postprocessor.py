"""搜索后处理模块。

对 WebSearchService 返回的搜索结果进行去重、时效性评分和重排序，
提升最终进入 LLM 上下文的搜索质量。
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from src.services.search_types import SearchResult

logger = logging.getLogger(__name__)


# 域名权威度评分（0-1），用于结果排序
_DOMAIN_AUTHORITY: Dict[str, float] = {
    "gov.cn": 0.95,
    "gov.hk": 0.95,
    "wikipedia.org": 0.9,
    "zh.wikipedia.org": 0.9,
    "baike.baidu.com": 0.85,
    "news.sina.com.cn": 0.8,
    "news.qq.com": 0.8,
    "people.com.cn": 0.85,
    "xinhuanet.com": 0.85,
    "cctv.com": 0.8,
    "sina.com.cn": 0.75,
    "sohu.com": 0.75,
    "163.com": 0.75,
    "ifeng.com": 0.75,
    "thepaper.cn": 0.8,
    "github.com": 0.85,
    "stackoverflow.com": 0.85,
    "open-meteo.com": 0.9,
    # 贵金属 / 汇率 / 金融数据权威来源
    "gold.org": 0.9,
    "lbma.org.uk": 0.95,
    "sge.com.cn": 0.95,
    "xaus.com": 0.85,
    "goldapi.io": 0.85,
    "frankfurter.app": 0.85,
    "exchangerate-api.com": 0.85,
    "investing.com": 0.75,
    "xetra-gold.com": 0.8,
}

# 低质量域名，降低排序权重
_LOW_QUALITY_DOMAINS = {
    "youtube.com", "youtu.be", "bilibili.com", "tiktok.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "weibo.com", "reddit.com", "pinterest.com", "taobao.com",
    "tmall.com", "jd.com", "amazon.com",
}


@dataclass
class ScoredResult:
    """带评分的搜索结果。

    扩展字段 source_id / domain 供 CitationBackfiller 引用补全与多源交叉验证使用。
    """

    result: SearchResult
    relevance_score: float = 0.0
    freshness_score: float = 0.0
    authority_score: float = 0.0
    final_score: float = 0.0
    numeric_values: List[Decimal] = field(default_factory=list)
    source_id: int = 0  # 供 CitationBackfiller 引用使用（1-based）
    domain: str = ""  # 独立域名，供交叉验证

    def to_search_result(self) -> SearchResult:
        return self.result


class SearchPostprocessor:
    """搜索结果后处理器。"""

    def __init__(self):
        pass

    def process(
        self,
        results: List[SearchResult],
        top_k: Optional[int] = None,
    ) -> List[SearchResult]:
        """对搜索结果进行完整后处理。

        Args:
            results: 原始搜索结果列表。
            top_k: 返回前 K 个结果，None 则返回全部。

        Returns:
            处理后的搜索结果列表。
        """
        if not results:
            return []

        # 1. 去重
        deduped = self.deduplicate(results)

        # 2. 评分
        scored = self.score(deduped)

        # 3. 重排序
        sorted_results = self.rerank(scored)

        # 4. 截断
        if top_k:
            sorted_results = sorted_results[:top_k]

        return [s.result for s in sorted_results]

    def deduplicate(self, results: List[SearchResult]) -> List[SearchResult]:
        """基于 URL 和内容相似度去重。"""
        seen_urls: Set[str] = set()
        seen_hashes: Set[str] = set()
        deduped = []

        for r in results:
            url = self._normalize_url(r.url)
            if url in seen_urls:
                continue

            content_hash = self._content_hash(r.content)
            if content_hash in seen_hashes:
                continue

            seen_urls.add(url)
            seen_hashes.add(content_hash)
            deduped.append(r)

        return deduped

    def score(self, results: List[SearchResult]) -> List[ScoredResult]:
        """对结果进行多维评分，并填充 source_id / domain 扩展字段。

        source_id 按 1-based 顺序分配，供 CitationBackfiller 引用补全使用；
        domain 为归一化后的独立域名，供多源交叉验证使用。
        """
        scored = []
        for idx, r in enumerate(results, start=1):
            authority = self._authority_score(r.url)
            freshness = self._freshness_score(r.content)
            relevance = self._relevance_score(r)
            domain = self._extract_domain(r.url)

            # 最终得分：权威 0.3 + 时效 0.3 + 相关 0.4
            final = authority * 0.3 + freshness * 0.3 + relevance * 0.4

            scored.append(
                ScoredResult(
                    result=r,
                    relevance_score=relevance,
                    freshness_score=freshness,
                    authority_score=authority,
                    final_score=final,
                    source_id=idx,
                    domain=domain,
                )
            )
        return scored

    def rerank(self, scored_results: List[ScoredResult]) -> List[ScoredResult]:
        """按最终得分降序重排。"""
        return sorted(scored_results, key=lambda x: x.final_score, reverse=True)

    def cross_validate_numeric(
        self,
        results: List[SearchResult],
        reference_values: List[Decimal],
        tolerance: float = 0.05,
    ) -> Tuple[List[SearchResult], float]:
        """将搜索结果中的数值与参考数值交叉验证，返回增强结果和一致性分数。

        对于与参考值偏差在容忍度内的搜索结果，提升其相关分；
        偏差过大的结果降低相关分，并在结果中附加标记。

        Args:
            results: 原始搜索结果。
            reference_values: 参考数值列表（如工具返回的价格）。
            tolerance: 相对偏差容忍度，默认 5%。

        Returns:
            (增强后的结果列表, 整体一致性分数)
        """
        if not results or not reference_values:
            return results, 0.0

        validated = []
        matched_count = 0
        for r in results:
            nums = [item["value"] for item in self.extract_numeric_values(r.content)]
            if not nums:
                validated.append(r)
                continue

            # 判断该结果中是否有数值与参考值接近
            best_match_deviation = min(
                abs(n - ref) / ref if ref != 0 else float("inf")
                for n in nums
                for ref in reference_values
            )
            if best_match_deviation <= tolerance:
                matched_count += 1

            # 将一致性信息注入 content（供 LLM 参考）
            extra = ""
            if best_match_deviation <= tolerance:
                extra = " [该结果数值与权威数据源一致]"
            else:
                extra = " [该结果数值与权威数据源存在偏差，仅供参考]"

            validated.append(
                SearchResult(
                    title=r.title,
                    url=r.url,
                    content=r.content + extra,
                    source=r.source,
                    engine=r.engine,
                )
            )

        consistency = matched_count / len(results) if results else 0.0
        return validated, consistency

    def extract_numeric_values(self, text: str) -> List[Dict[str, Any]]:
        """从文本中提取数值及其上下文，供交叉验证使用。

        将原私有方法提升为公开方法，返回结构化信息以便 AnswerVerifier
        进行多源交叉一致性校验。

        Args:
            text: 待提取的文本。

        Returns:
            数值信息列表，每项形如：
            {"value": Decimal, "raw": "4,067.98", "context": "...金价4067.98元...", "position": int}
        """
        if not text:
            return []
        results: List[Dict[str, Any]] = []
        # 匹配整数/小数，支持千分位逗号
        for m in re.finditer(
            r"[¥$€£]?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
            text,
        ):
            raw = m.group(1)
            num_str = raw.replace(",", "")
            try:
                value = Decimal(num_str)
            except (InvalidOperation, ValueError):
                continue
            # 取匹配位置前后 40 字符作为上下文，便于后续语义判断
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            context = text[start:end]
            results.append(
                {
                    "value": value,
                    "raw": raw,
                    "context": context,
                    "position": m.start(1),
                }
            )
        return results

    def cross_source_validate(
        self,
        results: List[SearchResult],
        tolerance: float = 0.05,
    ) -> Dict[str, List[Decimal]]:
        """对搜索结果中的数值做多源交叉验证。

        算法：
            1. 对每个 result 提取数值及其上下文。
            2. 按"数值相近（±tolerance）"聚类。
            3. 统计每个聚类涉及的独立域名数。
            4. 域名数 ≥ 2 且偏差 ≤ tolerance → validated；
               域名数 = 1 → single_source；
               域名数 ≥ 2 但偏差 > tolerance → conflicting。

        Args:
            results: 搜索结果列表。
            tolerance: 聚类时数值相近的相对偏差容忍度，默认 5%。

        Returns:
            {
                "validated_values": [Decimal, ...],     # ≥2 个独立域名一致的数值
                "single_source_values": [Decimal, ...], # 仅 1 个来源的数值
                "conflicting_values": [Decimal, ...],   # 多源但偏差大的数值
            }
        """
        validated: List[Decimal] = []
        single_source: List[Decimal] = []
        conflicting: List[Decimal] = []

        if not results:
            return {
                "validated_values": validated,
                "single_source_values": single_source,
                "conflicting_values": conflicting,
            }

        # 收集 (value, domain) 配对
        value_domain_pairs: List[Tuple[Decimal, str]] = []
        for r in results:
            domain = self._extract_domain(r.url)
            for item in self.extract_numeric_values(r.content):
                # 过滤掉过小或过大的无意义数值（如纯年份、版本号、百分比等噪音）
                value = item["value"]
                if self._is_noise_number(value, item["context"]):
                    continue
                value_domain_pairs.append((value, domain))

        if not value_domain_pairs:
            return {
                "validated_values": validated,
                "single_source_values": single_source,
                "conflicting_values": conflicting,
            }

        # 按数值相近聚类（贪心）
        clusters: List[List[Tuple[Decimal, str]]] = []
        for value, domain in value_domain_pairs:
            placed = False
            for cluster in clusters:
                # 以聚类中第一个值为基准
                base = cluster[0][0]
                if base == 0:
                    continue
                deviation = abs(value - base) / abs(base)
                if deviation <= tolerance:
                    cluster.append((value, domain))
                    placed = True
                    break
            if not placed:
                clusters.append([(value, domain)])

        # 统计每个聚类的独立域名数
        for cluster in clusters:
            domains = {d for _, d in cluster}
            base = cluster[0][0]
            if len(domains) >= 2:
                # 检查聚类内部偏差是否在容忍度内
                values = [v for v, _ in cluster]
                max_dev = self._cluster_max_deviation(values)
                if max_dev <= tolerance:
                    validated.append(base)
                else:
                    conflicting.append(base)
            else:
                single_source.append(base)

        logger.debug(
            "cross_source_validate: validated=%d, single=%d, conflicting=%d",
            len(validated),
            len(single_source),
            len(conflicting),
        )
        return {
            "validated_values": validated,
            "single_source_values": single_source,
            "conflicting_values": conflicting,
        }

    def _extract_domain(self, url: str) -> str:
        """从 URL 提取归一化独立域名（去除 www. 前缀）。

        Args:
            url: 原始 URL。

        Returns:
            归一化域名，解析失败返回空字符串。
        """
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            return netloc
        except Exception:
            return ""

    def _is_noise_number(self, value: Decimal, context: str) -> bool:
        """判断数值是否为噪音（年份、版本号、百分比、纯序号等）。

        Args:
            value: 提取到的数值。
            context: 数值所在的上下文文本。

        Returns:
            True 表示该数值应被过滤，False 表示保留。
        """
        # 1900-2099 范围内的整数视为年份噪音
        if value == value.to_integral_value():
            int_val = int(value)
            if 1900 <= int_val <= 2099:
                return True
        # 过小数值（< 1）多为比率/小数噪音
        if 0 < abs(value) < Decimal("1"):
            # 但保留明显是价格的小数（如 0.5 元）—— 仅过滤 < 0.1
            if abs(value) < Decimal("0.1"):
                return True
        # 上下文含版本号/编号特征
        if re.search(r"v\d|version|版本|第\s*\d+\s*[章节条]", context, re.IGNORECASE):
            return True
        return False

    def _cluster_max_deviation(self, values: List[Decimal]) -> float:
        """计算数值列表内最大相对偏差。

        Args:
            values: 同聚类内的数值列表。

        Returns:
            最大相对偏差（0-1），列表为空或基准为 0 返回 0.0。
        """
        if not values:
            return 0.0
        base = values[0]
        if base == 0:
            return 0.0
        return max(abs(v - base) / abs(base) for v in values)

    def _normalize_url(self, url: str) -> str:
        """归一化 URL 用于去重。"""
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower().lstrip("www.")
            path = parsed.path.rstrip("/")
            return f"{netloc}{path}"
        except Exception:
            return url.lower()

    def _content_hash(self, content: str) -> str:
        """基于内容前 200 字符生成哈希。"""
        text = (content or "").strip()[:200]
        # 去除标点和空格后哈希
        text = re.sub(r"\s+", "", text)
        return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()

    def _authority_score(self, url: str) -> float:
        """基于域名计算权威度得分。"""
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
        except Exception:
            return 0.5

        if any(domain in netloc for domain in _LOW_QUALITY_DOMAINS):
            return 0.2

        for suffix, score in _DOMAIN_AUTHORITY.items():
            if suffix in netloc:
                return score

        return 0.5

    def _freshness_score(self, content: str) -> float:
        """基于内容中的时间信息计算时效性得分。"""
        if not content:
            return 0.0

        # 提取年份 2024-2039（中文与数字之间无单词边界，直接匹配 4 位数字）
        current_year = datetime.now(timezone.utc).year
        years = re.findall(r"(20[2-3]\d)", content)
        if years:
            max_year = max(int(y) for y in years)
            if max_year >= current_year:
                # 若同时包含当前日期或小时级时间，认为非常新鲜
                if self._contains_recent_time(content):
                    return 1.0
                return 0.85
            if max_year == current_year - 1:
                return 0.7
            if max_year >= current_year - 2:
                return 0.4
            return 0.2

        # 若没有时间信息，给中等得分
        return 0.5

    def _contains_recent_time(self, content: str) -> bool:
        """判断内容中是否包含小时/分钟级或今日/当前等近期时间描述。"""
        recent_patterns = [
            r"\d{2}:\d{2}",  # 14:32
            r"\d{2}:\d{2}:\d{2}",  # 14:32:00
            r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}",  # 2026-06-27 14:32
            r"刚刚|刚刚更新|刚刚发布|几分钟前|小时前|今日|今天|当前|现在|实时",
        ]
        return any(re.search(p, content) for p in recent_patterns)

    def _relevance_score(self, result: SearchResult) -> float:
        """简单相关性得分：标题和摘要越长、越完整分越高。"""
        content_len = len(result.content or "")
        title_len = len(result.title or "")
        score = min(1.0, (content_len + title_len * 2) / 1000.0)
        return max(0.3, score)
