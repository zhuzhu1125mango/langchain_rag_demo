"""请求链路追踪模型。

记录每次问答请求的完整链路，包括意图决策、工具调用、搜索结果、失败原因等，
用于后续分析、调试和效果评估。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.sql import func

from src.database import Base


class RequestTrace(Base):
    """请求链路追踪模型。"""

    __tablename__ = "request_traces"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), nullable=True, index=True)
    user_id = Column(String(64), nullable=True, index=True)

    question = Column(Text, nullable=False)
    resolved_question = Column(Text, nullable=True)

    intent_decision = Column(JSON, nullable=True)
    primary_mode = Column(String(32), nullable=True)

    tool_calls = Column(JSON, nullable=True)
    search_results = Column(JSON, nullable=True)
    kb_results = Column(JSON, nullable=True)

    fallback_triggered = Column(Boolean, default=False)
    fallback_reason = Column(String(255), nullable=True)
    output_pollution_detected = Column(Boolean, default=False)

    final_answer = Column(Text, nullable=True)
    total_latency_ms = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
