"""
RAG Knowledge Base QA System API - FastAPI入口文件

本文件是RAG知识库问答系统的API服务入口，负责：
1. 初始化FastAPI应用
2. 配置CORS跨域支持
3. 注册数据库表（应用启动时自动创建）
4. 挂载所有API路由
5. 提供健康检查接口

服务启动后：
- API地址: http://localhost:8000
- 自动文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health
"""

import logging
import os
import secrets
import hmac
import time
import asyncio
from logging.handlers import TimedRotatingFileHandler
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from src.database import async_engine, Base, init_db
from src.config import settings, LOG_DIR
from src.exceptions import AppException
from src.utils.security import validate_secret_key, SecretKeyValidationError
from src.utils.error_handling import log_exception, ErrorSeverity
from src.auth import get_current_user, require_admin, CurrentUser
from src.api import (
    document_router,
    document_ws_router,
    chat_router,
    session_router,
    category_router,
    tag_router,
    feedback_router,
    learning_router,
    experiment_router,
    knowledge_base_router,
    wiki_router,
    config_router,
    notification_router,
    badcase_router,
    evaluation_router,
    auth_router,
    trace_router,
    audit_router,
    admin_models_router,
)
from src.middleware.metrics import MetricsMiddleware, get_metrics, reset_metrics

try:
    from src.middleware.prometheus import get_prometheus_metrics
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

# W6 #16 / OPS-3：request_id、user_id、stage 经 contextvars 贯穿到 services/ 层日志
# （此前仅中间件自身日志带 request_id，服务层无法关联具体请求/用户/阶段）
from src.utils.log_context import (  # noqa: E402
    ContextLogFilter,
    build_formatter,
    request_scope,
)


# W6 #14：生产日志统一 stdout（由 Docker/外部采集收敛），规避多 worker 场景下
# RotatingFileHandler 轮转丢日志；开发环境保留文件轮转（backend/logs/app.log）
_log_handlers: list = [logging.StreamHandler()]
if not settings.IS_PRODUCTION:
    _log_handlers.append(
        TimedRotatingFileHandler(
            os.path.join(LOG_DIR, "app.log"),
            when="midnight",
            backupCount=max(1, int(settings.security.LOG_RETENTION_DAYS or 30)),
            encoding="utf-8",
        )
    )
_log_formatter = build_formatter(getattr(settings.logging, "LOG_FORMAT", "text"))
_request_filter = ContextLogFilter()
for _h in _log_handlers:
    _h.setFormatter(_log_formatter)
    _h.addFilter(_request_filter)
# 同时在 logger 级注入，保证第三方 handler（如测试 caplog）也能拿到上下文字段
logging.getLogger("rag_system").addFilter(_request_filter)

_log_level = getattr(
    logging, str(getattr(settings.logging, "LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO
)
logging.basicConfig(
    level=_log_level,
    handlers=_log_handlers,
)
logger = logging.getLogger("rag_system")


class RequestTracingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        request_id = str(uuid4())
        # OPS-3：请求级日志上下文（request_id/user_id/stage 自动注入服务层日志）
        with request_scope(request_id):
            await self._handle(scope, receive, send, request_id)

    async def _handle(self, scope, receive, send, request_id: str):
        # SEC-3：统一解析受信代理后的真实客户端 IP，写回 scope["client"]，
        # 使限流/审计等所有 request.client.host 读取点口径一致。
        from src.utils.rate_limit import resolve_client_ip

        client_ip = resolve_client_ip(scope)
        if scope.get("client"):
            scope["client"] = (client_ip, scope["client"][1])

        if scope["type"] == "http":
            request = Request(scope, receive)
            request.state.request_id = request_id

            start_time = time.time()
            # 仅记录 path，不记录 query string（其中可能携带 api_key 等凭据）
            logger.info(f"Request started: {request.method} {request.url.path} [{request_id}]")

            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    duration = time.time() - start_time
                    status_code = message["status"]
                    logger.info(
                        f"Request completed: {request.method} {request.url.path} [{request_id}] "
                        f"Status: {status_code} Duration: {duration:.2f}s"
                    )
                await send(message)

            await self.app(scope, receive, send_wrapper)
        elif scope["type"] == "websocket":
            logger.info(f"WebSocket connection started: {scope.get('path', '/')} [{request_id}]")

            async def send_wrapper(message):
                if message["type"] == "websocket.close":
                    logger.info(f"WebSocket connection closed: {scope.get('path', '/')} [{request_id}]")
                await send(message)

            await self.app(scope, receive, send_wrapper)
        else:
            await self.app(scope, receive, send)


async def _periodic_learning_task():
    """后台定时学习任务"""
    from src.services.learning_engine import learning_engine
    from src.services.strategy_manager import get_strategy_manager

    # 等待系统启动完成
    await asyncio.sleep(30)

    while True:
        try:
            # LEARN W-A：使用进程内唯一实例，学习调整直接落到在线决策所用 manager
            strategy_manager = await get_strategy_manager()
            result = await learning_engine.check_and_trigger_learning(strategy_manager)
            if result.get("status") != "skipped":
                logger.info(f"自动学习触发成功: {result}")
            else:
                logger.debug(f"自动学习跳过: {result.get('reason')}")
        except Exception as e:
            logger.error(f"自动学习任务异常: {e}", exc_info=True)

        # 每小时检查一次是否需要学习
        await asyncio.sleep(3600)


async def _validate_model_config() -> None:
    """启动前校验模型配置（A2 去硬编码配套）。

    模型是核心依赖（开发/生产一致执行）：三项模型名必填，且必须已存在于本地
    Ollama（/api/tags）；缺失或不可用时拒绝启动，避免配错模型在首次调用才暴露。

    SKIP_OLLAMA_CHECK=true 时跳过 Ollama 连接与模型存在性校验（仅保留模型名
    非空校验）：容器内 Ollama 位于宿主机（host.docker.internal），宿主机服务
    未就绪不应阻塞容器启动；模型缺失将在首次调用时暴露。
    """
    import ollama

    from src.services.model_manager import model_manager

    # P1：有 model_registry.json 时以注册表为准，不再强制要求 OLLAMA_* 三项；
    # 外部 API-only 部署也能启动。legacy（无文件）仍走原有 Ollama 校验。
    model_manager.assert_configured()
    registry = model_manager.registry
    if registry.source != "legacy-env":
        logger.info(
            "模型注册表校验通过: source=%s, providers=%d, models=%d, routes=%d",
            registry.source,
            len(registry.providers),
            len(registry.models),
            len(registry.routes),
        )
        return

    m = settings.model
    required = {
        "OLLAMA_MODEL_NAME": m.OLLAMA_MODEL_NAME,
        "FAST_LLM_MODEL_NAME": m.FAST_LLM_MODEL_NAME,
        "EMBEDDING_MODEL_NAME": m.EMBEDDING_MODEL_NAME,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"启动失败: 模型配置缺失 {', '.join(missing)}，请在 .env 中填写对应模型名（无内置默认）"
        )

    if os.getenv("SKIP_OLLAMA_CHECK", "").lower() == "true":
        logger.warning(
            "SKIP_OLLAMA_CHECK=true: 跳过 Ollama 连接与模型存在性校验"
            "（模型缺失将在首次调用时暴露）"
        )
        return

    try:
        client = ollama.Client(host=m.OLLAMA_HOST) if m.OLLAMA_HOST else ollama.Client()
        response = await asyncio.to_thread(client.list)
    except Exception as e:
        raise RuntimeError(
            f"启动失败: 无法连接 Ollama({m.OLLAMA_HOST or '默认地址'})，请先启动 Ollama 服务: {e}"
        )

    available = set()
    for item in response.get("models", []):
        available.add(item.get("name", ""))
        available.add(item.get("model", ""))

    to_check = [
        ("OLLAMA_MODEL_NAME", m.OLLAMA_MODEL_NAME),
        ("FAST_LLM_MODEL_NAME", m.FAST_LLM_MODEL_NAME),
        ("EMBEDDING_MODEL_NAME", m.EMBEDDING_MODEL_NAME),
    ]
    if m.OLLAMA_DIRECT_MODEL_NAME:
        to_check.append(("OLLAMA_DIRECT_MODEL_NAME", m.OLLAMA_DIRECT_MODEL_NAME))
    # rerank 模型仅在启用且由 Ollama 加载时校验（sentence_transformers 走 HF 本地缓存，不在 /api/tags）
    p = settings.processing
    if p.KB_RERANK_ENABLED and p.KB_RERANK_MODEL and p.KB_RERANK_PROVIDER.lower() == "ollama":
        to_check.append(("KB_RERANK_MODEL", p.KB_RERANK_MODEL))
    s = settings.search
    if s.SEARCH_ENABLE_RERANK and s.SEARCH_RERANK_MODEL and s.SEARCH_RERANK_PROVIDER.lower() == "ollama":
        to_check.append(("SEARCH_RERANK_MODEL", s.SEARCH_RERANK_MODEL))

    absent = [f"{key}={value}" for key, value in to_check if value not in available]
    if absent:
        raise RuntimeError(
            "启动失败: 以下模型未在本地 Ollama 中找到，请先 ollama pull 或修正 .env 配置: "
            + ", ".join(absent)
        )
    logger.info("模型配置校验通过: " + ", ".join(f"{k}={v}" for k, v in to_check))


def _validate_registration_policy() -> None:
    """SEC-2：生产默认关闭开放注册；确需开放必须显式确认。"""
    if not settings.IS_PRODUCTION or not settings.security.AUTH_ALLOW_REGISTRATION:
        return
    if not settings.security.ALLOW_OPEN_REGISTRATION_IN_PROD:
        raise RuntimeError(
            "生产环境启动失败: AUTH_ALLOW_REGISTRATION=true。"
            "请设置 AUTH_ALLOW_REGISTRATION=false，或显式设置 "
            "ALLOW_OPEN_REGISTRATION_IN_PROD=true 以确认开放注册。"
        )
    logger.warning(
        "生产环境已显式允许开放注册（ALLOW_OPEN_REGISTRATION_IN_PROD=true）；"
        "请确认已配置聊天/上传配额与外部访问控制"
    )


def _runtime_env_summary() -> str:
    """一行写清「判据是什么、结论是什么」。

    为什么需要：`IS_PRODUCTION` 是**推导出来**的（APP_ENV 优先，否则看 IN_DOCKER），
    同一份 .env 在不同机器上可能得出不同结论。出问题时最容易问错的一句话就是
    「它到底以为自己在什么环境里？」——日志里应该直接有答案，而不是让人去读代码。
    「是否设置」只打 set/unset，不打印任何凭据内容。
    """
    sec = settings.security
    return (
        f"APP_ENV={settings.APP_ENV or '-'} IN_DOCKER={settings.IN_DOCKER} "
        f"IS_PRODUCTION={settings.IS_PRODUCTION}"
        f" | AUTH_ALLOW_REGISTRATION={sec.AUTH_ALLOW_REGISTRATION}"
        f" MINIO_SECURE={settings.minio.MINIO_SECURE}"
        f" ADMIN_KEY={'set' if sec.ADMIN_KEY else 'unset'}"
        f" API_KEY={'set' if sec.API_KEY else 'unset'}"
        f" METRICS_TOKEN={'set' if sec.METRICS_TOKEN else 'unset'}"
    )


def _dangerous_env_warnings() -> list[str]:
    """列出「能启动、但已知危险」的组合。纯函数，便于单测。

    刻意只告警不阻断：这些都可能是刻意的取舍（例如内网单机不配 METRICS_TOKEN）。
    真正必须拦的组合在 lifespan 里直接 raise，这里不重复。
    """
    sec = settings.security
    out: list[str] = []
    if not settings.IS_PRODUCTION:
        return out

    if not sec.ADMIN_KEY:
        out.append(
            "生产模式未配置 ADMIN_KEY：管理员账号被锁死时没有应急通道，"
            "/health/e2e 也无法用密钥调用（只能靠管理员登录态）"
        )
    if not sec.METRICS_TOKEN:
        out.append(
            "生产模式未配置 METRICS_TOKEN：/metrics 无鉴权，任何能连到后端端口的人都能读到"
            "知识库/会话计数与延迟分布（仅在内网且端口不外露时勉强可接受）"
        )
    return out


def _log_runtime_environment() -> None:
    logger.info("运行环境: %s", _runtime_env_summary())
    for warning in _dangerous_env_warnings():
        logger.warning("[env-check] %s", warning)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI生命周期管理函数

    在应用启动时自动创建所有数据库表
    在应用关闭时无需特殊清理操作
    """
    # 先把「它以为自己在什么环境」写进日志，再做校验：
    # 校验失败直接退出时，这行是唯一能解释「为什么退出」的东西。
    _log_runtime_environment()

    # 生产级安全检查：无论是否 Docker，凡生产模式一律强制
    if settings.IS_PRODUCTION:
        try:
            validate_secret_key(settings.security.SECRET_KEY, in_docker=True)
        except SecretKeyValidationError as e:
            raise RuntimeError(f"SECRET_KEY 校验失败: {e}")

        # 关键凭据非空校验
        _missing_creds = []
        if not settings.database.POSTGRES_PASSWORD:
            _missing_creds.append("POSTGRES_PASSWORD")
        if not settings.minio.MINIO_ACCESS_KEY:
            _missing_creds.append("MINIO_ACCESS_KEY")
        if not settings.minio.MINIO_SECRET_KEY:
            _missing_creds.append("MINIO_SECRET_KEY")
        if _missing_creds:
            _msg = f"生产环境启动失败: 关键凭据为空: {', '.join(_missing_creds)}，请在环境配置中设置强密码"
            logger.error(_msg)
            raise RuntimeError(_msg)

        # SEC-2：生产默认关闭开放注册。AUTH_ALLOW_REGISTRATION 默认 true 是
        # 为了开发体验；生产若仍为 true，必须显式设置
        # ALLOW_OPEN_REGISTRATION_IN_PROD=true 才允许启动。
        _validate_registration_policy()

        # W6 #11：生产未启用 MinIO TLS 时告警（compose 内网明文为已知取舍，
        # 须显式设置 MINIO_SECURE=false 声明；暴露公网必须启用 TLS）
        if not settings.minio.MINIO_SECURE:
            logger.warning(
                "MINIO_SECURE=false：MinIO 走明文 HTTP 传输，仅应在受信任内网使用；"
                "如暴露公网必须启用 TLS 并设置 MINIO_SECURE=true"
            )
    else:
        # 开发模式兜底：未设置 SECRET_KEY 时生成临时随机密钥（重启后签名失效）
        if not settings.security.SECRET_KEY:
            settings.security.SECRET_KEY = secrets.token_urlsafe(48)
            logger.warning(
                "SECRET_KEY 未设置，已生成临时随机密钥（仅限开发环境，重启后旧签名失效）；"
                "生产部署必须设置强密钥或 APP_ENV=prod"
            )

    if settings.IS_PRODUCTION:
        # 生产 schema 由 alembic 统一管理（scripts/start-prod.sh：upgrade head /
        # stamp head 对齐）。此处 create_all 会与迁移竞争：全新库上 create_all
        # 先建全量表（无 alembic_version），随后的 upgrade head 撞 DuplicateTable
        # 只能靠 stamp 兜底对齐——schema 出现两个写入方。跳过后 alembic 为唯一来源。
        logger.info("生产模式：跳过 init_db 建表（schema 由 alembic 迁移管理）")
    else:
        await init_db()
        logger.info("Database tables created successfully")

    # 上次进程被强杀/OOM/重启打断时，文档会停在 processing_status="processing"：
    # 状态机只有 completed / failed 两个出口，两条都没走到就没人再管它 ——
    # 前端永久转圈，同一文件重传还会被「内容重复」拦住。启动时统一回收。
    try:
        from src.services.document_tasks import recover_stale_processing_documents

        recovered = await recover_stale_processing_documents()
        if recovered["recovered"]:
            logger.warning(
                "启动回收：%s 篇文档的处理在上次运行中被中断，已标记为失败",
                recovered["recovered"],
            )
    except Exception as recover_err:
        log_exception(
            logger,
            recover_err,
            action="startup.recover.stale_documents",
            message="启动回收僵死文档失败，部分文档可能仍停在「处理中」",
        )

    # W4-23：加载运行时配置覆盖（UI 修改的 processing 调优参数落盘于
    # backend/data/runtime_config.json，重启不丢失）
    from src.services.runtime_config_service import load_runtime_overrides
    load_runtime_overrides()

    # P2：DB 有 Provider/Route 配置时优先于文件/env；没有则回退 P1 文件/legacy。
    from src.services.model_manager import model_manager
    await model_manager.load_from_db()
    # 模型配置校验（A2）：legacy 仍校验 Ollama；DB/file registry 走注册表校验。
    await _validate_model_config()

    # MODEL-P2.1：模型用量增量定期落库（best-effort，DB 不可用仅告警）
    usage_flush_interval = float(settings.model.MODEL_USAGE_FLUSH_INTERVAL_S or 0)
    usage_flush_task = None
    if usage_flush_interval > 0:
        from src.services.model_usage_store import flush_usage_deltas, periodic_flush

        usage_flush_task = asyncio.create_task(periodic_flush(usage_flush_interval))
        logger.info("模型用量落库任务已启动: interval=%ss", usage_flush_interval)

    # SEC-14：数据库日志/trace 保留期清扫（每天一次；失败仅告警）
    from src.services.retention_service import periodic_retention

    retention_task = asyncio.create_task(
        periodic_retention(float(settings.security.RETENTION_SWEEP_INTERVAL_S or 86400))
    )

    # 预热核心异步服务，避免首次请求阻塞事件循环
    try:
        from src.services.vector_store import VectorStoreManager
        from src.services.rag_chain import RAGChain
        from src.services.cache_service import CacheService

        # Redis 为必需依赖，初始化必须成功；设置短超时避免启动时长时间挂死
        await asyncio.wait_for(CacheService.get_instance(), timeout=8.0)
        vector_store = await VectorStoreManager.get_instance()
        rag_chain = await RAGChain.get_instance(vector_store)
        logger.info("核心异步服务预热完成")

        # A 冷启动预热：并发加载默认主力模型与检索侧模型，消除首问 20~40s 长尾。
        # 此前只预热 rag_chain.llm（qwen3:4b 思考模型），而默认 deep_thinking=off
        # 实际走 llm_direct（qwen3:4b-instruct-2507）——默认主力模型从未预热；
        # bge-m3 embedding（语义缓存+检索）与 CrossEncoder reranker 也首问才懒加载。
        # 分组：GPU 组串行，CPU 组（embedding num_gpu=0 + reranker device=cpu）与之
        # 并发，互不争抢显存。全部为最佳努力（best-effort），失败仅记录警告，不中断启动。
        #
        # 4B 主模型（deep_thinking=on 专用）不预热：4GB 显存下 4B 工作集 ≈3.9GB 会
        # 驻留并驱逐 3B 直答模型（实测 ollama ps 仅剩 4B，默认路径被迫每次换载，
        # generate 恶化到 60s+）。深度思考首问改为按需加载 4B（10~30s，用户主动
        # 开启时可接受）；换用 ≥8GB 显存机器后可恢复预热 4B。
        async def _warm(factory, label):
            try:
                await asyncio.wait_for(factory(), timeout=60.0)
                logger.info(f"{label} 预热完成")
            except Exception as e:
                logger.warning(f"{label} 预热失败，首次调用可能较慢: {e}")

        async def _preheat_llm_models():
            # 仅预热默认主力（deep_thinking=off 直答 + 辅助任务共用），使其独占常驻显存
            if rag_chain.llm_direct is not None:
                await _warm(lambda: rag_chain.llm_direct.ainvoke("hello"), "LLM(direct)")

        async def _preheat_retrieval_models():
            from src.services.model_manager import model_manager
            from src.services.hybrid_search import KBReranker

            async def _embed():
                await model_manager.get_embeddings().aembed_query("预热")

            async def _rerank():
                reranker = await KBReranker.get_instance()
                await reranker.rerank("预热", [{"content": "预热", "rrf_score": 1.0}], top_k=1)

            async def _intent_examples():
                # 意图路由 embedding 分类器的示例向量首问才批量计算（CPU 上约数秒），
                # 预热一次使其进入进程内缓存，避免首问承担该一次性成本
                await rag_chain.intent_router._embedding_classifier.classify("预热")

            await asyncio.gather(
                _warm(_embed, "bge-m3 embedding"),
                _warm(_rerank, "KB reranker(CrossEncoder)"),
                _warm(_intent_examples, "意图路由示例向量"),
            )

        await asyncio.gather(
            _preheat_llm_models(),
            _preheat_retrieval_models(),
            return_exceptions=True,
        )
    except Exception as e:
        logger.error(f"核心异步服务预热失败: {e}", exc_info=True)
        raise RuntimeError(f"核心异步服务预热失败: {e}")

    # W6 #22：注册主事件循环，供 progress_manager 从工作线程投递 WS 通知与延迟清理
    from src.services.progress_manager import set_main_loop
    set_main_loop(asyncio.get_running_loop())

    # LEARN W-A：启动时回载持久化的学习权重/阈值（灰度开关默认关）。
    # rag_chain 预热已创建单例策略管理器，此处按开关把 strategy_configs 中的
    # 权重与决策阈值回载到该实例；失败仅告警，不阻断启动。
    if settings.decision.LEARNING_WEIGHT_RELOAD_ENABLED:
        try:
            from src.services.strategy_manager import get_strategy_manager
            await (await get_strategy_manager()).load_from_store()
            logger.info("学习权重回载完成（LEARNING_WEIGHT_RELOAD_ENABLED=true）")
        except Exception as reload_err:
            logger.warning(f"学习权重回载失败: {reload_err}")

    # 启动后台定时学习任务
    learning_task = asyncio.create_task(_periodic_learning_task())
    logger.info("后台定时学习任务已启动")

    # 阶段一 D4：Wiki lint 周期调度（WIKI_LINT_INTERVAL_HOURS=0 时不启动）
    try:
        from src.services.wiki_lint_scheduler import start_scheduler, stop_scheduler

        if start_scheduler() is not None:
            logger.info("Wiki lint 周期调度已启动")
    except Exception as e:
        logger.warning(f"Wiki lint 周期调度启动失败（不影响主流程）: {e}")

    # P1-3：一致性巡检周期调度（CONSISTENCY_AUDIT_INTERVAL_HOURS=0 时不启动）。
    # 取代原先依赖 Windows 计划任务的做法——schtasks 在受限机器上被安全策略拦死，
    # 而「每天巡检一次」不该挂在某台机器的任务计划里。
    try:
        from src.services.consistency_scheduler import start_scheduler as start_audit

        if start_audit() is not None:
            logger.info("一致性巡检周期调度已启动")
    except Exception as e:
        logger.warning(f"一致性巡检周期调度启动失败（不影响主流程）: {e}")

    yield

    # 应用关闭时取消后台任务
    learning_task.cancel()
    try:
        await learning_task
    except asyncio.CancelledError:
        logger.info("后台定时学习任务已取消")

    # MODEL-P2.1：停止周期落库并做最后一次 flush
    if usage_flush_task is not None:
        usage_flush_task.cancel()
        try:
            await usage_flush_task
        except asyncio.CancelledError:
            logger.info("模型用量落库任务已取消")
        try:
            await flush_usage_deltas()
        except Exception as exc:
            logger.warning("关闭时模型用量落库失败: %s", exc)

    retention_task.cancel()
    try:
        await retention_task
    except asyncio.CancelledError:
        logger.info("保留期清扫任务已取消")

    # 关机前排空流式问答运行：生成已与连接解耦，进程退出不会经过任何请求的
    # finally。这里取消在途运行并等部分答案落库，否则用户看到的是「问题还在、
    # 回答凭空消失」，且没有任何日志解释
    try:
        from src.services.chat_run import get_run_registry

        # 两段式：先给 15s 让在途回答自然写完（关机不等于用户点了「停止」），
        # 再取消剩余的并等部分答案落库。预算与 uvicorn/docker 的超时见 DEPLOY 文档。
        drained_runs = await get_run_registry().drain(grace=15.0, timeout=10.0)
        if drained_runs["running"]:
            logger.info(
                "关机前流式问答收尾: 在途 %s 个（自然完成 %s / 取消后落定 %s / 强制终止 %s），保留 %s 个",
                drained_runs["running"],
                drained_runs["completed"],
                drained_runs["cancelled"],
                drained_runs["forced"],
                drained_runs["retained"],
            )
    except Exception as run_drain_err:
        log_exception(
            logger,
            run_drain_err,
            action="startup.shutdown.chat_run_drain",
            message="关机前排空流式问答运行失败，在途回答可能未落库",
        )

    # 关机前排空语义缓存失效任务：删除/下架后触发的失效是 fire-and-forget，
    # 任务还排在事件循环里就退出 = 这次删除从没清过缓存，且日志上毫无痕迹
    try:
        from src.services.semantic_cache_service import drain_invalidations

        drained_cache = await drain_invalidations(timeout=10.0)
        if drained_cache["pending"]:
            logger.info(
                "关机前语义缓存失效收尾: 待处理 %s 个、完成 %s 个、超时取消 %s 个",
                drained_cache["pending"],
                drained_cache["completed"],
                drained_cache["cancelled"],
            )
    except Exception as cache_drain_err:
        log_exception(
            logger,
            cache_drain_err,
            action="startup.shutdown.semantic_cache_drain",
            message="关机前排空语义缓存失效任务失败，已删内容的答案可能在 TTL 内继续命中",
        )

    # 关机前排空 Wiki 去抖编译队列：pending 集合与 20s 计时器都只在进程内存里，
    # 直接退出等于把「已回报加入队列」的文档永久丢掉（Wiki 缺页且无人知晓）
    try:
        from src.services.wiki_compile_scheduler import drain as drain_wiki_compile

        drained = await drain_wiki_compile(timeout=30.0)
        if drained["fired"] or drained["running"]:
            logger.info(
                "关机前 Wiki 编译收尾: 立即触发 %s 个 KB、等待 %s 个在途任务、超时取消 %s 个",
                drained["fired"],
                drained["running"],
                drained["cancelled"],
            )
    except Exception as drain_err:
        log_exception(
            logger,
            drain_err,
            action="startup.shutdown.wiki_drain",
            message="关机前排空 Wiki 编译队列失败，待编译文档将永久缺页",
        )

    try:
        from src.services.wiki_lint_scheduler import stop_scheduler

        await stop_scheduler()
    except Exception as stop_err:
        # 此前是裸 pass：lint 循环停不下来时，它会在 DB/连接池关闭后继续 tick，
        # 每分钟产生一批「连接已关闭」错误，而日志里没有任何「没停下来」的记录
        log_exception(
            logger,
            stop_err,
            action="startup.shutdown.wiki_lint_stop",
            message="Wiki lint 调度器停止失败，后台循环可能在资源关闭后继续运行",
        )

    try:
        from src.services.consistency_scheduler import stop_scheduler as stop_audit

        await stop_audit()
    except Exception as stop_err:
        log_exception(
            logger,
            stop_err,
            action="startup.shutdown.consistency_audit_stop",
            message="一致性巡检调度器停止失败，后台循环可能在资源关闭后继续运行",
        )


app = FastAPI(
    title="RAG Knowledge Base QA System API",
    description="基于LangChain的私有文档知识库问答系统API，支持多格式文档导入、智能问答、会话管理等功能",
    version="1.0.0",
    lifespan=lifespan,
    redirect_slashes=False,
)

app.add_middleware(RequestTracingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.ALLOWED_ORIGINS,
    allow_credentials=settings.cors.ALLOW_CREDENTIALS,
    allow_methods=settings.cors.ALLOW_METHODS,
    allow_headers=settings.cors.ALLOW_HEADERS,
)

app.add_middleware(MetricsMiddleware)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    logger.error(
        f"Request {request_id} failed: {exc.error_code} - {exc.detail}",
        exc_info=True
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "detail": exc.detail,
            "request_id": request_id
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    # 走到这里说明没有任何一层兜住——按 programming 记，而不是让分类器按异常类型猜：
    # 未捕获异常意味着「代码里少了一个 except」，这是需要人改代码的信号，
    # 混进 recoverable 会和「外部服务抖了一下」一样沉在 warning 里。
    # W6 #15：消息体只记异常类型——str(exc) 可能携带 SQL 片段、文件路径、
    # 内部拓扑等敏感信息；完整堆栈经 exc_info 落服务端日志供排障
    log_exception(
        logger, exc,
        action="request.unhandled",
        severity=ErrorSeverity.PROGRAMMING,
        message="未捕获异常（请求失败）",
        path=request.url.path,
        method=request.method,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "detail": "Internal Server Error",
            "request_id": request_id
        }
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": "HTTP_ERROR",
            "detail": exc.detail,
            "request_id": request_id
        },
        # 保留端点显式设置的响应头：Retry-After（限流后该等多久）与
        # WWW-Authenticate（401 的鉴权方式）原先被这里吃掉，客户端只能瞎猜。
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    request_id = getattr(request.state, "request_id", str(uuid4()))
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "VALIDATION_ERROR",
            "detail": exc.errors(),
            "request_id": request_id
        }
    )


app.include_router(document_router, prefix="/api", dependencies=[Depends(get_current_user)])
# 认证路由：login/register 匿名可用，不挂全局鉴权依赖
app.include_router(auth_router, prefix="/api")
app.include_router(trace_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(audit_router, prefix="/api", dependencies=[Depends(get_current_user)])
# 模型 Provider/Model/Route 管理：路由内已挂 require_admin，不叠加全局登录依赖
app.include_router(admin_models_router, prefix="/api")
# document_ws_router 仅包含 WebSocket 端点，认证在端点内通过 get_current_user_for_ws 处理，
# 原因同 notification_router：router 级 HTTP 依赖在 WebSocket 上下文中缺少 request 对象会失败。
app.include_router(document_ws_router, prefix="/api")
app.include_router(chat_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(session_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(category_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(tag_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(feedback_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(learning_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(experiment_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(knowledge_base_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(wiki_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(config_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(badcase_router, prefix="/api", dependencies=[Depends(get_current_user)])
app.include_router(evaluation_router, prefix="/api", dependencies=[Depends(get_current_user)])
# notification_router 仅包含 WebSocket 端点，认证在端点内通过 get_current_user_for_ws 处理，
# 避免全局 HTTP 依赖在 WebSocket 上下文中因缺少 request 对象而失败。
app.include_router(notification_router, prefix="/api")


@app.get("/")
def read_root():
    """根路径欢迎接口"""
    return {"message": "RAG Knowledge Base QA System API"}


@app.get("/health/live")
async def health_live():
    """浅探活（SEC-12）：仅表示进程可响应，不检查依赖。"""
    return {"status": "alive"}


@app.get("/health")
async def health_check():
    """兼容旧探针：等同于 /health/live。"""
    return {"status": "healthy"}


async def _collect_health_checks(*, include_ollama: bool, detailed: bool) -> dict:
    """探测 DB/Redis/Milvus/MinIO（可选 Ollama），供 ready 与 detail 共用。"""
    checks: dict = {}

    async def _record(name: str, coro_factory) -> None:
        try:
            await asyncio.wait_for(coro_factory(), timeout=3.0)
            checks[name] = {"status": "healthy"} if detailed else "ok"
        except Exception as e:
            # 错误详情仅进日志，避免向客户端泄露连接串/拓扑
            logger.error("健康检查 %s 异常: %s", name, e, exc_info=True)
            checks[name] = (
                {"status": "unhealthy", "error": f"{name} 连接失败"} if detailed else "fail"
            )

    async def _probe_db():
        async with async_engine.begin() as conn:
            await conn.run_sync(lambda c: None)

    async def _probe_redis():
        from src.services.cache_service import CacheService

        cache = await CacheService.get_instance()
        if not await cache.ping():
            raise RuntimeError("Redis ping 失败或缓存服务不可用")

    async def _probe_milvus():
        from src.services.milvus_service import MilvusService

        milvus = await MilvusService.get_instance()
        await milvus.client.has_collection(settings.milvus.MILVUS_COLLECTION_NAME)

    async def _probe_minio():
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, minio._client.bucket_exists, settings.minio.MINIO_BUCKET_NAME
        )

    async def _probe_ollama():
        import httpx

        from src.services.model_registry import _normalize_ollama_base_url

        # 同 _probe_ollama_reachable：OLLAMA_HOST 可能是不带 scheme 的 host
        host = _normalize_ollama_base_url(settings.model.OLLAMA_HOST) or "http://localhost:11434"
        async with httpx.AsyncClient(timeout=2.5) as client:
            resp = await client.get(f"{host.rstrip('/')}/api/version")
            resp.raise_for_status()

    await _record("database", _probe_db)
    await _record("redis", _probe_redis)
    await _record("milvus", _probe_milvus)
    await _record("minio", _probe_minio)
    if include_ollama:
        await _record("ollama", _probe_ollama)
    return checks


@app.get("/health/ready")
async def health_ready():
    """就绪探针（SEC-12）：任一关键依赖不可用返回 503，供容器编排使用。"""
    checks = await _collect_health_checks(include_ollama=False, detailed=False)
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )


@app.get("/health/detail")
async def health_check_detail(current_user: CurrentUser = Depends(require_admin)):
    """详细健康检查接口（管理接口：暴露依赖拓扑与错误，需管理员权限）。"""
    checks = await _collect_health_checks(include_ollama=True, detailed=True)
    overall_status = "healthy" if all(c["status"] == "healthy" for c in checks.values()) else "unhealthy"
    return {"status": overall_status, "checks": checks}


# P1-4 端到端探针：ready 刻意不含 Ollama（外部服务，挂了就重启容器会引发风暴），
# 于是「11/11 全绿但问答全废」成了一个看不见的盲区。这里补的就是它。
# 代价是一次真实 LLM 调用，所以要限流 + 只给管理员/定时任务调。
_e2e_probe_lock = asyncio.Lock()
_e2e_last_run_at: float = 0.0


async def _probe_ollama_reachable(timeout: float = 3.0) -> bool:
    """模型服务是否可达。单独探一次是为了把「模型没了」和「代码坏了」分开。

    OLLAMA_HOST 允许三种写法（`host` / `host:port` / 完整 URL），这里必须走
    `_normalize_ollama_base_url`：直接拼 `f"{host}/api/version"` 时，值为
    `host.docker.internal` 会得到没有 scheme 的地址，httpx 直接抛错 → 探针
    永远报「Ollama 不可达」。2026-11-08 实测：模型明明在跑，探针 100% 假红。
    """
    import httpx

    from src.services.model_registry import _normalize_ollama_base_url

    host = _normalize_ollama_base_url(settings.model.OLLAMA_HOST) or "http://localhost:11434"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{host.rstrip('/')}/api/version")
            resp.raise_for_status()
        return True
    except Exception:
        return False


@app.get("/health/e2e")
async def health_e2e(request: Request, current_user: CurrentUser = Depends(require_admin)):
    """端到端探针：真的问一句（意图路由 + 生成）。

    与 `/health/ready` 的分工：ready 回答「依赖连得上吗」，本端点回答「业务答得出吗」。
    与 `/health/detail` 的分工：detail 只探连接，本端点会产生一次真实 LLM 调用。

    **覆盖边界（别读成「全链路」）**：探针不指定知识库，所以不触达 Milvus——
    健康检查不该依赖某个用户的数据，库被删了不该让探针变红。向量检索那一段由
    `scripts/fault_drill.sh`（停 Milvus 看检索是否明确失败）和
    `scripts/delete_drill.sh`（删除后四处零残留）覆盖。

    返回：
    - 200 `{ok, latency_ms, answer_type, rid}`
    - 503 `{ok: false, stage, latency_ms, rid}`，stage ∈
      `ollama_unreachable` / `generation` / `timeout`；
      `retrieval` 仅在链路内部真的发生检索异常时出现（当前探针路径不会走到）

    为什么要分 stage：三种失败的处置完全不同——模型没了要去起 Ollama，
    检索异常要查 Milvus，生成异常要查提示词/模型/代码。只回一个 error 等于没回。
    """
    if not settings.health_probe.HEALTH_E2E_ENABLED:
        raise HTTPException(status_code=404, detail="端到端探针已禁用（HEALTH_E2E_ENABLED=false）")

    rid = str(getattr(request.state, "request_id", "") or uuid4())
    min_interval = max(0, int(settings.health_probe.HEALTH_E2E_MIN_INTERVAL_SECONDS))

    global _e2e_last_run_at
    async with _e2e_probe_lock:
        now = time.monotonic()
        wait = min_interval - (now - _e2e_last_run_at)
        # `_e2e_last_run_at == 0` 表示「本次进程还没跑过」：monotonic 的起点是开机时间，
        # 容器刚起来时它可能小于 min_interval，直接相减会把首次探针误判成 429。
        if _e2e_last_run_at > 0 and wait > 0:
            raise HTTPException(
                status_code=429,
                detail=f"端到端探针每 {min_interval}s 只能调一次，请 {int(wait) + 1}s 后再试",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        _e2e_last_run_at = now

        started = time.perf_counter()

        def _fail(stage: str, error: str) -> JSONResponse:
            latency_ms = int((time.perf_counter() - started) * 1000)
            logger.error(
                "端到端探针失败 stage=%s rid=%s latency_ms=%s error=%s",
                stage, rid, latency_ms, error,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "stage": stage,
                    "error": error,
                    "latency_ms": latency_ms,
                    "rid": rid,
                },
            )

        if not await _probe_ollama_reachable():
            return _fail("ollama_unreachable", "Ollama 不可达")

        try:
            from src.services.rag_chain import RAGChain

            chain = await RAGChain.get_instance()
            answer, _sources, _meta, answer_type = await asyncio.wait_for(
                chain.run(
                    settings.health_probe.HEALTH_E2E_QUESTION,
                    user_id=current_user.user_id,
                ),
                timeout=float(settings.health_probe.HEALTH_E2E_TIMEOUT_SECONDS),
            )
        except asyncio.TimeoutError:
            return _fail(
                "timeout",
                f"超过 {settings.health_probe.HEALTH_E2E_TIMEOUT_SECONDS}s 未完成",
            )
        except Exception as exc:
            # 阶段判定是启发式的：RAGChain 把检索与生成的异常都原样上抛，
            # 没有专门的阶段字段。按异常来源模块区分 Milvus（检索）与其他（生成），
            # 比统一报「内部错误」更有用；判错时的代价只是定位慢一点。
            module = type(exc).__module__ or ""
            stage = "retrieval" if "milvus" in module.lower() else "generation"
            return _fail(stage, f"{type(exc).__name__}: {str(exc)[:200]}")

        if not (answer or "").strip():
            # 空回答比异常更隐蔽：链路都通了，但用户拿到的是空白。
            return _fail("generation", "模型返回空答案")

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "端到端探针通过 rid=%s latency_ms=%s answer_type=%s",
            rid, latency_ms, answer_type,
        )
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "answer_type": answer_type,
            "rid": rid,
        }


@app.get("/metrics")
def get_metrics_endpoint(request: Request):
    """获取服务监控指标接口（支持 Prometheus 格式，W2-12）。

    鉴权：METRICS_TOKEN 非空时要求 Authorization: Bearer <token>
    （hmac 常量时间比较；Prometheus 侧经 authorization.credentials_file
    携带同一令牌，两者均由 Docker secrets 注入）。令牌为空（本地/开发/
    无 Prometheus 部署）时不鉴权，保持开箱可用。
    """
    expected = settings.security.METRICS_TOKEN
    if expected:
        provided = request.headers.get("authorization", "")
        provided_token = provided[7:] if provided.startswith("Bearer ") else ""
        if not provided_token or not hmac.compare_digest(provided_token.encode(), expected.encode()):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid metrics token",
                headers={"WWW-Authenticate": "Bearer"},
            )
    if PROMETHEUS_AVAILABLE:
        return get_prometheus_metrics()
    return get_metrics()


@app.post("/metrics/reset")
def reset_metrics_endpoint(current_user: CurrentUser = Depends(require_admin)):
    """重置监控指标接口（管理操作：需 ADMIN_KEY 或开发模式）"""
    reset_metrics()
    return {"message": "监控指标已重置"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)