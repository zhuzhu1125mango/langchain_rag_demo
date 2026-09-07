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
import time
import asyncio
from logging.handlers import RotatingFileHandler
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from src.database import async_engine, Base, init_db
from src.config import settings, LOG_DIR
from src.exceptions import AppException
from src.utils.security import validate_secret_key, SecretKeyValidationError
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
    config_router,
    notification_router,
    badcase_router,
    evaluation_router,
    auth_router,
    trace_router,
)
from src.middleware.metrics import MetricsMiddleware, get_metrics, reset_metrics

try:
    from src.middleware.prometheus import get_prometheus_metrics
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        # 根日志轮转：单文件 10MB、保留 5 份，与 start.py 的 rag_system.log 策略一致
        RotatingFileHandler(
            os.path.join(LOG_DIR, "app.log"),
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ]
)
logger = logging.getLogger("rag_system")


class RequestTracingMiddleware:
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        request_id = str(uuid4())
        
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
    from src.services.strategy_manager import create_default_strategy_manager

    # 等待系统启动完成
    await asyncio.sleep(30)

    while True:
        try:
            strategy_manager = await create_default_strategy_manager()
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
    """
    import ollama

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI生命周期管理函数

    在应用启动时自动创建所有数据库表
    在应用关闭时无需特殊清理操作
    """
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
    else:
        # 开发模式兜底：未设置 SECRET_KEY 时生成临时随机密钥（重启后签名失效）
        if not settings.security.SECRET_KEY:
            settings.security.SECRET_KEY = secrets.token_urlsafe(48)
            logger.warning(
                "SECRET_KEY 未设置，已生成临时随机密钥（仅限开发环境，重启后旧签名失效）；"
                "生产部署必须设置强密钥或 APP_ENV=production"
            )

    # 模型配置校验（A2）：模型名必填且存在于本地 Ollama，缺失即启动失败
    await _validate_model_config()

    await init_db()
    logger.info("Database tables created successfully")

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

        # 预热 LLM，触发 Ollama 将模型加载到内存，避免首个用户请求阻塞 20~40 秒。
        # 该步骤为最佳努力（best-effort），失败仅记录警告，不中断启动。
        try:
            if rag_chain.llm is not None:
                await asyncio.wait_for(rag_chain.llm.ainvoke("hello"), timeout=60.0)
                logger.info("LLM 预热完成")
        except Exception as e:
            logger.warning(f"LLM 预热失败，首次调用可能较慢: {e}")
    except Exception as e:
        logger.error(f"核心异步服务预热失败: {e}", exc_info=True)
        raise RuntimeError(f"核心异步服务预热失败: {e}")

    # 启动后台定时学习任务
    learning_task = asyncio.create_task(_periodic_learning_task())
    logger.info("后台定时学习任务已启动")

    yield

    # 应用关闭时取消后台任务
    learning_task.cancel()
    try:
        await learning_task
    except asyncio.CancelledError:
        logger.info("后台定时学习任务已取消")


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
    logger.error(
        f"Request {request_id} failed with unexpected error: {str(exc)}",
        exc_info=True
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
        }
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


@app.get("/health")
async def health_check():
    """健康检查接口，用于服务状态监控"""
    return {"status": "healthy"}


@app.get("/health/detail")
async def health_check_detail():
    """详细健康检查接口，检查各依赖服务状态"""
    checks = {}
    
    try:
        async with async_engine.begin() as conn:
            await conn.run_sync(lambda c: None)
        checks["database"] = {"status": "healthy"}
    except Exception as e:
        # 详细错误仅进日志，避免向客户端泄露连接串/拓扑信息
        logger.error(f"健康检查数据库异常: {e}", exc_info=True)
        checks["database"] = {"status": "unhealthy", "error": "数据库连接失败"}

    try:
        from src.services.cache_service import CacheService
        cache = await CacheService.get_instance()
        if await cache.ping():
            checks["redis"] = {"status": "healthy"}
        else:
            checks["redis"] = {"status": "unhealthy", "error": "Redis ping 失败或缓存服务不可用"}
    except Exception as e:
        logger.error(f"健康检查 Redis 异常: {e}", exc_info=True)
        checks["redis"] = {"status": "unhealthy", "error": "Redis 连接失败"}
    
    overall_status = "healthy" if all(c["status"] == "healthy" for c in checks.values()) else "unhealthy"
    
    return {
        "status": overall_status,
        "checks": checks
    }


@app.get("/metrics")
def get_metrics_endpoint():
    """获取服务监控指标接口（支持 Prometheus 格式）"""
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