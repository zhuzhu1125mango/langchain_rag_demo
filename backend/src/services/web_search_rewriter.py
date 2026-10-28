"""联网搜索 Query 改写：基础改写 + LLM 多角度改写（CODE-1 拆分）。"""

import json
import logging
import re
from datetime import date
from typing import List


from src.config import settings

logger = logging.getLogger(__name__)

from src.services.web_search_types import _CODE_KEYWORDS, _RECENT_KEYWORDS, _STOPWORDS

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
            # SearXNG/DuckDuckGo 对时间过滤支持不一，这里加当年与去年增强实时性
            yr = date.today().year
            return f"{yr} OR {yr - 1}"
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
