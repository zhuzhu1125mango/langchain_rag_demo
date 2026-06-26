"""
评价反馈模型 - Feedback

定义问答评价数据模型，用于收集用户对回答的反馈
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, UUID
from sqlalchemy.sql import func
import uuid
from src.database import Base


class Feedback(Base):
    """
    评价反馈模型类
    
    收集用户对问答结果的评价，用于优化模型和服务质量
    """
    
    __tablename__ = "feedbacks"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # 评价唯一标识
    session_id = Column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)    # 关联会话ID（允许空值，支持匿名评价）
    message_id = Column(String, nullable=False)                           # 消息ID
    rating = Column(Integer, nullable=False)                              # 评分（1-5或1/-1）
    reason = Column(String)                                               # 评价原因/备注
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # 创建时间