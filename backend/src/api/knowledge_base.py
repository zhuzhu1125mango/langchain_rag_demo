"""
知识库管理API - KnowledgeBase API

提供知识库的CRUD操作接口，包括：
1. 创建知识库
2. 获取知识库列表
3. 获取知识库详情
4. 更新知识库
5. 删除知识库
6. 设置默认知识库
7. 智能推荐知识库
8. 生成知识图谱

注：重复文档检测、文档质量评估、文档自动分类已统一由 document 模块提供。
"""

from fastapi import APIRouter, Depends, HTTPException, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from pydantic import BaseModel
from typing import List, Optional
import uuid
from src.database import get_db
from src.models import KnowledgeBase, Document
from src.services.vector_store import VectorStoreManager
from src.services.rag_chain import RAGChain

router = APIRouter(prefix="/knowledge_bases", tags=["knowledge_bases"])


class KnowledgeBaseCreate(BaseModel):
    """创建知识库请求模型。"""
    name: str
    description: Optional[str] = None
    embedding_model: Optional[str] = "nomic-embed-text:latest"


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库请求模型。"""
    name: Optional[str] = None
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    status: Optional[str] = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应模型。"""
    id: str
    name: str
    description: Optional[str] = None
    embedding_model: str
    is_default: bool
    status: str
    document_count: int
    created_at: str
    updated_at: str


class KBRecommendationRequest(BaseModel):
    """知识库推荐请求模型。"""
    question: str
    top_k: Optional[int] = 3


class KBRecommendationResponse(BaseModel):
    """知识库推荐响应模型。"""
    kb_id: str
    relevance_score: float
    matched_chunks: int


class KnowledgeGraphResponse(BaseModel):
    """知识图谱响应模型。"""
    nodes: List[dict]
    edges: List[dict]
    summary: str


async def get_default_kb(db: AsyncSession) -> Optional[KnowledgeBase]:
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.is_default == True))
    return result.scalar_one_or_none()


async def set_single_default(db: AsyncSession, kb_id: uuid.UUID):
    await db.execute(update(KnowledgeBase).values(is_default=False))
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb:
        kb.is_default = True
    await db.commit()


@router.post("/", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.name == data.name))
    existing_kb = result.scalar_one_or_none()
    if existing_kb:
        raise HTTPException(status_code=400, detail="知识库名称已存在")

    count_result = await db.execute(select(func.count(KnowledgeBase.id)))
    is_first = count_result.scalar_one() == 0
    
    kb = KnowledgeBase(
        id=uuid.uuid4(),
        name=data.name,
        description=data.description,
        embedding_model=data.embedding_model,
        is_default=is_first
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)

    return KnowledgeBaseResponse(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        is_default=kb.is_default,
        status=kb.status,
        document_count=0,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
    )


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应模型。"""
    items: List[KnowledgeBaseResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


@router.get("/", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=1000, description="每页数量")
):
    subq = select(
        Document.kb_id,
        func.count(Document.id).label("doc_count")
    ).group_by(Document.kb_id).subquery()
    
    total_result = await db.execute(select(func.count(KnowledgeBase.id)))
    total = total_result.scalar_one()
    
    offset = (page - 1) * page_size
    
    result = await db.execute(
        select(
            KnowledgeBase,
            func.coalesce(subq.c.doc_count, 0).label("document_count")
        ).outerjoin(subq, KnowledgeBase.id == subq.c.kb_id)
        .offset(offset)
        .limit(page_size)
    )
    
    items = []
    for kb, doc_count in result.all():
        items.append(KnowledgeBaseResponse(
            id=str(kb.id),
            name=kb.name,
            description=kb.description,
            embedding_model=kb.embedding_model,
            is_default=kb.is_default,
            status=kb.status,
            document_count=doc_count,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
        ))
    
    total_pages = (total + page_size - 1) // page_size
    
    return KnowledgeBaseListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    try:
        kb_id_uuid = uuid.UUID(kb_id)
        
        subq = select(
            func.count(Document.id).label("doc_count")
        ).filter(Document.kb_id == kb_id_uuid).subquery()
        
        result = await db.execute(
            select(KnowledgeBase, func.coalesce(subq.c.doc_count, 0).label("document_count"))
            .filter(KnowledgeBase.id == kb_id_uuid)
            .outerjoin(subq, True)
        )
        
        row = result.one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        kb, doc_count = row
        
        return KnowledgeBaseResponse(
            id=str(kb.id),
            name=kb.name,
            description=kb.description,
            embedding_model=kb.embedding_model,
            is_default=kb.is_default,
            status=kb.status,
            document_count=doc_count,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    data: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db)
):
    try:
        kb_id_uuid = uuid.UUID(kb_id)
        
        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_id_uuid))
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")

        if data.name:
            existing_result = await db.execute(select(KnowledgeBase).filter(
                KnowledgeBase.name == data.name,
                KnowledgeBase.id != kb_id_uuid
            ))
            existing_kb = existing_result.scalar_one_or_none()
            if existing_kb:
                raise HTTPException(status_code=400, detail="知识库名称已存在")
            kb.name = data.name
        
        if data.description is not None:
            kb.description = data.description
        
        if data.embedding_model:
            kb.embedding_model = data.embedding_model
        
        if data.status:
            kb.status = data.status

        await db.commit()
        await db.refresh(kb)

        doc_count_result = await db.execute(select(func.count(Document.id)).filter(Document.kb_id == kb_id_uuid))
        doc_count = doc_count_result.scalar_one()
        
        return KnowledgeBaseResponse(
            id=str(kb.id),
            name=kb.name,
            description=kb.description,
            embedding_model=kb.embedding_model,
            is_default=kb.is_default,
            status=kb.status,
            document_count=doc_count,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")


@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db)
):
    try:
        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == uuid.UUID(kb_id)))
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")

        if kb.is_default:
            other_result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id != uuid.UUID(kb_id)))
            other_kbs = other_result.scalars().all()
            if other_kbs:
                other_kbs[0].is_default = True
            else:
                raise HTTPException(status_code=400, detail="不能删除最后一个知识库")

        doc_result = await db.execute(select(Document).filter(Document.kb_id == kb.id))
        docs = doc_result.scalars().all()
        
        from src.services.minio_service import MinioService
        minio_service = MinioService()
        vector_store = await VectorStoreManager.get_instance()

        for doc in docs:
            if doc.file_path.startswith("minio://"):
                minio_service.delete_file(doc.file_path)
            await vector_store.delete_by_document_id(str(doc.id))

        await vector_store.save_vector_store()
        
        for doc in docs:
            await db.delete(doc)
        
        await db.delete(kb)
        await db.commit()

        return {"message": "知识库已删除"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")


@router.post("/{kb_id}/set_default")
async def set_default_knowledge_base(kb_id: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == uuid.UUID(kb_id)))
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")

        await set_single_default(db, uuid.UUID(kb_id))
        
        return {"message": "已设置为默认知识库"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")


@router.get("/default", response_model=KnowledgeBaseResponse)
async def get_default_knowledge_base(db: AsyncSession = Depends(get_db)):
    kb = await get_default_kb(db)
    if not kb:
        raise HTTPException(status_code=404, detail="没有找到默认知识库")
    
    doc_count_result = await db.execute(select(func.count(Document.id)).filter(Document.kb_id == kb.id))
    doc_count = doc_count_result.scalar_one()
    
    return KnowledgeBaseResponse(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        is_default=kb.is_default,
        status=kb.status,
        document_count=doc_count,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
    )


@router.post("/recommend", response_model=List[KBRecommendationResponse])
async def recommend_knowledge_bases(request: KBRecommendationRequest):
    """
    基于问题自动推荐相关知识库

    Args:
        request: 请求数据（问题、返回数量）

    Returns:
        list: 推荐的知识库列表，按相关性排序
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        recommendations = await rag_chain.recommend_knowledge_bases(request.question, request.top_k)
        return recommendations
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"知识库推荐失败: {str(e)}")


class KnowledgeGraphRequest(BaseModel):
    """知识图谱生成请求模型。"""
    kb_ids: Optional[List[str]] = None


@router.post("/knowledge_graph", response_model=KnowledgeGraphResponse)
async def generate_knowledge_graph(request: KnowledgeGraphRequest = Body(...)):
    """
    生成知识库关系图谱

    Args:
        request: 包含知识库ID列表的请求体（可选）

    Returns:
        dict: 知识图谱数据
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.generate_knowledge_graph(request.kb_ids)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成知识图谱失败: {str(e)}")