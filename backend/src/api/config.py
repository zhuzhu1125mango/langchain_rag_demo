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
from typing import Optional, Dict, Any
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
    fast_llm_model_name: str = ""


class SystemConfigResponse(BaseModel):
    """系统配置响应数据模型"""
    processing: ProcessingConfig
    model: ModelConfig
    supported_extensions: list


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
    settings.processing.CHUNK_SIZE = 500
    settings.processing.CHUNK_OVERLAP = 50
    settings.processing.TOP_K = 3

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
            "chunk_size": 500,
            "chunk_overlap": 50,
            "top_k": 3
        }
    }