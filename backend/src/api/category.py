"""
分类管理API - Category API

提供文档分类的CRUD操作接口，支持多级分类结构：
1. 创建分类
2. 获取分类列表
3. 更新分类
4. 删除分类
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import List, Optional
import uuid
from src.database import get_db
from src.models import Category

router = APIRouter(prefix="/categories", tags=["categories"])


class CategoryResponse(BaseModel):
    """分类响应数据模型"""
    id: str
    name: str
    description: Optional[str]
    parent_id: Optional[str]
    sort_order: int


class CategoryCreate(BaseModel):
    """分类创建/更新请求数据模型"""
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    sort_order: int = 0


@router.post("/")
async def create_category(data: CategoryCreate, db: AsyncSession = Depends(get_db)):
    """
    创建分类
    
    Args:
        data: 分类数据（名称、描述、父分类ID、排序）
        db: 数据库会话
        
    Returns:
        dict: {"id": 分类ID, "name": 分类名称}
    """
    result = await db.execute(select(Category).filter(Category.name == data.name))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="分类名称已存在")
    
    parent_id = uuid.UUID(data.parent_id) if data.parent_id else None
    
    category = Category(
        name=data.name,
        description=data.description,
        parent_id=parent_id,
        sort_order=data.sort_order
    )
    db.add(category)
    await db.commit()
    await db.refresh(category)
    return {"id": str(category.id), "name": category.name}


@router.get("/", response_model=List[CategoryResponse])
async def list_categories(db: AsyncSession = Depends(get_db)):
    """
    获取分类列表
    
    Args:
        db: 数据库会话
        
    Returns:
        list: 分类列表（按排序顺序）
    """
    result = await db.execute(select(Category).order_by(Category.sort_order))
    categories = result.scalars().all()
    return [
        CategoryResponse(
            id=str(c.id),
            name=c.name,
            description=c.description,
            parent_id=str(c.parent_id) if c.parent_id else None,
            sort_order=c.sort_order
        ) for c in categories
    ]


@router.put("/{category_id}")
async def update_category(category_id: str, data: CategoryCreate, db: AsyncSession = Depends(get_db)):
    """
    更新分类
    
    Args:
        category_id: 分类ID
        data: 更新数据（名称、描述、父分类ID、排序）
        db: 数据库会话
        
    Returns:
        dict: {"message": "更新成功"}
    """
    try:
        result = await db.execute(select(Category).filter(Category.id == uuid.UUID(category_id)))
        category = result.scalar_one_or_none()
        if not category:
            raise HTTPException(status_code=404, detail="分类不存在")
        
        category.name = data.name
        category.description = data.description
        category.parent_id = uuid.UUID(data.parent_id) if data.parent_id else None
        category.sort_order = data.sort_order
        
        await db.commit()
        return {"message": "更新成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的分类ID")


@router.delete("/{category_id}")
async def delete_category(category_id: str, db: AsyncSession = Depends(get_db)):
    """
    删除分类
    
    Args:
        category_id: 分类ID
        db: 数据库会话
        
    Returns:
        dict: {"message": "删除成功"}
    """
    try:
        result = await db.execute(select(Category).filter(Category.id == uuid.UUID(category_id)))
        category = result.scalar_one_or_none()
        if not category:
            raise HTTPException(status_code=404, detail="分类不存在")
        
        await db.delete(category)
        await db.commit()
        return {"message": "删除成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的分类ID")
