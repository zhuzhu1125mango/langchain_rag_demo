"""引用补全模块。

生成答案后，用 embedding 匹配为答案中每个事实声明句补上来源编号 [n]，
或标记为未验证 [?]，不依赖 LLM 的指令遵循能力。

针对 7B 本地模型引用遵守率不稳定（约 60~70%）的问题，提供确定性的后处理保障。
"""

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger(__name__)

# 句子分隔符：中英文句号、问号、感叹号
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
# 已有引用标记 [n] 的匹配
_CITATION_RE = re.compile(r"\[(\d+|\?)\]")
# 纯格式行：分隔线、标题、列表符号、代码块标记
_FORMAT_LINE_RE = re.compile(
    r"^\s*(?:[-=*]{3,}|#{1,6}\s|```|>\s*|\s*[-*+]\s+|\s*\d+\.\s+)|^\s*$"
)
# 最小事实声明长度（字）
_MIN_FACT_SENTENCE_LEN = 5


class CitationBackfiller:
    """引用补全器：用 embedding 匹配为答案中每个事实声明句补上来源编号。

    设计目标：
        - 不依赖 LLM 指令遵循，确定性补全引用。
        - 复用项目配置的 OllamaEmbeddings（默认 bge-m3）。
        - embeddings 不可用时降级为"仅校验已有引用"，不阻塞主流程。

    补全规则：
        1. 已有 [n] → 校验 n 是否在有效 source_index 范围内，无效则改为 [?]。
        2. 无引用 → embedding 余弦相似度匹配，最高分：
           - score > CITATION_MATCH_THRESHOLD → 补 [n]
           - score ≤ 阈值 → 补 [?]（未验证标记）
    """

    def __init__(self, embeddings: Optional[Any] = None):
        """初始化引用补全器。

        Args:
            embeddings: OllamaEmbeddings 实例，复用项目配置的 embedding 模型。
                        为 None 时降级为"仅校验已有引用"模式。
        """
        self.embeddings = embeddings
        self.match_threshold: float = settings.search.CITATION_MATCH_THRESHOLD

    async def backfill(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
    ) -> str:
        """补全引用并返回处理后的答案。

        Args:
            answer: LLM 生成的原始答案，可能已含部分 [n] 引用。
            sources: 来源列表，每项形如
                     {"source_index": 1, "title": ..., "content": ...}。

        Returns:
            补全引用后的答案文本。embeddings 不可用时仅校验已有引用。
        """
        if not answer or not answer.strip():
            return answer

        # 无来源时，仅校验已有引用是否有效（无来源则所有 [n] 都改为 [?]）
        valid_indices = self._collect_valid_indices(sources)

        # 切句
        sentences = self._split_sentences(answer)
        if not sentences:
            return answer

        # 预计算 source embeddings（仅在有可用 embeddings 且有来源时）
        source_embeddings: List[Tuple[int, List[float]]] = []
        if self.embeddings is not None and sources:
            source_embeddings = await self._compute_source_embeddings(sources)

        # 逐句处理
        processed_sentences: List[str] = []
        for sent in sentences:
            processed = await self._process_sentence(sent, valid_indices, source_embeddings)
            processed_sentences.append(processed)

        return "".join(processed_sentences)

    def _collect_valid_indices(self, sources: List[Dict[str, Any]]) -> set:
        """收集有效的 source_index 集合。

        Args:
            sources: 来源列表。

        Returns:
            有效 source_index 集合，无来源时返回空集。
        """
        valid = set()
        for s in sources or []:
            idx = s.get("source_index")
            if isinstance(idx, int) and idx > 0:
                valid.add(idx)
        return valid

    def _split_sentences(self, text: str) -> List[str]:
        """按行 + 中英文句末标点切句，保留标点与换行。

        先按行拆分，使独占一行的格式行（标题、分隔线、列表符号）被单独识别；
        再对每行按句末标点切分。这样格式行与正文混合时正文仍能被补全。

        Args:
            text: 原始答案文本。

        Returns:
            切分后的句子片段列表（保留标点和换行等空白）。
        """
        if not text:
            return []

        sentences: List[str] = []
        # 先按行拆分（保留换行符）
        for line in text.splitlines(keepends=True):
            if not line.strip():
                # 空行原样保留
                sentences.append(line)
                continue
            # 独占一行的格式行整行保留，不再按句末标点切分
            if _FORMAT_LINE_RE.match(line.strip()):
                sentences.append(line)
                continue
            # 正文行按句末标点切分
            parts = _SENTENCE_SPLIT_RE.split(line)
            for part in parts:
                if part:
                    sentences.append(part)
        return sentences

    async def _process_sentence(
        self,
        sentence: str,
        valid_indices: set,
        source_embeddings: List[Tuple[int, List[float]]],
    ) -> str:
        """处理单个句子：校验已有引用或补全缺失引用。

        Args:
            sentence: 原始句子文本。
            valid_indices: 有效 source_index 集合。
            source_embeddings: 预计算的 (source_index, embedding) 列表。

        Returns:
            处理后的句子。
        """
        stripped = sentence.strip()
        # 非事实句或格式行，原样返回
        if (
            not stripped
            or _FORMAT_LINE_RE.match(stripped)
            or len(stripped) < _MIN_FACT_SENTENCE_LEN
        ):
            return sentence

        existing_citations = _CITATION_RE.findall(stripped)

        if existing_citations:
            # 已有引用 → 校验有效性
            return self._validate_existing_citations(sentence, existing_citations, valid_indices)

        # 无引用 → embedding 匹配补全
        return await self._backfill_citation(sentence, source_embeddings)

    def _validate_existing_citations(
        self,
        sentence: str,
        citations: List[str],
        valid_indices: set,
    ) -> str:
        """校验已有引用编号是否在有效范围内，无效则改为 [?]。

        Args:
            sentence: 原始句子。
            citations: 已有引用标记列表（如 ["1", "?", "3"]）。
            valid_indices: 有效 source_index 集合。

        Returns:
            校验后的句子。无有效来源时所有 [n] 改为 [?]。
        """
        if not valid_indices:
            # 无有效来源，所有数字引用改为 [?]
            return _CITATION_RE.sub("[?]", sentence)

        def _replace(match: re.Match) -> str:
            tag = match.group(1)
            if tag == "?":
                return match.group(0)
            try:
                idx = int(tag)
            except ValueError:
                return "[?]"
            return match.group(0) if idx in valid_indices else "[?]"

        return _CITATION_RE.sub(_replace, sentence)

    async def _backfill_citation(
        self,
        sentence: str,
        source_embeddings: List[Tuple[int, List[float]]],
    ) -> str:
        """为无引用句子补全来源编号。

        Args:
            sentence: 原始句子。
            source_embeddings: 预计算的 (source_index, embedding) 列表。

        Returns:
            补全引用后的句子。无法匹配时追加 [?]。
        """
        # 无可用 embeddings 或无来源 → 追加 [?]
        if not self.embeddings or not source_embeddings:
            return self._append_citation(sentence, "?")

        # 计算句子 embedding
        try:
            query_embedding = await self.embeddings.aembed_query(sentence.strip())
        except Exception as e:
            logger.warning("embedding 查询失败，降级为 [?]：%s", e)
            return self._append_citation(sentence, "?")

        # 计算与各 source 的余弦相似度，取最高分
        best_idx, best_score = self._find_best_match(query_embedding, source_embeddings)

        if best_score > self.match_threshold and best_idx is not None:
            return self._append_citation(sentence, str(best_idx))
        return self._append_citation(sentence, "?")

    async def _compute_source_embeddings(
        self, sources: List[Dict[str, Any]]
    ) -> List[Tuple[int, List[float]]]:
        """批量预计算所有来源的 embedding，缓存复用。

        Args:
            sources: 来源列表。

        Returns:
            (source_index, embedding) 列表，计算失败或无可用内容时返回空列表。
        """
        valid_sources = [
            s
            for s in sources
            if isinstance(s.get("source_index"), int)
            and s.get("source_index", 0) > 0
            and (s.get("title") or s.get("content"))
        ]
        if not valid_sources:
            return []

        # 拼接 title + content 作为 embedding 输入
        texts = [
            f"{s.get('title', '')} {s.get('content', '')}".strip()
            for s in valid_sources
        ]
        try:
            embeddings = await self.embeddings.aembed_documents(texts)
        except Exception as e:
            logger.warning("批量计算 source embedding 失败，引用补全降级：%s", e)
            return []

        return [
            (s["source_index"], emb)
            for s, emb in zip(valid_sources, embeddings)
        ]

    def _find_best_match(
        self,
        query_embedding: List[float],
        source_embeddings: List[Tuple[int, List[float]]],
    ) -> Tuple[Optional[int], float]:
        """找出与 query embedding 余弦相似度最高的 source。

        Args:
            query_embedding: 句子 embedding。
            source_embeddings: (source_index, embedding) 列表。

        Returns:
            (最佳 source_index, 最高相似度分数)，无来源返回 (None, 0.0)。
        """
        if not source_embeddings:
            return None, 0.0

        best_idx: Optional[int] = None
        best_score = -1.0
        for src_idx, emb in source_embeddings:
            score = self._cosine_similarity(query_embedding, emb)
            if score > best_score:
                best_score = score
                best_idx = src_idx
        return best_idx, max(0.0, best_score)

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """计算两个向量的余弦相似度。

        Args:
            vec_a: 向量 A。
            vec_b: 向量 B。

        Returns:
            余弦相似度 [-1, 1]，任一向量为零向量返回 0.0。
        """
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _append_citation(sentence: str, tag: str) -> str:
        """在句子末尾（句末标点之前）追加引用标记。

        Args:
            sentence: 原始句子。
            tag: 引用标记（数字或 "?"）。

        Returns:
            追加 [tag] 后的句子。若句末是标点，引用插入在标点前。
        """
        stripped = sentence.rstrip()
        trailing_ws = sentence[len(stripped):]
        if not stripped:
            return sentence

        # 句末标点（中英文）前插入引用
        if stripped[-1] in "。！？!?":
            return f"{stripped[:-1]}[{tag}]{stripped[-1]}{trailing_ws}"
        return f"{stripped}[{tag}]{trailing_ws}"
