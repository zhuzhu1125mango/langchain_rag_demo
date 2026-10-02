"""用户级 API Key 模型（E.6，阶段二 MCP 前置）。

每用户可发放/吊销自己的 API Key，供 MCP 客户端、脚本、CI 以该用户身份
调用 API——Key 解析出真实 user_id 后走与 JWT 完全一致的 owner 隔离链路，
修复全局 API_KEY 命中后固定返回 user_id="api_key_user" 的多用户归属串号隐患。

安全约定：DB 仅存 SHA-256(key)（高熵随机串无字典风险，O(1) 索引查询；
不用 bcrypt——每请求 ~100ms 不可接受）；明文仅在创建响应返回一次。
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UUID
from sqlalchemy.sql import func

from src.database import Base


class UserApiKey(Base):
    """用户级 API Key 表。"""

    __tablename__ = "user_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    # 用途备注（必填），供审计追溯，如 "claude-code-office"
    name = Column(String(64), nullable=False)
    # SHA-256 hex（64 字符），唯一索引支持认证链 O(1) 查询
    key_hash = Column(String(64), nullable=False, unique=True, index=True)
    # 明文前缀（"rag_" + 前 8 字符），供列表识别与日志脱敏，永不存全量明文
    key_prefix = Column(String(12), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # 可选有效期；NULL = 永久
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # 最近使用时间（泄露感知线索）；写库节流由认证链控制（>60s 才更新）
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    # 软吊销标记；吊销后下一请求即失效（认证链无缓存）
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    def is_valid(self, now: datetime) -> bool:
        """有效 = 未吊销且未过期。

        Args:
            now: 带时区的当前时间（timezone-aware）。

        Returns:
            bool: True = 该 Key 可用于认证。
        """
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True
