"""AGT-1 流式运行注册表测试：事件缓冲、续传游标、停止与回收。

重点不是「函数能跑」，而是几条一旦错了就会造成用户可见故障的契约：

- 断连不取消生成（这里验证订阅者退出不影响运行）
- 续传不丢帧/不重复（游标语义）
- 越权读不到别人的回答
- 停止后部分答案必须落库（否则刷新只剩问题）
"""

import asyncio

import pytest
from fastapi import HTTPException

from src.api.chat import runs as runs_api
from src.api.chat.runs import iter_run_frames
from src.config import settings
from src.services import chat_run as chat_run_module
from src.services.chat_run import (
    ChatRun,
    ChatRunRegistry,
    RunLimitExceeded,
    defer_task,
    format_sse,
    get_current_run,
    reset_current_run,
    set_current_run,
)


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    from src.services.chat_run import get_run_registry

    registry = get_run_registry()
    registry.clear()
    # 心跳/保留期默认值在单测里不需要真实等待
    monkeypatch.setattr(settings.chat_run, "CHAT_RUN_HEARTBEAT_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings.chat_run, "CHAT_RUN_RETENTION_SECONDS", 300, raising=False)
    monkeypatch.setattr(settings.chat_run, "CHAT_RUN_IDLE_TIMEOUT_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings.chat_run, "CHAT_RUN_MAX_PER_USER", 5, raising=False)
    monkeypatch.setattr(settings.chat_run, "CHAT_RUN_MAX_FRAMES", 4000, raising=False)
    yield
    registry.clear()


class _User:
    def __init__(self, user_id: str):
        self.user_id = user_id


def _consume(frames):
    """把 SSE 帧还原成 (seq, payload) 便于断言。"""
    import json

    out = []
    for line in frames.split("\n"):
        if line.startswith("data: "):
            out.append(json.loads(line[6:]))
    return out


# ---------------------------------------------------------------------------
# 事件缓冲与游标
# ---------------------------------------------------------------------------
class TestBufferAndCursor:
    async def test_publish_assigns_increasing_seq_and_embedded_id(self):
        run = ChatRun("r1", "s1", "u1")
        assert run.publish({"type": "content", "content": "a"}) == 1
        assert run.publish({"type": "content", "content": "b"}) == 2
        frames = []
        run.finish("completed")
        async for seq, frame in run.subscribe(0):
            frames.append((seq, frame))
        assert [s for s, _ in frames] == [1, 2]
        # id: 行必须存在，否则浏览器重连拿不到 Last-Event-ID
        assert frames[0][1].startswith("id: 1\n")

    async def test_buffer_overflow_drops_oldest_and_marks_gap(self):
        run = ChatRun("r1", "s1", "u1", max_frames=3)
        for i in range(5):
            run.publish({"type": "content", "content": str(i)})
        run.finish("completed")

        assert run.dropped == 2
        assert run.oldest_seq == 3
        # 游标早于最旧帧 → 必须判为不可续传（否则客户端会静默丢内容）
        assert run.can_resume_from(1) is False
        # 恰好停在最旧帧之前（after=2）仍可续传：下一条就是缓冲里的 3
        assert run.can_resume_from(2) is True
        assert run.can_resume_from(3) is True
        # 从头重放永远允许
        assert run.can_resume_from(0) is True

        got = [p["content"] async for _, frame in run.subscribe(0) for p in _consume(frame)]
        assert got == ["2", "3", "4"]

    async def test_subscribe_replays_then_follows_live(self):
        run = ChatRun("r1", "s1", "u1")
        run.publish({"type": "content", "content": "1"})
        run.publish({"type": "content", "content": "2"})

        received = []

        async def consumer():
            async for _seq, frame in run.subscribe(0):
                received.extend(p["content"] for p in _consume(frame))

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.01)
        assert received == ["1", "2"]

        run.publish({"type": "content", "content": "3"})
        await asyncio.sleep(0.01)
        run.finish("completed")
        await asyncio.wait_for(task, timeout=2)
        assert received == ["1", "2", "3"]

    async def test_subscriber_exit_does_not_finish_run(self):
        """客户端断连只是订阅者离开，运行必须继续——这正是「断线不再丢答案」。"""
        run = ChatRun("r1", "s1", "u1")
        run.publish({"type": "content", "content": "1"})

        agen = run.subscribe(0)
        await agen.__anext__()
        await agen.aclose()
        await asyncio.sleep(0)

        assert run.status == "running"
        assert run.subscriber_count == 0
        run.publish({"type": "content", "content": "2"})
        assert run.published == 2

    async def test_two_subscribers_have_independent_cursors(self):
        run = ChatRun("r1", "s1", "u1")
        run.publish({"type": "content", "content": "1"})
        run.publish({"type": "content", "content": "2"})
        run.finish("completed")

        first = [p["content"] async for _, f in run.subscribe(0) for p in _consume(f)]
        second = [p["content"] async for _, f in run.subscribe(1) for p in _consume(f)]
        assert first == ["1", "2"]
        assert second == ["2"]

    async def test_terminal_run_rejects_new_events(self):
        run = ChatRun("r1", "s1", "u1")
        run.publish({"type": "content", "content": "1"})
        run.finish("stopped", "user_stop")
        assert run.publish({"type": "content", "content": "late"}) is None
        assert run.published == 1
        # 幂等：后到的终态不能覆盖先到的
        run.finish("completed")
        assert run.status == "stopped"


# ---------------------------------------------------------------------------
# 停止与落库
# ---------------------------------------------------------------------------
class TestStopAndSettle:
    async def test_stop_cancels_task_and_marks_status(self):
        run = ChatRun("r1", "s1", "u1")
        started = asyncio.Event()

        async def worker():
            started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                run.finish("stopped", run.stop_reason or "cancelled")
                raise

        task = asyncio.create_task(worker())
        run.attach_task(task)
        await asyncio.wait_for(started.wait(), timeout=2)

        assert run.request_cancel("user_stop") == "cancelled"
        await run.settle(timeout=2)
        assert run.status == "stopped"
        assert run.stop_reason == "user_stop"

    async def test_stop_on_finished_run_is_noop(self):
        run = ChatRun("r1", "s1", "u1")
        run.finish("completed")
        assert run.request_cancel("user_stop") == "already_finished"

    async def test_settle_waits_for_deferred_persist_task(self):
        """停止后必须等部分答案落库：否则用户刷新只看到问题，看不到「已停止」。"""
        run = ChatRun("r1", "s1", "u1")
        persisted = []

        async def slow_save():
            await asyncio.sleep(0.05)
            persisted.append(True)

        async def worker():
            token = set_current_run(run)
            try:
                task = asyncio.create_task(slow_save())
                defer_task(task)
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                run.finish("stopped", "user_stop")
                raise
            finally:
                reset_current_run(token)

        task = asyncio.create_task(worker())
        run.attach_task(task)
        await asyncio.sleep(0.02)
        run.request_cancel("user_stop")
        await run.settle(timeout=3)
        assert persisted == [True]

    async def test_defer_without_run_context_is_silent(self):
        """单测直调（无运行上下文）时不能抛异常。"""

        async def noop():
            return None

        task = asyncio.create_task(noop())
        defer_task(task)
        await task


# ---------------------------------------------------------------------------
# 注册表：限流 / 查找 / 回收
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_per_user_limit(self, monkeypatch):
        monkeypatch.setattr(settings.chat_run, "CHAT_RUN_MAX_PER_USER", 2, raising=False)
        registry = ChatRunRegistry()
        registry.create("s1", "u1")
        registry.create("s2", "u1")
        with pytest.raises(RunLimitExceeded):
            registry.create("s3", "u1")
        # 其他用户不受影响
        assert registry.create("s4", "u2").user_id == "u2"

    def test_finished_runs_do_not_count_toward_limit(self, monkeypatch):
        monkeypatch.setattr(settings.chat_run, "CHAT_RUN_MAX_PER_USER", 1, raising=False)
        registry = ChatRunRegistry()
        first = registry.create("s1", "u1")
        first.finish("completed")
        # 终态不占用并发额度，否则用户答完一次就被锁住
        assert registry.create("s2", "u1").run_id != first.run_id

    def test_find_active_by_session_scoped_to_owner(self):
        registry = ChatRunRegistry()
        mine = registry.create("s1", "u1")
        registry.create("s1", "u2")  # 同会话 ID、不同用户（理论上不该发生，防御性）
        found = registry.find_active_by_session("s1", "u1")
        assert found is not None and found.run_id == mine.run_id
        # 换个人来问同一会话 ID：拿不到别人的运行
        assert registry.find_active_by_session("s1", "u3") is None

    def test_find_active_ignores_terminal_runs(self):
        registry = ChatRunRegistry()
        run = registry.create("s1", "u1")
        run.finish("completed")
        assert registry.find_active_by_session("s1", "u1") is None

    def test_sweep_removes_expired_terminal_runs(self, monkeypatch):
        monkeypatch.setattr(settings.chat_run, "CHAT_RUN_RETENTION_SECONDS", 10, raising=False)
        registry = ChatRunRegistry()
        run = registry.create("s1", "u1")
        run.finish("completed")
        result = registry.sweep(now=run.finished_at + 11)
        assert result["expired"] == 1
        assert registry.get(run.run_id) is None

    async def test_sweep_cancels_stalled_running_run(self, monkeypatch):
        """长时间无事件的运行必须被回收并留痕，否则缓冲与 LLM 一直挂着。"""
        monkeypatch.setattr(settings.chat_run, "CHAT_RUN_IDLE_TIMEOUT_SECONDS", 5, raising=False)
        registry = ChatRunRegistry()
        run = registry.create("s1", "u1")
        sleeper = asyncio.ensure_future(asyncio.sleep(60))
        run.attach_task(sleeper)
        result = registry.sweep(now=run.last_event_at + 6)
        assert result["stalled"] == 1
        assert run.stop_reason == "idle_timeout"
        await asyncio.gather(sleeper, return_exceptions=True)

    async def test_drain_cancels_running_runs(self):
        registry = ChatRunRegistry()
        run = registry.create("s1", "u1")

        async def worker():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                run.finish("stopped", run.stop_reason or "shutdown")
                raise

        run.attach_task(asyncio.create_task(worker()))
        await asyncio.sleep(0.01)
        result = await registry.drain(timeout=2)
        assert result["running"] == 1
        assert run.status == "stopped"


# ---------------------------------------------------------------------------
# 端点：权限与续传契约
# ---------------------------------------------------------------------------
class TestEndpoints:
    def test_missing_run_returns_404(self):
        with pytest.raises(HTTPException) as exc:
            await_ = runs_api._authorize(None, "nope", _User("u1"))
            del await_
        assert exc.value.status_code == 404

    def test_cross_user_access_denied(self):
        run = ChatRun("r1", "s1", "owner")
        with pytest.raises(HTTPException) as exc:
            runs_api._authorize(run, "r1", _User("intruder"))
        assert exc.value.status_code == 403

    async def test_resume_rejects_gap(self):
        run = ChatRun("r1", "s1", "u1", max_frames=2)
        chat_run_module.get_run_registry().register(run)
        for i in range(4):
            run.publish({"type": "content", "content": str(i)})
        assert run.dropped == 2

        request = _FakeRequest(headers={})
        with pytest.raises(HTTPException) as exc:
            await runs_api.resume_run_stream(run.run_id, request, after=1, current_user=_User("u1"))
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "resume_gap"

    async def test_resume_uses_last_event_id_header(self):
        run = ChatRun("r1", "s1", "u1")
        run.publish({"type": "content", "content": "1"})
        run.publish({"type": "content", "content": "2"})
        run.finish("completed")
        chat_run_module.get_run_registry().register(run)

        request = _FakeRequest(headers={"last-event-id": "1"})
        response = await runs_api.resume_run_stream(
            run.run_id, request, after=0, current_user=_User("u1")
        )
        frames = [chunk async for chunk in response.body_iterator]
        payloads = [p for f in frames for p in _consume(f)]
        assert [p["content"] for p in payloads] == ["2"]

    async def test_stop_endpoint_returns_status(self):
        run = ChatRun("r1", "s1", "u1")
        chat_run_module.get_run_registry().register(run)

        async def worker():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                run.finish("stopped", run.stop_reason or "user_stop")
                raise

        run.attach_task(asyncio.create_task(worker()))
        await asyncio.sleep(0.01)

        result = await runs_api.stop_run(run.run_id, current_user=_User("u1"))
        assert result["stopped"] is True
        assert result["status"] == "stopped"

        # 再点一次：幂等，不报错
        again = await runs_api.stop_run(run.run_id, current_user=_User("u1"))
        assert again["stopped"] is False

    async def test_active_run_lookup(self):
        registry = chat_run_module.get_run_registry()
        run = registry.create("s1", "u1")
        got = await runs_api.get_active_run("s1", current_user=_User("u1"))
        assert got["run_id"] == run.run_id
        assert got["after"] == 0
        assert (await runs_api.get_active_run("s1", current_user=_User("u2")))["run_id"] is None


# ---------------------------------------------------------------------------
# SSE 帧与心跳
# ---------------------------------------------------------------------------
class TestFrames:
    def test_format_sse_shape(self):
        frame = format_sse({"type": "run", "run_id": "abc"}, seq=7)
        assert frame == 'id: 7\ndata: {"type": "run", "run_id": "abc"}\n\n'

    async def test_heartbeat_emitted_while_idle(self, monkeypatch):
        monkeypatch.setattr(settings.chat_run, "CHAT_RUN_HEARTBEAT_SECONDS", 1, raising=False)
        run = ChatRun("r1", "s1", "u1")
        agen = iter_run_frames(run, 0)
        # 空闲 1s 后应产出注释帧，否则长检索会被代理按空闲超时掐断
        first = await asyncio.wait_for(agen.__anext__(), timeout=3)
        assert first.startswith(": ")
        run.finish("completed")
        await agen.aclose()

    async def test_frames_end_after_terminal(self):
        run = ChatRun("r1", "s1", "u1")
        run.publish({"type": "content", "content": "hi"})
        run.finish("completed")
        frames = [frame async for frame in iter_run_frames(run, 0)]
        payloads = [p for f in frames for p in _consume(f)]
        assert payloads == [{"type": "content", "content": "hi"}]


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


# ---------------------------------------------------------------------------
# 入口回归：提前返回的错误也必须是完整 SSE 帧
# ---------------------------------------------------------------------------
class TestStreamEntryErrors:
    async def test_blocked_input_emits_valid_sse_frame(self, monkeypatch):
        """裸 JSON 缺 "data:" 前缀时前端整帧丢弃，用户只看到转圈直到断连。"""
        from types import SimpleNamespace

        from src.api.chat import stream as stream_api
        from src.api.chat.common import MessageRequest

        monkeypatch.setattr(
            stream_api.sensitive_filter,
            "detect",
            lambda q: (True, "敏感词", "测试"),
        )

        request = SimpleNamespace(state=SimpleNamespace(request_id="req-1"))
        response = await stream_api.stream_answer(
            request,
            MessageRequest(question="测试问题"),
            current_user=_User("u1"),
        )
        frames = [chunk async for chunk in response.body_iterator]

        assert len(frames) == 1
        assert frames[0].startswith("data: ")
        assert frames[0].endswith("\n\n")
        assert "输入包含敏感内容" in frames[0]


class TestRawPublishCursor:
    async def test_raw_frames_carry_seq_id(self):
        """管线产出的帧原本没有 id:，续传游标会卡在首帧 → 重连重复渲染。"""
        run = ChatRun("r1", "s1", "u1")
        assert run.publish_raw('data: {"type":"content","content":"a"}\n\n') == 1
        assert run.publish_raw('data: {"type":"content","content":"b"}\n\n') == 2
        run.finish("completed")

        frames = [f async for _s, f in run.subscribe(0)]
        assert frames[0].startswith("id: 1\ndata: ")
        assert frames[1].startswith("id: 2\ndata: ")
        # 从 1 之后续传只能拿到第 2 帧
        rest = [p async for _s, f in run.subscribe(1) for p in _consume(f)]
        assert rest == [{"type": "content", "content": "b"}]

    async def test_capacity_precheck_creates_nothing(self, monkeypatch):
        monkeypatch.setattr(settings.chat_run, "CHAT_RUN_MAX_PER_USER", 1, raising=False)
        registry = ChatRunRegistry()
        registry.create("s1", "u1")
        with pytest.raises(RunLimitExceeded):
            registry.assert_capacity("u1")
        assert registry.stats()["running"] == 1
