"""
kb_ids 校验与 Milvus 表达式防注入单元测试。

覆盖两层防御：
- API 边界：parse_uuid_list 格式校验 / validate_kb_ownership 归属校验
- 存储层兜底：MilvusService._build_filter_expr / _safe_id 拒绝非法 ID

不依赖真实服务，可随单元测试全量运行。
"""

import uuid as uuid_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.services.milvus_service import MilvusService
from src.utils.validators import parse_uuid_list, validate_kb_ownership


VALID_ID = "123e4567-e89b-12d3-a456-426614174000"
VALID_ID_2 = "00000000-0000-0000-0000-000000000001"


class TestParseUuidList:
    """parse_uuid_list 格式校验。"""

    def test_empty_input_returns_empty_list(self):
        assert parse_uuid_list(None) == []
        assert parse_uuid_list([]) == []

    def test_valid_ids_normalized_and_deduped(self):
        result = parse_uuid_list([VALID_ID.upper(), VALID_ID, VALID_ID_2])
        assert result == [VALID_ID, VALID_ID_2]

    def test_malicious_expression_rejected(self):
        """含引号/布尔片段的注入字符串必须被拒绝。"""
        for payload in [
            f'{VALID_ID}" || kb_id != "',
            "'; DROP COLLECTION x; --",
            f"{VALID_ID} or 1==1",
            "",
            "   ",
        ]:
            with pytest.raises(HTTPException) as exc:
                parse_uuid_list([payload])
            assert exc.value.status_code == 400

    def test_error_message_truncated(self):
        """错误提示截断超长输入，避免反射放大。"""
        with pytest.raises(HTTPException) as exc:
            parse_uuid_list(["A" * 500])
        assert len(exc.value.detail) < 100


class TestValidateKbOwnership:
    """validate_kb_ownership 归属校验。"""

    @pytest.mark.asyncio
    async def test_empty_list_passes(self):
        db = AsyncMock()
        user = SimpleNamespace(user_id="user_a")
        await validate_kb_ownership(db, [], user)
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_foreign_kb_rejected(self):
        """列表中包含不属于当前用户的知识库时整体拒绝。"""
        owned = SimpleNamespace(scalars=lambda: [uuid_mod.UUID(VALID_ID)])
        db = AsyncMock()
        db.execute = AsyncMock(return_value=owned)
        user = SimpleNamespace(user_id="user_a")

        with pytest.raises(HTTPException) as exc:
            await validate_kb_ownership(db, [VALID_ID, VALID_ID_2], user)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_all_owned_passes(self):
        owned = SimpleNamespace(
            scalars=lambda: [uuid_mod.UUID(VALID_ID), uuid_mod.UUID(VALID_ID_2)]
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=owned)
        user = SimpleNamespace(user_id="user_a")

        await validate_kb_ownership(db, [VALID_ID, VALID_ID_2], user)


class TestValidateKbOwnershipSql:
    """回归守卫：validate_kb_ownership 必须在 SQL 层限定 owner_id。

    背景：该函数曾只按 id 查询、不限定 owner_id，`current_user` 参数在函数体内
    从未被引用，使「对象级授权」退化为「知识库是否存在」的存在性检查——
    任何登录用户拿到他人 kb_id 即可检索其全部内容。
    上面那组基于 AsyncMock 的用例伪造了 execute 的返回值，只断言了长度逻辑，
    无法发现该缺陷。这里改为捕获真实语句并断言 owner_id 条件存在于编译后的 SQL。
    """

    @pytest.mark.asyncio
    async def test_sql_filters_by_owner_id(self):
        captured = {}

        class _Result:
            @staticmethod
            def scalars():
                return []

        async def _execute(stmt):
            captured["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return _Result()

        db = SimpleNamespace(execute=_execute)
        user = SimpleNamespace(user_id="user_a")

        with pytest.raises(HTTPException) as exc:
            await validate_kb_ownership(db, [VALID_ID], user)
        assert exc.value.status_code == 403

        sql = captured["sql"]
        assert "owner_id" in sql, f"SQL 未限定 owner_id，对象级授权被绕过: {sql}"
        assert "user_a" in sql, f"SQL 未使用当前用户 ID: {sql}"

    @pytest.mark.asyncio
    async def test_uppercase_uuid_normalized_not_denied(self):
        """大写 UUID 归一化后应命中自己的知识库，不被误判为无权限。"""

        class _Result:
            @staticmethod
            def scalars():
                return [uuid_mod.UUID(VALID_ID)]

        async def _execute(stmt):
            return _Result()

        db = SimpleNamespace(execute=_execute)
        user = SimpleNamespace(user_id="user_a")

        # 不抛 HTTPException 即通过
        await validate_kb_ownership(db, [VALID_ID.upper()], user)


class TestMilvusFilterExprSafety:
    """MilvusService 表达式构造兜底。"""

    def test_build_expr_with_valid_uuids(self):
        expr = MilvusService._build_filter_expr(kb_ids=[VALID_ID])
        assert expr == f'kb_id in ["{VALID_ID}"]'

    def test_build_expr_combined(self):
        expr = MilvusService._build_filter_expr(
            document_ids=[VALID_ID_2], kb_ids=[VALID_ID]
        )
        assert expr == f'kb_id in ["{VALID_ID}"] && document_id in ["{VALID_ID_2}"]'

    def test_build_expr_rejects_injection(self):
        """注入payload 必须在表达式构造层被拦截（防御纵深）。"""
        with pytest.raises(ValueError):
            MilvusService._build_filter_expr(kb_ids=[f'{VALID_ID}" || kb_id != "x" || kb_id == "y'])
        with pytest.raises(ValueError):
            MilvusService._build_filter_expr(document_ids=["anything-non-uuid"])

    def test_safe_id_accepts_uuid_object(self):
        u = uuid_mod.uuid4()
        assert MilvusService._safe_id(u) == str(u)

    def test_delete_expr_helpers_are_gated(self):
        """_safe_id 是所有 delete/query 表达式的必经校验。"""
        with pytest.raises(ValueError):
            MilvusService._safe_id("'; DELETE --")
