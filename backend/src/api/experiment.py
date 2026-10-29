"""
A/B测试实验API接口

提供实验的创建、管理和分析功能
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from src.auth import get_current_user, require_admin, CurrentUser
from src.services.experiment_manager import experiment_manager
from src.utils.error_handling import log_exception

router = APIRouter(prefix="/experiments", tags=["experiments"])

logger = logging.getLogger("rag_system")


def _fail(exc: Exception, action: str) -> HTTPException:
    """实验接口兜底：记结构化异常后统一返回 500，避免把内部细节透给前端。"""
    log_exception(
        logger, exc,
        action=action,
        message="实验接口未预期异常",
    )
    return HTTPException(status_code=500, detail="内部错误，请稍后重试")


class CreateExperimentRequest(BaseModel):
    """创建实验请求模型。"""
    name: str
    description: Optional[str] = None
    variants: Optional[List[Dict[str, Any]]] = None
    metrics: Optional[List[str]] = None


class BatchDeleteExperimentsRequest(BaseModel):
    """批量删除实验请求模型。"""
    experiment_ids: List[str]


@router.post("/", summary="创建实验")
async def create_experiment(
    request: CreateExperimentRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """创建新的A/B测试实验"""
    try:
        experiment_id = await experiment_manager.create_experiment(
            name=request.name,
            description=request.description,
            variants=request.variants,
            metrics=request.metrics,
            owner_id=current_user.user_id
        )
        return {"success": True, "experiment_id": experiment_id}
    except Exception as e:
        raise _fail(e, "experiment.create")


@router.get("/", summary="获取实验列表")
async def list_experiments(
    status: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取实验列表，支持状态过滤"""
    try:
        experiments = await experiment_manager.list_experiments(status=status, owner_id=current_user.user_id)
        return {"success": True, "data": experiments}
    except Exception as e:
        raise _fail(e, "experiment.list")


@router.delete("/batch", summary="批量删除实验")
async def batch_delete_experiments(
    request: BatchDeleteExperimentsRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """批量删除实验及其关联数据（变体、指标、结果、分流记录）"""
    try:
        result = await experiment_manager.delete_experiments(request.experiment_ids, owner_id=current_user.user_id)
        return {"success": True, "data": result}
    except Exception as e:
        raise _fail(e, "experiment.batch_delete")


@router.get("/admin/legacy", summary="管理员查看遗留空 owner 实验")
async def list_legacy_experiments(
    status: Optional[str] = None,
    current_user: CurrentUser = Depends(require_admin),
):
    """SEC-6：普通用户不可见遗留空 owner 实验；管理员可显式查看并处理。"""
    experiments = await experiment_manager.list_experiments(
        status=status, owner_id=current_user.user_id, include_legacy=True
    )
    return {"success": True, "data": experiments}


@router.get("/{experiment_id}", summary="获取实验详情")
async def get_experiment(
    experiment_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取指定实验的详细信息"""
    try:
        experiment = await experiment_manager.get_experiment(experiment_id, owner_id=current_user.user_id)
        if experiment:
            return {"success": True, "data": experiment}
        else:
            raise HTTPException(status_code=404, detail="实验不存在")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e, "experiment.get")


@router.post("/{experiment_id}/start", summary="启动实验")
async def start_experiment(
    experiment_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """启动实验"""
    try:
        success = await experiment_manager.start_experiment(experiment_id, owner_id=current_user.user_id)
        if success:
            return {"success": True, "message": "实验已启动"}
        else:
            raise HTTPException(status_code=400, detail="无法启动实验")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e, "experiment.start")


@router.post("/{experiment_id}/stop", summary="停止实验")
async def stop_experiment(
    experiment_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """停止实验"""
    try:
        success = await experiment_manager.stop_experiment(experiment_id, owner_id=current_user.user_id)
        if success:
            return {"success": True, "message": "实验已停止"}
        else:
            raise HTTPException(status_code=400, detail="无法停止实验")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e, "experiment.stop")


@router.post("/{experiment_id}/allocate", summary="分配流量")
async def allocate_traffic(
    experiment_id: str,
    user_id: str,
    session_id: Optional[str] = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    """为用户分配实验变体"""
    try:
        variant_id = await experiment_manager.allocate_traffic(
            experiment_id=experiment_id,
            user_id=user_id,
            session_id=session_id,
            owner_id=current_user.user_id
        )
        if variant_id:
            return {"success": True, "variant_id": variant_id}
        else:
            raise HTTPException(status_code=400, detail="流量分配失败")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e, "experiment.allocate_traffic")


@router.post("/{experiment_id}/metrics", summary="记录指标")
async def record_metric(
    experiment_id: str,
    variant_id: str,
    metric_name: str,
    metric_value: float,
    current_user: CurrentUser = Depends(get_current_user),
):
    """记录实验指标数据"""
    try:
        await experiment_manager.record_metric(
            experiment_id=experiment_id,
            variant_id=variant_id,
            metric_name=metric_name,
            metric_value=metric_value,
            owner_id=current_user.user_id
        )
        return {"success": True, "message": "指标记录成功"}
    except Exception as e:
        raise _fail(e, "experiment.record_metric")


@router.get("/{experiment_id}/metrics", summary="获取指标")
async def get_metrics(
    experiment_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取实验的指标数据"""
    try:
        metrics = await experiment_manager.get_metrics(experiment_id, owner_id=current_user.user_id)
        return {"success": True, "data": metrics}
    except Exception as e:
        raise _fail(e, "experiment.get_metrics")


@router.post("/{experiment_id}/analyze", summary="分析实验")
async def analyze_experiment(
    experiment_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """分析实验结果"""
    try:
        result = await experiment_manager.analyze_experiment(experiment_id, owner_id=current_user.user_id)
        return {"success": True, "data": result}
    except Exception as e:
        raise _fail(e, "experiment.analyze")


@router.get("/{experiment_id}/result", summary="获取实验结果")
async def get_experiment_result(
    experiment_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取实验的分析结果"""
    try:
        result = await experiment_manager.get_experiment_result(experiment_id, owner_id=current_user.user_id)
        if result:
            return {"success": True, "data": result}
        else:
            raise HTTPException(status_code=404, detail="实验结果不存在")
    except HTTPException:
        raise
    except Exception as e:
        raise _fail(e, "experiment.get_result")