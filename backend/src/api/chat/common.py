"""聊天 API 共享层：请求模型、会话 ID 解析、配额依赖、A/B 分流（CODE-1）。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Union, Annotated
import uuid
import logging
from src.config import settings
from src.auth import get_current_user, CurrentUser
from src.services.experiment_manager import experiment_manager
from src.utils.quota import QuotaUnavailable, acquire_chat_slot, release_chat_slot

logger = logging.getLogger("src.api.chat")

router = APIRouter(tags=["chat"])

class MessageRequest(BaseModel):
    """消息请求数据模型"""
    question: str
    session_id: Optional[str] = None
    kb_ids: Optional[List[str]] = None
    use_web_search: Optional[bool] = False
    search_mode: Optional[str] = "simple"
    # 深度思考开关：on（开启思考）| off（关闭思考）
    deep_thinking: Optional[str] = "off"

    @field_validator("deep_thinking")
    @classmethod
    def _validate_deep_thinking(cls, v: Optional[str]) -> str:
        if v is None:
            return "off"
        allowed = {"on", "off"}
        if v not in allowed:
            raise ValueError(f"deep_thinking 必须是 {sorted(allowed)} 之一")
        return v


def _parse_session_id(raw: Optional[str]) -> Optional[uuid.UUID]:
    """解析会话 ID；非法值返回 400 而非 500。"""
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="无效的会话ID")


async def chat_quota_dependency(current_user: CurrentUser = Depends(get_current_user)):
    """SEC-2：聊天入口统一扣减速率/日额度/并发配额，响应结束后释放并发槽。"""
    try:
        await acquire_chat_slot(current_user.user_id)
    except QuotaUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="配额服务暂不可用，请稍后再试",
            headers={"Retry-After": "30"},
        ) from exc
    try:
        yield
    finally:
        await release_chat_slot(current_user.user_id)


# LEARN W-C：A/B 变体可实验化参数白名单（仅这些键可从 variant.config 覆盖到问答链路）
_AB_WHITELIST = (
    "search_mode",
    "decision_vote_when_kb_selected",
    "kb_hybrid_rerank_top_k",
    "strategy_weights",
)


async def _resolve_ab_config(user_id: str, session_id: Optional[str]) -> Optional[dict]:
    """LEARN W-C：按部署级配置解析本请求的 A/B 变体，返回白名单参数覆盖。

    未开启开关 / 未配置实验 / 实验非 running / 分流失败 → None（不分流、行为不变）。
    """
    if not settings.experiment.AB_EXPERIMENT_CHAT_ENABLED:
        return None
    experiment_id = settings.experiment.AB_CHAT_EXPERIMENT_ID
    if not experiment_id:
        return None
    try:
        allocation = await experiment_manager.allocate_traffic_for_chat(
            experiment_id=experiment_id, user_id=user_id, session_id=session_id
        )
    except Exception as e:
        logger.warning(f"A/B 分流失败，跳过本次分流: {e}")
        return None
    if not allocation:
        return None
    raw = allocation.get("config") or {}
    cfg = {k: raw[k] for k in _AB_WHITELIST if k in raw}
    cfg["experiment_id"] = experiment_id
    cfg["variant_id"] = allocation.get("variant_id")
    return cfg


async def _record_ab_metric(ab_config: Optional[dict], elapsed_ms: float) -> None:
    """LEARN W-C：记录系统级实验 response_time 指标（fail-soft，不影响回答）。"""
    if not ab_config:
        return
    experiment_id = ab_config.get("experiment_id")
    variant_id = ab_config.get("variant_id")
    if not experiment_id or not variant_id:
        return
    try:
        await experiment_manager.record_metric_for_chat(
            experiment_id=experiment_id,
            variant_id=variant_id,
            metric_name="response_time",
            metric_value=elapsed_ms,
        )
    except Exception as e:
        logger.warning(f"记录 A/B 指标失败: {e}")

class ApprovalResolveRequest(BaseModel):
    """HITL 审批回执（AM-2）。"""

    approved: bool
    # 可选：拒绝理由（批准时可留空）
    reason: str = ""

class SuggestionRequest(BaseModel):
    """推荐问题请求数据模型"""
    question: Optional[str] = None
    session_id: Optional[str] = None
    kb_ids: Optional[List[str]] = None

class FeedbackRequest(BaseModel):
    """消息反馈请求数据模型"""
    rating: Annotated[Union[int, str], Field(union_mode='left_to_right')]
    reason: str = ""
    session_id: Optional[str] = None


class RewriteRequest(BaseModel):
    """问题重写请求数据模型"""
    question: str


class ClassifyRequest(BaseModel):
    """问题分类请求数据模型"""
    question: str


class CompareRequest(BaseModel):
    """答案对比请求数据模型"""
    question: str
    kb_ids: List[str]


class EnhanceContextRequest(BaseModel):
    """上下文增强请求数据模型"""
    question: str
    session_id: Optional[str] = None
