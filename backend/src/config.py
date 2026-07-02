"""项目配置中心。

使用 pydantic-settings 管理数据库/MinIO/Milvus/Redis/安全/CORS/模型/处理/标题生成/搜索等配置，
所有子配置统一从根目录 .env 文件读取。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional, List, Dict
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

    # HNSW 索引参数（知识库数据量增大后适当提升以平衡召回与性能）
    MILVUS_HNSW_M: int = 16
    MILVUS_EF_CONSTRUCTION: int = 128
    MILVUS_EF: int = 128

    # Flush 限速与退避配置：Milvus 默认对单集合 flush 限流 0.1 QPS，
    # 客户端通过令牌桶限速 + 指数退避避免触发服务端限流。
    # 注意：aiolimiter.AsyncLimiter(max_rate, time_period) 中 max_rate 既是速率也是最大容量，
    # 因此配置为 0.15 时 async with（默认获取 1 容量）会触发 ValueError。
    # 代码中将其转换为 max_rate=1, time_period=1/MILVUS_FLUSH_RATE_LIMIT 使用。
    MILVUS_FLUSH_RATE_LIMIT: float = 0.15        # 每秒允许的最大 flush 次数（需 <= 服务端 0.1 的倒数窗口）
    MILVUS_FLUSH_MAX_RETRY: int = 3              # rate limit 触发后的最大重试次数
    MILVUS_FLUSH_BASE_WAIT: float = 1.5          # 首次退避等待秒数

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
    API_KEY: Optional[str] = None  # 全局 API Key（自托管单实例认证）

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class CorsSettings(BaseSettings):
    """跨域配置。"""

    ALLOWED_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:8080", "http://127.0.0.1:5173"]
    ALLOW_CREDENTIALS: bool = True
    ALLOW_METHODS: List[str] = ["*"]
    ALLOW_HEADERS: List[str] = ["*"]
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class ModelSettings(BaseSettings):
    EMBEDDING_MODEL_NAME: str = "bge-m3:latest"
    # Embedding 向量维度，需与 EMBEDDING_MODEL_NAME 对应。bge-m3 为 1024，nomic-embed-text 为 768。
    EMBEDDING_DIMENSION: int = 1024
    OLLAMA_MODEL_NAME: str = "deepseek-r1:7b-qwen-distill-q4_K_M"
    # 用于意图路由、标题生成、Query改写等轻量/结构化任务
    FAST_LLM_MODEL_NAME: str = "qwen2.5:7b"
    # Ollama 服务地址，None 时使用 ollama 包默认行为
    OLLAMA_HOST: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class ProcessingSettings(BaseSettings):
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    TOP_K: int = 3

    # 混合检索相关配置
    KB_ENABLE_HYBRID_SEARCH: bool = True
    KB_HYBRID_SEARCH_TOP_K: int = 20
    KB_HYBRID_RERANK_TOP_K: int = 5
    KB_RRF_K: int = 60
    KB_RERANK_MIN_SCORE: float = 0.0  # 重排序最低置信度，低于此值的结果将被过滤
    # 知识库重排序模型及加载方式：sentence_transformers | ollama
    KB_RERANK_MODEL: str = "qllama/bge-reranker-v2-m3:latest"
    KB_RERANK_PROVIDER: str = "ollama"

    # 上下文构建相关配置
    CONTEXT_TOKEN_BUDGET: int = 4000
    CONTEXT_COMPRESSION_ENABLED: bool = False

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class TitleGenerationSettings(BaseSettings):
    TITLE_GENERATION_ENABLED: bool = True
    TITLE_MAX_LENGTH: int = 30
    TITLE_FALLBACK_LENGTH: int = 30
    # 覆盖默认模型（None 则使用 settings.model.FAST_LLM_MODEL_NAME）
    TITLE_GENERATION_MODEL: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class IntentRouterSettings(BaseSettings):
    """意图路由配置。"""

    # 是否启用 LLM 语义路由层
    INTENT_ROUTER_USE_LLM: bool = True
    # LLM 路由超时时间（秒），超时自动降级到规则路由
    INTENT_ROUTER_LLM_TIMEOUT: float = 3.0
    # 最低置信度阈值，低于此值走 DIRECT_LLM
    INTENT_ROUTER_CONFIDENCE_THRESHOLD: float = 0.4
    # 两意图最高分差小于此值时触发澄清
    INTENT_ROUTER_AMBIGUITY_GAP: float = 0.15
    # 覆盖默认模型（None 则使用 settings.model.FAST_LLM_MODEL_NAME）
    INTENT_ROUTER_LLM_MODEL: Optional[str] = None
    # LLM 路由参考的最大历史轮数
    INTENT_ROUTER_MAX_HISTORY_TURNS: int = 5
    # 是否启用规则冲突检测，冲突时转 LLM 裁决
    INTENT_ROUTER_RULE_CONFLICT_DETECTION: bool = True
    # 工具推荐时所需实体的最低完整度（0~1），低于此值从 suggested_tools 中移除
    INTENT_ROUTER_TOOL_ENTITY_THRESHOLD: float = 0.5
    # 是否启用 Embedding 意图分类层
    INTENT_ROUTER_USE_EMBEDDING: bool = True
    # Embedding 意图分类相似度阈值，低于此值退化为 LLM 路由
    INTENT_ROUTER_EMBEDDING_THRESHOLD: float = 0.55
    # Embedding 分类歧义阈值，最高与次高分差小于此值触发澄清
    INTENT_ROUTER_EMBEDDING_AMBIGUITY_GAP: float = 0.08

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class EvaluationSettings(BaseSettings):
    """检索与生成评估配置。"""

    # 是否启用 RAG 链路自动评估
    EVALUATION_ENABLED: bool = True
    # 检索评估 embedding 相似度阈值
    EVALUATION_RETRIEVAL_SIMILARITY_THRESHOLD: float = 0.6
    # 生成评估使用的任务角色（覆盖默认 fast 模型）
    EVALUATION_JUDGE_MODEL: Optional[str] = None
    # 自动评估时 faithfulness 低分阈值
    EVALUATION_FAITHFULNESS_LOW: float = 0.4
    # 自动评估时 relevance 低分阈值
    EVALUATION_RELEVANCE_LOW: float = 0.4

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")


class SearchSettings(BaseSettings):
    SEARCH_PROVIDER: str = "searxng"
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
    SEARCH_RERANK_MODEL: str = "qllama/bge-reranker-v2-m3:latest"
    SEARCH_RERANK_PROVIDER: str = "ollama"  # sentence_transformers | ollama
    SEARCH_RERANK_TOP_K: int = 5
    # 注入 LLM 上下文的最终搜索结果条数（与 SEARCH_MAX_RESULTS 区分：
    # 后者是向搜索引擎请求的条数，前者是经重排后实际喂给模型的条数）
    SEARCH_CONTEXT_RESULTS: int = 5
    SEARCH_CACHE_TTL: int = 3600
    SEARCH_CONTENT_CACHE_TTL: int = 86400

    # Phase 3：Function Calling / ReAct Agent
    SEARCH_ENABLE_FUNCTION_CALLING: bool = True
    SEARCH_ENABLE_REACT: bool = False
    SEARCH_REACT_MAX_STEPS: int = 3
    SEARCH_AGENT_FALLBACK_TO_PHASE2: bool = True

    # 引用补全
    CITATION_MATCH_THRESHOLD: float = 0.65       # embedding 相似度阈值
    CITATION_ENABLE_EMBEDDING: bool = True        # 是否用 embedding 匹配（False 退化为关键词）

    # 答案校验
    ANSWER_VERIFIER_LLM_THRESHOLD: int = 200      # 答案字数超过此值才触发 LLM 复核
    ANSWER_CONFIDENCE_WARNING: float = 0.7        # 正常阈值
    ANSWER_CONFIDENCE_LOW: float = 0.4            # 低置信度阈值

    # Query 改写
    QUERY_REWRITE_MODEL: Optional[str] = None     # None 则使用 settings.model.FAST_LLM_MODEL_NAME
    QUERY_REWRITE_MAX_QUERIES: int = 4            # 改写后最多保留的 query 数
    QUERY_REWRITE_CONTEXT_TURNS: int = 3          # 上下文补全回溯轮数

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
    intent_router: IntentRouterSettings = IntentRouterSettings()
    search: SearchSettings = SearchSettings()
    evaluation: EvaluationSettings = EvaluationSettings()

    IN_DOCKER: bool = False
    # 任务级模型角色映射（可选），key 为任务名，value 为 settings.model 中的字段名
    MODEL_TASK_ROLES: Optional[Dict[str, str]] = None
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

settings = Settings()

DATA_DIR = os.path.join(BASE_DIR, "data")
VECTOR_DB_DIR = os.path.join(BASE_DIR, "vector_db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(VECTOR_DB_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)