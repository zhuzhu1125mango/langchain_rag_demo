"""输出清洗模块。

检测并清洗 LLM 输出中的工具调用 JSON 污染、代码块、思考标签等异常内容。
并提供 AnswerVerifier 对生成答案与检索来源进行事实一致性校验。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger(__name__)


# 污染模式正则
_TOOL_CALL_POLLUTION_PATTERNS = [
    re.compile(r'\[\s*\{\s*"name"\s*:\s*"\w+"', re.IGNORECASE),
    re.compile(r'\{\s*"name"\s*:\s*"\w+"', re.IGNORECASE),
    re.compile(r'"tool"\s*:\s*"\w+"', re.IGNORECASE),
    re.compile(r'"arguments"\s*:\s*\{\s*"', re.IGNORECASE),
    re.compile(r'<tool_call>\s*\[', re.IGNORECASE),
    re.compile(r'<tool_call>\s*\{', re.IGNORECASE),
    re.compile(r'<RichMediaReference>', re.IGNORECASE),
]

# 需要过滤的思考/元数据标签
_THINKING_TAG_PATTERNS = [
    re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<thinking>.*?</thinking>', re.DOTALL | re.IGNORECASE),
    re.compile(r'</think>', re.IGNORECASE),
]

# json 代码块
_JSON_CODE_BLOCK_PATTERN = re.compile(r'```json\s*([\s\S]*?)\s*```', re.IGNORECASE)

# 答案开头常见的搜索过程前缀（用于兜底过滤）
_ANSWER_PREFIX_PATTERNS = [
    re.compile(r'^(根据搜索结果[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(根据参考信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(根据提供的参考信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(资料显示[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(数据显示[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(经查询[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(我查询到[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(我认为[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(在我看来[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(根据以上信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(综上所述[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(结合参考信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(结合以上信息[，：:；]?)\s*', re.IGNORECASE),
]


class NumericHallucinationDetector:
    """检测 LLM 输出中的数字是否与参考数据一致。"""

    _NUMBER_PATTERN = re.compile(
        r"(?:约|大概|大约|约为)?\s*[¥$€£]?\s*"
        r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:元|美元|欧元|日元|港元|英镑|元/克|美元/盎司|%)?",
        re.UNICODE,
    )

    @classmethod
    def extract_numbers(cls, text: str) -> List[Dict[str, Any]]:
        """从文本中提取带上下文的数值。"""
        results = []
        for m in cls._NUMBER_PATTERN.finditer(text):
            raw = m.group(0)
            num_str = m.group(1).replace(",", "")
            try:
                value = Decimal(num_str)
            except InvalidOperation:
                continue
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            context = text[start:end]
            results.append({
                "value": value,
                "raw": raw,
                "context": context,
                "position": m.start(),
            })
        return results

    @classmethod
    def detect(
        cls,
        answer: str,
        reference_numbers: List[Decimal],
        tolerance: float = 0.05,
    ) -> Tuple[bool, List[str]]:
        """检测答案中的数值是否超出参考范围。

        Args:
            answer: LLM 生成的答案。
            reference_numbers: 参考数值列表（如工具返回的价格）。
            tolerance: 相对偏差容忍度。

        Returns:
            (has_hallucination, warnings)
        """
        if not reference_numbers:
            numbers = cls.extract_numbers(answer)
            if numbers:
                return True, ["无权威参考数据，但答案包含具体数值，可能存在幻觉"]
            return False, []

        numbers = cls.extract_numbers(answer)
        hallucinated = []

        for num in numbers:
            value = num["value"]
            closest = min(reference_numbers, key=lambda v: abs(v - value))
            if closest == 0:
                deviation = float("inf")
            else:
                deviation = abs(value - closest) / closest
            if deviation > tolerance:
                hallucinated.append({
                    "value": value,
                    "closest_reference": closest,
                    "deviation": deviation,
                    "context": num["context"],
                })

        warnings = [
            f"答案中的数值 {h['value']} 与参考值 {h['closest_reference']} 偏差 {h['deviation']:.2%}"
            for h in hallucinated
        ]
        return bool(hallucinated), warnings


class OutputSanitizer:
    """LLM 输出清洗器。"""

    @staticmethod
    def looks_like_tool_call(text: str) -> bool:
        """判断文本是否包含工具调用 JSON 污染。"""
        if not text:
            return False
        return any(p.search(text) for p in _TOOL_CALL_POLLUTION_PATTERNS)

    @staticmethod
    def extract_json_code_block(text: str) -> Optional[str]:
        """从文本中提取 ```json ... ``` 代码块内容。"""
        match = _JSON_CODE_BLOCK_PATTERN.search(text)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def remove_thinking_tags(text: str) -> str:
        """移除 <think> / <thinking> 等思考标签。"""
        cleaned = text
        for pattern in _THINKING_TAG_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip()

    @staticmethod
    def remove_answer_prefix(text: str) -> str:
        """移除答案开头常见的搜索过程前缀（兜底过滤）。

        仅处理文本开头处的前缀，不修改正文内容。
        """
        if not text:
            return text
        cleaned = text
        for pattern in _ANSWER_PREFIX_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip()

    @classmethod
    def sanitize(
        cls,
        text: str,
        reference_numbers: Optional[List[Decimal]] = None,
        low_confidence_threshold: float = 0.6,
    ) -> Tuple[str, bool]:
        """清洗 LLM 输出。

        Args:
            text: 原始 LLM 输出。
            reference_numbers: 可选的参考数值列表，用于数字幻觉检测。
            low_confidence_threshold: 低置信度阈值，低于此值时对具体数字更严格。

        Returns:
            (cleaned_text, was_polluted): 清洗后的文本，以及是否检测到污染。
        """
        if not text:
            return text, False

        was_polluted = cls.looks_like_tool_call(text)

        # 移除思考标签
        cleaned = cls.remove_thinking_tags(text)

        # 如果整段是 JSON 工具调用，直接清空
        stripped = cleaned.strip()
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict) and "name" in data and "arguments" in data:
                    return "", True
                if isinstance(data, list) and all(
                    isinstance(item, dict) and "name" in item for item in data
                ):
                    return "", True
            except Exception:
                pass

        # 移除 json 代码块（如果是工具调用）
        def replace_json_block(match):
            content = match.group(1).strip()
            if cls.looks_like_tool_call(content):
                return ""
            return content

        cleaned = _JSON_CODE_BLOCK_PATTERN.sub(replace_json_block, cleaned)

        # 移除 <tool_call> 标签块
        cleaned = re.sub(r'<tool_call>\s*[\s\S]*?\s*</tool_call>', '', cleaned, flags=re.IGNORECASE)

        # 移除独立的工具 JSON 行
        lines = []
        for line in cleaned.splitlines():
            if cls.looks_like_tool_call(line):
                continue
            lines.append(line)
        cleaned = "\n".join(lines)

        # 数字幻觉检测
        if reference_numbers:
            has_hallucination, warnings = NumericHallucinationDetector.detect(
                cleaned, reference_numbers, tolerance=low_confidence_threshold / 12
            )
            if has_hallucination:
                # 在答案末尾追加风险提示，不直接删除整段（避免误杀）
                cleaned += "\n\n[系统提示：以上回答中的部分数值与权威数据源存在偏差，建议您通过官方渠道核实。]"
                was_polluted = True

        # 兜底移除答案开头常见搜索过程前缀
        cleaned = cls.remove_answer_prefix(cleaned)

        return cleaned.strip(), was_polluted


# ---------------------------------------------------------------------------
# AnswerVerifier：答案事实一致性校验
# ---------------------------------------------------------------------------

# 答案中的引用标记 [n] 或 [?]
_ANSWER_CITATION_RE = re.compile(r"\[(\d+|\?)\]")
# 日期模式：2026年、2026-06-27、今天/昨日/本周/上周
_DATE_PATTERNS = [
    re.compile(r"\d{4}\s*年"),
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}/\d{1,2}/\d{1,2}"),
    re.compile(r"今天|今日|昨天|昨日|前天|本周|上周|本月|上月|今年|去年"),
]
# 句末标点（用于统计事实句）
_FACT_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
# 格式行（标题、分隔线、列表符号）—— 不计入事实句
_FORMAT_LINE_RE = re.compile(
    r"^\s*(?:[-=*]{3,}|#{1,6}\s|```|>\s*|\s*[-*+]\s+|\s*\d+\.\s+)|^\s*$"
)


@dataclass
class VerificationResult:
    """答案校验结果。

    Attributes:
        is_consistent: 答案是否与来源一致（无严重冲突）。
        confidence: 综合置信度 0~1。
        warnings: 警告信息列表。
        unsupported_claims: 未在 sources 中找到支撑的声明。
        conflicting_claims: 与 sources 冲突的声明。
        citation_coverage: 引用覆盖率（有 [n] 的句子 / 总事实句，[?] 不计入分子）。
        cross_source_consistency: 多源交叉一致的比例。
        source_authority: 来源平均权威度。
        freshness_score: 来源平均新鲜度。
    """

    is_consistent: bool
    confidence: float
    warnings: List[str] = field(default_factory=list)
    unsupported_claims: List[str] = field(default_factory=list)
    conflicting_claims: List[str] = field(default_factory=list)
    citation_coverage: float = 0.0
    cross_source_consistency: float = 0.0
    source_authority: float = 0.0
    freshness_score: float = 0.0


class AnswerVerifier:
    """答案事实一致性校验器。

    校验生成答案与检索来源的一致性，输出置信度与警告，
    供前端展示可信度角标与风险提示。

    分级策略：
        - 答案 < ANSWER_VERIFIER_LLM_THRESHOLD 字 → 纯规则校验（0 次 LLM）。
        - 答案 ≥ 阈值 → 规则初筛 + LLM 复核（1 次 LLM，可选）。

    置信度计算：
        confidence = 0.4 * cross_source_consistency
                   + 0.3 * citation_coverage
                   + 0.2 * source_authority
                   + 0.1 * freshness_score
    """

    def __init__(self, embeddings: Optional[Any] = None, llm: Optional[Any] = None):
        """初始化答案校验器。

        Args:
            embeddings: 预留，用于后续 LLM 复核时的语义校验。
            llm: 预留，答案较长时触发 LLM 复核。
        """
        self.embeddings = embeddings
        self.llm = llm
        # 延迟导入 SearchPostprocessor 以避免循环依赖
        self._postprocessor = None
        # 置信度阈值
        self.warning_threshold: float = settings.search.ANSWER_CONFIDENCE_WARNING
        self.low_threshold: float = settings.search.ANSWER_CONFIDENCE_LOW
        self.llm_trigger_len: int = settings.search.ANSWER_VERIFIER_LLM_THRESHOLD

    def _get_postprocessor(self):
        """延迟加载 SearchPostprocessor 实例（避免循环导入）。"""
        if self._postprocessor is None:
            from src.services.search_postprocessor import SearchPostprocessor

            self._postprocessor = SearchPostprocessor()
        return self._postprocessor

    async def verify(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
        cross_source_data: Optional[Dict[str, List[Decimal]]] = None,
    ) -> VerificationResult:
        """校验答案与检索结果的一致性。

        Args:
            answer: LLM 生成的答案（可能已含 [n] 引用）。
            sources: 来源列表，每项形如
                     {"source_index": 1, "title": ..., "content": ..., "url": ...}。
            cross_source_data: 来自 SearchPostprocessor.cross_source_validate 的结果，
                               包含 validated_values / single_source_values / conflicting_values。

        Returns:
            VerificationResult 校验结果。
        """
        if not answer or not answer.strip():
            return VerificationResult(
                is_consistent=True, confidence=1.0, warnings=[]
            )

        warnings: List[str] = []
        unsupported_claims: List[str] = []
        conflicting_claims: List[str] = []

        # 1. 数字一致性
        self._check_numeric_consistency(
            answer, sources, cross_source_data, unsupported_claims, conflicting_claims, warnings
        )

        # 2. 日期一致性
        self._check_date_consistency(answer, sources, warnings)

        # 3. 引用真实性
        valid_indices = self._collect_valid_indices(sources)
        self._check_citation_validity(answer, valid_indices, warnings)

        # 4. 计算四因子
        cross_source_consistency = self._compute_cross_source_consistency(
            answer, cross_source_data
        )
        citation_coverage = self._compute_citation_coverage(answer)
        source_authority = self._compute_source_authority(sources)
        freshness_score = self._compute_freshness_score(sources)

        # 5. 综合置信度
        confidence = (
            0.4 * cross_source_consistency
            + 0.3 * citation_coverage
            + 0.2 * source_authority
            + 0.1 * freshness_score
        )
        confidence = max(0.0, min(1.0, confidence))

        is_consistent = confidence >= self.low_threshold and not conflicting_claims

        # 低置信度或冲突追加警告
        if confidence < self.low_threshold:
            warnings.append("当前回答可信度较低，未在多个独立来源中得到验证")
        elif confidence < self.warning_threshold:
            warnings.append("部分信息来源单一或时效性存疑")

        return VerificationResult(
            is_consistent=is_consistent,
            confidence=round(confidence, 4),
            warnings=warnings,
            unsupported_claims=unsupported_claims,
            conflicting_claims=conflicting_claims,
            citation_coverage=round(citation_coverage, 4),
            cross_source_consistency=round(cross_source_consistency, 4),
            source_authority=round(source_authority, 4),
            freshness_score=round(freshness_score, 4),
        )

    def _check_numeric_consistency(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
        cross_source_data: Optional[Dict[str, List[Decimal]]],
        unsupported_claims: List[str],
        conflicting_claims: List[str],
        warnings: List[str],
    ) -> None:
        """校验答案中的数值与来源数值的一致性。

        Args:
            answer: 答案文本。
            sources: 来源列表。
            cross_source_data: 多源交叉验证数据。
            unsupported_claims: 收集未支撑声明。
            conflicting_claims: 收集冲突声明。
            warnings: 收集警告。
        """
        postprocessor = self._get_postprocessor()
        answer_nums = postprocessor.extract_numeric_values(answer)
        if not answer_nums:
            return

        # 收集来源中的所有数值
        source_nums: List[Decimal] = []
        for s in sources or []:
            content = s.get("content", "") or ""
            for item in postprocessor.extract_numeric_values(content):
                source_nums.append(item["value"])

        # 多源交叉数据
        validated_values = (cross_source_data or {}).get("validated_values", [])
        conflicting_values = (cross_source_data or {}).get("conflicting_values", [])

        tolerance = 0.05
        for num_item in answer_nums:
            value = num_item["value"]
            # 跳过噪音数值（年份等）
            if postprocessor._is_noise_number(value, num_item["context"]):
                continue

            # 检查是否在冲突数值中
            if self._is_value_in_list(value, conflicting_values, tolerance):
                conflicting_claims.append(
                    f"数值 {value} 与多源数据存在冲突"
                )
                continue

            # 检查是否在来源数值中有相近值
            if source_nums:
                closest = self._find_closest(value, source_nums)
                if closest is not None:
                    deviation = (
                        abs(value - closest) / abs(closest)
                        if closest != 0
                        else float("inf")
                    )
                    if deviation > tolerance:
                        # 来源中有数值但偏差大
                        unsupported_claims.append(
                            f"数值 {value} 与来源最接近值 {closest} 偏差 {deviation:.2%}"
                        )
                # closest is None 表示来源数值都为 0，跳过
            else:
                # 来源无数值但答案有具体数值
                unsupported_claims.append(f"数值 {value} 未在来源中找到支撑")

    def _check_date_consistency(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
        warnings: List[str],
    ) -> None:
        """校验答案中的日期是否在来源中出现。

        Args:
            answer: 答案文本。
            sources: 来源列表。
            warnings: 收集警告。
        """
        answer_dates = set()
        for pattern in _DATE_PATTERNS:
            for m in pattern.finditer(answer):
                answer_dates.add(m.group(0))

        if not answer_dates:
            return

        # 收集来源中的日期
        source_dates = set()
        source_text = " ".join(
            s.get("content", "") or "" for s in sources or []
        )
        for pattern in _DATE_PATTERNS:
            for m in pattern.finditer(source_text):
                source_dates.add(m.group(0))

        # 答案中的日期在来源中完全不存在 → 警告
        missing_dates = answer_dates - source_dates
        # 过滤掉相对日期（今天/昨天等），这些不需要来源支撑
        relative_dates = {"今天", "今日", "昨天", "昨日", "前天", "本周", "上周", "本月", "上月", "今年", "去年"}
        missing_absolute = missing_dates - relative_dates
        if missing_absolute:
            warnings.append(
                f"答案中的日期 {', '.join(sorted(missing_absolute))} 未在来源中出现"
            )

    def _check_citation_validity(
        self,
        answer: str,
        valid_indices: set,
        warnings: List[str],
    ) -> None:
        """校验答案中的引用编号是否有效。

        Args:
            answer: 答案文本。
            valid_indices: 有效 source_index 集合。
            warnings: 收集警告。
        """
        citations = _ANSWER_CITATION_RE.findall(answer)
        if not citations:
            return

        for tag in citations:
            if tag == "?":
                continue
            try:
                idx = int(tag)
            except ValueError:
                continue
            if valid_indices and idx not in valid_indices:
                warnings.append(f"引用 [{idx}] 超出有效来源范围")

    def _compute_cross_source_consistency(
        self,
        answer: str,
        cross_source_data: Optional[Dict[str, List[Decimal]]],
    ) -> float:
        """计算多源交叉一致性比例。

        答案中的关键数值在 validated_values 中的比例。

        Args:
            answer: 答案文本。
            cross_source_data: 多源交叉验证数据。

        Returns:
            交叉一致性比例 0~1。无数值返回 0.0。
        """
        if not cross_source_data:
            return 0.0

        postprocessor = self._get_postprocessor()
        answer_nums = postprocessor.extract_numeric_values(answer)
        # 过滤噪音数值
        key_nums = [
            item["value"]
            for item in answer_nums
            if not postprocessor._is_noise_number(item["value"], item["context"])
        ]
        if not key_nums:
            return 0.0

        validated = cross_source_data.get("validated_values", [])
        if not validated:
            return 0.0

        matched = sum(
            1 for n in key_nums if self._is_value_in_list(n, validated, 0.05)
        )
        return matched / len(key_nums)

    def _compute_citation_coverage(self, answer: str) -> float:
        """计算引用覆盖率：有 [n] 引用的事实句 / 总事实句。

        [?] 不计入分子。

        Args:
            answer: 答案文本。

        Returns:
            引用覆盖率 0~1。无事实句返回 0.0。
        """
        fact_sentences = self._extract_fact_sentences(answer)
        if not fact_sentences:
            return 0.0

        cited_count = 0
        for sent in fact_sentences:
            # 统计有效数字引用 [n]（非 [?]）
            numeric_citations = [
                t for t in _ANSWER_CITATION_RE.findall(sent) if t != "?"
            ]
            if numeric_citations:
                cited_count += 1

        return cited_count / len(fact_sentences)

    def _compute_source_authority(self, sources: List[Dict[str, Any]]) -> float:
        """计算来源平均权威度。

        Args:
            sources: 来源列表。

        Returns:
            平均权威度 0~1。无来源返回 0.0。
        """
        if not sources:
            return 0.0

        postprocessor = self._get_postprocessor()
        scores = []
        for s in sources:
            url = s.get("url", "") or ""
            if url:
                scores.append(postprocessor._authority_score(url))
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def _compute_freshness_score(self, sources: List[Dict[str, Any]]) -> float:
        """计算来源平均新鲜度。

        Args:
            sources: 来源列表。

        Returns:
            平均新鲜度 0~1。无来源返回 0.0。
        """
        if not sources:
            return 0.0

        postprocessor = self._get_postprocessor()
        scores = []
        for s in sources:
            content = s.get("content", "") or ""
            if content:
                scores.append(postprocessor._freshness_score(content))
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def _extract_fact_sentences(self, text: str) -> List[str]:
        """提取事实声明句（排除格式行和过短句）。

        Args:
            text: 答案文本。

        Returns:
            事实句列表。
        """
        if not text:
            return []

        sentences: List[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or _FORMAT_LINE_RE.match(stripped):
                continue
            for part in _FACT_SENTENCE_SPLIT_RE.split(stripped):
                if part and len(part.strip()) >= 5:
                    sentences.append(part)
        return sentences

    def _collect_valid_indices(self, sources: List[Dict[str, Any]]) -> set:
        """收集有效的 source_index 集合。"""
        valid = set()
        for s in sources or []:
            idx = s.get("source_index")
            if isinstance(idx, int) and idx > 0:
                valid.add(idx)
        return valid

    @staticmethod
    def _is_value_in_list(
        value: Decimal, candidates: List[Decimal], tolerance: float
    ) -> bool:
        """判断数值是否在候选列表中（相对偏差 ≤ tolerance）。"""
        for cand in candidates:
            if cand == 0:
                continue
            if abs(value - cand) / abs(cand) <= tolerance:
                return True
        return False

    @staticmethod
    def _find_closest(value: Decimal, candidates: List[Decimal]) -> Optional[Decimal]:
        """在候选列表中找最接近的数值。"""
        if not candidates:
            return None
        non_zero = [c for c in candidates if c != 0]
        if not non_zero:
            return None
        return min(non_zero, key=lambda c: abs(value - c))

    def format_warning_suffix(self, result: VerificationResult) -> str:
        """根据校验结果生成追加到答案末尾的警告文本。

        Args:
            result: 校验结果。

        Returns:
            警告文本，无需追加时返回空字符串。
        """
        if result.confidence >= self.warning_threshold:
            return ""
        if result.confidence < self.low_threshold:
            return "\n\n⚠️ 当前回答可信度较低，未在多个独立来源中得到验证，请谨慎参考。"
        return "\n\n⚠️ 部分信息来源单一或时效性存疑，建议核实。"
