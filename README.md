# 📚 LangChain RAG 知识库问答系统

基于 **LangChain + Ollama + Milvus** 构建的企业级私有文档 RAG（Retrieval-Augmented Generation）知识库问答系统，支持完全离线运行，数据安全可控。

---

## ✨ 功能特性

| 功能模块 | 特性描述 | 状态 |
|---------|---------|------|
| 📁 **多格式文档支持** | TXT, PDF, DOCX, XLSX, PPTX, MD, CSV, JSON, HTML, EPUB | ✅ |
| 🔍 **智能检索** | 基于向量数据库的语义检索，支持策略模式融合 | ✅ |
| 🌐 **联网搜索** | 支持 DuckDuckGo / SearXNG / Tavily，Query 改写 + 重排 + Redis 缓存 | ✅ |
| 🤖 **搜索 Agent** | Function Calling / ReAct Agent，支持多步推理与网页抓取 | ✅ |
| 💬 **流式输出** | 实时打字效果，提升用户体验 | ✅ |
| 🏷️ **智能会话命名** | 首条消息发送后自动调用 LLM 生成会话标题 | ✅ |
| 🧹 **问候过滤** | 你好 / hi / 在吗 等日常对话直接走 LLM，跳过知识库检索 | ✅ |
| 🔗 **来源展示** | 前端同时展示知识库片段与网页搜索来源（标题/URL） | ✅ |
| 🔒 **完全离线** | 使用本地 Ollama 模型，无需付费 API | ✅ |
| 🖥️ **界面支持** | Vue3 生产级界面 | ✅ |
| 📊 **实验框架** | 支持 A/B 测试和策略权重调整 | ✅ |
| 🧠 **动态学习** | 基于用户反馈的规则学习引擎 | ✅ |
| 📈 **监控告警** | 集成 Prometheus + Grafana 监控体系 | ✅ |
| 🏷️ **标签分类** | 文档分类和标签管理 | ✅ |
| ⭐ **评价反馈** | 用户评价收集和反馈学习 | ✅ |
| ⚙️ **系统配置** | 支持在线调整分块、检索、模型等处理配置 | ✅ |
| 🔔 **实时通知** | WebSocket 实时推送知识库/文档/上传任务变更 | ✅ |

---

## 🛠️ 技术栈

### 核心技术

| 分类 | 技术 | 版本 | 说明 |
|------|------|------|------|
| 后端框架 | FastAPI | ^0.115.0 | 现代高性能 API 框架 |
| ORM | SQLAlchemy | ^2.0.34 | 异步数据库操作 |
| 向量数据库 | Milvus | ^2.6.17 | 分布式向量存储 |
| LLM 集成 | Ollama | ^0.2.10 | 本地模型运行 |
| 嵌入模型 | bge-m3 | latest | 离线多语言向量嵌入，1024 维 |
| 对象存储 | MinIO | latest | 分布式对象存储 |
| 前端框架 | Vue 3 | ^3.5.35 | 现代前端框架 |
| 状态管理 | Pinia | ^3.0.4 | 状态管理 |
| 数据缓存 | Vue Query | ^5.100.14 | 客户端数据缓存 |
| UI 组件 | Element Plus | ^2.14.1 | 企业级组件库 |
| 缓存服务 | Redis | ^7.0 | 搜索结果与网页内容缓存 |
| 监控系统 | Prometheus | ^3.5.0 | 监控指标采集 |
| 可视化 | Grafana | ^12.0.2 | 监控仪表盘 |

### 支持的文档格式

| 类别 | 格式 | 说明 |
|------|------|------|
| 文本文档 | TXT, MD, MARKDOWN | 纯文本文件 |
| PDF | PDF | 便携式文档格式 |
| Word | DOCX, DOC | Microsoft Word |
| Excel | XLSX, XLS | Microsoft Excel |
| PowerPoint | PPTX, PPT | Microsoft PowerPoint |
| 数据文件 | CSV, JSON | 结构化数据 |
| 网页 | HTML, HTM | 网页文件 |
| 电子书 | EPUB | 电子出版物 |

---

## 🧠 模型分工

系统默认在 Ollama 本地部署多个模型，按任务特点分工协作，降低主模型负载并提升响应速度：

| 任务 | 默认模型 | 说明 |
|------|---------|------|
| 主生成模型 | `deepseek-r1:7b-qwen-distill-q4_K_M` | 复杂推理、RAG 最终答案生成、Agent / Function Calling |
| 轻量任务模型 | `qwen2.5:7b` | 意图路由、会话标题生成、Query 改写等结构化/低延迟任务 |
| Embedding 模型 | `bge-m3:latest` | 多语言文档向量化，输出 1024 维稠密向量 |
| 重排序模型 | `qllama/bge-reranker-v2-m3:latest` | 知识库检索与网页搜索结果精排，通过 Ollama 本地调用 |

通过环境变量可灵活调整：

```env
OLLAMA_MODEL_NAME=deepseek-r1:7b-qwen-distill-q4_K_M
FAST_LLM_MODEL_NAME=qwen2.5:7b
EMBEDDING_MODEL_NAME=bge-m3:latest
EMBEDDING_DIMENSION=1024
KB_RERANK_MODEL=qllama/bge-reranker-v2-m3:latest
SEARCH_RERANK_MODEL=qllama/bge-reranker-v2-m3:latest
```

> ⚠️ **Embedding 维度变更注意**：默认 Embedding 从 `nomic-embed-text`（768 维）切换为 `bge-m3`（1024 维）。现有 Milvus 集合会在服务启动时自动检测维度不一致并重建，旧知识库数据将丢失。请在启动前运行 `cd backend && uv run python scripts/migrate_embedding_model.py --backup` 备份并重新上传文档。

---

## 🚀 快速开始

### 环境分离说明

项目采用**开发/生产环境分离**策略，通过独立的 Docker Compose 配置文件和环境变量实现环境隔离。

| 环境 | Docker 配置 | 数据卷 | 容器后缀 | 适用场景 |
|------|-----------|--------|---------|---------|
| **开发环境** | `docker-compose.dev.yml` | `*_dev` | `-dev` | 本地开发、测试、调试 |
| **生产环境** | `docker-compose.yml` | `*_prod` | `-prod` | 正式部署 |

**核心特性：**
- ✅ 开发/生产数据完全隔离
- ✅ 开发环境包含完整应用栈（后端 + 前端 + 基础设施）
- ✅ 开发环境支持代码热重载
- ✅ 生产环境包含完整监控体系（含 Alertmanager）

---

### 方式一：开发环境快速启动（推荐开发）

使用 `scripts/start-dev.ps1`（Windows PowerShell 7）、`scripts/start-dev.bat`（Windows Batch）或 `scripts/start-dev.sh`（Linux/Mac）一键启动开发环境，**包含完整应用栈**。推荐在 Windows 上使用 PowerShell 7 获得最佳彩色输出体验。

**一键启动：**
```bash
# 进入项目目录
cd langchain_rag_demo

# Windows (PowerShell 7，推荐)：一键启动开发环境
.\scripts\start-dev.ps1

# Windows (Batch)：一键启动开发环境（自动检测 PowerShell 7）
.\scripts\start-dev.bat

# Linux/Mac：一键启动开发环境
./scripts/start-dev.sh

# 可选：跳过健康检查等待
# Windows PowerShell 7: .\scripts\start-dev.ps1 -SkipWait
# Windows Batch: set SKIP_WAIT=1 && .\scripts\start-dev.bat
# Linux/Mac: SKIP_WAIT=1 ./scripts/start-dev.sh
```

**脚本功能特性：**
- ✅ 自动检测 Docker / Docker Compose 安装状态
- ✅ 自动检查 `.env.dev` 配置文件
- ✅ 自动备份旧的 `.env` 配置
- ✅ 启动后自动进行容器健康检查（最多 120s）
- ✅ 彩色进度日志，直观展示每个服务状态
- ✅ 启动完成后汇总显示所有服务访问地址

**开发环境服务：**
- 后端 API（FastAPI，源码挂载，支持热重载）
- 前端界面（Vite 开发服务器，源码挂载）
- PostgreSQL（数据库）
- MinIO（对象存储）
- Milvus（向量数据库）
- etcd（Milvus 依赖）
- Redis（缓存服务）
- SearXNG（私有化聚合搜索引擎）
- Prometheus（监控）
- Grafana（监控面板）
- PostgreSQL Exporter（数据库监控导出器）

**访问地址：**
- 后端 API: `http://localhost:8000`
- API 文档: `http://localhost:8000/docs`
- 前端界面: `http://localhost:5173`
- PostgreSQL: `localhost:5433`
- MinIO 控制台: `http://localhost:9001`
- Milvus: `localhost:19530`
- Redis: `localhost:6379`
- SearXNG: `http://localhost:8080`
- Prometheus: `http://localhost:9090`
- Grafana: `http://localhost:3000`

**查看后端日志（观察热重载输出）：**
```bash
# Windows PowerShell 7
.\scripts\logs-dev.ps1 backend
# Windows Batch
.\scripts\logs-dev.bat backend
# Linux/Mac
./scripts/logs-dev.sh backend
# 或直接查看
docker logs -f backend-dev
```

**查看前端日志（观察 Vite 输出）：**
```bash
# Windows PowerShell 7
.\scripts\logs-dev.ps1 frontend
# Windows Batch
.\scripts\logs-dev.bat frontend
# Linux/Mac
./scripts/logs-dev.sh frontend
# 或直接查看
docker logs -f frontend-dev
```

**查看所有服务实时日志：**
```bash
# Windows PowerShell 7
.\scripts\logs-dev.ps1
# Windows Batch
.\scripts\logs-dev.bat
# Linux/Mac
./scripts/logs-dev.sh
```

**停止开发环境：**
```bash
# Windows PowerShell 7
.\scripts\stop-dev.ps1
# Windows Batch
.\scripts\stop-dev.bat
# Linux/Mac
./scripts/stop-dev.sh
```

**清理开发环境（包括数据）：**
```bash
# Windows PowerShell 7
.\scripts\stop-dev.ps1 -Clean
# Windows Batch
.\scripts\stop-dev.bat --clean
# Linux/Mac
./scripts/stop-dev.sh --clean
```

---

### 方式二：生产环境部署（推荐部署）

使用 `scripts/start-prod.ps1`（Windows PowerShell 7）、`scripts/start-prod.bat`（Windows Batch）或 `scripts/start-prod.sh`（Linux/Mac）启动完整生产环境，包含所有服务。推荐在 Windows 上使用 PowerShell 7 获得最佳彩色输出体验。

**启动前准备：**
1. 创建并配置 `.env.prod` 文件
2. 务必为所有密码项设置强密码（POSTGRES_PASSWORD / MINIO_ROOT_PASSWORD / MINIO_ACCESS_KEY / MINIO_SECRET_KEY / GF_SECURITY_ADMIN_PASSWORD）

**一键启动：**
```bash
# 进入项目目录
cd langchain_rag_demo

# Windows (PowerShell 7，推荐)：启动生产环境（会提示确认）
.\scripts\start-prod.ps1

# Windows (Batch)：启动生产环境（自动检测 PowerShell 7）
.\scripts\start-prod.bat

# Linux/Mac：启动生产环境
./scripts/start-prod.sh

# 可选参数：
#   PowerShell 7: -SkipWait -Force（跳过健康检查等待，跳过确认提示）
#   Batch: set SKIP_WAIT=1 && set FORCE_START=1 && .\scripts\start-prod.bat
#   Linux/Mac: SKIP_WAIT=1 ./scripts/start-prod.sh / FORCE_START=1 ./scripts/start-prod.sh
```

**脚本功能特性：**
- ⚠️ 启动前确认提示（避免误操作）
- ✅ 自动检测 Docker / Docker Compose 安装状态
- ✅ 安全检查：验证关键密码变量已设置且非默认值
- ✅ 自动备份旧的 `.env` 配置
- ✅ 启动后自动进行容器健康检查（最多 180s）
- ✅ 彩色进度日志，直观展示每个服务状态
- ✅ **密码安全**：生产环境不显示实际密码值，仅显示变量名

**生产环境服务：**
- 后端 API 服务（FastAPI，镜像构建）
- 前端界面（Nginx 静态服务，镜像构建）
- PostgreSQL
- MinIO
- Milvus
- etcd
- Redis（搜索结果与网页内容缓存）
- SearXNG（私有化聚合搜索引擎）
- Prometheus
- Grafana
- Alertmanager（生产专用，告警通知）
- PostgreSQL Exporter

**访问地址：**
- 前端界面: `http://localhost`
- API 文档: `http://localhost:8000/docs`
- PostgreSQL: `localhost:5433`
- MinIO 控制台: `http://localhost:9001`
- Milvus: `localhost:19530`
- Redis: `localhost:6379`
- SearXNG: `http://localhost:8080`
- Grafana: `http://localhost:3000`
- Prometheus: `http://localhost:9090`
- Alertmanager: `http://localhost:9093`

**查看服务日志：**
```bash
# Windows PowerShell 7（查看所有日志）
.\scripts\logs-prod.ps1
# Windows PowerShell 7（查看指定服务日志）
.\scripts\logs-prod.ps1 backend

# Windows Batch（查看所有日志）
.\scripts\logs-prod.bat
# Windows Batch（查看指定服务日志）
.\scripts\logs-prod.bat backend

# Linux/Mac（查看所有日志）
./scripts/logs-prod.sh
# Linux/Mac（查看指定服务日志）
./scripts/logs-prod.sh backend
```

**停止生产环境：**
```bash
# Windows PowerShell 7
.\scripts\stop-prod.ps1
# Windows Batch
.\scripts\stop-prod.bat
# Linux/Mac
./scripts/stop-prod.sh
```

**清理生产环境（危险操作，会删除所有数据）：**
```bash
# Windows PowerShell 7（需要输入 DELETE 确认）
.\scripts\stop-prod.ps1 -Clean
# Windows Batch（需要输入 DELETE 确认）
.\scripts\stop-prod.bat --clean
# Linux/Mac（需要输入 DELETE 确认）
./scripts/stop-prod.sh --clean
```

---

### 方式三：本地开发模式（前后端本地运行）

依赖服务在 Docker 容器中运行，前后端在本地命令行运行（适合深入调试）：

#### 1. 启动依赖服务

```bash
cd langchain_rag_demo

# 启动数据库与搜索基础设施（不含前后端）
docker-compose -f docker-compose.dev.yml up -d postgres minio etcd milvus-standalone redis searxng prometheus grafana postgres-exporter

# 等待服务就绪（Milvus 首次启动需要 1-2 分钟）
```

#### 2. 配置环境变量

```bash
cd backend

# 复制开发环境变量（项目根目录的 .env.dev 需要先复制到 .env）
cp ../.env.dev ../.env

# 确保环境变量正确指向容器内的服务
# 本地开发时 .env.dev 已默认使用 localhost:5433 / localhost:9000 / localhost:19530 / localhost:6379 / localhost:8080
```

#### 3. 启动后端

```bash
cd backend

# 同步依赖（自动创建 .venv 虚拟环境，按 uv.lock 锁定版本安装）
uv sync

# 启动服务（推荐方式）
uv run python start.py

# 开发模式启动（代码改动后自动热重载，无需手动重启）
uv run python start.py --reload

# API文档: http://localhost:8000/docs
```

**开发热重载说明：**

| 启动命令 | 模式 | 适用场景 |
|---------|------|---------|
| `uv run python start.py` | 普通模式 | 调试断点、性能测试、首次启动检查 |
| `uv run python start.py --reload` | 热重载模式 | 日常开发迭代（推荐） |

热重载模式基于 uvicorn `--reload`，保存 `backend/src/` 下任意 `.py` 文件后约 1-2 秒自动重启服务。

> ⚠️ 注意事项：
> - 仅限本地开发使用，生产/Docker 环境不要启用
> - 修改 `config.py`、`.env` 等配置文件后，部分单例（如 OllamaEmbeddings 客户端）可能不会重建，若发现"改了没生效"请手动重启一次
> - 首次启动仍会执行 Ollama / PostgreSQL / MinIO / Milvus 连通性检查
> - 也可通过环境变量启用：`$env:RELOAD_MODE="true"; uv run python start.py`

#### 4. 启动前端

```bash
cd frontend

# 安装依赖
pnpm install

# 开发模式启动
pnpm dev

# 访问前端: http://localhost:5173
```

---

## 📁 项目结构

```
langchain_rag_demo/
├── docker-compose.yml        # Docker Compose 配置（生产环境）
├── docker-compose.dev.yml    # Docker Compose 配置（开发环境，含应用）
├── .env                      # 环境变量配置（自动切换）
├── .env.dev                  # 开发环境变量配置
├── .env.prod                 # 生产环境变量配置
├── scripts/                  # 根目录脚本（开发/生产启停）
│   ├── start-dev.ps1         # 开发环境启动脚本（PowerShell 7，推荐）
│   ├── start-prod.ps1        # 生产环境启动脚本（PowerShell 7，推荐）
│   ├── logs-dev.ps1          # 开发环境日志查看（PowerShell 7）
│   ├── logs-prod.ps1         # 生产环境日志查看（PowerShell 7）
│   ├── stop-dev.ps1          # 开发环境停止脚本（PowerShell 7）
│   ├── stop-prod.ps1         # 生产环境停止脚本（PowerShell 7）
│   ├── start-dev.bat         # 开发环境启动脚本（Windows Batch，PowerShell 7 入口）
│   ├── start-prod.bat        # 生产环境启动脚本（Windows Batch，PowerShell 7 入口）
│   ├── logs-dev.bat          # 开发环境日志查看（Windows Batch，PowerShell 7 入口）
│   ├── logs-prod.bat         # 生产环境日志查看（Windows Batch，PowerShell 7 入口）
│   ├── stop-dev.bat          # 开发环境停止脚本（Windows Batch，PowerShell 7 入口）
│   ├── stop-prod.bat         # 生产环境停止脚本（Windows Batch，PowerShell 7 入口）
│   ├── start-dev.sh          # 开发环境启动脚本（Linux/Mac）
│   ├── start-prod.sh         # 生产环境启动脚本（Linux/Mac）
│   ├── logs-dev.sh           # 开发环境日志查看（Linux/Mac）
│   ├── logs-prod.sh          # 生产环境日志查看（Linux/Mac）
│   ├── stop-dev.sh           # 开发环境停止脚本（Linux/Mac）
│   └── stop-prod.sh          # 生产环境停止脚本（Linux/Mac）
├── README.md                 # 项目说明文档
├── docs/                     # 文档目录
│   ├── api.md                # API 接口文档
│   └── deployment.md         # 部署指南
├── backend/                  # 后端服务
│   ├── Dockerfile.dev        # 后端开发 Dockerfile（热重载）
│   ├── Dockerfile.prod       # 后端生产 Dockerfile
│   ├── start.py              # 后端入口（环境检查 + FastAPI 启动）
│   ├── pyproject.toml        # uv 依赖配置
│   ├── uv.lock               # 锁定依赖版本（唯一依赖描述）
│   ├── scripts/              # 数据库迁移脚本
│   ├── tests/                # 测试用例
│   └── src/                  # 源码目录（统一以 `from src.* import` 方式引用）
│       ├── config.py         # 配置文件
│       ├── database.py       # 数据库连接
│       ├── dependencies.py   # 依赖注入
│       ├── exceptions.py     # 异常定义
│       ├── main.py           # FastAPI 入口
│       ├── api/              # REST API 路由
│       │   ├── chat.py                 # 聊天问答接口
│       │   ├── document.py             # 文档管理接口
│       │   ├── knowledge_base.py       # 知识库管理接口
│       │   ├── session.py              # 会话管理接口
│       │   ├── experiment.py           # A/B 测试实验接口
│       │   ├── category.py             # 分类管理接口
│       │   ├── tag.py                  # 标签管理接口
│       │   ├── feedback.py             # 评价反馈接口
│       │   ├── learning.py             # 学习引擎接口
│       │   ├── config.py               # 系统配置接口
│       │   └── notification.py         # WebSocket 实时通知接口
│       ├── services/         # 业务逻辑层
│       │   ├── rag_chain.py              # RAG 问答链（核心编排）
│       │   ├── decision_pipeline.py      # 决策管道（是否检索 KB / 联网 / Agent）
│       │   ├── strategy_manager.py       # 策略管理器
│       │   ├── web_search_service.py     # 联网搜索服务
│       │   ├── search_agent.py           # Function Calling / ReAct Agent
│       │   ├── context_enhancer.py       # 上下文增强（指代消解、压缩、摘要）
│       │   ├── question_processor.py     # 问题处理（改写、分类、建议）
│       │   ├── kb_comparator.py          # 知识库对比
│       │   ├── document_analyzer.py      # 文档分析（去重、质量、分类）
│       │   ├── kb_recommender.py         # 知识库推荐
│       │   ├── knowledge_graph_generator.py # 知识图谱生成
│       │   ├── cache_service.py          # Redis 缓存服务
│       │   ├── vector_store.py           # 向量存储管理
│       │   ├── minio_service.py          # MinIO 对象存储服务
│       │   ├── milvus_service.py         # Milvus 服务
│       │   └── document_processor.py     # 文档解析与分块
│       ├── models/           # 数据库模型（SQLAlchemy）
│       ├── middleware/       # 中间件
│       └── utils/            # 工具函数
├── frontend/                 # 前端应用（Vue 3）
│   ├── Dockerfile.dev        # 前端开发 Dockerfile（Vite 服务器）
│   ├── Dockerfile.prod       # 前端生产 Dockerfile（Nginx）
│   ├── src/                  # 源码目录
│   │   ├── views/            # 页面视图
│   │   │   ├── ChatView.vue           # 对话页面（支持网页来源展示）
│   │   │   ├── KnowledgeBaseView.vue  # 知识库管理
│   │   │   ├── HistoryView.vue        # 历史记录
│   │   │   ├── SettingsView.vue       # 设置中心
│   │   │   └── Settings/              # 设置子页面
│   │   │       ├── GeneralView.vue    # 通用设置
│   │   │       ├── TagView.vue        # 标签管理
│   │   │       ├── CategoryView.vue   # 分类管理
│   │   │       ├── LearningView.vue   # 学习引擎设置
│   │   │       ├── FeedbackView.vue   # 反馈统计
│   │   │       └── ExperimentView.vue # A/B 实验管理
│   │   ├── components/       # 公共组件
│   │   ├── stores/           # Pinia 状态管理
│   │   ├── queries/          # Vue Query 数据获取
│   │   └── utils/            # 工具函数
│   └── nginx.conf            # Nginx 配置
├── configs/                  # 配置目录
│   ├── prometheus/           # Prometheus 配置
│   │   ├── prometheus.yml    # 生产环境配置
│   │   ├── prometheus.dev.yml # 开发环境配置
│   │   └── alerts.yml        # 告警规则
│   ├── grafana/              # Grafana 配置
│   ├── alertmanager/         # Alertmanager 配置
│   ├── postgres/             # PostgreSQL 配置
│   └── searxng/              # SearXNG 配置
│       └── settings.yml      # SearXNG 搜索引擎配置
```

### 📜 配套脚本说明

| 类别 | 脚本名 | 说明 |
|------|--------|------|
| **启动** | `start-dev.ps1 / start-dev.bat / start-dev.sh` | 开发环境启动，含健康检查、状态汇总 |
| **启动** | `start-prod.ps1 / start-prod.bat / start-prod.sh` | 生产环境启动，含安全检查、确认提示 |
| **日志** | `logs-dev.ps1 / logs-dev.bat / logs-dev.sh` | 查看开发环境实时日志，可指定服务 |
| **日志** | `logs-prod.ps1 / logs-prod.bat / logs-prod.sh` | 查看生产环境实时日志，可指定服务 |
| **停止** | `stop-dev.ps1 / stop-dev.bat / stop-dev.sh` | 停止开发环境，`-Clean`（PowerShell）或 `--clean`（Batch/Linux）同时清除数据 |
| **停止** | `stop-prod.ps1 / stop-prod.bat / stop-prod.sh` | 停止生产环境，`-Clean`（PowerShell）或 `--clean`（Batch/Linux）同时清除数据（危险） |

**Windows PowerShell 7 脚本参数：**
- `-SkipWait`：跳过健康检查等待，适用于快速启动
- `-Force`：跳过生产环境确认提示（谨慎使用）
- `-Clean`：停止时同时删除数据卷（会清除所有数据）

**Windows Batch / Linux/Mac 脚本参数：**
- `SKIP_WAIT=1`：跳过健康检查等待，适用于快速启动
- `FORCE_START=1`：跳过生产环境确认提示（谨慎使用）
- `--clean`：停止时同时删除数据卷（会清除所有数据）

**使用建议：**
- ✅ **推荐 Windows 用户使用 PowerShell 7 脚本（.ps1）**：提供最佳彩色输出和用户体验
- Batch 脚本（.bat）会自动检测 PowerShell 7 并调用相应的 .ps1 脚本
- 如未安装 PowerShell 7，脚本会降级为 Batch 方式运行，并提示安装地址

---

## 🔧 配置说明

### 环境变量配置

项目包含多个环境变量配置文件，用于不同层次的配置管理：

#### 配置文件层次

| 文件 | 用途 | 优先级 | 使用场景 |
|------|------|--------|---------|
| **Docker Compose** | 生产环境配置 | 最高 | 生产环境启动时注入 |
| **.env.dev / .env.prod** | 项目级环境配置 | 中 | 开发/生产环境启动时复制为 `.env` |

#### 配置加载机制

**后端配置加载逻辑**（`backend/src/config.py`）：

- 统一通过 `pydantic-settings` 读取环境变量和项目根目录 `.env` 文件
- Docker Compose 通过 `environment` 将环境变量注入容器，优先级高于 `.env` 文件
- 开发环境和生产环境通过不同的 Docker Compose 配置文件实现隔离

#### 环境变量配置文件说明

| 文件 | 用途 | 使用场景 |
|------|------|---------|
| `.env.dev` | 项目级开发环境配置 | 启动开发环境时复制为 `.env` |
| `.env.prod` | 项目级生产环境配置 | 启动生产环境前必须配置 |
| `.env` | 项目级运行时配置 | Docker Compose 实际读取 |

#### 配置对比

| 配置项 | 开发环境 (`.env.dev`) | 生产环境 (`.env.prod`) |
|--------|---------------------|---------------------|
| 数据库名 | `app_dev` | `app_prod` |
| 用户名 | `dev_user` | `prod_user` |
| 密码 | 简单密码 | **必须手动设置强密码** |
| MinIO 安全 | `false` | `true` |
| 重启策略 | `no` | `unless-stopped` |
| 数据卷 | `*_dev` | `*_prod` |
| 监控告警 | 不启用 Alertmanager | 启用 Alertmanager |
| 后端运行方式 | 源码挂载 + 热重载 | 镜像构建 |
| 前端运行方式 | Vite 开发服务器 | Nginx 静态服务 |

### 配置示例

```env
# ===========================================
# 开发环境配置 (.env.dev)
# ===========================================
POSTGRES_DB=app_dev
POSTGRES_USER=dev_user
POSTGRES_PASSWORD=dev_password
MINIO_ROOT_USER=dev_minio
MINIO_ROOT_PASSWORD=dev_minio_password
MINIO_ACCESS_KEY=dev_access_key
MINIO_SECRET_KEY=dev_secret_key
MINIO_BUCKET_NAME=documents
MINIO_SECURE=false

# Redis
REDIS_PASSWORD=dev_redis_password

# 安全密钥：开发环境可不设置，后端启动时自动生成临时随机密钥
#（重启后旧签名失效）；生产环境必须设置为强密钥
SECRET_KEY=
```

```env
# ===========================================
# 生产环境配置 (.env.prod)
# ===========================================
POSTGRES_DB=app_prod
POSTGRES_USER=prod_user
POSTGRES_PASSWORD=  # 必须手动设置！
MINIO_ROOT_USER=prod_minio
MINIO_ROOT_PASSWORD=  # 必须手动设置！
MINIO_ACCESS_KEY=  # 必须手动设置！
MINIO_SECRET_KEY=  # 必须手动设置！
MINIO_BUCKET_NAME=documents
MINIO_SECURE=true

# Redis
REDIS_PASSWORD=  # 必须手动设置！

# 安全密钥（必须设置为强密钥，否则后端启动失败）
SECRET_KEY=  # 必须手动设置！

# 环境标记：Docker 部署无需设置（由 IN_DOCKER 推断为生产）；
# 非 Docker 的生产部署（直接 uvicorn/systemd 运行）必须设置，否则不执行强校验
APP_ENV=production

# Milvus 集合 schema/维度不匹配时是否允许删除重建（破坏性！旧向量数据全部丢失）
# 默认 false：不匹配时启动失败并提示；确认可接受数据丢失后才改为 true
MILVUS_REBUILD_ON_MISMATCH=false
```

### 核心配置参数说明

| 参数 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `CHUNK_SIZE` | 文本分割大小（字符数） | 512 | 200-1000 |
| `CHUNK_OVERLAP` | 分割重叠大小 | 64 | 0-200 |
| `TOP_K` | 检索文档数量 | 3 | 1-10 |
| `POSTGRES_PORT` | 数据库端口 | 5433（本地）/ 5432（容器内） | - |
| `MINIO_ROOT_USER` | MinIO 管理员账号 | - | - |
| `MINIO_ROOT_PASSWORD` | MinIO 管理员密码 | - | - |
| `MINIO_ACCESS_KEY` | 后端连接 MinIO 的 Access Key | - | - |
| `MINIO_SECRET_KEY` | 后端连接 MinIO 的 Secret Key | - | - |
| `MINIO_BUCKET_NAME` | MinIO 存储桶名称 | `documents` | - |
| `MINIO_SECURE` | 是否启用 HTTPS | `false` | true/false |
| `SECRET_KEY` | JWT/安全签名密钥 | 无（开发缺失时启动自动生成临时密钥） | 生产环境必须设置为强密钥，否则启动失败 |
| `APP_ENV` | 显式环境标记 | 空（按 IN_DOCKER 推断） | 非 Docker 生产部署必须设为 `production` |
| `MILVUS_REBUILD_ON_MISMATCH` | 集合 schema/维度不匹配时是否删除重建 | `false` | 破坏性操作，开启前必须完成数据迁移/备份 |
| `REDIS_HOST` | Redis 主机 | `localhost` | 容器内自动覆盖为 `redis` |
| `REDIS_PORT` | Redis 端口 | `6379` | - |
| `REDIS_DB` | Redis 数据库编号 | `0` | 0-15 |
| `REDIS_PASSWORD` | Redis 密码 | `dev_redis_password` | 必填，生产环境必须设置为强密码 |
| `SEARCH_PROVIDER` | 搜索引擎 | `searxng` | `duckduckgo` / `searxng` / `tavily` |
| `SEARCH_MAX_RESULTS` | 单次搜索返回结果数 | 10 | 1-20 |
| `SEARCH_FETCH_TIMEOUT` | 网页抓取超时（秒） | 10 | - |
| `SEARCH_MAX_FETCH` | 最大并发抓取网页数 | 5 | - |
| `SEARCH_MIN_CONTENT_LENGTH` | 网页内容最小长度 | 100 | - |
| `SEARXNG_BASE_URL` | SearXNG 服务地址 | `http://localhost:8080` | 容器内使用 `http://searxng:8080` |
| `SEARXNG_TIMEOUT` | SearXNG 请求超时（秒） | 10 | - |
| `SEARCH_ENABLE_MULTI_QUERY` | 是否启用 LLM 多角度 Query 改写 | `true` | true/false |
| `SEARCH_NUM_QUERIES` | Query 改写生成的查询数 | 3 | - |
| `SEARCH_ENABLE_RERANK` | 是否启用 Cross-Encoder 语义重排 | `true` | true/false |
| `EMBEDDING_DIMENSION` | Embedding 向量维度 | `1024` | 需与 `EMBEDDING_MODEL_NAME` 对应 |
| `FAST_LLM_MODEL_NAME` | 轻量任务模型 | `qwen2.5:7b` | 意图路由/标题/Query 改写 |
| `KB_RERANK_MODEL` | 知识库重排模型 | `qllama/bge-reranker-v2-m3:latest` | - |
| `KB_RERANK_PROVIDER` | 知识库重排加载方式 | `ollama` | `sentence_transformers` / `ollama` |
| `SEARCH_RERANK_MODEL` | 搜索重排模型 | `qllama/bge-reranker-v2-m3:latest` | - |
| `SEARCH_RERANK_PROVIDER` | 搜索重排加载方式 | `ollama` | `sentence_transformers` / `ollama` |
| `SEARCH_RERANK_TOP_K` | 重排后返回 Top-K | 5 | - |
| `SEARCH_CACHE_TTL` | 搜索结果缓存时间（秒） | 3600 | - |
| `SEARCH_CONTENT_CACHE_TTL` | 网页内容缓存时间（秒） | 86400 | - |
| `SEARCH_ENABLE_FUNCTION_CALLING` | 是否启用 Function Calling | `false` | true/false |
| `SEARCH_ENABLE_REACT` | 是否启用 ReAct Agent | `false` | true/false |
| `SEARCH_REACT_MAX_STEPS` | ReAct 最大推理步数 | 3 | - |
| `SEARCH_AGENT_FALLBACK_TO_PHASE2` | Agent 失败是否回退到普通搜索 | `true` | true/false |
| `TITLE_GENERATION_ENABLED` | 是否启用 LLM 自动生成会话标题 | `true` | true/false |
| `TITLE_MAX_LENGTH` | 生成标题的最大长度 | `30` | - |
| `TITLE_FALLBACK_LENGTH` | 生成失败时截取问题前 N 字 | `30` | - |
| `TITLE_GENERATION_MODEL` | 指定标题生成模型 | - | 留空则使用 `FAST_LLM_MODEL_NAME` |

### Docker 服务端口

| 服务 | 端口 | 说明 |
|------|------|------|
| 后端 API | 8000 | FastAPI 服务 |
| 前端 | 5173 / 80 | Vite（开发）/ Nginx（生产） |
| PostgreSQL | 5433 | 业务数据库 |
| MinIO | 9000 | 对象存储 |
| MinIO 控制台 | 9001 | 管理界面 |
| Milvus | 19530 | 向量数据库 |
| Redis | 6379 | 缓存服务 |
| SearXNG | 8080 | 私有化聚合搜索引擎 |
| Prometheus | 9090 | 监控指标 |
| Alertmanager | 9093 | 告警管理 |
| Grafana | 3000 | 监控面板 |

**注意：** 开发/生产环境使用相同的端口，但数据完全隔离。同一时间只能运行一个环境。

---

## 🎯 使用方式

### 基本使用流程

1. **创建知识库**
   - 进入知识库管理页面
   - 点击"新建知识库"
   - 输入知识库名称和描述

2. **上传文档**
   - 选择目标知识库
   - 上传支持格式的文档
   - 等待文档处理完成

3. **提问**
   - 进入对话页面
   - 输入问题
   - 选择要查询的知识库（可选）
   - 点击发送

4. **查看答案**
   - 系统显示回答内容
   - 显示参考来源和相关文档片段
   - 支持流式打字效果

5. **评价反馈**
   - 对回答进行有用/无用评价
   - 可选填写评价原因
   - 系统学习用户反馈

### 高级功能

- **会话管理**: 支持多会话切换和历史记录
- **文档标签**: 为文档添加分类和标签
- **A/B 测试**: 创建实验对比不同策略效果
- **学习引擎**: 基于反馈自动优化检索策略

### 联网搜索与 Agent

系统支持三种搜索模式，通过 `use_web_search=true` 与 `search_mode` 控制：

| 模式 | `search_mode` | 说明 |
|------|---------------|------|
| 简单联网搜索 | `simple` | 单次搜索，结果经 LLM 改写、Cross-Encoder 重排后进入上下文 |
| Function Calling | `function_calling` | 本地模型以 prompt-based 方式调用 `web_search` / `fetch_webpage` 工具（需开启 `SEARCH_ENABLE_FUNCTION_CALLING=true`） |
| ReAct Agent | `agent` | 多轮 Thought → Action → Observation 迭代搜索（需开启 `SEARCH_ENABLE_REACT=true`） |

**前端使用示例**：

在对话页面勾选联网搜索开关，系统会自动根据问题类型选择知识库、联网或混合模式。搜索结果会在答案下方显示为可点击的网页来源卡片，知识库来源仍可点击查看原始文档片段。

**注意事项**：

- Function Calling / ReAct 默认关闭，避免对现有流程产生影响；开启前请确保模型能遵循工具调用格式
- 代码类查询（如 `langchain_ollama.ChatOllama`）会保留 `_`、`.`、`#` 等关键符号，避免搜索精度下降
- 搜索失败时会抛出带搜索引擎名称的 `WebSearchError`，不再静默返回空结果

---

## 📊 监控系统

### 访问地址

| 服务 | 地址 | 用户名/密码 |
|------|------|-------------|
| Grafana | http://localhost:3000 | admin / 见 `.env.dev` 或 `.env.prod` 中 `GF_SECURITY_ADMIN_PASSWORD` |
| Prometheus | http://localhost:9090 | - |
| Alertmanager | http://localhost:9093 | -（仅生产环境） |

### 监控指标

| 指标名称 | 类型 | 说明 |
|----------|------|------|
| `rag_request_total` | Counter | 请求总数 |
| `rag_request_errors_total` | Counter | 请求错误总数 |
| `rag_request_duration_seconds` | Histogram | 请求耗时分布 |
| `rag_active_requests` | Gauge | 当前活跃请求数 |
| `rag_llm_calls_total` | Counter | LLM 调用次数 |
| `rag_llm_call_duration_seconds` | Histogram | LLM 调用耗时分布 |
| `rag_vector_search_total` | Counter | 向量检索次数 |
| `rag_vector_search_duration_seconds` | Histogram | 向量检索耗时分布 |
| `rag_documents_processed_total` | Counter | 文档处理次数 |
| `rag_document_process_duration_seconds` | Histogram | 文档处理耗时分布 |
| `rag_kb_queries_total` | Counter | 知识库查询次数 |
| `rag_strategy_decisions_total` | Counter | 策略决策次数 |

### 告警规则

系统预设以下告警规则（仅生产环境启用 Alertmanager）：

- **请求延迟告警**: 请求耗时超过 5 秒
- **错误率告警**: 错误率超过 5%
- **服务不可用告警**: 服务健康检查失败
- **文档处理失败告警**: 文档处理失败率超过 10%

---

## 🧪 测试

### 后端测试

```bash
cd backend
python -m pytest tests/ -v
```

### 前端测试

```bash
cd frontend

# 单元测试
pnpm test:unit

# E2E 测试（需要先构建）
pnpm build
pnpm test:e2e
```

### 健康检查

```bash
# 检查后端服务
curl http://localhost:8000/health

# 检查数据库连接（开发环境）
docker exec -it postgres-dev psql -U dev_user -d app_dev -c "SELECT 1"
```

---

## 📖 文档

文档索引见 [docs/README.md](docs/README.md)。结构：核心参考（architecture / api）置于顶层，操作指南归入 `guide/`，设计文档归入 `design/`，一次性报告归入 `archive/`。

| 文档 | 说明 | 路径 |
|------|------|------|
| 架构总览 | 服务拓扑、RAG 决策管线、代码地图 | [docs/architecture.md](docs/architecture.md) |
| API 文档 | 完整的 API 接口说明 | [docs/api.md](docs/api.md) |
| 部署指南 | 开发/生产环境部署说明 | [docs/guide/deployment.md](docs/guide/deployment.md) |
| 开发与测试指南 | 本地开发、测试执行、CI 说明、踩坑记录 | [docs/guide/development.md](docs/guide/development.md) |
| 监控运维指南 | 监控栈使用、指标说明、告警规则与处置 | [docs/guide/monitoring.md](docs/guide/monitoring.md) |
| 优化路线图 | 后续完善与优化执行计划 | [docs/design/improvement-roadmap.md](docs/design/improvement-roadmap.md) |
| 专项设计文档 | 检索增强/意图路由/搜索优化等设计 | `docs/design/` |
| 代码审查报告（已归档） | 历史审查与修复记录 | [docs/archive/code-review-2026-08.md](docs/archive/code-review-2026-08.md) |
| 快速开始 | 项目入门指南 | 本文件 |

---

## 📄 许可证

MIT License

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

### 开发规范

请参考项目代码注释与现有测试用例了解开发规范。

---

## ⚠️ 注意事项

1. **环境隔离**: 开发环境和生产环境使用不同的数据卷，确保数据隔离
2. **密码安全**: 生产环境部署前务必设置强密码，不要使用默认密码
3. **SECRET_KEY**: 生产环境必须在 `.env.prod` 中设置强密钥，否则后端容器启动失败
4. **数据卷管理**: 使用 `docker-compose down -v` 会删除数据卷，**谨慎操作**
5. **模型下载**: 首次运行需要下载 Ollama 模型，可能需要较长时间
6. **模型拉取**: 首次运行前请在宿主机执行 `ollama pull bge-m3:latest`、`ollama pull qllama/bge-reranker-v2-m3:latest`、`ollama pull qwen2.5:7b` 与 `ollama pull deepseek-r1:7b-qwen-distill-q4_K_M`，耗时较长
7. **内存要求**: 建议至少 8GB 内存，模型越大需要内存越多
8. **端口冲突**: 确保常用端口（5433, 6379, 8000, 8080, 9000, 9001, 19530, 3000, 9090, 5173）未被占用
9. **环境切换**: 同一时间只能运行一种环境（开发或生产），切换前请先停止当前环境
10. **联网搜索稳定性**: DuckDuckGo 等搜索引擎可能因网络或反爬策略临时不可用，可切换至自托管 SearXNG

### 环境切换流程

```bash
# 从开发环境切换到生产环境
# 1. 停止开发环境（保留数据）
# Windows PowerShell 7 (推荐):
.\scripts\stop-dev.ps1
# Windows Batch:
.\scripts\stop-dev.bat
# Linux/Mac:
./scripts/stop-dev.sh

# 2. 启动生产环境
# Windows PowerShell 7 (推荐):
.\scripts\start-prod.ps1
# Windows Batch:
.\scripts\start-prod.bat
# Linux/Mac:
./scripts/start-prod.sh

# 从生产环境切换到开发环境
# 1. 停止生产环境
# Windows PowerShell 7 (推荐):
.\scripts\stop-prod.ps1
# Windows Batch:
.\scripts\stop-prod.bat
# Linux/Mac:
./scripts/stop-prod.sh

# 2. 启动开发环境
# Windows PowerShell 7 (推荐):
.\scripts\start-dev.ps1
# Windows Batch:
.\scripts\start-dev.bat
# Linux/Mac:
./scripts/start-dev.sh
```

> 💡 切换前请确认：
> 1. 没有正在处理的文档或对话
> 2. 已使用相应的 `.env.dev` 或 `.env.prod` 配置好参数
> 3. 数据如有需要可提前备份

---

## 📞 支持

如有问题，请提交 Issue 或联系开发团队。
