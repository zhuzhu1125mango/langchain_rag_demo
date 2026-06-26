from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
from src.config import settings

SQLALCHEMY_ASYNC_DATABASE_URL = (
    f"postgresql+asyncpg://{settings.database.POSTGRES_USER}:"
    f"{settings.database.POSTGRES_PASSWORD}@"
    f"{settings.database.POSTGRES_HOST}:"
    f"{settings.database.POSTGRES_PORT}/"
    f"{settings.database.POSTGRES_DB}"
)

async_engine = create_async_engine(
    SQLALCHEMY_ASYNC_DATABASE_URL,
    echo=True,
    pool_size=20,
    max_overflow=50,
    pool_timeout=30,
    pool_recycle=3600,
)

AsyncSessionLocal = sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# 导出async_session_maker供后台任务使用
async_session_maker = AsyncSessionLocal

Base = declarative_base()

async def get_db():
    """FastAPI 依赖注入的异步数据库会话生成器。"""
    async with AsyncSessionLocal() as session:
        yield session

async def init_db():
    """初始化数据库表结构。"""
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@asynccontextmanager
async def async_session():
    """异步上下文管理器，供后台任务获取独立会话。"""
    session = AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()