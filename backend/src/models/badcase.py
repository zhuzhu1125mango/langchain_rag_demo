"""Badcase 反馈模型。

记录用户反馈中识别出的问题样本，用于后续分析、学习与优化。
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text, UUID
from sqlalchemy.sql import func

from src.database import Base


class Badcase(Base):
    """Badcase 模型。

    存储用户认为不满意的问答结果，包括问题、答案、检索结果、反馈类型、
    自动分类标签以及是否已学习等状态。
    """

    __tablename__ = "badcases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(String(36), nullable=True, index=True)
    message_id = Column(String(64), nullable=True, index=True)
    user_id = Column(String(64), nullable=True, index=True)

    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    retrieved_sources = Column(JSON, nullable=True)
    intent_decision = Column(JSON, nullable=True)

    feedback_type = Column(String(32), nullable=False, default="negative")  # negative / inaccurate / incomplete / other
    reason = Column(Text, nullable=True)
    category = Column(String(32), nullable=True, index=True)  # retrieval_failure / hallucination / incomplete_answer / wrong_answer / other
    severity = Column(Integer, nullable=False, default=1)  # 1-5

    learning_applied = Column(Boolean, default=False)
    learning_result = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
