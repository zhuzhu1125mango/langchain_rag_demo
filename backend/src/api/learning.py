"""
学习引擎API接口

提供学习引擎的管理和查询功能
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from src.auth import get_current_user, require_admin, CurrentUser
from src.database import async_session
from src.models import Session as SessionModel
from src.services.learning_engine import learning_engine
from src.services.strategy_manager import get_strategy_manager

router = APIRouter(prefix="/learning", tags=["learning"])

logger = logging.getLogger("rag_system")


class LearningConfigUpdate(BaseModel):
    """学习引擎配置更新请求模型。"""
    enabled: Optional[bool] = None
    learning_interval_hours: Optional[int] = None


@router.get("/stats", summary="获取学习统计信息")
async def get_learning_stats(current_user: CurrentUser = Depends(get_current_user)):
    """获取学习引擎的统计信息"""
    try:
        await learning_engine.load_config()
        stats = await learning_engine.get_learning_stats(owner_id=current_user.user_id)
        execution_stats = stats.get("execution_stats", {})

        misclassification_count = 0
        try:
            misclassified = await learning_engine.get_misclassification_analysis(
                50, owner_id=current_user.user_id
            )
            misclassification_count = len(misclassified.get("misclassified_cases", []))
        except Exception:
            pass

        # 适配前端期望的数据结构
        adapted_stats = {
            "total_executions": execution_stats.get("total_count", 0),
            "successful_learnings": execution_stats.get("knowledge_base_used_count", 0),
            "misclassification_count": misclassification_count,
            "avg_confidence": execution_stats.get("average_confidence", 0.0)
        }
        return {"success": True, "data": adapted_stats}
    except Exception as e:
        logger.error("学习引擎操作失败", exc_info=True)
        raise HTTPException(status_code=500, detail="内部错误，请稍后重试")


@router.get("/misclassification", summary="获取误分类分析")
async def get_misclassification_analysis(
    limit: int = 50,
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取误分类案例分析报告"""
    try:
        analysis = await learning_engine.get_misclassification_analysis(
            limit, owner_id=current_user.user_id
        )
        misclassified_cases = analysis.get("misclassified_cases", [])

        # 适配前端期望的数据结构，将 rule_learner 的格式转换为前端 MisclassificationCase 格式
        adapted_cases = []
        for idx, case in enumerate(misclassified_cases[:limit]):
            adapted_cases.append({
                "id": f"mis-{idx}",
                "question": case.get("question", ""),
                "actual_decision": bool(case.get("predicted", 0)),
                "correct_decision": bool(case.get("actual", 0)),
                "confidence": case.get("confidence", 0.0),
                "created_at": analysis.get("analysis_time", "")
            })
        return {"success": True, "data": adapted_cases}
    except Exception as e:
        logger.error("学习引擎操作失败", exc_info=True)
        raise HTTPException(status_code=500, detail="内部错误，请稍后重试")


@router.post("/trigger", summary="触发学习")
async def trigger_learning(current_user: CurrentUser = Depends(require_admin)):
    """手动触发学习流程（全局操作，需管理员权限）"""
    try:
        # LEARN W-A：使用进程内唯一实例，学习调整直接落到在线决策所用 manager
        strategy_manager = await get_strategy_manager()
        result = await learning_engine.trigger_learning(strategy_manager)
        return {"success": True, "data": result}
    except Exception as e:
        logger.error("学习引擎操作失败", exc_info=True)
        raise HTTPException(status_code=500, detail="内部错误，请稍后重试")


async def _session_owner_id(session_id: str) -> str:
    """读取会话归属；会话不存在返回 404，用于学习样本落库时派生 owner。"""
    try:
        session_uuid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="无效的会话ID")
    async with async_session() as db:
        session = await db.get(SessionModel, session_uuid)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session.user_id


@router.post("/record-execution", summary="记录执行")
async def record_execution(
    session_id: str,
    question: str,
    final_decision: bool,
    final_confidence: float,
    strategy_results: Dict[str, float],
    used_knowledge_base: bool,
    current_user: CurrentUser = Depends(require_admin),
):
    """记录策略执行（管理操作；owner 由会话归属派生，不信任请求方）。"""
    try:
        owner_id = await _session_owner_id(session_id)
        execution_id = await learning_engine.record_execution(
            session_id=session_id,
            question=question,
            final_decision=final_decision,
            final_confidence=final_confidence,
            strategy_results=strategy_results,
            used_knowledge_base=used_knowledge_base,
            user_id=owner_id,
        )
        return {"success": True, "execution_id": execution_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("学习引擎操作失败", exc_info=True)
        raise HTTPException(status_code=500, detail="内部错误，请稍后重试")


@router.post("/record-feedback", summary="记录反馈")
async def record_feedback(
    execution_id: str,
    feedback_score: float,
    reason: Optional[str] = None,
    current_user: CurrentUser = Depends(require_admin),
):
    """记录用户反馈（管理操作；execution 归属由样本记录自身决定）。"""
    try:
        await learning_engine.record_feedback(
            execution_id=execution_id,
            feedback_score=feedback_score,
            reason=reason
        )
        return {"success": True}
    except Exception as e:
        logger.error("学习引擎操作失败", exc_info=True)
        raise HTTPException(status_code=500, detail="内部错误，请稍后重试")


@router.put("/config", summary="更新学习引擎配置")
async def update_learning_config(
    config: LearningConfigUpdate,
    current_user: CurrentUser = Depends(require_admin),
):
    """更新学习引擎配置（全局操作，需管理员权限）"""
    try:
        await learning_engine.load_config()
        if config.enabled is not None:
            if config.enabled:
                learning_engine.enable()
            else:
                learning_engine.disable()

        if config.learning_interval_hours is not None:
            learning_engine.set_learning_interval(config.learning_interval_hours)

        await learning_engine.save_config()

        return {
            "success": True,
            "data": {
                "enabled": learning_engine.enabled,
                "learning_interval_hours": learning_engine.learning_interval_hours
            }
        }
    except Exception as e:
        logger.error("学习引擎操作失败", exc_info=True)
        raise HTTPException(status_code=500, detail="内部错误，请稍后重试")


@router.get("/config", summary="获取学习引擎配置")
async def get_learning_config(current_user: CurrentUser = Depends(get_current_user)):
    """获取学习引擎配置"""
    await learning_engine.load_config()
    return {
        "success": True,
        "data": {
            "enabled": learning_engine.enabled,
            "learning_interval_hours": learning_engine.learning_interval_hours,
            "last_learning_time": learning_engine.last_learning_time.isoformat() if learning_engine.last_learning_time else None
        }
    }


@router.post("/enable", summary="启用学习引擎")
async def enable_learning(current_user: CurrentUser = Depends(require_admin)):
    """启用学习引擎（全局操作，需管理员权限）"""
    await learning_engine.load_config()
    learning_engine.enable()
    await learning_engine.save_config()
    return {"success": True, "message": "学习引擎已启用"}


@router.post("/disable", summary="禁用学习引擎")
async def disable_learning(current_user: CurrentUser = Depends(require_admin)):
    """禁用学习引擎（全局操作，需管理员权限）"""
    await learning_engine.load_config()
    learning_engine.disable()
    await learning_engine.save_config()
    return {"success": True, "message": "学习引擎已禁用"}
