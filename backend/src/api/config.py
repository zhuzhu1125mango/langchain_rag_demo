"""
系统配置API - Config API

提供系统配置的查询和更新接口，包括：
1. 获取系统配置
2. 更新处理配置（分块大小、重叠大小等）
3. 获取处理状态信息
4. 重置配置到默认值
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from src.config import settings
from src.auth import get_current_user, require_admin, CurrentUser
from src.services.document_processor import SUPPORTED_EXTENSIONS

router = APIRouter(prefix="/config", tags=["config"])


class ProcessingConfig(BaseModel):
    """处理配置数据模型"""
    chunk_size: int = 500
    chunk_overlap: int = 50
    top_k: int = 3


class ModelConfig(BaseModel):
    """模型配置数据模型（值始终由 settings 填充，无内置默认）"""
    embedding_model_name: str = ""
    embedding_dimension: int = 0
    ollama_model_name: str = ""
    # 深度思考关闭时使用的非思考直答模型；未配置时为空串（回退主模型 + reasoning=False）
    ollama_direct_model_name: str = ""
    fast_llm_model_name: str = ""


class SystemConfigResponse(BaseModel):
    """系统配置响应数据模型"""
    processing: ProcessingConfig
    model: ModelConfig
    supported_extensions: list


class FeatureStatus(BaseModel):
    """部署级能力开关状态（只读，供前端能力面板展示）。"""
    key: str
    title: str
    enabled: bool
    scope: str
    risk: str
    env_var: str
    note: str


def _feature_statuses() -> List[FeatureStatus]:
    """汇总默认关闭或需运维显式开启的能力，避免前端误以为已生效。"""
    approval_mode = str(getattr(settings.guardrail, "AGENT_APPROVAL_MODE", "off")).lower()
    return [
        FeatureStatus(
            key="wiki_compile",
            title="LLM-Wiki 编译",
            enabled=bool(settings.wiki_compile.WIKI_COMPILE_ENABLED),
            scope="admin",
            risk="low",
            env_var="WIKI_COMPILE_ENABLED",
            note="将文档预编译为结构化 Wiki 页面；会增加文档处理时间和模型调用成本。",
        ),
        FeatureStatus(
            key="agent_web_fetch",
            title="Agent 网页抓取",
            enabled=bool(settings.search.AGENT_WEB_FETCH_ENABLED),
            scope="admin",
            risk="high",
            env_var="AGENT_WEB_FETCH_ENABLED",
            note="允许 Agent 抓取网页正文；存在 SSRF/提示注入外泄风险，开启后建议同时开启高危工具审批。",
        ),
        FeatureStatus(
            key="agent_approval",
            title="高危工具人工审批",
            enabled=approval_mode != "off",
            scope="admin",
            risk="medium",
            env_var="AGENT_APPROVAL_MODE",
            note=f"当前模式：{approval_mode}；开启后高危工具执行前会在聊天中弹出审批卡片。",
        ),
        FeatureStatus(
            key="ab_chat",
            title="A/B 实验接入聊天",
            enabled=bool(settings.experiment.AB_EXPERIMENT_CHAT_ENABLED),
            scope="admin",
            risk="low",
            env_var="AB_EXPERIMENT_CHAT_ENABLED",
            note="开启后聊天请求会按实验配置分流并回收指标；未开启时实验管理页可建实验但不会自动分流。",
        ),
        FeatureStatus(
            key="learning_weight_reload",
            title="学习权重回载",
            enabled=bool(settings.decision.LEARNING_WEIGHT_RELOAD_ENABLED),
            scope="admin",
            risk="medium",
            env_var="LEARNING_WEIGHT_RELOAD_ENABLED",
            note="开启后学习到的策略权重会作用于在线决策；反馈样本不足时可能影响效果。",
        ),
        FeatureStatus(
            key="learning_sample_collect",
            title="学习样本后台补采",
            enabled=bool(settings.decision.LEARNING_SAMPLE_COLLECT_BG_ENABLED),
            scope="admin",
            risk="low",
            env_var="LEARNING_SAMPLE_COLLECT_BG_ENABLED",
            note="KB 勾选短路时后台补采策略结果；会增加少量模型调用和写入开销。",
        ),
        FeatureStatus(
            key="agent_traj_eval",
            title="Agent 轨迹评测",
            enabled=bool(settings.search.AGENT_TRAJ_EVAL_ENABLED),
            scope="admin",
            risk="low",
            env_var="AGENT_TRAJ_EVAL_ENABLED",
            note="仅影响 A/B compare 判定门；在线抽样尚未启用，当前主要走离线评测。",
        ),
    ]


class ProcessingConfigUpdate(BaseModel):
    """处理配置更新请求数据模型"""
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    top_k: Optional[int] = None


@router.get("/", response_model=SystemConfigResponse)
async def get_system_config():
    """
    获取系统配置
    
    Returns:
        SystemConfigResponse: 系统配置信息
    """
    return SystemConfigResponse(
        processing=ProcessingConfig(
            chunk_size=settings.processing.CHUNK_SIZE,
            chunk_overlap=settings.processing.CHUNK_OVERLAP,
            top_k=settings.processing.TOP_K
        ),
        model=ModelConfig(
            embedding_model_name=settings.model.EMBEDDING_MODEL_NAME,
            embedding_dimension=settings.model.EMBEDDING_DIMENSION,
            ollama_model_name=settings.model.OLLAMA_MODEL_NAME,
            fast_llm_model_name=settings.model.FAST_LLM_MODEL_NAME,
        ),
        supported_extensions=SUPPORTED_EXTENSIONS
    )


@router.get("/processing", response_model=ProcessingConfig)
async def get_processing_config():
    """
    获取文档处理配置
    
    Returns:
        ProcessingConfig: 文档处理配置（分块大小、重叠大小等）
    """
    return ProcessingConfig(
        chunk_size=settings.processing.CHUNK_SIZE,
        chunk_overlap=settings.processing.CHUNK_OVERLAP,
        top_k=settings.processing.TOP_K
    )


@router.put("/processing", response_model=ProcessingConfig)
async def update_processing_config(
    config: ProcessingConfigUpdate,
    current_user: CurrentUser = Depends(require_admin),
):
    """
    更新文档处理配置（管理操作：需 ADMIN_KEY 或开发模式）

    Args:
        config: 处理配置更新数据
        current_user: 当前认证用户（经管理员鉴权）

    Returns:
        ProcessingConfig: 更新后的配置

    Raises:
        HTTPException: 参数验证失败时抛出
    """
    if config.chunk_size is not None:
        if config.chunk_size < 50 or config.chunk_size > 5000:
            raise HTTPException(
                status_code=400,
                detail="chunk_size 必须在 50 到 5000 之间"
            )
        settings.processing.CHUNK_SIZE = config.chunk_size
    
    if config.chunk_overlap is not None:
        if config.chunk_overlap < 0 or config.chunk_overlap >= settings.processing.CHUNK_SIZE:
            raise HTTPException(
                status_code=400,
                detail="chunk_overlap 必须大于等于 0 且小于 chunk_size"
            )
        settings.processing.CHUNK_OVERLAP = config.chunk_overlap
    
    if config.top_k is not None:
        if config.top_k < 1 or config.top_k > 20:
            raise HTTPException(
                status_code=400,
                detail="top_k 必须在 1 到 20 之间"
            )
        settings.processing.TOP_K = config.top_k

    # W4-23：运行时覆盖落盘（重启后仍生效）；失败仅告警，内存值不受影响
    from src.services.runtime_config_service import save_runtime_overrides
    save_runtime_overrides()

    # P2-4 审计：配置变更（best-effort）
    from src.services.audit_service import record_audit
    await record_audit(
        action="config.update",
        resource_type="config",
        detail={
            "chunk_size": settings.processing.CHUNK_SIZE,
            "chunk_overlap": settings.processing.CHUNK_OVERLAP,
            "top_k": settings.processing.TOP_K,
        },
        actor_user_id=current_user.user_id,
    )

    return ProcessingConfig(
        chunk_size=settings.processing.CHUNK_SIZE,
        chunk_overlap=settings.processing.CHUNK_OVERLAP,
        top_k=settings.processing.TOP_K
    )


@router.get("/model", response_model=ModelConfig)
async def get_model_config():
    """
    获取模型配置
    
    Returns:
        ModelConfig: 模型配置信息
    """
    return ModelConfig(
        embedding_model_name=settings.model.EMBEDDING_MODEL_NAME,
        embedding_dimension=settings.model.EMBEDDING_DIMENSION,
        ollama_model_name=settings.model.OLLAMA_MODEL_NAME,
        ollama_direct_model_name=settings.model.OLLAMA_DIRECT_MODEL_NAME or "",
        fast_llm_model_name=settings.model.FAST_LLM_MODEL_NAME,
    )


@router.post("/reset")
async def reset_config(current_user: CurrentUser = Depends(require_admin)):
    """
    重置配置到默认值（管理操作：需 ADMIN_KEY 或开发模式）

    Args:
        current_user: 当前认证用户（经管理员鉴权）

    Returns:
        dict: {"message": "配置已重置"}
    """
    settings.processing.CHUNK_SIZE = 512
    settings.processing.CHUNK_OVERLAP = 64
    settings.processing.TOP_K = 3

    # W4-23：重置后同步清除落盘的运行时覆盖，避免重启后覆盖值复活
    from src.services.runtime_config_service import save_runtime_overrides
    save_runtime_overrides()

    # P2-4 审计：配置重置（best-effort）
    from src.services.audit_service import record_audit
    await record_audit(
        action="config.reset",
        resource_type="config",
        detail={"chunk_size": 512, "chunk_overlap": 64, "top_k": 3},
        actor_user_id=current_user.user_id,
    )

    return {"message": "配置已重置为默认值"}


@router.get("/info")
async def get_system_info():
    """
    获取系统信息
    
    Returns:
        dict: 系统信息（版本、支持的文件类型等）
    """
    return {
        "version": "1.0.0",
        "description": "RAG Knowledge Base QA System",
        "supported_extensions": SUPPORTED_EXTENSIONS,
        "processing_defaults": {
            "chunk_size": 512,
            "chunk_overlap": 64,
            "top_k": 3
        },
        # E.6：前端设置页据此渲染「API 密钥」功能未启用空态与达上限禁用
        "user_api_keys_enabled": bool(
            getattr(settings, "USER_API_KEYS_ENABLED", None)
            or getattr(settings.security, "USER_API_KEYS_ENABLED", False)
        ),
        "user_api_keys_max_per_user": int(
            getattr(settings, "USER_API_KEYS_MAX_PER_USER", None)
            or getattr(settings.security, "USER_API_KEYS_MAX_PER_USER", 5)
        ),
        # 能力面板：只读展示默认关闭/需运维开启的功能，避免界面“看起来可用但不生效”
        "features": [feature.model_dump() for feature in _feature_statuses()],
    }