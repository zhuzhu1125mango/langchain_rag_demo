"""CODE-2：异常分级与统一记录入口的单元测试。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.exceptions import ValidationException
from src.utils.error_handling import (
    ErrorSeverity,
    classify_exception,
    format_context,
    is_recoverable,
    log_exception,
)


class _FakeHttpxError(Exception):
    """模拟 httpx 抛出的传输层异常（按模块前缀识别）。"""


_FakeHttpxError.__module__ = "httpx"
_FakeHttpxError.__name__ = "ConnectError"


class _FakeMilvusError(Exception):
    """模拟 MilvusException：类名不含特征，靠消息里的 rate limit 识别。"""


class TestClassify:
    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("bad type"),
            AttributeError("no attr"),
            KeyError("missing"),
            IndexError("oob"),
            AssertionError("boom"),
            NameError("nope"),
            ZeroDivisionError("div"),
            NotImplementedError("todo"),
        ],
    )
    def test_programming_errors(self, exc):
        assert classify_exception(exc) is ErrorSeverity.PROGRAMMING

    def test_invalid_argument_is_programming(self):
        class InvalidArgumentError(Exception):
            pass

        assert classify_exception(InvalidArgumentError("bad arg")) is ErrorSeverity.PROGRAMMING

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("slow"),
            asyncio.TimeoutError("slow"),
            ConnectionResetError("reset"),
            ConnectionRefusedError("refused"),
            BrokenPipeError("pipe"),
        ],
    )
    def test_recoverable_builtin_errors(self, exc):
        assert classify_exception(exc) is ErrorSeverity.RECOVERABLE

    def test_recoverable_by_module_prefix(self):
        assert classify_exception(_FakeHttpxError("connect failed")) is ErrorSeverity.RECOVERABLE

    def test_recoverable_by_message_signal(self):
        assert classify_exception(_FakeMilvusError("request rejected: rate limit exceeded")) is (
            ErrorSeverity.RECOVERABLE
        )

    def test_business_validation_is_reportable(self):
        """业务校验失败不是「可恢复」，也不该被当成编程错误自动重试。"""
        assert classify_exception(ValidationException("bad input")) is ErrorSeverity.REPORTABLE

    def test_http_exception_is_reportable(self):
        assert classify_exception(HTTPException(status_code=404)) is ErrorSeverity.REPORTABLE

    def test_unknown_error_defaults_to_reportable(self):
        assert classify_exception(RuntimeError("unexpected")) is ErrorSeverity.REPORTABLE

    def test_message_mentioning_connection_does_not_override_programming(self):
        """编程错误即便文案里带 connection 也应保持 PROGRAMMING（类型优先）。"""
        assert classify_exception(KeyError("connection refused")) is ErrorSeverity.PROGRAMMING

    def test_is_recoverable_helper(self):
        assert is_recoverable(TimeoutError()) is True
        assert is_recoverable(RuntimeError()) is False


class TestLogException:
    def test_recoverable_logs_warning_without_stack(self, caplog):
        logger = logging.getLogger("test.error_handling.recoverable")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(
                logger, TimeoutError("upstream slow"), action="kb.search", kb_id="kb-1"
            )
        assert severity is ErrorSeverity.RECOVERABLE
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None, "可恢复异常不应刷堆栈"
        assert "action=kb.search" in record.getMessage()
        assert "severity=recoverable" in record.getMessage()
        assert "error=TimeoutError" in record.getMessage()
        assert "kb_id=kb-1" in record.getMessage()

    def test_reportable_logs_error_with_stack(self, caplog):
        logger = logging.getLogger("test.error_handling.reportable")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(logger, RuntimeError("boom"), action="document.classify")
        assert severity is ErrorSeverity.REPORTABLE
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None

    def test_programming_logs_error_with_stack(self, caplog):
        logger = logging.getLogger("test.error_handling.programming")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(logger, TypeError("oops"), action="document.quality")
        assert severity is ErrorSeverity.PROGRAMMING
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert "severity=programming" in record.getMessage()

    def test_severity_override_wins(self, caplog):
        """调用方显式分级优先于自动判定（如确知某 SDK 错误可降级）。"""
        logger = logging.getLogger("test.error_handling.override")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(
                logger,
                RuntimeError("weird"),
                action="kb.upload",
                severity=ErrorSeverity.RECOVERABLE,
            )
        assert severity is ErrorSeverity.RECOVERABLE
        assert caplog.records[-1].levelno == logging.WARNING

    def test_context_rendering_skips_none(self):
        text = format_context(
            "document.delete", ErrorSeverity.REPORTABLE, RuntimeError("x"), doc_id="d-1", kb_id=None
        )
        assert "doc_id=d-1" in text
        assert "kb_id" not in text
        assert text.startswith("action=document.delete")


class TestFailMapping:
    """CODE-2 边界映射：业务异常→4xx 且不打 error 日志；未预期→500 且记录。"""

    def test_not_found_maps_404(self):
        from src.api.admin_models import _fail
        from src.services.model_config_service import ModelConfigNotFound

        resp = _fail(ModelConfigNotFound("provider 不存在"), "admin_models.test")
        assert resp.status_code == 404
        assert "provider 不存在" in str(resp.detail)

    def test_conflict_maps_409(self):
        from src.api.admin_models import _fail
        from src.services.model_config_service import ModelConfigConflict

        assert _fail(ModelConfigConflict("已存在"), "admin_models.test").status_code == 409

    def test_business_validation_maps_400(self):
        from src.api.admin_models import _fail
        from src.services.model_config_service import ModelConfigError

        assert _fail(ModelConfigError("参数非法"), "admin_models.test").status_code == 400

    def test_unexpected_maps_500_and_logs(self, caplog):
        import logging as _logging

        from src.api.admin_models import _fail

        logger = _logging.getLogger("rag_system")
        with caplog.at_level(_logging.DEBUG, logger="rag_system"):
            resp = _fail(RuntimeError("db down"), "admin_models.registry.apply")

        assert resp.status_code == 500
        assert "db down" not in str(resp.detail), "对外文案不应泄漏内部异常"
        record = caplog.records[-1]
        assert record.levelno == _logging.ERROR
        assert "action=admin_models.registry.apply" in record.getMessage()
        assert "severity=reportable" in record.getMessage()


class TestApiFailHelpers:
    """CODE-2 第三批：experiment / learning 接口兜底统一为"记录 + 500"，不泄漏内部细节。"""

    @pytest.mark.parametrize(
        "module,action",
        [
            ("src.api.experiment", "experiment.analyze"),
            ("src.api.learning", "learning.trigger"),
        ],
    )
    def test_unexpected_maps_500_and_records_action(self, module, action, caplog):
        import importlib
        import logging as _logging

        _fail = importlib.import_module(module)._fail
        logger = _logging.getLogger("rag_system")
        with caplog.at_level(_logging.DEBUG, logger="rag_system"):
            resp = _fail(ValueError("内部细节 db=rag"), action)

        assert resp.status_code == 500
        assert "内部细节" not in str(resp.detail)
        record = caplog.records[-1]
        assert record.levelno == _logging.ERROR
        assert f"action={action}" in record.getMessage()
        assert "severity=reportable" in record.getMessage()

    def test_learning_stats_degrades_without_silence(self, caplog):
        """误分类统计失败应保留主统计，但必须留下可恢复级日志（不能静默归零）。"""
        logger = logging.getLogger("rag_system")
        with caplog.at_level(logging.DEBUG, logger="rag_system"):
            log_exception(
                logger, RuntimeError("rule learner down"),
                action="learning.stats.misclassification",
                severity=ErrorSeverity.RECOVERABLE,
                message="误分类样本统计失败，计数降级为 0",
            )
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert "action=learning.stats.misclassification" in record.getMessage()


class TestObservedDegradation:
    """第七批：模型 fallback 与检索降级路径的日志语义回归。

    这几条路径此前要么只打一句 warning（丢失 action/severity），
    要么在流式已经下发内容后静默中断，导致「回答被截断」无从定位。
    """

    def test_route_fallback_records_recoverable(self, caplog):
        """主模型失败切换备用模型属正常降级：warning + recoverable，且带 model 定位。"""
        from src.services.model_adapters import ChatModelGroup

        class _Boom:
            async def ainvoke(self, *args, **kwargs):
                raise ConnectionError("provider down")

        class _Ok:
            async def ainvoke(self, *args, **kwargs):
                return "answer"

        group = ChatModelGroup([_Boom(), _Ok()], labels=["primary", "backup"], task="chat")
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            assert asyncio.run(group.ainvoke("q")) == "answer"

        hits = [r for r in caplog.records if "action=model.route.ainvoke" in r.getMessage()]
        assert hits, "fallback 必须留下带 action 的记录"
        message = hits[0].getMessage()
        assert hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in message
        assert "task=chat" in message and "model=primary" in message

    def test_stream_interruption_is_reportable(self, caplog):
        """流式已下发分片后中断：不能切模型，必须按需上报并继续上抛。"""
        from src.services.model_adapters import ChatModelGroup

        class _Flaky:
            def astream(self, *args, **kwargs):
                async def gen():
                    yield "chunk-1"
                    raise RuntimeError("mid-stream boom")

                return gen()

        group = ChatModelGroup([_Flaky()], labels=["primary"], task="chat")

        async def _drain():
            seen = []
            async for chunk in group.astream("q"):
                seen.append(chunk)
            return seen

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            with pytest.raises(RuntimeError):
                asyncio.run(_drain())

        hits = [r for r in caplog.records if "action=model.route.astream.interrupted" in r.getMessage()]
        assert hits, "流式中断必须留痕"
        assert hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()

    def test_hybrid_failure_falls_back_to_dense(self, caplog):
        """混合检索挂掉但 dense 可用：只记可恢复级，结果照常返回。"""
        from src.services.kb_retrieval_service import KBRetrievalService

        class _Store:
            async def search_hybrid(self, *args, **kwargs):
                raise ConnectionError("milvus down")

            async def search_dense(self, *args, **kwargs):
                return ["doc-1", "doc-2"]

        service = KBRetrievalService(vector_store=_Store())
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            docs = asyncio.run(service.retrieve("q", kb_ids=["kb-1"]))

        assert docs == ["doc-1", "doc-2"]
        messages = [r.getMessage() for r in caplog.records]
        assert any("action=kb.retrieval.hybrid" in m and "severity=recoverable" in m for m in messages)
        assert not any("action=kb.retrieval.dense_fallback" in m for m in messages)

    def test_dense_fallback_failure_is_logged_before_raise(self, caplog):
        """两级检索都失败：必须能在日志里看到是哪一层挂的，再上抛。"""
        from src.services.kb_retrieval_service import KBRetrievalService

        class _Store:
            async def search_hybrid(self, *args, **kwargs):
                raise ConnectionError("milvus down")

            async def search_dense(self, *args, **kwargs):
                raise RuntimeError("dense down")

        service = KBRetrievalService(vector_store=_Store())
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            with pytest.raises(RuntimeError):
                asyncio.run(service.retrieve("q", kb_ids=["kb-1"]))

        messages = [r.getMessage() for r in caplog.records]
        assert any("action=kb.retrieval.hybrid" in m for m in messages)
        fatal = [r for r in caplog.records if "action=kb.retrieval.dense_fallback" in r.getMessage()]
        assert fatal and fatal[0].levelno == logging.ERROR
        assert "severity=reportable" in fatal[0].getMessage()


class TestNotificationAndWikiGrading:
    """第八批：WS 推送失败与 Wiki 空白基线降级的回归。"""

    def test_ws_send_failure_drops_connection_with_log(self, caplog):
        """推送失败要摘除连接，并留下可定位的 connection_id（此前完全静默）。"""
        from src.api.notification import ConnectionManager

        class _DeadSocket:
            async def send_json(self, message):
                raise ConnectionResetError("client gone")

        manager = ConnectionManager()
        manager.active_connections["conn-1"] = {
            "websocket": _DeadSocket(),
            "channels": set(),
            "user_id": "u1",
        }

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            asyncio.run(manager.send_personal_message("conn-1", {"type": "ping"}))

        assert "conn-1" not in manager.active_connections, "失效连接必须被摘除"
        hits = [r for r in caplog.records if "action=notification.send_personal" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "connection_id=conn-1" in hits[0].getMessage()

    def test_wiki_blank_baseline_is_reportable(self, caplog):
        """读不到既有页会让整库按「新建」重编（revision 归零），必须按需上报。"""
        from src.services.wiki_compiler import WikiCompiler

        class _BrokenDB:
            async def execute(self, *args, **kwargs):
                raise ConnectionError("db down")

        compiler = WikiCompiler()
        with caplog.at_level(logging.WARNING, logger="wiki_compiler"):
            pages = asyncio.run(compiler._load_existing_pages(_BrokenDB(), "kb-1"))

        assert pages == []
        hits = [r for r in caplog.records if "action=wiki.compile.load_existing_pages" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()
        assert "kb_id=kb-1" in hits[0].getMessage()


class TestContextAndBadcaseGrading:
    """上下文增强与 badcase 闭环的分级：降级要留痕，数据丢失要升级。"""

    def test_compress_failure_degrades_with_warning(self, caplog):
        """压缩失败降级为截断：可恢复，记 warning 且不刷堆栈。"""
        from src.services.context_enhancer import ContextEnhancer

        class _BrokenLLM:
            async def ainvoke(self, prompt):
                raise TimeoutError("upstream timeout")

        enhancer = ContextEnhancer(_BrokenLLM(), max_summary_length=20)
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(enhancer._compress_context("x" * 100))

        assert result == "x" * 20, "降级必须真的截断到上限"
        hits = [r for r in caplog.records if "action=context.compress" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        text = hits[0].getMessage()
        assert "severity=recoverable" in text
        assert "context_len=100" in text

    def test_summary_failure_keeps_previous(self, caplog):
        """摘要更新失败沿用旧摘要：不丢历史，且能按 session 定位。"""
        from src.services.context_enhancer import ContextEnhancer

        class _BrokenLLM:
            async def ainvoke(self, prompt):
                raise ConnectionResetError("llm down")

        enhancer = ContextEnhancer(_BrokenLLM())
        enhancer._set_summary("s-42", "旧摘要")

        history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "在"}]
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            summary = asyncio.run(enhancer._update_conversation_summary(history, "s-42"))

        assert summary == "旧摘要"
        assert enhancer._get_summary("s-42") == "旧摘要", "失败不得覆盖已有摘要"
        hits = [r for r in caplog.records if "action=context.update_summary" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "session_id=s-42" in hits[0].getMessage()

    def test_badcase_persist_loss_is_reportable_and_rolls_back(self, caplog):
        """反馈未入库属数据损失：即便底层是连接抖动也要按需上报并回滚会话。"""
        from src.services.badcase.feedback_handler import (
            BadcaseCreateRequest,
            BadcaseFeedbackHandler,
        )

        class _StubClassifier:
            async def learn_from_feedback(self, **kwargs):
                return {"applied": True}

        class _BrokenSession:
            def __init__(self):
                self.rollback_called = False

            def add(self, record):
                pass

            async def commit(self):
                raise ConnectionError("db down")

            async def refresh(self, record):
                raise AssertionError("commit 失败后不应再 refresh")

            async def rollback(self):
                self.rollback_called = True

        handler = BadcaseFeedbackHandler()
        handler.embedding_classifier = _StubClassifier()
        session = _BrokenSession()
        request = BadcaseCreateRequest(question="报销流程怎么走", user_id="u1")

        with caplog.at_level(logging.WARNING, logger="src.services.badcase.feedback_handler"):
            result = asyncio.run(handler.handle(request, db_session=session))

        assert result["badcase_id"] is None
        assert result["category"] == "retrieval_failure", "分类结果仍要返回，不能因落库失败而丢"
        assert session.rollback_called, "commit 失败后必须回滚，否则同一会话后续查询会报错"
        hits = [r for r in caplog.records if "action=badcase.persist" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        text = hits[0].getMessage()
        assert "severity=reportable" in text, "连接类异常也要因数据损失升级为 reportable"
        assert "category=retrieval_failure" in text

    def test_learning_failure_does_not_block_feedback(self, caplog):
        """在线学习失败只损失样本收益，反馈本身仍要处理完成。"""
        from src.services.badcase.feedback_handler import (
            BadcaseCreateRequest,
            BadcaseFeedbackHandler,
        )

        class _BrokenClassifier:
            async def learn_from_feedback(self, **kwargs):
                raise ConnectionError("embedding service down")

        handler = BadcaseFeedbackHandler()
        handler.embedding_classifier = _BrokenClassifier()
        request = BadcaseCreateRequest(
            question="你们的服务时间是？",
            retrieved_sources=[{"doc": "a"}],
            reason="回答错误",
        )

        with caplog.at_level(logging.WARNING, logger="src.services.badcase.feedback_handler"):
            result = asyncio.run(handler.handle(request, db_session=None))

        assert result["category"] == "wrong_answer", "学习失败不影响分类"
        assert result["learning_result"] is None
        hits = [r for r in caplog.records if "action=badcase.learning.apply" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in hits[0].getMessage()


class TestLongTailGrading:
    """1~2 处/文件的长尾：只处理「失败后仍有可见后果」的点。"""

    def test_audit_write_loss_is_reportable(self, caplog, monkeypatch):
        """审计失败不阻断业务，但审计链缺口属合规问题，必须按需上报。"""
        from src.services import audit_service

        class _BrokenSessionFactory:
            def __call__(self):
                raise ConnectionError("db down")

        monkeypatch.setattr(audit_service, "AsyncSessionLocal", _BrokenSessionFactory())

        with caplog.at_level(logging.WARNING, logger="src.services.audit_service"):
            asyncio.run(
                audit_service.record_audit(
                    action="document.delete",
                    resource_type="document",
                    resource_id="doc-1",
                    actor_user_id="u1",
                )
            )

        hits = [r for r in caplog.records if "action=audit.record" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        text = hits[0].getMessage()
        assert "severity=reportable" in text, "连接类异常也要因审计缺口升级"
        assert "audit_action=document.delete" in text
        assert "resource_type=document" in text

    def test_broadcast_failure_is_visible_and_prunes(self, caplog):
        """广播发送失败要摘除连接并留痕，否则分不清「用户关页面」与「全员收不到」。"""
        from src.services.notification_service import (
            Notification,
            NotificationType,
            broadcast,
            channel_connections,
        )

        class _DeadSocket:
            async def send_json(self, message):
                raise ConnectionResetError("client gone")

        dead = _DeadSocket()
        channel_connections.clear()
        channel_connections["kb:9"] = {dead}

        note = Notification(type=NotificationType.KB_UPDATED, channel="kb:9", data={"kb_id": "9"})
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            asyncio.run(broadcast("kb:9", note))

        assert dead not in channel_connections["kb:9"], "失效连接必须摘除"
        hits = [r for r in caplog.records if "action=notification.service.broadcast" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "channel=kb:9" in hits[0].getMessage()
        channel_connections.clear()

    def test_kb_recommend_failure_degrades_visibly(self, caplog):
        """推荐失败返回空列表是「静默功能失效」，此前只有一句 error，无法聚合定位。"""
        from src.services.kb_recommender import KBRecommender

        class _BrokenChain:
            def _has_vector_store(self):
                return True

            async def _retrieve_documents(self, question, kb_ids=None):
                raise RuntimeError("vector store exploded")

        recommender = KBRecommender(_BrokenChain())
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(recommender.recommend_knowledge_bases("报销流程", kb_ids=["kb-1"]))

        assert result == []
        hits = [r for r in caplog.records if "action=kb.recommend" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR, "RuntimeError 应判为需上报"
        text = hits[0].getMessage()
        assert "kb_scope=1" in text
        assert "top_k=3" in text


class TestSchedulerAndTraceGrading:
    """后台任务与埋点的分级：失败不阻断主流程，但缺口必须可追。"""

    def test_wiki_compile_skips_doc_with_trace(self, caplog, monkeypatch):
        """单篇 raw chunks 拉取失败只跳过该文档，日志必须能定位到 doc_id。"""
        from src import database
        from src.services import wiki_compile_scheduler as sched
        from src.services import wiki_rebuild

        class _FakeSession:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *args):
                return False

        async def _broken_load(kb_id, doc_id):
            raise ConnectionError("milvus down")

        monkeypatch.setattr(database, "async_session_maker", lambda: _FakeSession())
        monkeypatch.setattr(wiki_rebuild, "load_raw_chunks", _broken_load)

        with caplog.at_level(logging.WARNING, logger="wiki_compile_scheduler"):
            asyncio.run(sched._compile_batch("kb-7", ["d1", "d2"]))

        hits = [r for r in caplog.records if "action=wiki.scheduler.load_chunks" in r.getMessage()]
        assert len(hits) == 2, "每篇拉取失败都要留痕"
        assert all(h.levelno == logging.WARNING for h in hits), "Milvus 抖动属可恢复"
        text = hits[0].getMessage()
        assert "kb_id=kb-7" in text
        assert "doc_id=d1" in text

    def test_trace_persist_failure_keeps_trace_id(self, caplog, monkeypatch):
        """trace 落库失败不应静默：链路无留痕时要能按 trace_id 反查。"""
        from src import database
        from src.services.trace_collector import TraceCollector

        def _broken_factory():
            raise ConnectionError("db down")

        monkeypatch.setattr(database, "AsyncSessionLocal", _broken_factory)
        collector = TraceCollector(trace_id="t-1")
        collector.data.update({"question": "q"})

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            asyncio.run(collector.save_async())

        hits = [r for r in caplog.records if "action=trace.persist" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "trace_id=t-1" in hits[0].getMessage()

    def test_rate_limit_probe_warns_once(self, caplog, monkeypatch):
        """限流 Redis 探测失败会静默退化为单进程：按需上报，但只在首次记录。"""
        from src.services import cache_service
        from src.utils import rate_limit

        monkeypatch.setattr(rate_limit, "_probe_warned", False)
        # 用裸 object 顶替 CacheService：内部属性探测必然抛 AttributeError（代码漂移）
        monkeypatch.setattr(cache_service, "CacheService", object())

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            assert rate_limit._redis_client() is None
            assert rate_limit._redis_client() is None

        hits = [r for r in caplog.records if "action=rate_limit.redis_probe" in r.getMessage()]
        assert len(hits) == 1, "每请求都会走到这里，重复记录会淹没真正的告警"
        assert hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()


class TestConversationAndProgressGrading:
    """对话体验类降级（标题/改写/进度推送）：不阻断回答，但失败要能统计。"""

    def test_title_generation_falls_back_with_log(self, caplog, monkeypatch):
        from src.services.title_generator import TitleGenerator

        class _BrokenLLM:
            async def ainvoke(self, prompt):
                raise TimeoutError("llm timeout")

        gen = TitleGenerator()

        async def _fake_get_llm():
            return _BrokenLLM()

        monkeypatch.setattr(gen, "_get_llm", _fake_get_llm)

        with caplog.at_level(logging.WARNING, logger="src.services.title_generator"):
            title = asyncio.run(gen.generate_title("如何配置模型路由"))

        assert title, "失败必须回落到截断标题，不能返回空"
        hits = [r for r in caplog.records if "action=conversation.title.generate" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in hits[0].getMessage()

    def test_multi_query_rewrite_failure_returns_empty(self, caplog):
        from src.services.query_rewriter import QueryRewriter

        class _BrokenLLM:
            async def ainvoke(self, prompt):
                raise ConnectionError("upstream reset")

        rewriter = QueryRewriter(llm=_BrokenLLM())
        with caplog.at_level(logging.WARNING, logger="src.services.query_rewriter"):
            queries = asyncio.run(rewriter._llm_rewrite("RAG 怎么调优", None))

        assert queries == []
        hits = [r for r in caplog.records if "action=query.rewrite.multi" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "query_len=" in hits[0].getMessage()

    def test_progress_push_failure_prunes_and_logs(self, caplog):
        """上传进度推送失败此前完全静默，用户侧只看到「进度条卡住」。"""
        from src.services import progress_manager as pm

        class _DeadConn:
            async def send_json(self, message):
                raise ConnectionResetError("client gone")

        class _Progress:
            def to_dict(self):
                return {"upload_id": "up-1", "progress": 50}

        dead = _DeadConn()
        pm.progress_store.clear()
        pm.ws_connections.clear()
        pm.progress_store["up-1"] = _Progress()
        pm.ws_connections["up-1"] = [dead]

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            asyncio.run(pm.notify_ws_clients_async("up-1"))

        assert dead not in pm.ws_connections["up-1"]
        hits = [r for r in caplog.records if "action=progress.push" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "upload_id=up-1" in hits[0].getMessage()
        pm.progress_store.clear()
        pm.ws_connections.clear()


class TestLearningAndSynthesisGrading:
    """学习成果与知识回流：失败不阻断主流程，但不能「静默丢失」。"""

    def test_threshold_persist_failure_is_honest(self, caplog, monkeypatch):
        """阈值只写进内存时接口仍报 success：必须上报并在返回值里如实标注。"""
        from src.services import rule_learner as rl

        class _BrokenStore:
            async def update_strategy_config(self, *args, **kwargs):
                raise ConnectionError("db down")

        class _SM:
            def __init__(self):
                self.threshold = 0.5

            def set_threshold(self, value):
                self.threshold = value

        learner = rl.RuleLearner()
        learner.min_samples_for_training = 1

        async def _samples(limit=1000, owner_id=None):
            return [{"confidence": 0.8, "label": 1}]

        monkeypatch.setattr(learner, "extract_training_samples", _samples)
        monkeypatch.setattr(rl, "sample_store", _BrokenStore())

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(learner.update_threshold_based_on_feedback(_SM()))

        assert result["status"] == "success"
        assert result["threshold_persisted"] is False, "写库失败不能假装持久化成功"
        hits = [r for r in caplog.records if "action=rule_learner.threshold.persist" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()

    def test_synthesis_dedupe_failure_is_fail_open_but_visible(self, caplog, monkeypatch):
        """去重检索失败按「不存在」处理（可能重复建页），故障本身要能看到。"""
        from src.services import kb_retrieval_service as krs
        from src.services.wiki_synthesis_service import WikiSynthesisService

        class _BrokenRetrieval:
            def __init__(self, *args, **kwargs):
                pass

            async def retrieve(self, *args, **kwargs):
                raise ConnectionError("milvus down")

        monkeypatch.setattr(krs, "KBRetrievalService", _BrokenRetrieval)
        service = WikiSynthesisService()

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            exists = asyncio.run(service._similar_synthesis_exists(["kb-1", "kb-2"], "报销流程"))

        assert exists is False, "fail-open：检索不可用时不许阻断回流"
        hits = [r for r in caplog.records if "action=wiki.synthesis.dedupe" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "kb_scope=2" in hits[0].getMessage()


class TestDocumentAndEvalGrading:
    """文档链路与评估器：降级要留痕，结论错误要升级。"""

    def test_preview_does_not_mask_parse_failure_as_client_error(self, monkeypatch, tmp_path):
        """解析/IO 失败曾被包成 ValueError，路由据此回 400：服务端故障被说成「请求有问题」。"""
        from src.services import document_processor as dp

        target = tmp_path / "a.txt"
        target.write_text("内容", encoding="utf-8")

        def _broken_load(path):
            raise OSError("minio object unreadable")

        monkeypatch.setattr(dp, "load_document", _broken_load)

        with pytest.raises(OSError):
            dp.preview_document(str(target))

    def test_preview_keeps_user_fixable_error_as_valueerror(self, tmp_path):
        """文件不存在等调用方可纠正的问题仍抛 ValueError（路由映射 400）。"""
        from src.services import document_processor as dp

        with pytest.raises(ValueError):
            dp.preview_document(str(tmp_path / "missing.txt"))

    def test_duplicate_detection_failure_is_reportable(self, caplog):
        """返回空列表 = 「没有重复」，上传流程据此放行：结论错了，不能只记一条 error。"""
        from src.services.document_analyzer import DocumentAnalyzer

        class _BrokenChain:
            sentence_transformer = object()

            def _has_vector_store(self):
                return True

            def _init_similarity_model(self):
                pass

            async def _retrieve_documents(self, *args, **kwargs):
                raise ConnectionError("milvus down")

        analyzer = DocumentAnalyzer(llm=None, rag_chain=_BrokenChain())
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(analyzer.detect_duplicates("报销制度正文", kb_id="kb-1"))

        assert result == []
        hits = [r for r in caplog.records if "action=document.detect_duplicates" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        text = hits[0].getMessage()
        assert "severity=reportable" in text, "重复检测误判会让重复文档持续入库"
        assert "kb_id=kb-1" in text

    def test_citation_source_embedding_failure_is_visible(self, caplog):
        """来源向量算不出来 → 全句只能标 [?]，等于引用补全整体失效。"""
        from src.services.citation_backfiller import CitationBackfiller

        class _BrokenEmb:
            async def aembed_documents(self, texts):
                raise ConnectionError("embedding down")

        filler = CitationBackfiller(embeddings=_BrokenEmb())
        sources = [{"source_index": 1, "title": "制度", "content": "报销流程"}]

        with caplog.at_level(logging.WARNING, logger="src.services.citation_backfiller"):
            result = asyncio.run(filler._compute_source_embeddings(sources))

        assert result == []
        hits = [r for r in caplog.records if "action=citation.backfill.source_embedding" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "sources=1" in hits[0].getMessage()

    def test_sentence_embedding_failures_are_throttled(self, caplog, monkeypatch):
        """逐句失败按句发生：前几次记录，之后按间隔记录，避免一条长答案刷几十行。"""
        from src.services import citation_backfiller as cb

        class _BrokenEmb:
            async def aembed_query(self, text):
                raise TimeoutError("embedding timeout")

        monkeypatch.setattr(cb, "_sentence_embed_failures", 0)
        filler = cb.CitationBackfiller(embeddings=_BrokenEmb())
        source_embeddings = [(1, [0.1, 0.2])]

        with caplog.at_level(logging.WARNING, logger="src.services.citation_backfiller"):
            for i in range(5):
                out = asyncio.run(
                    filler._backfill_citation(f"第{i}句是足够长的事实陈述。", source_embeddings)
                )

        assert "[?]" in out
        hits = [r for r in caplog.records if "action=citation.backfill.sentence_embedding" in r.getMessage()]
        assert len(hits) == 3, "只记前 3 次，其余按 50 次间隔"
        assert "failures=1" in hits[0].getMessage()

    def test_retrieval_similarity_zero_is_reportable(self, caplog, monkeypatch):
        """评估器自身故障算出的 0 分，与「真的毫不相关」同分，必须上报。"""
        from src.services.evaluation.retrieval_evaluator import RetrievalEvaluator

        class _BrokenEmb:
            async def aembed_query(self, question):
                raise RuntimeError("embedding model crashed")

        evaluator = RetrievalEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: _BrokenEmb())

        with caplog.at_level(logging.WARNING, logger="src.services.evaluation.retrieval_evaluator"):
            score = asyncio.run(evaluator._evaluate_similarity("报销流程怎么走", ["doc-a"]))

        assert score == 0.0
        hits = [r for r in caplog.records if "action=retrieval_eval.similarity" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()
        assert "docs=1" in hits[0].getMessage()


class TestStrategyAndToolGrading:
    """路由策略与外部工具：弃权/降级都有语义，日志要能区分「模型说 NO」与「模型挂了」。"""

    def test_semantic_strategy_init_failure_is_visible(self, caplog, monkeypatch):
        """示例向量算不出来 → 该策略从此永久弃权，等同语义路由失效。"""
        from src.services import strategies
        from src.services.strategies import semantic as semantic_mod

        def _broken_get_embeddings():
            raise ConnectionError("embedding service down")

        monkeypatch.setattr(semantic_mod.model_manager, "get_embeddings", _broken_get_embeddings)

        strategy = strategies.semantic.SemanticStrategy()
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            asyncio.run(strategy.initialize())

        assert strategy._greeting_embeddings is None
        assert strategy._knowledge_embeddings is None
        hits = [r for r in caplog.records if "action=strategy.semantic.init" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        text = hits[0].getMessage()
        assert "severity=recoverable" in text
        assert "examples=" in text

    def test_llm_strategy_timeout_counts_as_abstention(self, caplog):
        """调用失败时置信度 0.3 会低于弃权阈值：不是「判定不需要知识库」，日志要说清。"""
        from src.services.strategies.llm_inference import LLMInferenceStrategy

        class _BrokenLLM:
            async def ainvoke(self, prompt):
                raise TimeoutError("fast model timeout")

        strategy = LLMInferenceStrategy()
        strategy.llm = _BrokenLLM()

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            decision = asyncio.run(strategy.should_use_knowledge_base("季度营收目标是多少"))

        assert decision is False
        assert strategy.get_confidence() == 0.3
        hits = [r for r in caplog.records if "action=strategy.llm_inference.decide" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "弃权" in hits[0].getMessage()

    def test_llm_strategy_init_failure_is_visible(self, caplog, monkeypatch):
        """拿不到模型时策略整场弃权，要能对上「重启后路由变少一票」。"""
        from src.services.strategies import llm_inference as li_mod

        async def _broken_get_chat_llm(*args, **kwargs):
            raise RuntimeError("registry 未配置 fast 模型")

        monkeypatch.setattr(li_mod.model_manager, "get_chat_llm", _broken_get_chat_llm)

        strategy = li_mod.LLMInferenceStrategy()
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            asyncio.run(strategy.initialize())

        assert strategy.llm is None
        hits = [r for r in caplog.records if "action=strategy.llm_inference.init" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR, "配置类错误应判为需上报"

    def test_weather_geocode_failure_is_distinguishable(self, caplog, monkeypatch):
        """地理编码失败会被上层说成「未找到城市」，只有这条日志能区分网络故障与地名拼错。"""
        from src.services.tools.plugins import _weather_impl as impl

        def _broken_client():
            raise ConnectionError("open-meteo unreachable")

        monkeypatch.setattr(impl, "_get_http_client", _broken_client)

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(impl._geocode_city("吕梁"))

        assert result is None
        hits = [r for r in caplog.records if "action=weather.geocode" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "city=吕梁" in hits[0].getMessage()

    def test_singleton_cleanup_failure_names_the_class(self, caplog):
        """清理失败仍强制重置，代价是旧连接可能残留：必须能指出是哪个单例。"""
        from src.utils.async_singleton import AsyncSingleton

        class _LeakySingleton(AsyncSingleton["_LeakySingleton"]):
            async def _async_init(self):
                return None

            async def _async_cleanup(self):
                raise ConnectionResetError("close failed")

        async def _scenario():
            await _LeakySingleton.get_instance()
            with caplog.at_level(logging.WARNING, logger="src.utils.async_singleton"):
                await _LeakySingleton.reset_instance()

        asyncio.run(_scenario())

        hits = [r for r in caplog.records if "action=singleton.cleanup" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "singleton=_LeakySingleton" in hits[0].getMessage()


class TestWikiGraphAndABGrading:
    """Wiki 侧链路、知识图谱与 A/B 埋点：降级往往「看起来正常」，日志要能反查。"""

    def test_route_prior_failure_falls_back_visibly(self, caplog, monkeypatch):
        """先验算不出来 → 检索悄悄退回纯 chunk 分，排序变化无人知晓。"""
        from src import database as db_mod
        from src.services import wiki_route_prior as wrp

        def _broken_maker():
            raise ConnectionError("db down")

        monkeypatch.setattr(db_mod, "async_session_maker", _broken_maker)

        with caplog.at_level(logging.WARNING, logger="wiki_route_prior"):
            result = asyncio.run(wrp.WikiRoutePrior().get_priors("报销流程", ["kb-1", "kb-2"]))

        assert result == {}
        hits = [r for r in caplog.records if "action=wiki.route_prior.compute" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "kb_scope=2" in hits[0].getMessage()

    def test_graph_generate_failure_is_logged_with_scope(self, caplog):
        """空图 + 「生成失败」文案用户可见，失败原因只在服务端日志里。"""
        from src.services.knowledge_graph_generator import KnowledgeGraphGenerator

        class _BrokenChain:
            def _has_vector_store(self):
                return True

            async def _retrieve_documents(self, *args, **kwargs):
                raise ConnectionError("milvus down")

        generator = KnowledgeGraphGenerator(llm=None, rag_chain=_BrokenChain())
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(generator.generate_knowledge_graph(kb_ids=["kb-1"]))

        assert result["nodes"] == [] and result["edges"] == []
        assert "生成失败" in result["summary"]
        hits = [r for r in caplog.records if "action=graph.generate" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "kb_scope=1" in hits[0].getMessage()

    def test_intent_router_timeout_keeps_original_type(self, caplog, monkeypatch):
        """包成 RuntimeError 交给上层兜底，但超时（可恢复）与解析失败要能分开统计。"""
        from src.services.intent_router.llm_router import LLMRouter

        class _BrokenLLM:
            def invoke(self, prompt):
                raise TimeoutError("router model timeout")

        class _FakeCtx:
            resolved_question = "季度营收目标是多少"
            compressed_history = "无"

        class _FakeBuilder:
            def build(self, question, history=None):
                return _FakeCtx()

        async def _stub_get_llm():
            return _BrokenLLM()

        router = LLMRouter()
        monkeypatch.setattr(router, "_get_llm", _stub_get_llm)
        monkeypatch.setattr(router, "_get_context_builder", lambda: _FakeBuilder())

        with caplog.at_level(logging.WARNING, logger="intent_router.llm"):
            with pytest.raises(RuntimeError):
                asyncio.run(router.route("季度营收目标是多少"))

        hits = [r for r in caplog.records if "action=intent_router.llm.timeout" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in hits[0].getMessage()

    def test_ab_metric_failures_are_throttled(self, caplog, monkeypatch):
        """该埋点每个响应都会写：实验服务挂了也不能变成每请求一条日志。"""
        from src.api.chat import common as chat_common

        class _BrokenExperiment:
            async def record_metric_for_chat(self, **kwargs):
                raise ConnectionError("experiment db down")

        monkeypatch.setattr(chat_common, "experiment_manager", _BrokenExperiment())
        monkeypatch.setattr(chat_common, "_ab_metric_failures", 0)

        cfg = {"experiment_id": "exp-1", "variant_id": "v-a"}
        with caplog.at_level(logging.WARNING, logger="src.api.chat"):
            for _ in range(5):
                asyncio.run(chat_common._record_ab_metric(cfg, 123.0))

        hits = [r for r in caplog.records if "action=ab.record_metric" in r.getMessage()]
        assert len(hits) == 3, "前 3 次记录，之后每 100 次一条"
        assert "failures=1" in hits[0].getMessage()
        assert "experiment_id=exp-1" in hits[0].getMessage()


class TestRerankAndPriceToolGrading:
    """重排分数与价格工具：失败被冒充成「低相关 / 默认值」，结论会直接错。"""

    def test_rerank_score_failure_returns_none(self, caplog):
        """打分失败必须以 None 表达；0.0 会被下游当成「判定不相关」而滤掉候选。"""
        from src.services.ollama_reranker import OllamaReranker

        reranker = OllamaReranker("grading-rerank-model")
        reranker._initialized = True

        class _BrokenClient:
            async def generate(self, model, prompt):
                raise ConnectionError("ollama connection refused")

        reranker._client = _BrokenClient()

        with caplog.at_level(logging.WARNING, logger="ollama_reranker"):
            score = asyncio.run(reranker._score_pair("查询", "文档"))

        assert score is None
        hits = [r for r in caplog.records if "action=rerank.ollama.score" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in hits[0].getMessage()
        assert "model=grading-rerank-model" in hits[0].getMessage()

    def test_rerank_unparseable_output_is_logged(self, caplog):
        """模型没按格式给分数：与「0 分」区分开，否则一条不相关都查不出来。"""
        from src.services import ollama_reranker as mod

        reranker = mod.OllamaReranker("grading-rerank-parse")
        reranker._initialized = True

        class _GarbageClient:
            async def generate(self, model, prompt):
                return {"response": "I cannot rate this."}

        reranker._client = _GarbageClient()
        monkeypatch_marker = mod._parse_failures

        with caplog.at_level(logging.WARNING, logger="ollama_reranker"):
            score = asyncio.run(reranker._score_pair("查询", "文档"))

        assert score is None
        hits = [r for r in caplog.records if "action=rerank.ollama.parse" in r.getMessage()]
        assert hits and mod._parse_failures == monkeypatch_marker + 1
        assert "raw=I cannot rate this." in hits[0].getMessage()

    def test_exchange_rate_bad_amount_is_reportable(self, caplog, monkeypatch):
        """金额认不出来却按 1 算，用户会拿到一个「看着正常」的错误数字。"""
        from decimal import Decimal

        from src.services.tools.plugins import exchange_rate_tool as mod

        point = mod.PriceDataPoint(
            value=Decimal("7.1"),
            currency="CNY",
            unit="USD",
            timestamp=None,
            source="frankfurter",
            source_url="https://api.frankfurter.dev/v1/latest",
        )

        async def _fake_frankfurter(from_currency, to_currency):
            return point

        monkeypatch.setattr(mod, "_fetch_frankfurter", _fake_frankfurter)
        monkeypatch.delenv("FREE_EXCHANGE_RATE_API_KEY", raising=False)

        tool = mod.ExchangeRateTool()
        with caplog.at_level(logging.ERROR, logger="rag_system"):
            result = asyncio.run(
                tool.execute(from_currency="USD", to_currency="CNY", amount="很多", question="很多美元是多少人民币")
            )

        # 答案要么带「金额没认出来」的提示，要么直接因置信度不足拒答；
        # 两种情况都不能给出一个看着正常的数字
        assert ("未识别出兑换金额" in result.output) or ("无法获取可靠" in result.output)
        assert result.input_arguments["amount"] == "1", "金额确实被替换成了 1，故必须留痕"
        hits = [r for r in caplog.records if "action=tools.exchange_rate.amount_parse" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR, "按 1 计算会改变答案，必须是 reportable"
        assert "severity=reportable" in hits[0].getMessage()
        assert "raw_amount=很多" in hits[0].getMessage()


class TestApiAndToolBoundaryGrading:
    """路由与工具边界：跳过/超时这类「看起来正常」的失败要能反查。"""

    def test_batch_delete_invalid_id_is_reported(self, caplog):
        """非法 ID 被跳过是对的，但响应只说「已提交 N 个」，漏删对调用方不可见。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api import document_router
        from src.auth import get_current_user
        from src.database import get_db

        class _DB:
            async def execute(self, *args, **kwargs):
                raise AssertionError("非法 ID 不应发起查询")

        app = FastAPI()
        app.include_router(document_router)
        app.dependency_overrides[get_db] = lambda: _DB()
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="u-1")

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            resp = TestClient(app).post("/documents/batch/delete", json={"ids": ["not-a-uuid"]})

        assert resp.status_code == 200
        body = resp.json()
        assert body["invalid_ids"] == ["not-a-uuid"]
        assert body["deleted_count"] == 0
        hits = [r for r in caplog.records if "action=document.batch_delete.invalid_id" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "total=1" in hits[0].getMessage()

    def test_tool_timeout_is_logged_with_name(self, caplog, monkeypatch):
        """超时以 success=False 回给 Agent，但「哪个工具慢到 30s」只有日志能回答。"""
        from src.services.tools import tool_manager as tm_mod
        from src.services.tools.tool_manager import BaseTool, ToolManager, ToolResult

        class _SlowTool(BaseTool):
            name = "slow_tool"
            description = "慢工具"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> ToolResult:
                return ToolResult(tool_name=self.name, output="never")

        async def _always_timeout(coro, timeout=None):
            coro.close()
            raise asyncio.TimeoutError()

        manager = ToolManager()
        manager.register_tool(_SlowTool())
        monkeypatch.setattr(tm_mod.asyncio, "wait_for", _always_timeout)

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(manager.execute("slow_tool"))

        assert result.success is False and result.error == "工具执行超时"
        hits = [r for r in caplog.records if "action=tools.execute.timeout" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "tool=slow_tool" in hits[0].getMessage()
        assert "timeout_s=30.0" in hits[0].getMessage()


class TestAgentToolDegradationGrading:
    """工具侧降级：失败只回一句「失败」给模型时，日志必须能反查原因。"""

    def test_fetch_webpage_error_is_logged(self, caplog, monkeypatch):
        """抓取失败此前完全无日志：事后只能看到「Agent 没用上网页内容」。"""
        from src.config import settings
        from src.services.tools.plugins import fetch_webpage_tool as mod

        monkeypatch.setattr(settings.search, "AGENT_WEB_FETCH_ENABLED", True, raising=False)
        monkeypatch.setattr(mod, "validate_url_safe", lambda url: None)

        class _BrokenService:
            async def fetch_content(self, url, title="", snippet=""):
                raise ConnectionError("connection refused")

        tool = mod.FetchWebpageTool(web_search_service=_BrokenService())

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(tool.execute(url="https://example.com/a"))

        assert result.success is False and "抓取失败" in result.error
        hits = [r for r in caplog.records if "action=tools.fetch_webpage.execute" in r.getMessage()]
        assert hits, "抓取失败必须留痕"
        assert hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in hits[0].getMessage()
        assert "url=https://example.com/a" in hits[0].getMessage()

    def test_fetch_webpage_empty_body_is_reportable(self, caplog, monkeypatch):
        """抓取没报错但正文为空：多半是反爬/提取器失效，属质量退化。"""
        from src.config import settings
        from src.services.tools.plugins import fetch_webpage_tool as mod

        monkeypatch.setattr(settings.search, "AGENT_WEB_FETCH_ENABLED", True, raising=False)
        monkeypatch.setattr(mod, "_empty_content_hits", 0, raising=False)
        monkeypatch.setattr(mod, "validate_url_safe", lambda url: None)

        class _EmptyService:
            async def fetch_content(self, url, title="", snippet=""):
                return SimpleNamespace(content="")

        tool = mod.FetchWebpageTool(web_search_service=_EmptyService())

        with caplog.at_level(logging.ERROR, logger="rag_system"):
            result = asyncio.run(tool.execute(url="https://blocked.example.com/b"))

        assert "内容为空" in result.output
        hits = [r for r in caplog.records if "action=tools.fetch_webpage.empty_body" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()

    def test_kb_search_failure_is_logged_with_scope(self, caplog):
        """检索服务故障与「确实没有相关文档」在 Agent 侧同形，必须能分开。"""
        from src.services.tools.plugins.kb_search_tool import KBSearchTool

        class _BrokenService:
            async def retrieve(self, query, kb_ids=None):
                raise TimeoutError("milvus timeout")

        tool = KBSearchTool(kb_retrieval_service=_BrokenService(), kb_ids=["kb-1", "kb-2"])

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            result = asyncio.run(tool.execute(query="报销流程"))

        assert result.success is False and "知识库检索失败" in result.error
        hits = [r for r in caplog.records if "action=tools.kb_search.execute" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "severity=recoverable" in hits[0].getMessage()
        assert "kb_count=2" in hits[0].getMessage()

    def test_dropped_tool_call_is_reported(self, caplog, monkeypatch):
        """模型给了 JSON 却解析不出工具名 → 调用被丢弃，上层按「无需工具」硬答。"""
        from src.services import function_calling_agent as mod

        monkeypatch.setattr(mod, "_dropped_tool_call_hits", 0, raising=False)

        handler = object.__new__(mod.FunctionCallingHandler)
        content = '{"tool_name": "web_search", "query": "今天北京天气"}'

        with caplog.at_level(logging.ERROR, logger="function_calling_agent"):
            calls = handler._parse_tool_calls(content)

        assert calls == []
        hits = [r for r in caplog.records if "action=agent.fc.parse_tool_calls" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()
        assert "json_blocks=1" in hits[0].getMessage()

    def test_snippet_fallback_is_reported(self, caplog, monkeypatch):
        """全部正文抓取失败退回摘要：答案变浅，但此前完全没有痕迹。"""
        from src.services import web_search_service as mod
        from src.services.search_types import SearchResult

        monkeypatch.setattr(mod, "_fetch_all_failed_hits", 0, raising=False)

        svc = object.__new__(mod.WebSearchService)
        results = [SearchResult(title="t1", url="https://a.example.com", content="摘要一")]
        contents = svc._snippet_fallback(results, 5)

        assert len(contents) == 1 and contents[0].content == "摘要一"
        hits = [
            r for r in caplog.records
            if "action=web_search.build_context.fetch_all_failed" in r.getMessage()
        ]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()
        assert "candidates=1" in hits[0].getMessage()


class TestRelevanceGateAndStreamRetryGrading:
    """判定链上的静默降级：相关性门禁与 LLM 重试失败。"""

    def _retrieval_obj(self, monkeypatch, mod):
        obj = object.__new__(mod.RetrievalMixin)
        obj.sentence_transformer = None
        obj.similarity_util = None
        obj.similarity_threshold = 0.3
        monkeypatch.setattr(mod, "_similarity_unavailable_logged", False, raising=False)
        monkeypatch.setattr(
            mod.RetrievalMixin, "_rerank_score_available", staticmethod(lambda: False)
        )
        return obj

    def test_relevance_fallback_does_not_fabricate_score(self, caplog, monkeypatch):
        """测不出相关性时可以放行，但分数不能编一个 0.6 当真实测量值。"""
        from src.services.pipeline import retrieval as mod

        obj = self._retrieval_obj(monkeypatch, mod)
        monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)

        docs = [SimpleNamespace(page_content="报销流程见附件", metadata={})]
        with caplog.at_level(logging.ERROR, logger="rag_system"):
            score, has_relevant = asyncio.run(obj._calculate_relevance("怎么报销", docs))

        assert has_relevant is True, "测不出来时 fail-open，不能把知识库结果丢掉"
        assert score == 0.0, "0.6 是编出来的常量，会被当成「刚好过阈值」的真实分数"
        hits = [r for r in caplog.records if "action=pipeline.relevance.fallback" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()

    def test_relevance_fallback_logs_once(self, caplog, monkeypatch):
        """模型缺失是部署期问题，逐请求刷屏会盖掉真正的请求级故障。"""
        from src.services.pipeline import retrieval as mod

        obj = self._retrieval_obj(monkeypatch, mod)
        monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", None)

        docs = [SimpleNamespace(page_content="x", metadata={})]
        with caplog.at_level(logging.ERROR, logger="rag_system"):
            for _ in range(3):
                asyncio.run(obj._calculate_relevance("q", docs))

        hits = [r for r in caplog.records if "action=pipeline.relevance.fallback" in r.getMessage()]
        assert len(hits) == 1, "只应记首次"

    def test_stream_retry_exhausted_is_logged(self, caplog, monkeypatch):
        """重试耗尽后用户拿到的是「模型调用失败: xxx」，日志此前只有指标计数。"""
        from src.services.pipeline import generation as mod

        class _BrokenLLM:
            model = "grading-llm"
            labels = []

            def astream(self, prompt):
                async def _gen():
                    raise ConnectionError("ollama refused")
                    yield  # pragma: no cover
                return _gen()

        obj = object.__new__(mod.GenerationMixin)
        obj.llm = _BrokenLLM()
        obj.llm_direct = None

        async def _collect():
            out = []
            async for item in obj._stream_with_retry("p", [], [], "answer", max_retries=1):
                out.append(item)
            return out

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            chunks = asyncio.run(_collect())

        assert chunks and chunks[-1][3] == "error"
        assert "模型调用失败" in chunks[-1][0]
        hits = [r for r in caplog.records if "action=pipeline.generate.stream" in r.getMessage()]
        assert hits, "重试耗尽必须有日志"
        assert "attempt=1 max_retries=1" in hits[0].getMessage()
        assert "model=grading-llm" in hits[0].getMessage()
        assert "severity=recoverable" in hits[0].getMessage()


class TestVerificationSignalGrading:
    """校验链：结果算出来了却不让用户看到，等于没算。"""

    def test_conflicting_claims_override_high_confidence(self):
        """多源数值冲突是确定性证据，置信度再高也不能静默。"""
        from src.services.answer_verification_types import VerificationResult
        from src.services.answer_verifier import AnswerVerifier

        verifier = AnswerVerifier()
        result = VerificationResult(
            is_consistent=False,
            confidence=0.95,
            conflicting_claims=["数值 850 与多源数据存在冲突"],
        )

        suffix = verifier.format_warning_suffix(result)

        assert suffix, "is_consistent=False 却返回空后缀，等于把冲突吃掉了"
        assert "不一致" in suffix

    def test_citation_out_of_range_is_surfaced(self):
        """答案引用 [9] 但只有 1 个来源：用户点开对不上任何东西。"""
        from src.services.answer_verification_types import VerificationResult
        from src.services.answer_verifier import AnswerVerifier

        verifier = AnswerVerifier()
        result = VerificationResult(
            is_consistent=True,
            confidence=0.92,
            citation_issues=["引用 [9] 超出有效来源范围"],
            warnings=["引用 [9] 超出有效来源范围"],
        )

        suffix = verifier.format_warning_suffix(result)

        assert "引用编号" in suffix

    @pytest.mark.asyncio
    async def test_citation_issue_is_structured(self):
        """verification 结果要能被结构化判断，不必让调用方去解析告警文案。"""
        from src.services.answer_verifier import AnswerVerifier

        verifier = AnswerVerifier()
        answer = "金价 850 元 [9]。"
        sources = [
            {"source_index": 1, "title": "金价A", "content": "金价 850 元", "url": "https://a.com"}
        ]

        result = await verifier.verify(answer, sources)

        assert result.citation_issues == ["引用 [9] 超出有效来源范围"]
        assert "引用 [9] 超出有效来源范围" in result.warnings

    def test_trace_dropped_key_warns_once(self, caplog, monkeypatch):
        """代码与表结构不同步不是请求级异常，逐请求告警会把真问题淹掉。"""
        from src.services import trace_collector as mod

        monkeypatch.setattr(mod, "_dropped_key_warned", set(), raising=False)

        collector = mod.TraceCollector(trace_id="t-1")
        collector.data["question"] = "q"
        collector.data["verification"] = {"confidence": 0.9}

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            first = collector._to_record_payload()
            collector._to_record_payload()
            collector._to_record_payload()

        assert "verification" not in first and "question" in first
        hits = [r for r in caplog.records if "无对应列的键" in r.getMessage()]
        assert len(hits) == 1, "同一键只应提示一次"


class TestWikiRebuildIncompleteGrading:
    """Wiki 重建：先清后建，失败必须让「缺页」可见，而不是报「完成」。"""

    @staticmethod
    def _fake_db(docs):
        class _Result:
            def scalars(self):
                return self

            def all(self):
                return docs

        class _DB:
            async def execute(self, *args, **kwargs):
                return _Result()

            async def commit(self):
                return None

        return _DB()

    def test_partial_failure_is_aggregated(self, caplog, monkeypatch):
        """单篇失败的日志只能说明某一篇坏了，看不出「整个知识库少了哪几块」。"""
        from src.services import wiki_rebuild as mod

        docs = [
            SimpleNamespace(id="d1", filename="a.pdf"),
            SimpleNamespace(id="d2", filename="b.pdf"),
        ]

        class _Compiler:
            async def compile_document(self, db, kb_id, doc_ids, chunks):
                if doc_ids == ["d2"]:
                    raise RuntimeError("LLM 返回空")
                return SimpleNamespace(
                    pages_created=1, pages_updated=0, chunks_indexed=2, fact_retention_rate=0.9
                )

        async def _wipe(db, kb_id):
            return 5

        async def _chunks(kb_id, doc_id):
            return ["chunk"]

        monkeypatch.setattr(mod, "_wipe_existing", _wipe)
        monkeypatch.setattr(mod, "load_raw_chunks", _chunks)
        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", _Compiler)

        with caplog.at_level(logging.ERROR, logger="wiki_rebuild"):
            stats = asyncio.run(mod.rebuild_kb_wiki(self._fake_db(docs), "kb-1"))

        assert stats["documents_failed"] == 1
        assert stats["failed_documents"] == ["b.pdf"]
        assert stats["pages_created"] == 1
        hits = [r for r in caplog.records if "action=wiki.rebuild.incomplete" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()
        assert "failed=1 total=2" in hits[0].getMessage()

    def test_all_failed_says_wiki_wiped(self, caplog, monkeypatch):
        """全部失败 = 旧页面已删且没重建，这个 KB 的 Wiki 现在是空的。"""
        from src.services import wiki_rebuild as mod

        docs = [SimpleNamespace(id="d1", filename="only.pdf")]

        class _Compiler:
            async def compile_document(self, *args, **kwargs):
                raise ConnectionError("milvus down")

        async def _wipe(db, kb_id):
            return 7

        async def _chunks(kb_id, doc_id):
            return ["chunk"]

        monkeypatch.setattr(mod, "_wipe_existing", _wipe)
        monkeypatch.setattr(mod, "load_raw_chunks", _chunks)
        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", _Compiler)

        with caplog.at_level(logging.ERROR, logger="wiki_rebuild"):
            stats = asyncio.run(mod.rebuild_kb_wiki(self._fake_db(docs), "kb-2"))

        assert stats["pages_wiped"] == 7 and stats["pages_created"] == 0
        assert stats["documents_failed"] == 1
        hits = [r for r in caplog.records if "action=wiki.rebuild.incomplete" in r.getMessage()]
        assert hits and "Wiki 已被清空" in hits[0].getMessage()

    def test_index_page_failure_is_reportable(self, caplog, monkeypatch):
        """页面写进了库（列表可见）但向量没进去 → 问答永远检索不到这一页。"""
        from src.services.wiki_persist import PagePersistMixin

        class _BrokenStore:
            async def add_documents(self, docs, kb_id):
                raise TimeoutError("milvus write timeout")

        async def _get_instance():
            return _BrokenStore()

        monkeypatch.setattr(
            "src.services.vector_store.VectorStoreManager.get_instance", _get_instance
        )

        persist = object.__new__(PagePersistMixin)
        page = SimpleNamespace(title="报销制度", content="正文内容", page_type="entity")
        row = SimpleNamespace(id="page-1")

        with caplog.at_level(logging.ERROR, logger="rag_system"):
            indexed = asyncio.run(persist._index_page("kb-1", row, page))

        assert indexed == 0
        hits = [r for r in caplog.records if "action=wiki.persist.index_page" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()
        assert "检索不到" in hits[0].getMessage()

    def test_progress_callback_actually_runs(self, monkeypatch):
        """report() 是 async 却全部漏 await：重编译期间用户只看到「正在提交任务」。"""
        from src.services import wiki_rebuild as mod

        docs = [SimpleNamespace(id="d1", filename="a.pdf")]
        events = []

        class _Compiler:
            async def compile_document(self, *args, **kwargs):
                return SimpleNamespace(
                    pages_created=1, pages_updated=0, chunks_indexed=1, fact_retention_rate=0.9
                )

        async def _wipe(db, kb_id):
            return 2

        async def _chunks(kb_id, doc_id):
            return ["chunk"]

        def _progress(percent, message):
            events.append((percent, message))

        monkeypatch.setattr(mod, "_wipe_existing", _wipe)
        monkeypatch.setattr(mod, "load_raw_chunks", _chunks)
        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", _Compiler)

        asyncio.run(mod.rebuild_kb_wiki(self._fake_db(docs), "kb-3", progress_cb=_progress))

        messages = [m for _, m in events]
        assert any("待编译文档" in m for m in messages), "进度回调从未被调用"
        assert any("已清理既有 Wiki 页面" in m for m in messages)
        assert any("新建 1 页" in m for m in messages)
