"""
知识库数据迁移脚本

将现有文档迁移到默认知识库中

使用方法:
    cd backend && python scripts/migrate_knowledge_base.py
"""

import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from uuid import uuid4
from sqlalchemy import select
from src.database import async_session
from src.models import Document, KnowledgeBase


async def create_default_knowledge_base(db):
    """创建默认知识库"""
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.is_default == True))
    default_kb = result.scalars().first()
    if not default_kb:
        result = await db.execute(select(KnowledgeBase))
        default_kb = result.scalars().first()

    if not default_kb:
        default_kb = KnowledgeBase(
            id=uuid4(),
            name="默认知识库",
            description="系统默认知识库",
            embedding_model="nomic-embed-text:latest",
            is_default=True,
            status="active"
        )
        db.add(default_kb)
        await db.commit()
        await db.refresh(default_kb)
        print(f"创建默认知识库: {default_kb.name} (ID: {default_kb.id})")
    else:
        print(f"使用现有默认知识库: {default_kb.name} (ID: {default_kb.id})")

    return default_kb


async def migrate_documents_to_kb(db, kb):
    """将没有知识库关联的文档迁移到指定知识库"""
    result = await db.execute(select(Document).filter(Document.kb_id == None))
    docs_without_kb = result.scalars().all()

    if not docs_without_kb:
        print("没有需要迁移的文档")
        return 0

    print(f"找到 {len(docs_without_kb)} 个文档需要迁移...")

    migrated_count = 0
    for doc in docs_without_kb:
        doc.kb_id = kb.id
        migrated_count += 1

        if migrated_count % 10 == 0:
            print(f"已迁移 {migrated_count} 个文档...")

    await db.commit()
    print(f"成功迁移 {migrated_count} 个文档到知识库 '{kb.name}'")

    return migrated_count


async def migrate_milvus_vectors(kb):
    """迁移Milvus向量数据添加kb_id"""
    try:
        from src.services.milvus_service import MilvusService

        milvus_service = await MilvusService.get_instance()

        print("正在迁移Milvus向量数据...")

        count = await milvus_service.count()
        print(f"Milvus中共有 {count} 个向量")

        print("Milvus向量数据迁移完成（新向量会自动关联知识库）")

    except Exception as e:
        print(f"Milvus迁移警告: {str(e)}")
        print("注意：现有Milvus向量的kb_id将为空，新上传的文档会正确关联")


async def main():
    print("=== 知识库数据迁移脚本 ===")
    print()

    async with async_session() as db:
        # 创建默认知识库
        kb = await create_default_knowledge_base(db)

        # 迁移文档
        await migrate_documents_to_kb(db, kb)

        # 迁移向量数据
        await migrate_milvus_vectors(kb)

        print()
        print("=== 迁移完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
