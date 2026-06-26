"""
A/B测试实验API接口

提供实验的创建、管理和分析功能
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from src.services.experiment_manager import experiment_manager

router = APIRouter(prefix="/experiments", tags=["experiments"])


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
async def create_experiment(request: CreateExperimentRequest):
    """创建新的A/B测试实验"""
    try:
        experiment_id = await experiment_manager.create_experiment(
            name=request.name,
            description=request.description,
            variants=request.variants,
            metrics=request.metrics
        )
        return {"success": True, "experiment_id": experiment_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/", summary="获取实验列表")
async def list_experiments(status: Optional[str] = None):
    """获取实验列表，支持状态过滤"""
    try:
        experiments = await experiment_manager.list_experiments(status=status)
        return {"success": True, "data": experiments}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/batch", summary="批量删除实验")
async def batch_delete_experiments(request: BatchDeleteExperimentsRequest):
    """批量删除实验及其关联数据（变体、指标、结果、分流记录）"""
    try:
        result = await experiment_manager.delete_experiments(request.experiment_ids)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{experiment_id}", summary="获取实验详情")
async def get_experiment(experiment_id: str):
    """获取指定实验的详细信息"""
    try:
        experiment = await experiment_manager.get_experiment(experiment_id)
        if experiment:
            return {"success": True, "data": experiment}
        else:
            raise HTTPException(status_code=404, detail="实验不存在")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{experiment_id}/start", summary="启动实验")
async def start_experiment(experiment_id: str):
    """启动实验"""
    try:
        success = await experiment_manager.start_experiment(experiment_id)
        if success:
            return {"success": True, "message": "实验已启动"}
        else:
            raise HTTPException(status_code=400, detail="无法启动实验")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{experiment_id}/stop", summary="停止实验")
async def stop_experiment(experiment_id: str):
    """停止实验"""
    try:
        success = await experiment_manager.stop_experiment(experiment_id)
        if success:
            return {"success": True, "message": "实验已停止"}
        else:
            raise HTTPException(status_code=400, detail="无法停止实验")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{experiment_id}/allocate", summary="分配流量")
async def allocate_traffic(
    experiment_id: str,
    user_id: str,
    session_id: Optional[str] = None
):
    """为用户分配实验变体"""
    try:
        variant_id = await experiment_manager.allocate_traffic(
            experiment_id=experiment_id,
            user_id=user_id,
            session_id=session_id
        )
        if variant_id:
            return {"success": True, "variant_id": variant_id}
        else:
            raise HTTPException(status_code=400, detail="流量分配失败")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{experiment_id}/metrics", summary="记录指标")
async def record_metric(
    experiment_id: str,
    variant_id: str,
    metric_name: str,
    metric_value: float
):
    """记录实验指标数据"""
    try:
        await experiment_manager.record_metric(
            experiment_id=experiment_id,
            variant_id=variant_id,
            metric_name=metric_name,
            metric_value=metric_value
        )
        return {"success": True, "message": "指标记录成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{experiment_id}/metrics", summary="获取指标")
async def get_metrics(experiment_id: str):
    """获取实验的指标数据"""
    try:
        metrics = await experiment_manager.get_metrics(experiment_id)
        return {"success": True, "data": metrics}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{experiment_id}/analyze", summary="分析实验")
async def analyze_experiment(experiment_id: str):
    """分析实验结果"""
    try:
        result = await experiment_manager.analyze_experiment(experiment_id)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{experiment_id}/result", summary="获取实验结果")
async def get_experiment_result(experiment_id: str):
    """获取实验的分析结果"""
    try:
        result = await experiment_manager.get_experiment_result(experiment_id)
        if result:
            return {"success": True, "data": result}
        else:
            raise HTTPException(status_code=404, detail="实验结果不存在")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))