"""Wiki lint 周期调度（阶段一 D4，见 docs/design/wiki-navigable-workspace.md §5）。

lifespan 启动后台循环：每 WIKI_LINT_INTERVAL_HOURS 小时对全部含 active
wiki 页的知识库执行 lint_kb_wiki，结果记日志（矛盾/过时/孤儿页）。
0 = 关闭循环，仅保留手动 GET /api/wiki/lint。
"""

import asyncio
import logging
from typing import Optional

from src.config import settings
from src.services.wiki_lint import lint_kb_wiki

logger = logging.getLogger("rag_system")

_task: Optional[asyncio.Task] = None


async def lint_active_kbs() -> int:
    """对所有含 active wiki 页的知识库执行一次 lint，返回成功处理的 KB 数。"""
    from sqlalchemy import select

    from src.database import async_session_maker
    from src.models.wiki_page import WikiPage

    async with async_session_maker() as db:
        kb_ids = (
            await db.execute(
                select(WikiPage.kb_id)
                .filter(WikiPage.status == "active")
                .distinct()
            )
        ).scalars().all()

    count = 0
    for kb_id in kb_ids:
        try:
            async with async_session_maker() as db:
                report = await lint_kb_wiki(db, str(kb_id))
            summary = report.to_dict() if hasattr(report, "to_dict") else str(report)
            logger.info(f"Wiki 周期 lint 完成 kb={kb_id}: {summary}")
            count += 1
        except Exception as e:
            logger.warning(f"Wiki 周期 lint 失败 kb={kb_id}: {e}")
    return count


async def _loop() -> None:
    interval_hours = max(0, settings.wiki_compile.WIKI_LINT_INTERVAL_HOURS)
    if interval_hours <= 0:
        return
    while True:
        await asyncio.sleep(interval_hours * 3600)
        try:
            await lint_active_kbs()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Wiki lint 周期任务异常（继续下一轮）: {e}")


def start_scheduler() -> Optional[asyncio.Task]:
    """启动 lint 后台循环（幂等；间隔 ≤0 时不启动）。返回任务或 None。"""
    global _task
    if _task is not None and not _task.done():
        return _task
    if max(0, settings.wiki_compile.WIKI_LINT_INTERVAL_HOURS) <= 0:
        return None
    _task = asyncio.create_task(_loop())
    logger.info(
        f"Wiki lint 周期调度已启动（每 {settings.wiki_compile.WIKI_LINT_INTERVAL_HOURS} 小时）"
    )
    return _task


async def stop_scheduler() -> None:
    """取消后台循环（lifespan shutdown 调用；幂等）。"""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
