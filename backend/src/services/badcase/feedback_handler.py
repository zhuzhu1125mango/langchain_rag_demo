"""Badcase 反馈处理器。

接收用户负面反馈，自动归类问题类型，持久化到数据库，并触发意图路由等
模块的在线学习，形成 badcase 闭环。
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BadcaseCategory(str, Enum):
    """Badcase 问题分类。"""

    RETRIEVAL_FAILURE = "retrieval_failure"
    HALLUCINATION = "hallucination"
    INCOMPLETE_ANSWER = "incomplete_answer"
    WRONG_ANSWER = "wrong_answer"
    OTHER = "other"


@dataclass
class BadcaseCreateRequest:
    """Badcase 创建请求数据。"""

    question: str
    answer: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    user_id: Optional[str] = None
    feedback_type: str = "negative"
    reason: Optional[str] = None
    retrieved_sources: Optional[List[Dict[str, Any]]] = None
    intent_decision: Optional[Dict[str, Any]] = None


class BadcaseFeedbackHandler:
    """Badcase 反馈处理器。"""

    def __init__(self):
        self.embedding_classifier = None

    def _get_embedding_classifier(self):
        """延迟初始化意图分类器用于在线学习。"""
        if self.embedding_classifier is None:
            try:
                from src.services.intent_router.embedding_classifier import (
                    EmbeddingIntentClassifier,
                )

                self.embedding_classifier = EmbeddingIntentClassifier()
            except Exception as e:
                logger.warning(f"Badcase 在线学习初始化失败: {e}")
        return self.embedding_classifier

    @staticmethod
    def classify(
        question: str,
        answer: Optional[str] = None,
        retrieved_sources: Optional[List[Dict[str, Any]]] = None,
        feedback_type: str = "negative",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """基于规则自动分类 badcase 类型与严重程度。

        分类规则：
        - retrieval_failure：未检索到来源或来源明显不相关。
        - hallucination：回答包含参考信息中不存在的内容（依据用户 reason 关键词）。
        - incomplete_answer：回答遗漏关键信息（如“不够详细”、“漏了”）。
        - wrong_answer：回答与事实不符。
        - other：无法归类的其他问题。
        """
        category = BadcaseCategory.OTHER
        severity = 2

        reason_lower = (reason or "").lower()
        feedback_lower = feedback_type.lower()

        # 检索失败判定
        has_sources = bool(retrieved_sources)
        if not has_sources or "检索" in reason_lower or "找不到" in reason_lower or "没提到" in reason_lower:
            category = BadcaseCategory.RETRIEVAL_FAILURE
            severity = 4 if not has_sources else 3

        # 幻觉判定
        if any(kw in reason_lower for kw in {"编造", "没有提到", "不存在", "臆测", "胡说", "虚构"}):
            category = BadcaseCategory.HALLUCINATION
            severity = 5

        # 不完整判定
        if any(kw in reason_lower for kw in {"不完整", "不够详细", "漏了", "缺少", "太简单"}):
            category = BadcaseCategory.INCOMPLETE_ANSWER
            severity = 3

        # 错误判定
        if any(kw in reason_lower for kw in {"错误", "不对", "不准确", "不符"}):
            category = BadcaseCategory.WRONG_ANSWER
            severity = 4

        # 负面反馈但没有 reason 时，若来源为空则判定为检索失败
        if category == BadcaseCategory.OTHER and feedback_lower in {"negative", "dislike", "差评"}:
            if not has_sources:
                category = BadcaseCategory.RETRIEVAL_FAILURE
                severity = 3
            elif answer and len(answer) < 50:
                category = BadcaseCategory.INCOMPLETE_ANSWER
                severity = 2

        return {
            "category": category.value,
            "severity": severity,
        }

    async def _trigger_learning(
        self,
        badcase: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """触发在线学习。

        当前主要触发意图路由的 embedding 分类器学习：
        - 将 badcase 问题作为正确意图类别的示例加入。
        """
        classifier = self._get_embedding_classifier()
        if classifier is None:
            return None

        intent_decision = badcase.get("intent_decision") or {}
        predicted_mode = intent_decision.get("primary_mode", "direct_llm")
        # 默认希望 badcase 能被识别为需要知识库的模式
        actual_mode = "kb_only" if badcase.get("retrieved_sources") else "direct_llm"

        try:
            learn_result = await classifier.learn_from_feedback(
                question=badcase["question"],
                predicted_mode=predicted_mode,
                actual_mode=actual_mode,
                confidence=0.0,
            )
            logger.info(f"Badcase 触发在线学习: {learn_result}")
            return learn_result
        except Exception as e:
            logger.warning(f"Badcase 在线学习失败: {e}")
            return None

    async def handle(
        self,
        request: BadcaseCreateRequest,
        db_session=None,
    ) -> Dict[str, Any]:
        """处理一条 badcase 反馈。

        Args:
            request: Badcase 创建请求。
            db_session: 数据库会话（可选）。传入时会在该会话内保存。

        Returns:
            处理结果，包含 badcase_id、分类、学习结果等。
        """
        classification = self.classify(
            question=request.question,
            answer=request.answer,
            retrieved_sources=request.retrieved_sources,
            feedback_type=request.feedback_type,
            reason=request.reason,
        )

        badcase_data = {
            "question": request.question,
            "answer": request.answer,
            "session_id": request.session_id,
            "message_id": request.message_id,
            "user_id": request.user_id,
            "feedback_type": request.feedback_type,
            "reason": request.reason,
            "retrieved_sources": request.retrieved_sources,
            "intent_decision": request.intent_decision,
            "category": classification["category"],
            "severity": classification["severity"],
        }

        learning_result = await self._trigger_learning(badcase_data)
        badcase_data["learning_applied"] = learning_result is not None
        badcase_data["learning_result"] = learning_result

        badcase_id = None
        if db_session is not None:
            try:
                from src.models.badcase import Badcase

                record = Badcase(**badcase_data)
                db_session.add(record)
                await db_session.commit()
                await db_session.refresh(record)
                badcase_id = str(record.id)
            except Exception as e:
                logger.error(f"Badcase 持久化失败: {e}", exc_info=True)

        return {
            "badcase_id": badcase_id,
            "category": classification["category"],
            "severity": classification["severity"],
            "learning_result": learning_result,
        }


# 全局默认处理器
badcase_feedback_handler = BadcaseFeedbackHandler()
