"""生成评估器（答案级判分，全确定性规则，零 LLM 依赖）。

维度：
- relevance：问句与答案句的**语义相似度**（bge-m3，取答案中最相关句），
  按 [MIN_SIM, MAX_SIM] 线性映射到 0-1。
- faithfulness：答案**子句级语义依据率**（子句与上下文子句的最大余弦 ≥ 0.70 视为有依据），
  并叠加**数字一致性**闸门（子句中的数字必须能在上下文中找到，防止数值矛盾漏判）；
  embedding 不可用时降级为子句 bigram 覆盖兜底。

为什么不用 LLM-as-judge（2026-10-21 三轮抽检结论）：
- 3B judge 的 relevance **零区分度**——标准正确答案与完全无关答案同为 0.0，且理由
  与答案原文矛盾（三次重复稳定复现）；
- faithfulness 方向可辨但**绝对分被压在 0.7/0.4 两档**（全一致也只给 0.7），不可横向比较。
因此与 AM-1 轨迹判分器同口径，改为确定性规则：可复现、无并发占用、无 Ollama 单槽争抢。
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 相关性：语义相似度映射区间（bge-m3 实测：无关≈0.45，真实答案 0.80+）
RELEVANCE_MIN_SIM = 0.45
RELEVANCE_MAX_SIM = 0.92
# 相关性子句切分/最短长度
RELEVANCE_MIN_SENT_CHARS = 4
# 忠实度：答案子句与上下文子句的最大余弦阈值（bge-m3 实测：有依据 0.76+，无关≈0.4-0.56）
FAITHFULNESS_SEMANTIC_MIN_SIM = 0.70
# 忠实度词面兜底（embedding 不可用时）的子句 bigram 覆盖率阈值
FAITHFULNESS_MIN_COVERAGE = 0.5
FAITHFULNESS_MIN_CLAUSE_CHARS = 4

_PUNCT_RE = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()\[\]【】<>《》\-—_/\\]+")
_DISCLAIMER_RE = re.compile(r"⚠️ 当前回答可信度较低[^\n]*")
# 引用编号（[1]/[2]）是展示标记，不属答案内容；不清除会被当成"数字"触发数字一致性误判
_CITATION_RE = re.compile(r"\[\s*\d+\s*\]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# 拒答/未作答模式：这类句子与上下文语义上常相近（"无法找到相关信息"），
# 若按语义依据判定会被误计为「有依据」，需单独短路为 0（与 AM-1 compliance 同口径）。
_NON_ANSWER_RE = re.compile(
    r"(无法找到|找不到|未找到|没有找到|无法回答|无法提供|无法确定|暂无相关|暂无法|无法直接)"
)
_NON_ANSWER_MAX_CHARS = 40
_SENT_SPLIT_RE = re.compile(r"[。！？!?；;\n]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;。！？!?、\n]+")


@dataclass
class GenerationEvalResult:
    """生成评估结果。"""

    faithfulness: float = 0.0
    relevance: float = 0.0
    faithfulness_reason: str = ""
    relevance_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "faithfulness": self.faithfulness,
            "relevance": self.relevance,
            "faithfulness_reason": self.faithfulness_reason,
            "relevance_reason": self.relevance_reason,
        }


def _bigrams(text: str) -> set:
    """字符二元组集合（与 AM-1 轨迹判分器同口径）。"""
    t = _PUNCT_RE.sub("", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    if not da or not db:
        return 0.0
    return num / (da * db)


def strip_confidence_disclaimer(text: str) -> str:
    """剥离管道附加的置信度声明（展示层产物，不属答案内容，会系统性拉低判分）。"""
    return _DISCLAIMER_RE.sub("", text or "").strip()


def _clean_answer(text: str) -> str:
    """剥离置信度声明与引用编号，得到用于判分的纯答案文本。"""
    return _CITATION_RE.sub("", strip_confidence_disclaimer(text)).strip()


class GenerationEvaluator:
    """确定性生成质量评估器（无 LLM）。"""

    def __init__(self):
        self._embeddings = None

    def _get_embeddings(self):
        if self._embeddings is None:
            from src.services.model_manager import model_manager

            self._embeddings = model_manager.get_embeddings()
        return self._embeddings

    # ------------------------------------------------------------------
    # 忠实度：子句级语义依据 + 数字一致性
    # ------------------------------------------------------------------
    async def evaluate_faithfulness(
        self,
        answer: str,
        contexts: Sequence[str],
    ) -> Dict[str, Any]:
        """答案子句中能被参考上下文**语义支撑**的比例。

        子句与上下文子句的最大余弦 ≥ FAITHFULNESS_SEMANTIC_MIN_SIM 视为有依据
        （语义判定可覆盖合理改写，词面 bigram 回归在 embedding 不可用时兜底）。

        数字一致性：子句内出现的数字必须全部在上下文中出现，否则该子句判为无依据
        （数值矛盾无法靠语义相似度识别，如「3 个工作日」vs「30 个工作日」）。
        """
        clean = _clean_answer(answer)
        if not clean:
            return {"score": 0.0, "reason": "答案为空"}
        if len(clean) <= _NON_ANSWER_MAX_CHARS and _NON_ANSWER_RE.search(clean):
            return {"score": 0.0, "reason": "拒答/未作答，不计依据覆盖"}
        if not contexts or not any(c.strip() for c in contexts):
            return {"score": 0.0, "reason": "参考上下文为空"}

        clauses = [
            c.strip() for c in _CLAUSE_SPLIT_RE.split(clean)
            if len(c.strip()) >= FAITHFULNESS_MIN_CLAUSE_CHARS
        ]
        if not clauses:
            return {"score": 0.0, "reason": "答案无可判定子句"}

        ctx_text = "\n".join(contexts)
        try:
            sims = await self._clause_support_similarity(clauses, ctx_text)
        except Exception as e:
            logger.warning(f"忠实度 embedding 计算失败，降级为词面覆盖: {e}")
            return self._faithfulness_fallback(clauses, ctx_text)

        ctx_numbers = set(_NUMBER_RE.findall(ctx_text))
        supported = 0
        unsupported = []
        for clause, sim in zip(clauses, sims):
            numbers_ok = all(n in ctx_numbers for n in _NUMBER_RE.findall(clause))
            if sim >= FAITHFULNESS_SEMANTIC_MIN_SIM and numbers_ok:
                supported += 1
            else:
                unsupported.append(clause[:24])

        score = supported / len(clauses)
        reason = f"有依据子句 {supported}/{len(clauses)}"
        if unsupported:
            reason += f"；无依据示例：{unsupported[0]}"
        return {"score": round(min(1.0, score), 4), "reason": reason}

    async def _clause_support_similarity(self, clauses: Sequence[str], ctx_text: str) -> List[float]:
        """每个答案子句与上下文子句的最大余弦相似度。"""
        ctx_clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(ctx_text) if len(c.strip()) >= 2]
        if not ctx_clauses:
            return [0.0] * len(clauses)
        vectors = await self._get_embeddings().aembed_documents(list(clauses) + ctx_clauses)
        clause_vecs, ctx_vecs = vectors[:len(clauses)], vectors[len(clauses):]
        return [max(_cosine(v, c) for c in ctx_vecs) for v in clause_vecs]

    @staticmethod
    def _faithfulness_fallback(clauses: Sequence[str], ctx_text: str) -> Dict[str, Any]:
        """embedding 不可用时的词面 bigram 覆盖兜底。"""
        ctx_bigrams = _bigrams(ctx_text)
        ctx_numbers = set(_NUMBER_RE.findall(ctx_text))
        supported = 0
        unsupported = []
        for clause in clauses:
            cbg = _bigrams(clause)
            coverage = len(cbg & ctx_bigrams) / len(cbg) if cbg else 0.0
            numbers_ok = all(n in ctx_numbers for n in _NUMBER_RE.findall(clause))
            if coverage >= FAITHFULNESS_MIN_COVERAGE and numbers_ok:
                supported += 1
            else:
                unsupported.append(clause[:24])

        score = supported / len(clauses)
        reason = f"有依据子句 {supported}/{len(clauses)}（词面兜底）"
        if unsupported:
            reason += f"；无依据示例：{unsupported[0]}"
        return {"score": round(min(1.0, score), 4), "reason": reason}

    # ------------------------------------------------------------------
    # 相关度：问句 ↔ 答案句语义相似度
    # ------------------------------------------------------------------
    async def evaluate_relevance(self, answer: str, question: str) -> Dict[str, Any]:
        """答案与问题的语义相关度（取答案中最相关句的余弦相似度并线性映射）。"""
        clean = _clean_answer(answer)
        if not clean:
            return {"score": 0.0, "reason": "答案为空"}
        if not question or not question.strip():
            return {"score": 0.0, "reason": "问题为空"}

        sentences = [
            s.strip() for s in _SENT_SPLIT_RE.split(clean)
            if len(s.strip()) >= RELEVANCE_MIN_SENT_CHARS
        ] or [clean]

        try:
            vectors = await self._get_embeddings().aembed_documents([question] + sentences)
        except Exception as e:
            logger.warning(f"相关度 embedding 计算失败，降级为词面覆盖: {e}")
            return self._relevance_fallback(question, clean)

        q_vec, sent_vecs = vectors[0], vectors[1:]
        best = max((_cosine(q_vec, v) for v in sent_vecs), default=0.0)
        span = RELEVANCE_MAX_SIM - RELEVANCE_MIN_SIM
        score = (best - RELEVANCE_MIN_SIM) / span if span > 0 else 0.0
        score = max(0.0, min(1.0, score))
        return {
            "score": round(score, 4),
            "reason": f"最相关句相似度 {best:.3f}（映射区间 {RELEVANCE_MIN_SIM}-{RELEVANCE_MAX_SIM}）",
        }

    @staticmethod
    def _relevance_fallback(question: str, answer: str) -> Dict[str, Any]:
        """embedding 不可用时的词面覆盖兜底（问题 bigram 被答案覆盖比例）。"""
        qbg, abg = _bigrams(question), _bigrams(answer)
        coverage = len(qbg & abg) / len(qbg) if qbg else 0.0
        return {
            "score": round(min(1.0, coverage), 4),
            "reason": f"embedding 不可用，词面覆盖 {coverage:.3f}",
        }

    # ------------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------------
    async def evaluate(
        self,
        answer: str,
        question: str,
        contexts: Sequence[str],
    ) -> GenerationEvalResult:
        """同时评估忠实度与相关度。"""
        faithfulness_result = await self.evaluate_faithfulness(answer, contexts)
        relevance_result = await self.evaluate_relevance(answer, question)
        return GenerationEvalResult(
            faithfulness=faithfulness_result["score"],
            relevance=relevance_result["score"],
            faithfulness_reason=faithfulness_result["reason"],
            relevance_reason=relevance_result["reason"],
        )

    async def evaluate_batch(
        self,
        samples: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """批量评估多个答案（每条包含 answer/question/contexts）。"""
        results: List[Dict[str, Any]] = []
        for sample in samples:
            result = await self.evaluate(
                answer=sample.get("answer", ""),
                question=sample.get("question", ""),
                contexts=sample.get("contexts", []),
            )
            results.append(result.to_dict())

        if not results:
            return {"samples": [], "aggregated": {}}

        aggregated = {
            "faithfulness": sum(r["faithfulness"] for r in results) / len(results),
            "relevance": sum(r["relevance"] for r in results) / len(results),
        }
        return {"samples": results, "aggregated": aggregated}
