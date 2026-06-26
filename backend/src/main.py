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
import time
import asyncio
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from src.database import async_engine, Base, init_db
from src.config import settings, LOG_DIR
from src.exceptions import AppException
from src.api import (
    document_router,
    chat_router,
    session_router,
    category_router,
    tag_router,
    feedback_router,
    learning_router,
    experiment_router,
    knowledge_base_router,
    config_router,
    notification_router
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
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "app.log")), logging.StreamHandler()]
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
            logger.info(f"Request started: {request.method} {request.url} [{request_id}]")
            
            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    duration = time.time() - start_time
                    status_code = message["status"]
                    logger.info(
                        f"Request completed: {request.method} {request.url} [{request_id}] "
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
            strategy_manager = create_default_strategy_manager()
            result = await learning_engine.check_and_trigger_learning(strategy_manager)
            if result.get("status") != "skipped":
                logger.info(f"自动学习触发成功: {result}")
            else:
                logger.debug(f"自动学习跳过: {result.get('reason')}")
        except Exception as e:
            logger.error(f"自动学习任务异常: {e}", exc_info=True)

        # 每小时检查一次是否需要学习
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI生命周期管理函数

    在应用启动时自动创建所有数据库表
    在应用关闭时无需特殊清理操作
    """
    # 生产环境安全检查
    _default_secret = "your-secret-key-here-change-in-production"
    if settings.IN_DOCKER:
        _secret = settings.security.SECRET_KEY
        if not _secret or _secret == _default_secret or len(_secret) < 16:
            raise RuntimeError("SECRET_KEY 必须在 Docker 生产环境中设置为不少于 16 字符的强随机值")

    # 关键凭据非空校验（Docker 环境）
    if settings.IN_DOCKER:
        _missing_creds = []
        if not settings.database.POSTGRES_PASSWORD:
            _missing_creds.append("POSTGRES_PASSWORD")
        if not settings.minio.MINIO_SECRET_KEY:
            _missing_creds.append("MINIO_SECRET_KEY")
        if _missing_creds:
            _msg = f"生产环境启动失败: 关键凭据为空: {', '.join(_missing_creds)}，请在 .env.prod 中设置强密码"
            logger.error(_msg)
            raise RuntimeError(_msg)

    await init_db()
    logger.info("Database tables created successfully")

    # 预热核心异步服务，避免首次请求阻塞事件循环
    try:
        from src.services.vector_store import VectorStoreManager
        from src.services.rag_chain import RAGChain
        from src.services.cache_service import CacheService

        await CacheService.get_instance()
        vector_store = await VectorStoreManager.get_instance()
        await RAGChain.get_instance(vector_store)
        logger.info("核心异步服务预热完成")
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
    lifespan=lifespan
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


app.include_router(document_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(session_router, prefix="/api")
app.include_router(category_router, prefix="/api")
app.include_router(tag_router, prefix="/api")
app.include_router(feedback_router, prefix="/api")
app.include_router(learning_router, prefix="/api")
app.include_router(experiment_router, prefix="/api")
app.include_router(knowledge_base_router, prefix="/api")
app.include_router(config_router, prefix="/api")
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
        checks["database"] = {"status": "unhealthy", "error": str(e)}
    
    try:
        from src.services.cache_service import CacheService
        cache = await CacheService.get_instance()
        await cache.ping()
        checks["redis"] = {"status": "healthy"}
    except Exception as e:
        checks["redis"] = {"status": "unhealthy", "error": str(e)}
    
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
def reset_metrics_endpoint():
    """重置监控指标接口"""
    reset_metrics()
    return {"message": "监控指标已重置"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)