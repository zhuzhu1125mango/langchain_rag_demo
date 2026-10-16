"""Trace 归属写入测试（P1-1 数据隔离复核）。

多用户模式下 request_traces 存有完整问答内容，必须写入 user_id/session_id
归属字段，供后续 Trace 查询 API 按用户过滤，防止跨用户泄露。
"""

from src.services.trace_collector import TraceCollector


class _FakeSession:
    """AsyncSessionLocal 返回值的替身：捕获 add 进来的 RequestTrace 实例。"""

    def __init__(self):
        self.record = None

    def add(self, record):
        self.record = record

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestTraceOwnership:
    """TraceCollector 归属字段测试。"""

    def test_set_basic_writes_ownership(self):
        """set_basic 应将 user_id/session_id 写入采集数据。"""
        trace = TraceCollector()
        trace.set_basic(question="什么是RAG", session_id="sess-1", user_id="user-1")

        assert trace.data["user_id"] == "user-1"
        assert trace.data["session_id"] == "sess-1"

    def test_set_basic_defaults_to_none(self):
        """未传归属时字段为 None（开发模式匿名场景）。"""
        trace = TraceCollector()
        trace.set_basic(question="什么是RAG")

        assert trace.data["user_id"] is None
        assert trace.data["session_id"] is None

    async def test_save_async_persists_ownership(self, monkeypatch):
        """save_async 应将归属字段写入 RequestTrace 记录。"""
        fake = _FakeSession()
        monkeypatch.setattr("src.database.AsyncSessionLocal", lambda: fake)

        trace = TraceCollector()
        trace.set_basic(question="什么是RAG", session_id="sess-1", user_id="user-1")
        await trace.save_async()

        assert fake.record is not None
        assert fake.record.user_id == "user-1"
        assert fake.record.session_id == "sess-1"
        assert fake.record.question == "什么是RAG"


class TestTraceStagesAndTokens:
    """P1-2：分阶段耗时与 token 用量采集测试。"""

    def test_add_stage_appends_entries(self):
        trace = TraceCollector()
        trace.add_stage("intent_route", "done", 120)
        trace.add_stage("generate", "done", 800, detail={"tokens": 42})

        stages = trace.data["stages"]
        assert len(stages) == 2
        assert stages[0] == {"name": "intent_route", "status": "done", "latency_ms": 120}
        assert stages[1]["detail"] == {"tokens": 42}

    def test_set_token_usage_exact(self):
        trace = TraceCollector()
        trace.set_token_usage(100, 50, estimated=False)
        assert trace.data["token_usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "estimated": False,
        }

    def test_set_token_usage_estimated(self):
        trace = TraceCollector()
        trace.set_token_usage(30, 12, estimated=True)
        assert trace.data["token_usage"]["estimated"] is True


class TestTraceUnknownColumnFilter:
    """未知列键过滤：防止 RequestTrace(**data) 抛 TypeError 致整条 trace 落库失败。"""

    def test_to_record_payload_drops_unknown_keys(self):
        from src.models.request_trace import RequestTrace

        trace = TraceCollector()
        trace.set_basic(question="q", session_id="s", user_id="u")
        # 这两个键无对应列（此前会使整条 trace 落库抛 TypeError 并被 best-effort 吞掉）
        trace.data["semantic_cache_hit"] = {"score": 0.9}
        trace.data["verification"] = {"confidence": 0.5}

        payload = trace._to_record_payload()

        assert "semantic_cache_hit" not in payload
        assert "verification" not in payload
        assert payload["question"] == "q"
        # 过滤后可安全构造（修复前此处会抛 TypeError）
        RequestTrace(**payload)

    def test_to_record_payload_keeps_only_known_keys(self):
        from src.models.request_trace import RequestTrace

        known = {c.key for c in RequestTrace.__table__.columns}
        trace = TraceCollector()
        trace.set_basic(question="q", resolved_question="r")
        trace.add_stage("x", "done", 1)

        payload = trace._to_record_payload()

        assert set(payload) <= known
        assert payload["resolved_question"] == "r"
        assert "stages" in payload
