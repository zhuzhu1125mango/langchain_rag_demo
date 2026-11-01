"""流式问答运行注册表（AGT-1：会话可恢复 + 可取消）。

此前一次问答的生命周期绑死在 HTTP 连接上：`generate()` 就是 ASGI 响应生成器，
客户端一断连，`CancelledError` 从 yield 点抛进来，生成随即终止、只留下半截答案。
带来的实际后果：

- 切网/切标签/刷新页面 = 回答作废，只能重新生成（消耗一次完整 LLM 调用）；
- 用户点「停止」只是前端 abort，服务端在代理层感知断连之前仍在烧 token；
- 刷新后助手消息尚未落库，界面上只剩一个孤立的问题。

本模块把「运行」从「连接」里拆出来：生成跑在独立 task 里，事件写进带序号的
缓冲，任意客户端都可以凭 ``run_id`` + 序号重连续传，也可以显式停止。

边界（明确不做的事）：

- **缓冲在进程内**。进程重启即丢，跨重启恢复要等 AM-4（持久执行）；
  当前生产是单 uvicorn worker，进程内注册表足以覆盖断线场景。
- **不做 checkpoint**。续传是「重放已产生的事件 + 继续接收」，不是从半截
  Agent 步骤里恢复执行。
"""

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from contextvars import ContextVar
from typing import Any, AsyncIterator, Deque, Dict, List, Mapping, Optional, Set, Tuple

from src.config import settings
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("src.services.chat_run")

# 终态集合：进入后不再接受事件，只等待订阅者读空缓冲
TERMINAL_STATUSES = frozenset({"completed", "stopped", "failed"})
RUNNING = "running"


def format_sse(payload: Mapping[str, Any], seq: Optional[int] = None) -> str:
    """构造一帧 SSE。

    ``id:`` 是续传游标——浏览器 EventSource 的 Last-Event-ID 与前端手写的
    ``after`` 参数都依赖它；缺了它重连就只能从头重放。
    """
    body = json.dumps(payload, ensure_ascii=False)
    if seq is None:
        return f"data: {body}\n\n"
    return f"id: {seq}\ndata: {body}\n\n"


def format_sse_comment(text: str = "ping") -> str:
    """SSE 注释帧：只为保活，客户端按协议忽略。

    长检索/长思考期间可能几十秒没有事件，代理与浏览器会按空闲超时掐连接。
    """
    return f": {text}\n\n"


class RunLimitExceeded(RuntimeError):
    """同一用户的在途运行数超限。"""

    def __init__(self, user_id: str, limit: int):
        super().__init__(f"用户 {user_id} 的在途生成已达上限 {limit}")
        self.user_id = user_id
        self.limit = limit


class ChatRun:
    """一次问答生成的可重连句柄。

    事件缓冲按发布顺序编号（从 1 开始），订阅者各自持有游标，互不影响；
    超出上限时丢弃最旧帧（``dropped`` 计数），游标早于最旧帧的订阅者必须
    从头重放，由 :meth:`can_resume_from` 判定。
    """

    def __init__(
        self,
        run_id: str,
        session_id: str,
        user_id: str,
        max_frames: Optional[int] = None,
    ):
        self.run_id = run_id
        self.session_id = session_id
        self.user_id = user_id
        self.created_at = time.time()
        self.last_event_at = self.created_at
        self.finished_at: Optional[float] = None
        self.status = RUNNING
        self.stop_reason = ""
        # 已发布事件数（= 最新序号）
        self.published = 0
        # 因缓冲上限被丢弃的帧数；序号 s 的帧在下标 s - dropped - 1
        self.dropped = 0
        self.subscriber_count = 0

        self._frames: Deque[Tuple[int, str]] = deque()
        self._max_frames = max_frames or settings.chat_run.CHAT_RUN_MAX_FRAMES
        self._version = 0
        self._waiters: List[asyncio.Event] = []
        self._task: Optional[asyncio.Task] = None
        self._deferred: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def oldest_seq(self) -> int:
        """缓冲中仍可取到的最小序号；空缓冲时等于已发布数 + 1。"""
        return self.dropped + 1

    def can_resume_from(self, after: int) -> bool:
        """游标是否仍可续传（不丢帧）。after=0 表示从头重放，永远可行。"""
        if after <= 0:
            return True
        return after >= self.dropped

    def can_accept(self) -> bool:
        return not self.is_terminal

    # ------------------------------------------------------------------
    # 事件发布
    # ------------------------------------------------------------------
    def publish(self, payload: Mapping[str, Any]) -> Optional[int]:
        """发布一帧事件，返回序号；已终态则丢弃并返回 None。"""
        if self.is_terminal:
            return None
        self.published += 1
        seq = self.published
        self._frames.append((seq, format_sse(payload, seq)))
        self.last_event_at = time.time()
        while len(self._frames) > self._max_frames:
            self._frames.popleft()
            self.dropped += 1
        self._wake()
        return seq

    def publish_raw(self, frame: str) -> Optional[int]:
        """发布一段由管线产出的 SSE 文本（``data: ...\\n\\n``）。

        必须补 ``id:`` 行：客户端靠它推进续传游标，缺了就只有首帧有 id，
        重连时会从头重放，已显示的答案会重复渲染一遍。
        """
        if self.is_terminal:
            return None
        self.published += 1
        seq = self.published
        body = frame.strip("\n")
        self._frames.append((seq, f"id: {seq}\n{body}\n\n"))
        self.last_event_at = time.time()
        while len(self._frames) > self._max_frames:
            self._frames.popleft()
            self.dropped += 1
        self._wake()
        return seq

    def finish(self, status: str, reason: str = "") -> None:
        """置终态（幂等：先到的终态为准）。"""
        if self.is_terminal:
            return
        self.status = status
        self.stop_reason = reason
        self.finished_at = time.time()
        self._wake()

    # ------------------------------------------------------------------
    # 订阅（重放 + 实时跟随）
    # ------------------------------------------------------------------
    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[Tuple[int, str]]:
        """从 ``after_seq`` 之后开始产出 (seq, frame)，终态且读空后结束。

        多个订阅者可同时跟随同一运行；先到的事件照常重放，游标各自推进。
        """
        cursor = max(after_seq, self.dropped)
        self.subscriber_count += 1
        try:
            while True:
                version = self._version
                start = max(0, cursor - self.dropped)
                while start < len(self._frames):
                    seq, frame = self._frames[start]
                    start += 1
                    cursor = seq
                    yield seq, frame
                if self.is_terminal:
                    return
                await self._wait_change(version)
        finally:
            self.subscriber_count = max(0, self.subscriber_count - 1)

    async def _wait_change(self, seen_version: int) -> None:
        """等待新事件。入队后复查一次版本，避免 publish 落在检查与入队之间。"""
        if self._version != seen_version:
            return
        event = asyncio.Event()
        self._waiters.append(event)
        if self._version != seen_version:
            self._safe_discard_waiter(event)
            return
        try:
            await event.wait()
        finally:
            self._safe_discard_waiter(event)

    def _safe_discard_waiter(self, event: asyncio.Event) -> None:
        try:
            self._waiters.remove(event)
        except ValueError:
            # _wake 已整体取走列表，成员不在属正常路径
            pass

    def _wake(self) -> None:
        self._version += 1
        waiters, self._waiters = self._waiters, []
        for event in waiters:
            event.set()

    # ------------------------------------------------------------------
    # 任务句柄与取消
    # ------------------------------------------------------------------
    def attach_task(self, task: asyncio.Task) -> None:
        self._task = task

    def defer(self, task: asyncio.Task) -> None:
        """登记「本运行派生、需要等它落定」的任务（如取消时的部分答案落库）。"""
        self._deferred.add(task)
        task.add_done_callback(self._deferred.discard)

    def request_cancel(self, reason: str = "user_stop") -> str:
        """请求停止生成。返回 "cancelled" / "already_finished" / "no_task"。"""
        if self.is_terminal:
            return "already_finished"
        task = self._task
        if task is None or task.done():
            # 没有可取消的 task：可能刚建好还没启动，直接置终态避免悬挂
            self.finish("stopped", reason)
            return "no_task"
        self.stop_reason = reason
        task.cancel()
        return "cancelled"

    async def settle(self, timeout: float = 10.0) -> None:
        """等待生成 task 与派生任务（部分答案落库）落定。

        停止接口必须等这一步：否则用户点「停止」后立刻刷新，看到的仍是
        只有问题的会话——正是本次要修的观感。
        """
        deadline = time.monotonic() + timeout
        task = self._task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=timeout)
        pending = [t for t in list(self._deferred) if not t.done()]
        if pending:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining > 0:
                await asyncio.wait(pending, timeout=remaining)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "published": self.published,
            "oldest_seq": self.oldest_seq,
            "created_at": self.created_at,
            "last_event_at": self.last_event_at,
            "subscribers": self.subscriber_count,
        }


class ChatRunRegistry:
    """进程内运行表：创建/查找/回收。"""

    def __init__(self) -> None:
        self._runs: Dict[str, ChatRun] = {}

    # -- 生命周期 ------------------------------------------------------
    def _running_of(self, user_id: str) -> List[ChatRun]:
        return [r for r in self._runs.values() if r.user_id == user_id and not r.is_terminal]

    def assert_capacity(self, user_id: str) -> None:
        """落库前的预检：超限直接拒绝。

        若不预检，用户消息先落库、再 429，历史里就留下一条「有问无答」，
        用户下次打开会话会以为回答丢了。
        """
        limit = settings.chat_run.CHAT_RUN_MAX_PER_USER
        if len(self._running_of(user_id)) >= limit:
            raise RunLimitExceeded(user_id, limit)

    def create(
        self,
        session_id: str,
        user_id: str,
        question_preview: str = "",
    ) -> ChatRun:
        self.sweep()
        self.assert_capacity(user_id)
        running = self._running_of(user_id)
        run = ChatRun(str(uuid.uuid4()), session_id, user_id)
        self._runs[run.run_id] = run
        logger.info(
            "chat run 创建: run=%s session=%s user=%s preview=%r running=%d",
            run.run_id,
            session_id,
            user_id,
            question_preview[:40],
            len(running) + 1,
        )
        return run

    def get(self, run_id: str) -> Optional[ChatRun]:
        return self._runs.get(run_id)

    def register(self, run: ChatRun) -> ChatRun:
        """登记一个已构造的运行（测试与迁移场景用，正常路径走 create）。"""
        self._runs[run.run_id] = run
        return run

    def find_active_by_session(
        self, session_id: str, user_id: str
    ) -> Optional[ChatRun]:
        """找出该会话在途的运行（刷新/换设备后重新挂上）。"""
        self.sweep()
        best: Optional[ChatRun] = None
        for run in self._runs.values():
            if run.is_terminal or run.session_id != session_id:
                continue
            if run.user_id != user_id:
                continue
            if best is None or run.created_at > best.created_at:
                best = run
        return best

    def discard(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    # -- 回收 ----------------------------------------------------------
    def sweep(self, now: Optional[float] = None) -> Dict[str, int]:
        """回收过期终态运行 + 取消空闲卡死的运行。"""
        now = now or time.time()
        retention = settings.chat_run.CHAT_RUN_RETENTION_SECONDS
        idle_timeout = settings.chat_run.CHAT_RUN_IDLE_TIMEOUT_SECONDS
        expired = 0
        stalled = 0
        for run_id, run in list(self._runs.items()):
            if run.is_terminal:
                finished = run.finished_at or run.last_event_at
                if now - finished > retention:
                    self._runs.pop(run_id, None)
                    expired += 1
                continue
            if now - run.last_event_at > idle_timeout:
                log_exception(
                    logger,
                    TimeoutError(f"运行 {run_id} 超过 {idle_timeout}s 无新事件"),
                    action="chat_run.idle_timeout",
                    severity=ErrorSeverity.REPORTABLE,
                    message="流式运行长时间无事件，已取消回收（疑似生成卡死）",
                    run_id=run_id,
                    session_id=run.session_id,
                    published=run.published,
                )
                run.request_cancel("idle_timeout")
                stalled += 1
        return {"expired": expired, "stalled": stalled, "runs": len(self._runs)}

    async def drain(self, timeout: float = 10.0) -> Dict[str, int]:
        """关机前排空：取消在途运行并等部分答案落库。"""
        running = [r for r in self._runs.values() if not r.is_terminal]
        for run in running:
            run.request_cancel("shutdown")
        deadline = time.monotonic() + timeout
        for run in running:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            await run.settle(timeout=remaining)
        for run in running:
            if not run.is_terminal:
                run.finish("stopped", "shutdown")
        return {"running": len(running), "retained": len(self._runs)}

    def stats(self) -> Dict[str, int]:
        running = sum(1 for r in self._runs.values() if not r.is_terminal)
        return {"runs": len(self._runs), "running": running}

    def clear(self) -> None:
        """仅供测试：清空注册表。"""
        self._runs.clear()


_registry = ChatRunRegistry()


def get_run_registry() -> ChatRunRegistry:
    return _registry


# ----------------------------------------------------------------------
# 当前运行上下文：让深层代码（取消时的部分答案落库）把派生任务登记到 run 上
# ----------------------------------------------------------------------
_current_run: ContextVar[Optional[ChatRun]] = ContextVar("chat_current_run", default=None)


def set_current_run(run: ChatRun):
    return _current_run.set(run)


def reset_current_run(token) -> None:
    _current_run.reset(token)


def get_current_run() -> Optional[ChatRun]:
    return _current_run.get()


def defer_task(task: asyncio.Task) -> None:
    """把任务登记到当前运行（无运行上下文时静默忽略）。

    取消路径上落库任务是 ``create_task`` 出去的，运行结束就直接返回；
    停止接口要等它落定，否则用户点停止后立刻刷新会看不到「已停止」那条消息。
    """
    run = _current_run.get()
    if run is not None:
        run.defer(task)
