"""update_document 外键校验单元测试（P2 #7）。

验证文档更新接口对 category_id / kb_id 的外键校验：
- category_id：UUID 格式 + 存在性（Category 为全局资源，无归属字段）
- kb_id：UUID 格式 + 存在性 + 归属校验（复用 validate_kb_ownership，防越权移动文档）
- 校验抛出的 HTTPException 不被外层 except ValueError 吞掉
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import Select

from src.api.document import update_document
from src.models import Document, Category, KnowledgeBase

USER_ID = "user-1"


class FakeResult:
    def __init__(self, rows):
        self._rows = rows if isinstance(rows, list) else [rows]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return iter(self._rows)


class FakeDB:
    """按查询实体分发结果的假 AsyncSession。"""

    def __init__(self, doc, category=None, owned_kbs=None):
        self._doc = doc
        self._category = category
        self._owned_kbs = owned_kbs or []
        self.commit_calls = 0
        self.executed_entities = []

    async def execute(self, stmt, *args, **kwargs):
        entity = stmt.column_descriptions[0]["entity"]
        self.executed_entities.append(entity)
        if entity is Document:
            return FakeResult(self._doc)
        if entity is Category:
            return FakeResult(self._category)
        if entity is KnowledgeBase:
            return FakeResult(list(self._owned_kbs))
        raise AssertionError(f"意外的查询实体: {entity}")

    async def commit(self):
        self.commit_calls += 1

    async def refresh(self, obj):
        pass


def make_doc():
    return SimpleNamespace(
        id=uuid.uuid4(),
        owner_id=USER_ID,
        category_id=None,
        kb_id=uuid.uuid4(),
        tags=None,
        status=None,
    )


def make_user():
    return SimpleNamespace(user_id=USER_ID, is_authenticated=True)


def make_update(data_dict):
    from src.api.document import DocumentUpdate
    return DocumentUpdate(**data_dict)


class TestUpdateDocumentCategoryValidation:

    @pytest.mark.asyncio
    async def test_invalid_category_uuid_rejected(self):
        """category_id 非法 UUID 返回 400，不执行存在性查询。"""
        doc = make_doc()
        db = FakeDB(doc, category=None)
        data = make_update({"category_id": "not-a-uuid"})

        with pytest.raises(HTTPException) as exc:
            await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert exc.value.status_code == 400
        assert "无效的分类ID" in exc.value.detail
        assert Category not in db.executed_entities
        assert db.commit_calls == 0

    @pytest.mark.asyncio
    async def test_nonexistent_category_rejected(self):
        """category_id 不存在返回 404，不提交。"""
        doc = make_doc()
        db = FakeDB(doc, category=None)
        data = make_update({"category_id": str(uuid.uuid4())})

        with pytest.raises(HTTPException) as exc:
            await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert exc.value.status_code == 404
        assert "分类不存在" in exc.value.detail
        assert db.commit_calls == 0

    @pytest.mark.asyncio
    async def test_valid_category_assigned(self):
        """category_id 存在时正常赋值并提交。"""
        doc = make_doc()
        category = SimpleNamespace(id=uuid.uuid4())
        db = FakeDB(doc, category=category)
        data = make_update({"category_id": str(category.id)})

        result = await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert str(doc.category_id) == str(category.id)
        assert db.commit_calls == 1
        assert result == {"message": "更新成功"}


class TestUpdateDocumentKbValidation:

    @pytest.mark.asyncio
    async def test_invalid_kb_uuid_rejected(self):
        """kb_id 非法 UUID 返回 400，不执行归属校验。"""
        doc = make_doc()
        db = FakeDB(doc, owned_kbs=[])
        data = make_update({"kb_id": "not-a-uuid"})

        with pytest.raises(HTTPException) as exc:
            await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert exc.value.status_code == 400
        assert "无效的知识库ID" in exc.value.detail
        assert KnowledgeBase not in db.executed_entities
        assert db.commit_calls == 0

    @pytest.mark.asyncio
    async def test_kb_not_owned_rejected(self):
        """kb_id 不属于当前用户返回 403，文档不会被移动。"""
        doc = make_doc()
        other_kb_id = uuid.uuid4()
        db = FakeDB(doc, owned_kbs=[])  # 用户名下无此知识库
        data = make_update({"kb_id": str(other_kb_id)})

        with pytest.raises(HTTPException) as exc:
            await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert exc.value.status_code == 403
        assert doc.kb_id != other_kb_id
        assert db.commit_calls == 0

    @pytest.mark.asyncio
    async def test_valid_owned_kb_assigned(self):
        """kb_id 属于当前用户时正常赋值并提交。"""
        doc = make_doc()
        owned_kb_id = uuid.uuid4()
        db = FakeDB(doc, owned_kbs=[owned_kb_id])
        data = make_update({"kb_id": str(owned_kb_id)})

        result = await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert str(doc.kb_id) == str(owned_kb_id)
        assert db.commit_calls == 1
        assert result == {"message": "更新成功"}

    @pytest.mark.asyncio
    async def test_tags_only_update_skips_fk_queries(self):
        """仅更新 tags 时不触发外键校验查询。"""
        doc = make_doc()
        db = FakeDB(doc)
        data = make_update({"tags": ["a", "b"]})

        result = await update_document(str(doc.id), data, db=db, current_user=make_user())

        assert doc.tags == ["a", "b"]
        assert db.commit_calls == 1
        assert db.executed_entities == [Document]  # 仅归属校验的文档查询
