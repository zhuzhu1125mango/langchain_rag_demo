"""CODE-2：异常分级与统一记录入口的单元测试。"""

import asyncio
import logging

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
