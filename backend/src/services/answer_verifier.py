"""答案校验器：编排各项检查并生成警告后缀（CODE-1 拆分，检查项见 answer_verifier_checks）。"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.config import settings

logger = logging.getLogger(__name__)

from src.services.answer_verification_types import VerificationResult
from src.services.answer_verifier_checks import AnswerVerifierChecksMixin

class AnswerVerifier(AnswerVerifierChecksMixin):
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

        # 3. 引用真实性（单列 citation_issues：下游要能结构化判断引用是否对得上）
        citation_issues: List[str] = []
        valid_indices = self._collect_valid_indices(sources)
        self._check_citation_validity(answer, valid_indices, warnings, citation_issues)

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
            citation_issues=citation_issues,
            citation_coverage=round(citation_coverage, 4),
            cross_source_consistency=round(cross_source_consistency, 4),
            source_authority=round(source_authority, 4),
            freshness_score=round(freshness_score, 4),
        )

    def format_warning_suffix(self, result: VerificationResult) -> str:
        """根据校验结果生成追加到答案末尾的警告文本。

        判定顺序按「信号强弱」而非只看加权置信度：多源数值冲突与引用越界都属
        确定性证据，即使四项加权分很高也必须提示。此前只看 confidence，导致
        `is_consistent=False`（有冲突）的答案照样不带任何警告。

        Args:
            result: 校验结果。

        Returns:
            警告文本，无需追加时返回空字符串。
        """
        if result.conflicting_claims:
            return "\n\n⚠️ 不同来源对同一数据给出的数值不一致，建议以官方渠道为准。"
        if result.citation_issues:
            return "\n\n⚠️ 答案中的部分引用编号在来源列表中不存在，请谨慎参考。"
        if result.confidence >= self.warning_threshold:
            return ""
        if result.confidence < self.low_threshold:
            return "\n\n⚠️ 当前回答可信度较低，未在多个独立来源中得到验证，请谨慎参考。"
        return "\n\n⚠️ 部分信息来源单一或时效性存疑，建议核实。"
