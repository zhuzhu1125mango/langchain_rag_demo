"""上下文构建器。

负责将知识库检索结果、联网搜索结果、工具结果等多源信息整合为统一的 LLM 上下文：
1. 来源去重（按内容归一化 key）。
2. 动态预算管理（按 token 预算贪心选择高相关性片段）。
3. Lost in the Middle 重排序（缓解长上下文中间信息遗忘）。
4. 统一溯源编号 [1]、[2]…，并返回编号后的来源元数据。
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.documents import Document

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SourceItem:
    """统一来源项，可表示知识库片段、网页来源或工具结果。"""

    source_type: str  # "kb" | "web" | "tool"
    content: str
    score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def token_count(self) -> int:
        """粗略 token 估算（中文字符按 1 token，英文按空格分词）。"""
        return estimate_token_count(self.content)


def estimate_token_count(text: str) -> int:
    """粗略估算 token 数。

    优先按中文字符数 + 英文单词数统计；若环境安装了 tiktoken，则使用 tiktoken 精确计算。
    """
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        # 退化方案：中文字符按 1 token，英文按词
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        non_chinese = re.sub(r"[\u4e00-\u9fff]", "", text)
        english_words = len(non_chinese.split())
        return chinese_chars + english_words


class ContextBuilder:
    """多源上下文构建器。

    使用流程：
        builder = ContextBuilder()
        context_text, numbered_sources = builder.build_context(
            question="...",
            kb_docs=[Document(...), ...],
            web_sources=[{"title": ..., "content": ..., "url": ...}, ...],
            tool_results=[ToolResult(...), ...],
        )
    """

    def __init__(
        self,
        token_budget: Optional[int] = None,
        reserve_tokens: int = 512,
        min_relevance_score: float = 0.0,
    ):
        """初始化上下文构建器。

        Args:
            token_budget: 可用于参考信息的总 token 预算，None 时使用配置值。
            reserve_tokens: 为系统 prompt、历史对话、问题预留的 token 数。
            min_relevance_score: 来源最低相关度分数，低于该值会被过滤。
        """
        self.token_budget = token_budget or settings.processing.CONTEXT_TOKEN_BUDGET
        self.reserve_tokens = reserve_tokens
        self.min_relevance_score = min_relevance_score

    def build_context(
        self,
        question: str = "",
        kb_docs: Optional[List[Document]] = None,
        web_sources: Optional[List[Dict[str, Any]]] = None,
        tool_results: Optional[List[Any]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """构建统一上下文。

        Args:
            question: 用户问题（用于相关性排序，当前主要依赖传入分数）。
            kb_docs: 知识库检索到的 Document 列表。
            web_sources: 联网搜索结果列表，每项含 title/content/url 等。
            tool_results: 工具执行结果列表（如 ToolResult）。

        Returns:
            (context_text, numbered_sources):
                - context_text: 带统一编号 [n] 的参考信息文本。
                - numbered_sources: 编号后的来源元数据列表，下标 0 对应来源 [1]。
        """
        items = self._collect_items(kb_docs, web_sources, tool_results)
        if not items:
            return "", []

        items = self._deduplicate(items)
        items = self._filter_by_score(items)
        items = self._sort_by_relevance(items)
        items = self._apply_budget(items)
        items = self._lost_in_the_middle_reorder(items)
        items = self._assign_source_indices(items)

        context_text = self._format_context(items)
        numbered_sources = [self._item_to_metadata(item) for item in items]

        logger.info(
            f"ContextBuilder 完成: 原始来源 {len(kb_docs or []) + len(web_sources or []) + len(tool_results or [])} "
            f"-> 去重后 {len(items)} 条，估算 token {sum(item.token_count for item in items)}，预算 {self.token_budget}"
        )
        return context_text, numbered_sources

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------

    def _collect_items(
        self,
        kb_docs: Optional[List[Document]],
        web_sources: Optional[List[Dict[str, Any]]],
        tool_results: Optional[List[Any]],
    ) -> List[SourceItem]:
        """将多源输入统一转换为 SourceItem。"""
        items: List[SourceItem] = []

        for doc in kb_docs or []:
            metadata = doc.metadata if hasattr(doc, "metadata") else {}
            score = float(
                metadata.get("rerank_score", metadata.get("score", 0.0))
            )
            items.append(
                SourceItem(
                    source_type="kb",
                    content=doc.page_content,
                    score=score,
                    metadata={
                        "document_id": metadata.get("document_id", ""),
                        "filename": metadata.get("filename", metadata.get("source", "unknown")),
                        "source": metadata.get("source", ""),
                        "chunk_index": metadata.get("chunk_index", 0),
                        "total_chunks": metadata.get("total_chunks", 1),
                        "kb_id": metadata.get("kb_id", ""),
                        "source_kind": metadata.get("source_kind", "raw"),
                        "score": score,
                        "link_expanded": bool(metadata.get("link_expanded", False)),
                    },
                )
            )

        for src in web_sources or []:
            content = src.get("content") or src.get("page_content", "")
            if not content:
                continue
            items.append(
                SourceItem(
                    source_type="web",
                    content=content,
                    score=float(src.get("score", 0.0)),
                    metadata={
                        "title": src.get("title", ""),
                        "url": src.get("url", ""),
                        "source": src.get("source", "web_search"),
                        "score": src.get("score", 0.0),
                    },
                )
            )

        for tr in tool_results or []:
            if not getattr(tr, "success", True) or not getattr(tr, "output", ""):
                continue
            items.append(
                SourceItem(
                    source_type="tool",
                    content=str(tr.output),
                    score=1.0,  # 工具结果通常可信度高
                    metadata={
                        "tool_name": getattr(tr, "tool_name", "tool"),
                        "source": getattr(tr, "tool_name", "tool"),
                    },
                )
            )

        return items

    def _deduplicate(self, items: List[SourceItem]) -> List[SourceItem]:
        """按内容 hash 去重，保留分数最高的一条。"""
        seen: Dict[str, SourceItem] = {}
        for item in items:
            key = self._content_hash(item.content)
            if key not in seen or item.score > seen[key].score:
                seen[key] = item
        return list(seen.values())

    @staticmethod
    def _content_hash(content: str) -> str:
        """生成归一化内容 hash（忽略首尾空白与连续空白）。"""
        normalized = re.sub(r"\s+", " ", content.strip())
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def _filter_by_score(self, items: List[SourceItem]) -> List[SourceItem]:
        """按最低相关度过滤。"""
        if self.min_relevance_score <= 0:
            return items
        return [item for item in items if item.score >= self.min_relevance_score]

    def _sort_by_relevance(self, items: List[SourceItem]) -> List[SourceItem]:
        """按分数降序排列。"""
        return sorted(items, key=lambda x: (x.score, x.token_count), reverse=True)

    def _apply_budget(self, items: List[SourceItem]) -> List[SourceItem]:
        """按 token 预算贪心选择高分片段。"""
        budget = max(self.token_budget - self.reserve_tokens, 0)
        selected: List[SourceItem] = []
        used = 0
        for item in items:
            cost = item.token_count + 10  # 每个来源预留编号/换行开销
            if used + cost <= budget:
                selected.append(item)
                used += cost
            else:
                # 预算不足时停止（贪心策略）
                break
        return selected

    def _lost_in_the_middle_reorder(self, items: List[SourceItem]) -> List[SourceItem]:
        """Lost in the Middle 重排序。

        将高相关性来源放在上下文开头和结尾，低相关性的放中间，
        以缓解 LLM 对长上下文中间位置信息的遗忘。
        """
        if len(items) <= 2:
            return items

        result: List[Optional[SourceItem]] = [None] * len(items)
        left, right = 0, len(items) - 1
        # 输入已按分数降序
        for i, item in enumerate(items):
            if i % 2 == 0:
                result[left] = item
                left += 1
            else:
                result[right] = item
                right -= 1
        return [item for item in result if item is not None]

    def _assign_source_indices(self, items: List[SourceItem]) -> List[SourceItem]:
        """为每个来源分配统一编号 [1]、[2]…（写入 metadata）。"""
        for i, item in enumerate(items, start=1):
            item.metadata["source_index"] = i
        return items

    def _format_context(self, items: List[SourceItem]) -> str:
        """将来源项格式化为带编号的上下文文本。"""
        parts = []
        for item in items:
            idx = item.metadata.get("source_index", 0)
            header = f"[{idx}]"
            if item.source_type == "web":
                title = item.metadata.get("title", "")
                url = item.metadata.get("url", "")
                if title:
                    header += f" {title}"
                if url:
                    header += f" ({url})"
            elif item.source_type == "kb":
                filename = item.metadata.get("filename", "unknown")
                header += f" {filename}"
            elif item.source_type == "tool":
                tool_name = item.metadata.get("tool_name", "tool")
                header += f" 工具:{tool_name}"
            parts.append(f"{header}\n{item.content}")
        return "\n\n".join(parts)

    def _item_to_metadata(self, item: SourceItem) -> Dict[str, Any]:
        """将 SourceItem 转换为前端/下游需要的元数据字典。"""
        metadata = dict(item.metadata)
        metadata["source_type"] = item.source_type
        metadata["content"] = item.content
        metadata["score"] = item.score
        return metadata


# 保留向后兼容的便捷函数
def build_context(
    question: str = "",
    kb_docs: Optional[List[Document]] = None,
    web_sources: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Any]] = None,
    token_budget: Optional[int] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """便捷函数：使用默认配置构建上下文。"""
    builder = ContextBuilder(token_budget=token_budget)
    return builder.build_context(
        question=question,
        kb_docs=kb_docs,
        web_sources=web_sources,
        tool_results=tool_results,
    )
