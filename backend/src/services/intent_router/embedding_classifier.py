"""基于 Embedding 的意图分类器。

通过计算用户问题与预定义意图示例之间的余弦相似度，快速判断问题主模式。
作为规则 fast-path 与 LLM 语义路由之间的轻量级补充层，失败时自动降级。
"""

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings
from src.services.model_manager import model_manager
from src.services.intent_router.intent_examples import IntentExampleStore
from src.services.intent_router.models import (
    FallbackStrategy,
    IntentDecision,
    PrimaryMode,
    SearchPipeline,
)

logger = logging.getLogger("intent_router.embedding")


@dataclass
class EmbeddingClassifierResult:
    """Embedding 意图分类结果。"""

    primary_mode: PrimaryMode
    confidence: float
    confidence_scores: Dict[str, float]
    reasoning: str


class EmbeddingIntentClassifier:
    """基于本地 Embedding 模型的意图分类器。

    设计目标：
    - 不依赖 LLM，响应快速且资源占用低。
    - 使用项目已配置的 OllamaEmbeddings（默认 bge-m3）。
    - 分类器不可用时静默降级，不阻塞主流程。
    - 支持通过反馈样本进行轻量在线学习（阈值/示例调整）。
    """

    def __init__(
        self,
        embeddings: Optional[Any] = None,
        example_store: Optional[IntentExampleStore] = None,
        similarity_threshold: Optional[float] = None,
        ambiguity_gap: Optional[float] = None,
    ):
        self.embeddings = embeddings
        self.example_store = example_store or IntentExampleStore()
        self.similarity_threshold = similarity_threshold or getattr(
            settings.intent_router, "INTENT_ROUTER_EMBEDDING_THRESHOLD", 0.55
        )
        self.ambiguity_gap = ambiguity_gap or getattr(
            settings.intent_router, "INTENT_ROUTER_EMBEDDING_AMBIGUITY_GAP", 0.08
        )
        self._example_embeddings_cache: Dict[str, List[List[float]]] = {}
        self._model_name: Optional[str] = None
        self._feedback_buffer: List[Dict[str, Any]] = []

    def _get_embeddings(self) -> Optional[Any]:
        """获取或初始化 OllamaEmbeddings 实例（B2 共享单例）。"""
        # 外部注入的实例（如测试 mock）直接复用，不被默认模型覆盖
        if self.embeddings is not None and self._model_name is None:
            return self.embeddings
        current_model = settings.model.EMBEDDING_MODEL_NAME
        if self.embeddings is None or self._model_name != current_model:
            try:
                logger.info(f"初始化意图分类 Embedding 模型: {current_model}")
                self.embeddings = model_manager.get_embeddings()
                self._model_name = current_model
            except Exception as e:
                logger.warning(f"意图分类 Embedding 模型初始化失败: {e}")
                self.embeddings = None
        return self.embeddings

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def _compute_example_embeddings(self, mode: str) -> List[List[float]]:
        """计算指定模式下所有示例的 embeddings，带缓存。"""
        if mode in self._example_embeddings_cache:
            return self._example_embeddings_cache[mode]

        embeddings = self._get_embeddings()
        examples = self.example_store.get_examples(mode)
        if embeddings is None or not examples:
            return []

        try:
            embs = await embeddings.aembed_documents(examples)
        except Exception as e:
            logger.warning(f"模式 {mode} 示例 embedding 计算失败: {e}")
            return []

        self._example_embeddings_cache[mode] = embs
        return embs

    async def classify(self, question: str) -> EmbeddingClassifierResult:
        """对用户问题进行意图分类。

        Args:
            question: 用户问题文本。

        Returns:
            EmbeddingClassifierResult: 分类结果，包含主模式、置信度和各模式分数。
        """
        question = (question or "").strip()
        if not question:
            return EmbeddingClassifierResult(
                primary_mode=PrimaryMode.DIRECT_LLM,
                confidence=0.0,
                confidence_scores={},
                reasoning="问题为空，Embedding 分类器返回默认 DIRECT_LLM",
            )

        embeddings = self._get_embeddings()
        if embeddings is None:
            return EmbeddingClassifierResult(
                primary_mode=PrimaryMode.DIRECT_LLM,
                confidence=0.0,
                confidence_scores={},
                reasoning="Embedding 模型不可用，降级为 DIRECT_LLM",
            )

        try:
            query_embedding = await embeddings.aembed_query(question)
        except Exception as e:
            logger.warning(f"问题 embedding 计算失败: {e}")
            return EmbeddingClassifierResult(
                primary_mode=PrimaryMode.DIRECT_LLM,
                confidence=0.0,
                confidence_scores={},
                reasoning="问题 embedding 计算失败，降级为 DIRECT_LLM",
            )

        mode_scores: Dict[str, float] = {}
        for mode in self.example_store.list_modes():
            example_embs = await self._compute_example_embeddings(mode)
            if not example_embs:
                continue
            # 取该模式下所有示例与问题的最大相似度作为模式分数
            best_score = max(
                self._cosine_similarity(query_embedding, emb) for emb in example_embs
            )
            mode_scores[mode] = best_score

        if not mode_scores:
            return EmbeddingClassifierResult(
                primary_mode=PrimaryMode.DIRECT_LLM,
                confidence=0.0,
                confidence_scores={},
                reasoning="无可用示例 embedding，降级为 DIRECT_LLM",
            )

        sorted_modes = sorted(mode_scores.items(), key=lambda x: x[1], reverse=True)
        top_mode, top_score = sorted_modes[0]
        second_score = sorted_modes[1][1] if len(sorted_modes) > 1 else 0.0
        gap = top_score - second_score

        reasoning = (
            f"Embedding 分类：与 {top_mode} 意图示例最相似 "
            f"(score={top_score:.3f}, gap={gap:.3f})"
        )

        if top_score < self.similarity_threshold:
            reasoning = (
                f"Embedding 分类置信度不足（{top_score:.3f} < {self.similarity_threshold}），"
                f"交由 LLM 路由进一步判断"
            )
            return EmbeddingClassifierResult(
                primary_mode=PrimaryMode.DIRECT_LLM,
                confidence=top_score,
                confidence_scores=mode_scores,
                reasoning=reasoning,
            )

        try:
            primary_mode = PrimaryMode(top_mode)
        except ValueError:
            primary_mode = PrimaryMode.DIRECT_LLM

        return EmbeddingClassifierResult(
            primary_mode=primary_mode,
            confidence=top_score,
            confidence_scores=mode_scores,
            reasoning=reasoning,
        )

    def to_intent_decision(
        self,
        result: EmbeddingClassifierResult,
        question: str,
        has_kb: bool = False,
        use_web_search: bool = False,
    ) -> IntentDecision:
        """将分类结果转换为 IntentDecision。

        转换规则：
        - 置信度不足 → DIRECT_LLM，置信度分数写入 confidence_scores。
        - KB_ONLY 但未选择知识库 → 降为 DIRECT_LLM。
        - WEB_SEARCH / AGENT_RESEARCH 需要联网但未开启时保留模式，fallback 说明。
        - 歧义（最高与次高分差小于阈值）→ 触发澄清。
        """
        scores = result.confidence_scores or {}
        primary_mode = result.primary_mode

        if result.confidence < self.similarity_threshold:
            return IntentDecision(
                primary_mode=PrimaryMode.DIRECT_LLM,
                fallback_strategy=FallbackStrategy.NONE,
                reasoning=result.reasoning,
                confidence_scores=scores,
                llm_routed=False,
            )

        if primary_mode == PrimaryMode.KB_ONLY and not has_kb:
            return IntentDecision(
                primary_mode=PrimaryMode.DIRECT_LLM,
                fallback_strategy=FallbackStrategy.NONE,
                reasoning=f"{result.reasoning}；未选择知识库，降级为直接回答",
                confidence_scores=scores,
                llm_routed=False,
            )

        needs_clarify = False
        clarify_question = ""
        sorted_scores = sorted(scores.values(), reverse=True)
        if len(sorted_scores) >= 2 and (sorted_scores[0] - sorted_scores[1]) < self.ambiguity_gap:
            needs_clarify = True
            clarify_question = "您的问题可能涉及多个方面，能否再具体说明一下？"

        needs_kb = primary_mode in (PrimaryMode.KB_ONLY, PrimaryMode.HYBRID)
        needs_web = primary_mode in (PrimaryMode.WEB_SEARCH, PrimaryMode.HYBRID, PrimaryMode.AGENT_RESEARCH)
        needs_realtime = primary_mode == PrimaryMode.WEB_SEARCH
        suggested_tools: List[str] = []
        fallback = FallbackStrategy.NONE

        if primary_mode == PrimaryMode.TOOL_FIRST:
            suggested_tools = self._infer_tools_from_question(question)
            fallback = FallbackStrategy.LLM_DIRECT
        elif primary_mode in (PrimaryMode.WEB_SEARCH, PrimaryMode.AGENT_RESEARCH):
            suggested_tools = ["web_search"]
            fallback = FallbackStrategy.TELL_FAILURE if needs_realtime and not use_web_search else FallbackStrategy.WEB_SEARCH
        elif primary_mode == PrimaryMode.HYBRID:
            suggested_tools = ["web_search"]
            fallback = FallbackStrategy.LLM_DIRECT

        return IntentDecision(
            needs_kb=needs_kb,
            needs_web=needs_web,
            needs_realtime=needs_realtime,
            primary_mode=primary_mode,
            suggested_tools=suggested_tools,
            fallback_strategy=fallback,
            reasoning=result.reasoning,
            search_pipeline=SearchPipeline.FULL_PATH if primary_mode == PrimaryMode.AGENT_RESEARCH else SearchPipeline.FAST_PATH,
            confidence_scores=scores,
            needs_clarify=needs_clarify,
            clarify_question=clarify_question,
            llm_routed=False,
        )

    @staticmethod
    def _infer_tools_from_question(question: str) -> List[str]:
        """根据问题关键词推断可能需要的工具。"""
        q = question.lower()
        tools = []
        if any(kw in q for kw in {"几点", "时间", "日期", "星期", "今天几号"}):
            tools.append("get_current_time")
        if any(kw in q for kw in {"天气", "气温", "温度", "下雨", "空气质量"}):
            tools.append("weather_query")
        if any(kw in q for kw in {"金价", "银价", "贵金属", "黄金价格", "白银价格"}):
            tools.append("gold_price")
        if any(kw in q for kw in {"汇率", "兑换", "换算", "美元兑", "人民币"}):
            tools.append("exchange_rate")
        if any(kw in q for kw in {"计算", "等于", "+", "-", "*", "×", "÷", "/", "%"}):
            tools.append("calculator")
        if not tools:
            tools.append("calculator")
        return tools

    async def learn_from_feedback(
        self,
        question: str,
        predicted_mode: str,
        actual_mode: str,
        confidence: float,
    ) -> Dict[str, Any]:
        """根据单条反馈进行轻量在线学习。

        当前策略：
        - 将正确标注的问题加入 actual_mode 的示例库。
        - 若预测错误且置信度高，降低相似度阈值建议（供外部调参参考）。
        - 返回更新统计信息。
        """
        if predicted_mode == actual_mode:
            return {
                "status": "correct",
                "action": "none",
                "similarity_threshold": self.similarity_threshold,
            }

        self.example_store.add_examples(actual_mode, [question])
        # 清空 actual_mode 的 embedding 缓存以强制重新计算
        self._example_embeddings_cache.pop(actual_mode, None)

        threshold_adjustment = 0.0
        if confidence >= self.similarity_threshold:
            # 高置信度但预测错误，说明阈值可能偏低，建议微调
            threshold_adjustment = 0.01

        return {
            "status": "misclassification_learned",
            "predicted_mode": predicted_mode,
            "actual_mode": actual_mode,
            "added_examples": 1,
            "new_example_count": len(self.example_store.get_examples(actual_mode)),
            "suggested_threshold_adjustment": threshold_adjustment,
            "similarity_threshold": self.similarity_threshold,
        }

    def record_feedback(
        self,
        question: str,
        predicted_mode: str,
        actual_mode: str,
        confidence: float = 0.0,
    ) -> None:
        """记录一条意图分类反馈到内存缓冲区，供批量学习使用。"""
        self._feedback_buffer.append({
            "question": question,
            "predicted_mode": predicted_mode,
            "actual_mode": actual_mode,
            "confidence": confidence,
        })

    async def batch_learn(self, max_samples: Optional[int] = None) -> Dict[str, Any]:
        """批量学习内存缓冲区中的反馈样本。

        Args:
            max_samples: 最多处理的样本数，None 表示处理全部。

        Returns:
            学习统计信息。
        """
        samples = self._feedback_buffer[:max_samples] if max_samples else self._feedback_buffer
        if not samples:
            return {"status": "skipped", "reason": "无待学习反馈样本", "processed": 0}

        learned = 0
        correct = 0
        threshold_adjustment_total = 0.0
        for sample in samples:
            result = await self.learn_from_feedback(
                question=sample["question"],
                predicted_mode=sample["predicted_mode"],
                actual_mode=sample["actual_mode"],
                confidence=sample["confidence"],
            )
            if result["status"] == "misclassification_learned":
                learned += 1
                threshold_adjustment_total += result.get("suggested_threshold_adjustment", 0.0)
            else:
                correct += 1

        # 学习完成后清空已处理样本
        processed_count = len(samples)
        self._feedback_buffer = self._feedback_buffer[processed_count:]

        # 应用阈值微调（有界）
        if learned > 0 and threshold_adjustment_total > 0:
            new_threshold = min(
                0.9, self.similarity_threshold + threshold_adjustment_total / learned
            )
            self.similarity_threshold = new_threshold

        return {
            "status": "success",
            "processed": processed_count,
            "learned": learned,
            "correct": correct,
            "new_threshold": self.similarity_threshold,
        }

    def clear_cache(self) -> None:
        """清空示例 embedding 缓存。"""
        self._example_embeddings_cache.clear()
