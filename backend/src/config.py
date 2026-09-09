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
    # SQLAlchemy 回显 SQL 语句；生产保持 False（默认），开发可在 .env.dev 打开
    SQL_ECHO: bool = False
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class MinIOSettings(BaseSettings):
    MINIO_ENDPOINT: str = "localhost:9000"
    # 无默认凭据：缺失时由启动校验拦截（生产模式），避免弱凭据静默上线
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
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

    # BM25 sparse 词表持久化目录；留空使用 backend/src/data/bm25。
    # 词表与 collection 绑定落盘，服务重启后加载，保证历史 sparse 向量与查询编码空间一致。
    MILVUS_BM25_VOCAB_DIR: str = ""

    # 集合 schema/维度不匹配时是否允许删除重建（破坏性操作，旧向量数据全部丢失）。
    # 默认 False：不匹配时启动失败并给出明确提示，避免静默清空知识库数据。
    # 确认可接受数据丢失（或已完成迁移备份）后显式设为 true 重启以自动重建。
    MILVUS_REBUILD_ON_MISMATCH: bool = False

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class RedisSettings(BaseSettings):
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class SecuritySettings(BaseSettings):
    # 无默认值：生产模式缺失/弱值时启动失败；开发模式由启动逻辑生成临时随机密钥
    SECRET_KEY: str = ""
    # JWT access token 有效期（分钟），P1-1 多用户认证使用
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    API_KEY: Optional[str] = None  # 全局 API Key（自托管单实例认证）
    # 管理操作密钥（全局配置修改、/metrics/reset 等）。
    # 生产模式未设置时管理接口一律 403；设置后请求须携带匹配的 X-Admin-Key 头。
    ADMIN_KEY: Optional[str] = None
    # 是否开放 /api/auth/register 注册接口（关闭后仅能由已有账号或直接写库建号）
    AUTH_ALLOW_REGISTRATION: bool = True

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class CorsSettings(BaseSettings):
    """跨域配置。"""

    ALLOWED_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:8080", "http://127.0.0.1:5173"]
    ALLOW_CREDENTIALS: bool = True
    ALLOW_METHODS: List[str] = ["*"]
    ALLOW_HEADERS: List[str] = ["*"]
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class ModelSettings(BaseSettings):
    # 模型名一律由 .env 提供（无内置默认，A1 去硬编码），启动时经 /api/tags 校验存在性
    EMBEDDING_MODEL_NAME: Optional[str] = None
    # Embedding 向量维度，需与 EMBEDDING_MODEL_NAME 对应。bge-m3 为 1024，nomic-embed-text 为 768。
    EMBEDDING_DIMENSION: int = 1024
    OLLAMA_MODEL_NAME: Optional[str] = None
    # 深度思考关闭时使用的非思考专用模型（如 Qwen3-2507 指令版，从不生成思考块）。
    # 混合思考模型（如 qwen3:4b）无法通过提示词真正跳过思考；留空时回退主模型并绑定 reasoning=False。
    OLLAMA_DIRECT_MODEL_NAME: Optional[str] = None
    # 用于意图路由、标题生成、Query改写等轻量/结构化任务
    FAST_LLM_MODEL_NAME: Optional[str] = None
    # Ollama 服务地址，None 时使用 ollama 包默认行为
    OLLAMA_HOST: Optional[str] = None
    # 模型是否支持思考模式（qwen3、deepseek-r1 等思考类模型设 true；qwen2.5 等设 false）。
    # 设 false 时不向模型传递 think/reasoning 参数（否则触发 Ollama 400 错误）。
    OLLAMA_SUPPORTS_THINKING: bool = True
    # Ollama 上下文窗口（num_ctx）。RAG 单次请求 = 检索片段 + 历史对话 + 回答预留，
    # Ollama 默认约 4096 易截断检索内容；4GB 显存 + q8_0 KV cache 下 6144 为稳妥值。
    OLLAMA_NUM_CTX: int = 6144
    
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class ProcessingSettings(BaseSettings):
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 50
    TOP_K: int = 3

    # 单文件上传大小上限（MB）。服务端按实际接收字节数校验，不信任客户端声明。
    MAX_UPLOAD_SIZE_MB: int = 100

    # 混合检索相关配置
    KB_ENABLE_HYBRID_SEARCH: bool = True
    KB_HYBRID_SEARCH_TOP_K: int = 20
    KB_HYBRID_RERANK_TOP_K: int = 5
    KB_RRF_K: int = 60
    KB_RERANK_MIN_SCORE: float = 0.0  # 重排序最低置信度，低于此值的结果将被过滤
    # 是否启用知识库重排序（C1）：关闭或未配置模型时按 RRF 分数直接截断，不加载 reranker
    KB_RERANK_ENABLED: bool = True
    # 知识库重排序模型及加载方式：sentence_transformers | ollama
    # 留空/None 时自动关闭 rerank（按 RRF 截断），不隐式拉取用户未声明的模型
    KB_RERANK_MODEL: Optional[str] = None
    KB_RERANK_PROVIDER: str = "ollama"
    # rerank 分数相关性阈值（C8）：所有检索结果分数均低于此值时判定"无高度相关内容"
    KB_RELEVANCE_SCORE_THRESHOLD: float = 0.3

    # 上下文构建相关配置
    CONTEXT_TOKEN_BUDGET: int = 4000
    CONTEXT_COMPRESSION_ENABLED: bool = False
    # 指代消解 LLM 兜底开关（C3）：false 时仅做规则前置（问题不含指代词零 LLM 调用），
    # 历史上下文仍随提示词携带；true 时含指代词的问题走 LLM 消解
    CONTEXT_RESOLVE_USE_LLM: bool = False

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
    # LLM 路由超时时间（秒），超时自动降级到规则路由（C6：3.0→5.0，为冷启动留余量）
    INTENT_ROUTER_LLM_TIMEOUT: float = 5.0
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


class OcrSettings(BaseSettings):
    """OCR 深度解析配置（P0-2b：扫描版 PDF 走 OCR 分流管线）。

    两个后端均为可选依赖（uv group: ocr-mineru / ocr-paddle），懒导入；
    未安装或解析失败时由 load_document 回退内置 PyPDF 解析，不阻断上传。
    """

    # 是否启用扫描版 PDF 的 OCR 深度解析；关闭即整体回退旧解析管线（回退开关）
    DEEP_PARSING_ENABLED: bool = True
    # OCR 后端：auto（按已安装后端自动选择）| mineru | paddle
    OCR_BACKEND: str = "auto"
    # 推理设备：auto | cpu | cuda（paddle 后端将 cuda 映射为 gpu:0）
    OCR_DEVICE: str = "auto"
    # 扫描版判定阈值：平均每页可抽取字符数低于该值视为扫描版
    OCR_SCANNED_CHAR_THRESHOLD: int = 10
    # 扫描版判定采样的最大页数（大文件只读前 N 页）
    OCR_DETECT_MAX_PAGES: int = 20
    # mineru 子进程超时（秒）；paddle 为进程内调用，不受此项控制
    MINERU_TIMEOUT_SECONDS: int = 600
    # 模型下载源：modelscope | huggingface（离线部署先跑 scripts/download_ocr_models.py 预热缓存）
    MINERU_MODEL_SOURCE: str = "modelscope"
    PADDLE_MODEL_SOURCE: str = "modelscope"

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
    # 留空/None 时自动关闭搜索结果重排（按原文顺序截断），不隐式拉取用户未声明的模型
    SEARCH_RERANK_MODEL: Optional[str] = None
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

class DecisionSettings(BaseSettings):
    """问答决策与策略投票配置。"""

    # 用户显式勾选知识库时是否仍走多策略投票（C5）。
    # false：直接检索知识库（回答模板已保证结合模型自身知识），跳过投票以降低首字延迟。
    DECISION_VOTE_WHEN_KB_SELECTED: bool = False
    # 语义相似度策略开关（C8）：默认关闭——该策略依赖 sentence-transformers MiniLM，
    # 离线环境加载失败时会永久弃权，关闭后投票权重自然归一到其余策略
    STRATEGY_SEMANTIC_ENABLED: bool = False
    # 策略投票 LLM 调用超时（秒，C4）：超时按弃权处理（低置信度），不阻塞决策
    STRATEGY_LLM_TIMEOUT: float = 5.0

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")


class SemanticCacheSettings(BaseSettings):
    """语义缓存配置（P1-3）。仅纯知识库问答参与读写，Redis 不可用时 fail-open。"""

    # 总开关（false 时查找/写入全部跳过）
    SEMANTIC_CACHE_ENABLED: bool = True
    # 语义命中相似度阈值（bge-m3 余弦）
    SEMANTIC_CACHE_SIMILARITY_THRESHOLD: float = 0.92
    # 条目过期时间（小时），读取时惰性过滤
    SEMANTIC_CACHE_TTL_HOURS: int = 24
    # 单缓存范围（user + kb 范围）条目上限，超限删最旧
    SEMANTIC_CACHE_MAX_ENTRIES: int = 200
    # 命中回放切片长度（字符）
    SEMANTIC_CACHE_REPLAY_CHUNK_CHARS: int = 120

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

class WikiCompileSettings(BaseSettings):
    """LLM-Wiki 编译层配置（P2 前置编译增强，Phase 1）。默认全关，零风险合入。"""

    # 总开关（false 时文档摄入管线跳过编译阶段）
    WIKI_COMPILE_ENABLED: bool = False
    # 编译模型名，空 = 复用主模型（OLLAMA_MODEL_NAME）
    WIKI_COMPILE_MODEL: str = ""
    # 单文档触发的页面更新数上限（控 token 成本）
    WIKI_COMPILE_MAX_PAGES_PER_DOC: int = 10
    # 单文档编译总超时（秒），超时仅放弃编译，不阻断上传
    WIKI_COMPILE_TIMEOUT_SECONDS: int = 300
    # WiCER 式诊断探针（事实保留率自检，仅日志，不影响编译产物）
    WIKI_DIAGNOSTIC_PROBES: bool = False

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
    semantic_cache: SemanticCacheSettings = SemanticCacheSettings()
    wiki_compile: WikiCompileSettings = WikiCompileSettings()
    decision: DecisionSettings = DecisionSettings()
    evaluation: EvaluationSettings = EvaluationSettings()
    ocr: OcrSettings = OcrSettings()

    IN_DOCKER: bool = False
    # 显式环境标记：dev | production。未设置时按 IN_DOCKER 推断。
    # 非 Docker 的生产部署（裸跑 uvicorn/systemd）必须设置 APP_ENV=production，
    # 否则启动时不执行强密钥/凭据校验。
    APP_ENV: Optional[str] = None
    # 任务级模型角色映射（可选），key 为任务名，value 为 settings.model 中的字段名
    MODEL_TASK_ROLES: Optional[Dict[str, str]] = None

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    @property
    def IS_PRODUCTION(self) -> bool:
        """是否以生产级标准运行（决定启动时是否强制安全校验）。"""
        if self.APP_ENV:
            return self.APP_ENV == "production"
        return self.IN_DOCKER

settings = Settings()

DATA_DIR = os.path.join(BASE_DIR, "data")
VECTOR_DB_DIR = os.path.join(BASE_DIR, "vector_db")
LOG_DIR = os.path.join(BASE_DIR, "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(VECTOR_DB_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)