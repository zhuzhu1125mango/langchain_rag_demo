"""
标签管理API - Tag API

提供文档标签的CRUD操作接口：
1. 创建标签
2. 获取标签列表
3. 更新标签
4. 删除标签
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import List, Optional
import uuid
from src.database import get_db
from src.models import Tag

router = APIRouter(prefix="/tags", tags=["tags"])


class TagResponse(BaseModel):
    """标签响应数据模型"""
    id: str
    name: str
    color: str


class TagCreate(BaseModel):
    """标签创建/更新请求数据模型"""
    name: str
    color: Optional[str] = "#1890ff"


@router.post("/")
async def create_tag(data: TagCreate, db: AsyncSession = Depends(get_db)):
    """
    创建标签
    
    Args:
        data: 标签数据（名称、颜色）
        db: 数据库会话
        
    Returns:
        dict: {"id": 标签ID, "name": 标签名称}
    """
    result = await db.execute(select(Tag).filter(Tag.name == data.name))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="标签名称已存在")
    
    tag = Tag(
        name=data.name,
        color=data.color
    )
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return {"id": str(tag.id), "name": tag.name}


@router.get("/", response_model=List[TagResponse])
async def list_tags(db: AsyncSession = Depends(get_db)):
    """
    获取标签列表
    
    Args:
        db: 数据库会话
        
    Returns:
        list: 标签列表
    """
    result = await db.execute(select(Tag))
    tags = result.scalars().all()
    return [
        TagResponse(
            id=str(t.id),
            name=t.name,
            color=t.color
        ) for t in tags
    ]


@router.put("/{tag_id}")
async def update_tag(tag_id: str, data: TagCreate, db: AsyncSession = Depends(get_db)):
    """
    更新标签
    
    Args:
        tag_id: 标签ID
        data: 更新数据（名称、颜色）
        db: 数据库会话
        
    Returns:
        dict: {"message": "更新成功"}
    """
    try:
        result = await db.execute(select(Tag).filter(Tag.id == uuid.UUID(tag_id)))
        tag = result.scalar_one_or_none()
        if not tag:
            raise HTTPException(status_code=404, detail="标签不存在")
        
        tag.name = data.name
        tag.color = data.color
        
        await db.commit()
        return {"message": "更新成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的标签ID")


@router.delete("/{tag_id}")
async def delete_tag(tag_id: str, db: AsyncSession = Depends(get_db)):
    """
    删除标签
    
    Args:
        tag_id: 标签ID
        db: 数据库会话
        
    Returns:
        dict: {"message": "删除成功"}
    """
    try:
        result = await db.execute(select(Tag).filter(Tag.id == uuid.UUID(tag_id)))
        tag = result.scalar_one_or_none()
        if not tag:
            raise HTTPException(status_code=404, detail="标签不存在")
        
        await db.delete(tag)
        await db.commit()
        return {"message": "删除成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的标签ID")
