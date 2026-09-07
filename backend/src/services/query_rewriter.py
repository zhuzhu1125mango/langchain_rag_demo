"""查询改写器模块。

将用户原始问题改写为更适合搜索引擎的 query 列表，支持：
1. 多轮对话上下文补全（代词/省略实体）
2. 规则快速路径（时间/地点/价格/对比等模板）
3. LLM fallback（规则未命中时用 LLM 生成多角度 query）
4. 保底策略（始终包含原始问题作为第一个元素）

设计文档见 docs/design/search-optimization.md 第 3.1 节。
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config import settings
from src.services.model_manager import model_manager
from src.services.prompts.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)

# 代词/指示词，命中后需从对话历史补全实体
_PRONOUN_PATTERN = re.compile(
    r"(?:它|这个|那个|这|那|其|此|该|上面|刚才)", re.UNICODE
)

# 省略实体特征：问题很短且不含动词/动作词
_VERB_KEYWORDS = {"是", "有", "在", "多少", "怎么", "如何", "为什么", "什么", "哪", "吗", "呢"}

# 规则模板匹配关键词
_TIME_KEYWORDS = {"今天", "今日", "现在", "当前", "实时", "最新", "最近"}
_PRICE_KEYWORDS = {"价格", "多少钱", "金价", "银价", "油价", "股价", "汇率"}
_COMPARE_PATTERN = re.compile(r"(.+?)\s*(?:和|与|vs|VS|对比|比较)\s*(.+)")
_CITY_KEYWORDS = {"天气", "气温", "温度", "预报", "下雨", "空气质量"}
_HOW_TO_KEYWORDS = {"怎么", "如何", "怎样", "教程", "学习", "入门", "使用", "配置", "安装", "部署"}
_DEFINITION_KEYWORDS = {"是什么", "什么是", "定义", "概念", "含义", "意思", "介绍"}
_TROUBLESHOOTING_KEYWORDS = {"报错", "错误", "异常", "失败", "无法", "不能", "bug", "exception", "error", "debug", "调试"}
_CODE_KEYWORDS = {"代码", "code", "示例", "demo", "实现", "函数", "库", "package", "github"}
_LOCATION_KEYWORDS = {"哪里", "在哪", "位置", "地址", "路线", "距离", "附近"}
_PERSON_KEYWORDS = {"是谁", "什么人", "生平", "简介", "经历", "成就"}
_PRODUCT_KEYWORDS = {"评测", "推荐", "对比", "怎么样", "好用吗", "值得买"}

# 常见同义词/缩写扩展
_SYNONYM_MAP = {
    "ai": ["人工智能", "artificial intelligence"],
    "人工智能": ["AI", "artificial intelligence"],
    "llm": ["大模型", "大语言模型", "large language model"],
    "大模型": ["LLM", "large language model"],
    "rag": ["检索增强生成", "retrieval augmented generation"],
    "gpu": ["显卡", "图形处理器"],
    "cpu": ["处理器", "中央处理器"],
}

# 简单停用词，用于基础去噪
_STOPWORDS = {
    "的", "了", "在", "是", "我", "你", "他", "她", "它", "们",
    "这", "那", "哪", "什么", "怎么", "为什么", "如何", "请", "一下",
    "能否", "可以", "告诉", "帮忙", "呢", "吗", "吧", "啊",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall",
}


class QueryRewriter:
    """查询改写器：规则优先，LLM fallback，支持多轮上下文补全。

    核心保证：返回的 query 列表始终包含原始问题作为第一个元素，
    避免 LLM 改写丢失关键信息导致召回失败。
    """

    _UNSET = object()

    def __init__(self, llm: Any = _UNSET, num_queries: Optional[int] = None):
        """初始化改写器。

        Args:
            llm: LLM 实例（需支持 ainvoke），用于 fallback 改写。
                显式传入 None 表示禁用 LLM fallback；不传入则使用配置中的轻量模型。
            num_queries: 改写后最多保留的 query 数，None 时读取配置。
        """
        if llm is self._UNSET:
            # B3：默认 LLM 延迟到首次 rewrite 时经模型工厂异步构造
            self.llm = self._UNSET
        else:
            self.llm = llm
        self.num_queries = num_queries or settings.search.QUERY_REWRITE_MAX_QUERIES
        self.prompt_loader = PromptLoader()

    @staticmethod
    async def _build_default_llm() -> Optional[Any]:
        """经模型工厂异步构造默认轻量 LLM（query_rewrite 角色 + think=False）。"""
        try:
            return await model_manager.get_chat_llm(
                "query_rewrite",
                think=False,
                preferred=settings.search.QUERY_REWRITE_MODEL or None,
            )
        except Exception as e:
            logger.warning(f"QueryRewriter 默认 LLM 初始化失败: {e}")
            return None

    async def rewrite(
        self,
        question: str,
        conversation_context: Optional[List[Dict[str, Any]]] = None,
    ) -> List[str]:
        """改写用户问题为搜索 query 列表。

        Args:
            question: 用户原始问题。
            conversation_context: 最近 N 轮对话历史，格式 [{"role": "user"/"assistant", "content": "..."}]。

        Returns:
            去重后的 query 列表，第一个元素始终是原始问题。
        """
        if not question or not question.strip():
            return [question] if question else []

        question = question.strip()

        # B3：默认 LLM 懒加载（首次调用时经模型工厂异步构造）
        if self.llm is self._UNSET:
            self.llm = await self._build_default_llm()

        # 1. 上下文补全
        resolved_question = self._resolve_context(question, conversation_context)

        # 2. 同义词/缩写扩展（基于原始问题，避免被后续规则截断）
        synonym_queries = self._expand_synonyms(question)

        # 3. 规则快速路径
        rule_queries = self._rule_based_rewrite(resolved_question)

        # 4. LLM fallback（规则未命中时）
        llm_queries: List[str] = []
        if not rule_queries and self.llm and settings.search.SEARCH_ENABLE_MULTI_QUERY:
            llm_queries = await self._llm_rewrite(resolved_question, conversation_context)

        # 5. 合并：原始问题保底 + 同义词扩展 + 规则/LLM 结果
        all_queries = [question]  # 始终保留原始问题
        if resolved_question != question:
            all_queries.append(resolved_question)
        all_queries.extend(synonym_queries)
        all_queries.extend(rule_queries)
        all_queries.extend(llm_queries)

        # 6. 去重 + 截断
        return self._dedup_queries(all_queries)[: self.num_queries]

    def _resolve_context(
        self,
        question: str,
        conversation_context: Optional[List[Dict[str, Any]]],
    ) -> str:
        """多轮对话上下文补全：检测代词/省略实体，从历史提取实体补全。

        Args:
            question: 当前问题。
            conversation_context: 对话历史。

        Returns:
            补全后的完整问题。若无补全需要则原样返回。
        """
        if not conversation_context or not self._needs_context_resolution(question):
            return question

        # 取最近一轮的 assistant 消息作为上下文来源
        recent_context = ""
        for msg in reversed(conversation_context):
            if msg.get("role") == "user" and msg.get("content"):
                recent_context = msg["content"]
                break
            if msg.get("role") == "assistant" and msg.get("content") and not recent_context:
                # 如果没有 user 消息，也用 assistant 的回答提取实体
                recent_context = msg["content"]

        if not recent_context:
            return question

        # 从历史中提取核心实体（简单实现：取名词性短语）
        entity = self._extract_entity_from_history(recent_context)
        if not entity:
            return question

        # 代词替换
        if _PRONOUN_PATTERN.search(question):
            resolved = _PRONOUN_PATTERN.sub(entity, question)
            return resolved.strip()

        # 省略实体补全：问题很短且不含动词
        if len(question) < 8 and not any(kw in question for kw in _VERB_KEYWORDS):
            return f"{entity} {question}".strip()

        return question

    def _needs_context_resolution(self, question: str) -> bool:
        """判断当前问题是否需要上下文补全。"""
        # 含代词
        if _PRONOUN_PATTERN.search(question):
            return True
        # 很短且不含动词
        if len(question) < 8 and not any(kw in question for kw in _VERB_KEYWORDS):
            return True
        return False

    def _extract_entity_from_history(self, history_text: str) -> str:
        """从历史对话中提取核心实体（简单实现）。

        优先提取已知的实体关键词（价格/天气/时间等），
        其次取第一个有意义的长词组。
        """
        if not history_text:
            return ""

        # 优先匹配已知实体关键词
        for kw in _PRICE_KEYWORDS:
            if kw in history_text:
                return kw
        for kw in _CITY_KEYWORDS:
            if kw in history_text:
                return kw

        # 退化：取历史中第一个长度 > 2 的中文词组
        words = re.findall(r"[\u4e00-\u9fff]{2,}", history_text)
        return words[0] if words else ""

    def _rule_based_rewrite(self, question: str) -> List[str]:
        """规则快速路径：命中模板直接生成精确搜索 query。

        Args:
            question: 已补全的问题。

        Returns:
            规则生成的 query 列表，未命中规则则返回空列表。
        """
        queries: List[str] = []
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        year = str(now.year)
        q_lower = question.lower()

        # 模板 1：时间 + 价格类
        if any(kw in q_lower for kw in _TIME_KEYWORDS) and any(
            kw in q_lower for kw in _PRICE_KEYWORDS
        ):
            entity = self._extract_price_entity(question)
            queries.append(f"{today} {entity} 价格 人民币")
            queries.append(f"今日{entity} 实时价格")

        # 模板 2：天气 + 城市
        elif any(kw in q_lower for kw in _CITY_KEYWORDS):
            city = self._extract_city(question)
            if city:
                queries.append(f"{city} 今天 天气预报 实时温度")
                queries.append(f"{city} weather today")

        # 模板 3：对比类 A 和/与/vs B
        elif _COMPARE_PATTERN.search(question):
            match = _COMPARE_PATTERN.search(question)
            if match:
                a, b = match.group(1).strip(), match.group(2).strip()
                # 截取核心词，避免过长
                a = a[:20] if len(a) > 20 else a
                b = b[:20] if len(b) > 20 else b
                queries.append(f"{a} 评测")
                queries.append(f"{b} 评测")
                queries.append(f"{a} vs {b}")

        # 模板 4：最新/最近 + 事件
        elif any(kw in q_lower for kw in {"最新", "最近", "新闻", "进展", "动态"}):
            # 去掉停用词后的核心词
            core = " ".join(
                w for w in question.split() if w not in _STOPWORDS and len(w) > 1
            )
            if core:
                queries.append(f"{core} 最新进展 {year}")
                queries.append(f"{core} 新闻 {year}")

        # 模板 5：定义/概念类（优先于 how_to，避免"什么是机器学习"被误判为教程）
        elif any(kw in question for kw in _DEFINITION_KEYWORDS):
            queries.extend(self._definition_rewrite(question))

        # 模板 6：报错/调试类（优先于 how_to，避免"怎么解决"被误判）
        elif any(kw in q_lower for kw in _TROUBLESHOOTING_KEYWORDS):
            queries.extend(self._troubleshooting_rewrite(question))

        # 模板 7：教程/操作类
        elif any(kw in q_lower for kw in _HOW_TO_KEYWORDS):
            queries.extend(self._how_to_rewrite(question))

        # 模板 8：代码/实现类
        elif any(kw in q_lower for kw in _CODE_KEYWORDS):
            queries.extend(self._code_rewrite(question))

        # 模板 9：地点/位置类
        elif any(kw in question for kw in _LOCATION_KEYWORDS):
            queries.extend(self._location_rewrite(question))

        # 模板 10：人物类
        elif any(kw in question for kw in _PERSON_KEYWORDS):
            queries.extend(self._person_rewrite(question))

        # 模板 11：产品/评测类
        elif any(kw in question for kw in _PRODUCT_KEYWORDS):
            queries.extend(self._product_rewrite(question))

        return queries

    def _extract_price_entity(self, question: str) -> str:
        """从价格类问题中提取具体实体（黄金/白银/石油等）。"""
        entity_map = {
            "金价": "黄金",
            "黄金": "黄金",
            "银价": "白银",
            "白银": "白银",
            "油价": "石油",
            "石油": "石油",
            "股价": "股票",
            "股票": "股票",
            "汇率": "汇率",
        }
        for key, val in entity_map.items():
            if key in question:
                return val
        return "黄金"  # 默认

    def _extract_city(self, question: str) -> str:
        """从天气类问题中提取城市名。"""
        # 复用 weather_tool 的城市提取逻辑
        try:
            from src.services.tools.plugins._weather_impl import _extract_city_name

            city = _extract_city_name(question)
            return city or ""
        except ImportError:
            return ""

    def _expand_synonyms(self, question: str) -> List[str]:
        """对问题中的常见缩写/同义词做扩展，生成额外 query。"""
        queries: List[str] = []
        q_lower = question.lower()
        for key, synonyms in _SYNONYM_MAP.items():
            if key in q_lower:
                for syn in synonyms:
                    queries.append(question.lower().replace(key, syn))
        return queries

    def _extract_core_terms(self, question: str) -> List[str]:
        """提取问题中的核心实体词（简单实现：去停用词后的长度 >1 的词）。"""
        tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]+", question)
        return [t for t in tokens if t.lower() not in _STOPWORDS and len(t) > 1]

    def _how_to_rewrite(self, question: str) -> List[str]:
        """教程/操作类问题改写。"""
        core = " ".join(self._extract_core_terms(question))
        queries = []
        if core:
            queries.append(f"{core} 教程")
            queries.append(f"{core} 入门指南")
            queries.append(f"{core} 官方文档")
        return queries

    def _definition_rewrite(self, question: str) -> List[str]:
        """定义/概念类问题改写。"""
        # 去掉"是什么"等询问词，保留核心实体
        cleaned = question
        for kw in {"是什么", "什么是", "的定义", "的意思", "的含义"}:
            cleaned = cleaned.replace(kw, "")
        core = " ".join(self._extract_core_terms(cleaned))
        queries = []
        if core:
            queries.append(f"{core} 定义")
            queries.append(f"{core} 维基百科")
            queries.append(f"{core} 介绍")
        return queries

    def _troubleshooting_rewrite(self, question: str) -> List[str]:
        """报错/调试类问题改写。"""
        core = " ".join(self._extract_core_terms(question))
        queries = []
        if core:
            queries.append(f"{core} 报错 解决方案")
            queries.append(f"{core} stackoverflow")
            queries.append(f"{core} 排查")
        return queries

    def _code_rewrite(self, question: str) -> List[str]:
        """代码/实现类问题改写。"""
        core = " ".join(self._extract_core_terms(question))
        queries = []
        if core:
            queries.append(f"{core} 代码示例")
            queries.append(f"{core} github")
            queries.append(f"{core} implementation")
        return queries

    def _location_rewrite(self, question: str) -> List[str]:
        """地点/位置类问题改写。"""
        core = " ".join(self._extract_core_terms(question))
        queries = []
        if core:
            queries.append(f"{core} 地址")
            queries.append(f"{core} 位置")
            queries.append(f"{core} 怎么去")
        return queries

    def _person_rewrite(self, question: str) -> List[str]:
        """人物类问题改写。"""
        core = " ".join(self._extract_core_terms(question))
        queries = []
        if core:
            queries.append(f"{core} 简介")
            queries.append(f"{core} 生平")
            queries.append(f"{core} wikipedia")
        return queries

    def _product_rewrite(self, question: str) -> List[str]:
        """产品/评测类问题改写。"""
        core = " ".join(self._extract_core_terms(question))
        queries = []
        if core:
            queries.append(f"{core} 评测")
            queries.append(f"{core} 用户评价")
            queries.append(f"{core} 推荐")
        return queries

    async def _llm_rewrite(
        self,
        question: str,
        conversation_context: Optional[List[Dict[str, Any]]],
    ) -> List[str]:
        """LLM fallback：用大模型生成多角度搜索 query。

        Args:
            question: 已补全的问题。
            conversation_context: 对话历史。

        Returns:
            LLM 生成的 query 列表，失败时返回空列表。
        """
        # 格式化对话历史
        history_text = ""
        if conversation_context:
            recent = conversation_context[-settings.search.QUERY_REWRITE_CONTEXT_TURNS :]
            history_text = "\n".join(
                f"{msg.get('role', 'user')}: {msg.get('content', '')}"
                for msg in recent
            )

        # 优先从模板文件加载
        if self.prompt_loader.exists("query_rewriter"):
            prompt = await self.prompt_loader.load(
                "query_rewriter",
                num=self.num_queries,
                question=question,
            )
        else:
            prompt = (
                f"请将用户问题改写为 {self.num_queries} 个适合搜索引擎的查询短语。\n"
                "要求：\n"
                "1. 每个短语聚焦不同角度，覆盖问题的不同方面（如定义、教程、示例、常见问题、官方文档）\n"
                "2. 必须保留原问题中的核心实体、技术术语和时间限定词\n"
                "3. 对缩写或技术概念可同时输出中英文查询，提高召回\n"
                "4. 直接返回 JSON 数组，不要任何解释\n\n"
                "示例：\n"
                '用户问题："什么是 RAG"\n'
                '输出：["RAG 检索增强生成 定义", "RAG retrieval augmented generation 介绍", "RAG 技术原理 教程"]\n\n'
                f"对话历史（最近3轮）：\n{history_text}\n\n"
                f"用户问题：{question}\n\n"
                '输出格式：["查询1", "查询2", "查询3"]'
            )

        try:
            response = await self.llm.ainvoke(prompt)
            content = response.content.strip()
            # 尝试提取 JSON 数组
            match = re.search(r"\[[\s\S]*\]", content)
            if match:
                queries = json.loads(match.group())
                if isinstance(queries, list):
                    return [str(q).strip() for q in queries if str(q).strip()]
        except Exception as e:
            logger.warning(f"LLM Query 改写失败: {e}")

        return []

    def _dedup_queries(self, queries: List[str]) -> List[str]:
        """对 query 列表去重（按归一化 key）。"""
        seen: set = set()
        unique: List[str] = []
        for q in queries:
            key = self._normalize_query(q)
            if key and key not in seen:
                seen.add(key)
                unique.append(q)
        return unique

    def _normalize_query(self, query: str) -> str:
        """归一化 query 用于去重比较。"""
        # 去除非字母数字中文的字符
        return re.sub(r"[^\w\u4e00-\u9fff]", "", query).strip().lower()
