"""答案校验的具体检查项：数值/日期一致性、引用有效性、跨源一致性与可信度评分（CODE-1 拆分）。"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

from src.services.answer_verification_types import (
    _ANSWER_CITATION_RE,
    _DATE_PATTERNS,
    _FACT_SENTENCE_SPLIT_RE,
    _FORMAT_LINE_RE,
)

class AnswerVerifierChecksMixin:
    """校验检查项，由 AnswerVerifier 组合。"""

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
