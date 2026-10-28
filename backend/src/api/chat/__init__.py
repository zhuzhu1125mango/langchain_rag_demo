"""聊天问答 API（CODE-1 拆分后为包）。

子模块职责：
- ``common``  请求模型 / 会话 ID 解析 / 配额依赖 / A/B 分流配置
- ``message`` 非流式发送 ``/messages`` 与消息反馈
- ``stream``  SSE ``/stream``（含会话准备与收尾落库）
- ``tools``   审批回执、推荐问题、上下文增强、改写、分类、对比

对外 ``router`` 前缀与标签保持不变；``MessageRequest`` 等模型保留包级导出
（单测直接引用）。
"""

from fastapi import APIRouter

from . import message, stream, tools

router = APIRouter(prefix="/chat", tags=["chat"])

# 顺序：/messages（含 /messages/{id}/feedback）→ /stream → 其余静态路径
router.include_router(message.router)
router.include_router(stream.router)
router.include_router(tools.router)

# 兼容历史导入路径
from .common import (  # noqa: E402
    ApprovalResolveRequest,
    ClassifyRequest,
    CompareRequest,
    EnhanceContextRequest,
    FeedbackRequest,
    MessageRequest,
    RewriteRequest,
    SuggestionRequest,
    chat_quota_dependency,
)
from .common import _parse_session_id  # noqa: E402
from .message import send_message, submit_message_feedback  # noqa: E402
from .stream import stream_answer  # noqa: E402
from .tools import (  # noqa: E402
    classify_question,
    compare_knowledge_bases,
    enhance_context,
    get_suggestions,
    resolve_approval,
    rewrite_question,
)

__all__ = [
    "router",
    "MessageRequest",
    "SuggestionRequest",
    "FeedbackRequest",
    "ApprovalResolveRequest",
    "RewriteRequest",
    "ClassifyRequest",
    "CompareRequest",
    "EnhanceContextRequest",
    "chat_quota_dependency",
    "_parse_session_id",
    "send_message",
    "submit_message_feedback",
    "stream_answer",
    "resolve_approval",
    "get_suggestions",
    "enhance_context",
    "rewrite_question",
    "classify_question",
    "compare_knowledge_bases",
]
