"""ID 校验工具：API 边界的 UUID 校验与资源归属校验。

作为进入检索/存储层前的第一道防线：
- parse_uuid_list: 拒绝非 UUID 字符串（消除 Milvus/DB 过滤表达式注入面）
- validate_kb_ownership: 拒绝访问他人知识库（对象级授权）
"""

import uuid
from typing import List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser
from src.models import KnowledgeBase


def parse_uuid_list(
    raw_ids: Optional[List[str]], name: str = "kb_id"
) -> List[str]:
    """校验 ID 列表全部为合法 UUID，返回规范化（小写、去重、保序）后的列表。

    Args:
        raw_ids: 客户端传入的 ID 列表（可为 None/空）。
        name: 错误提示中显示的参数名。

    Returns:
        规范化后的 UUID 字符串列表；输入为空时返回 []。

    Raises:
        HTTPException: 任一 ID 非法时返回 400。
    """
    if not raw_ids:
        return []
    normalized = []
    for rid in raw_ids:
        try:
            normalized.append(str(uuid.UUID(str(rid))))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=400, detail=f"无效的{name}: {str(rid)[:64]}")
    return list(dict.fromkeys(normalized))


async def validate_kb_ownership(
    db: AsyncSession, kb_ids: List[str], current_user: CurrentUser
) -> None:
    """校验知识库 ID 列表全部属于当前用户。

    单次 SELECT ... IN 批量校验；任一 ID 不属于当前用户时整体拒绝，
    避免部分放行导致的越权检索。

    Raises:
        HTTPException: 存在非本人知识库时返回 403。
    """
    if not kb_ids:
        return
    # 复用 parse_uuid_list 规范化：非法 ID 统一返回 400，并转为小写去重集合。
    # 归一化是必需的——DB 中 str(UUID) 为小写，若调用方传入大写 UUID，
    # 下面的集合比较会把"自己的知识库"误判为无权限。
    normalized = set(parse_uuid_list(kb_ids))
    result = await db.execute(
        select(KnowledgeBase.id).filter(
            KnowledgeBase.id.in_([uuid.UUID(k) for k in normalized]),
            # 对象级授权：必须限定为当前用户所有。
            # 缺少此条件时本函数退化为"知识库是否存在"的存在性检查，
            # 任何登录用户都能借他人 kb_id 检索其全部内容。
            KnowledgeBase.owner_id == current_user.user_id,
        )
    )
    owned = {str(row) for row in result.scalars()}
    # 集合比较而非长度比较：调用方传入重复 ID 时长度比较会误判为无权
    if owned != normalized:
        raise HTTPException(status_code=403, detail="无权访问部分知识库")
