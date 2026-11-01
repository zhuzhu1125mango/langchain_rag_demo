"""流式运行的重连、停止与在途查询（AGT-1）。

三个端点都围绕同一个 ``run_id``：

- ``GET  /chat/runs/{run_id}/stream`` 续传：先重放游标之后的事件，再实时跟随
- ``POST /chat/runs/{run_id}/stop``   停止：服务端取消生成，等部分答案落库
- ``GET  /chat/runs/active``          刷新/换设备后找回该会话在途的运行

共同前提：``run_id`` 只在进程内有效（见 :mod:`src.services.chat_run`），
过期或进程重启后返回 404，前端据此回落到「重新加载会话历史」。
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from src.auth import CurrentUser, get_current_user
from src.config import settings
from src.services.chat_run import (
    ChatRun,
    format_sse_comment,
    get_run_registry,
)

logger = logging.getLogger("src.api.chat.runs")

router = APIRouter(tags=["chat"])

# SSE 响应头：禁用各级缓存与代理缓冲，否则事件会被攒成一大块，续传就没有意义
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


async def iter_run_frames(run: ChatRun, after: int = 0) -> AsyncIterator[str]:
    """产出该运行的 SSE 帧：重放 + 实时跟随 + 空闲心跳。

    客户端断连时本生成器被取消，但**不会**取消生成任务——这正是本次改造的核心。
    """
    heartbeat = float(settings.chat_run.CHAT_RUN_HEARTBEAT_SECONDS or 0)
    agen = run.subscribe(after)
    next_task: Optional[asyncio.Task] = None
    try:
        while True:
            if next_task is None:
                next_task = asyncio.ensure_future(agen.__anext__())
            if heartbeat > 0:
                # 不用 wait_for：超时会取消 __anext__，连带把异步生成器关掉，
                # 之后再也拿不到事件。这里只「看」超时，任务本身继续挂着。
                done, _ = await asyncio.wait({next_task}, timeout=heartbeat)
                if not done:
                    yield format_sse_comment()
                    continue
            else:
                await asyncio.wait({next_task})

            task, next_task = next_task, None
            try:
                _seq, frame = task.result()
            except StopAsyncIteration:
                return
            yield frame
    finally:
        # 先等被取消的 __anext__ 真正退出，再 aclose：异步生成器「正在运行」时
        # 调 aclose 会抛 RuntimeError（取消尚未落到生成器的 await 点上）
        if next_task is not None and not next_task.done():
            next_task.cancel()
            try:
                await next_task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            except Exception:
                # 生成器自身异常已在上面 result() 分支处理，这里只做清理
                pass
        await agen.aclose()


def _authorize(run: Optional[ChatRun], run_id: str, current_user: CurrentUser) -> ChatRun:
    if run is None:
        # 过期（超过保留窗口）或进程重启：让前端回落到历史加载，而不是空等
        raise HTTPException(
            status_code=404,
            detail={"code": "run_not_found", "message": "该回答的实时流已结束或已过期"},
        )
    if str(run.user_id) != str(current_user.user_id):
        # 越权读取他人回答属数据泄露，必须显式拒绝并留痕
        logger.warning(
            "拒绝跨用户访问流式运行: run=%s owner=%s requester=%s",
            run_id,
            run.user_id,
            current_user.user_id,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "run_forbidden", "message": "无权访问该回答"},
        )
    return run


@router.get("/runs/active")
async def get_active_run(
    session_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """查询会话在途运行（刷新/换设备后重新挂上）。"""
    run = get_run_registry().find_active_by_session(str(session_id), str(current_user.user_id))
    if run is None:
        return {"run_id": None}
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "status": run.status,
        # 客户端从头重放：它是新挂上来的，本地没有任何已收事件
        "after": 0,
        "published": run.published,
    }


@router.get("/runs/{run_id}/stream")
async def resume_run_stream(
    run_id: str,
    request: Request,
    after: int = 0,
    current_user: CurrentUser = Depends(get_current_user),
):
    """从指定游标续传。``after`` 缺省时回退到 SSE 的 Last-Event-ID。"""
    run = _authorize(get_run_registry().get(run_id), run_id, current_user)

    if not after:
        last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                after = int(last_event_id)
            except ValueError:
                # 浏览器自动重连可能带非数字 id；退化为从头重放，不报错打断用户
                logger.warning(
                    "Last-Event-ID 非数字，退化为从头重放: run=%s value=%r", run_id, last_event_id
                )
                after = 0

    if not run.can_resume_from(after):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "resume_gap",
                "message": "中间事件已被回收，请重新加载会话历史",
                "oldest_seq": run.oldest_seq,
            },
        )

    logger.info(
        "续传流式运行: run=%s after=%d published=%d status=%s",
        run_id,
        after,
        run.published,
        run.status,
    )
    return StreamingResponse(
        iter_run_frames(run, after), media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/runs/{run_id}/stop")
async def stop_run(
    run_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """停止生成：取消服务端任务，并等部分答案落库后才返回。"""
    run = _authorize(get_run_registry().get(run_id), run_id, current_user)

    if run.is_terminal:
        return {
            "run_id": run_id,
            "stopped": False,
            "status": run.status,
            "reason": run.stop_reason or "already_finished",
        }

    outcome = run.request_cancel("user_stop")
    # 等落库：不等的话用户点停止后立刻刷新，看到的仍是「只有一个问题」的会话
    await run.settle(timeout=8.0)
    logger.info(
        "停止生成: run=%s outcome=%s status=%s published=%d",
        run_id,
        outcome,
        run.status,
        run.published,
    )
    return {
        "run_id": run_id,
        "stopped": True,
        "status": run.status,
        "reason": run.stop_reason or "user_stop",
        "published": run.published,
    }
