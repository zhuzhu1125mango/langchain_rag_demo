"""项目配置中心。

使用 pydantic-settings 管理数据库/MinIO/Milvus/Redis/安全/CORS/模型/处理/标题生成/搜索等配置，
所有子配置统一从根目录 .env 文件读取。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")

class DatabaseSettings(BaseSettings):
    """PostgreSQL 数据库连接配置。"""

    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "langchain_rag_db"
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class MinIOSettings(BaseSettings):
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "admin"
    MINIO_SECRET_KEY: str = "password123"
    MINIO_BUCKET_NAME: str = "documents"
    MINIO_SECURE: bool = False
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class MilvusSettings(BaseSettings):
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530
    MILVUS_DATABASE: str = "default"
    MILVUS_COLLECTION_NAME: str = "documents"
    MILVUS_INDEX_NAME: str = "document_index"
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class RedisSettings(BaseSettings):
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class SecuritySettings(BaseSettings):
    SECRET_KEY: str = "your-secret-key-here-change-in-production"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class CorsSettings(BaseSettings):
    """跨域配置。"""

    ALLOWED_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:8080", "http://127.0.0.1:5173"]
    ALLOW_CREDENTIALS: bool = True
    ALLOW_METHODS: List[str] = ["*"]
    ALLOW_HEADERS: List[str] = ["*"]
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class ModelSettings(BaseSettings):
    EMBEDDING_MODEL_NAME: str = "nomic-embed-text:latest"
    OLLAMA_MODEL_NAME: str = "deepseek-r1:7b-qwen-distill-q4_K_M"
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class ProcessingSettings(BaseSettings):
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    TOP_K: int = 3
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class TitleGenerationSettings(BaseSettings):
    TITLE_GENERATION_ENABLED: bool = True
    TITLE_MAX_LENGTH: int = 30
    TITLE_FALLBACK_LENGTH: int = 30
    TITLE_GENERATION_MODEL: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class SearchSettings(BaseSettings):
    SEARCH_PROVIDER: str = "duckduckgo"
    SEARCH_API_KEY: Optional[str] = None
    SEARCH_MAX_RESULTS: int = 10
    SEARXNG_BASE_URL: Optional[str] = None
    SEARXNG_TIMEOUT: int = 10
    SEARCH_FETCH_TIMEOUT: int = 10
    SEARCH_MAX_FETCH: int = 5
    SEARCH_MIN_CONTENT_LENGTH: int = 100
    SEARCH_ENABLE_MULTI_QUERY: bool = True
    SEARCH_NUM_QUERIES: int = 3
    SEARCH_ENABLE_RERANK: bool = True
    SEARCH_RERANK_MODEL: str = "BAAI/bge-reranker-base"
    SEARCH_RERANK_TOP_K: int = 5
    SEARCH_CACHE_TTL: int = 3600
    SEARCH_CONTENT_CACHE_TTL: int = 86400

    # Phase 3：Function Calling / ReAct Agent
    SEARCH_ENABLE_FUNCTION_CALLING: bool = False
    SEARCH_ENABLE_REACT: bool = False
    SEARCH_REACT_MAX_STEPS: int = 3
    SEARCH_AGENT_FALLBACK_TO_PHASE2: bool = True

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class Settings(BaseSettings):
    """聚合所有子配置的根配置类。"""

    database: DatabaseSettings = DatabaseSettings()
    minio: MinIOSettings = MinIOSettings()
    milvus: MilvusSettings = MilvusSettings()
    redis: RedisSettings = RedisSettings()
    security: SecuritySettings = SecuritySettings()
    cors: CorsSettings = CorsSettings()
    model: ModelSettings = ModelSettings()
    processing: ProcessingSettings = ProcessingSettings()
    title_generation: TitleGenerationSettings = TitleGenerationSettings()
    search: SearchSettings = SearchSettings()
    
    IN_DOCKER: bool = False
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

settings = Settings()

DATA_DIR = os.path.join(BASE_DIR, "data")
VECTOR_DB_DIR = os.path.join(BASE_DIR, "vector_db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(VECTOR_DB_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)