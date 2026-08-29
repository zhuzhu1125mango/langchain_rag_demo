# 🚀 RAG 知识库问答系统 - 部署指南

## 目录

1. [环境要求](#1-环境要求)
2. [开发/生产环境分离](#2-开发生产环境分离)
3. [开发环境部署](#3-开发环境部署)
4. [生产环境部署](#4-生产环境部署)
5. [Docker 部署](#5-docker-部署)
6. [配置说明](#6-配置说明)
7. [监控告警](#7-监控告警)
8. [常见问题](#8-常见问题)
9. [故障排查](#9-故障排查)

---

## 1. 环境要求

### 1.1 基础依赖

| 依赖 | 版本 | 说明 | 必须 |
|------|------|------|------|
| Python | >= 3.10 | 后端语言 | ✅ |
| Node.js | >= 20 | 前端构建 | ✅ |
| pnpm | >= 8 | 前端包管理 | ✅ |
| Ollama | >= 0.1 | 本地 LLM 运行 | ✅ |
| Docker | >= 24 | 容器化部署 | ✅ |
| Docker Compose | >= 2.20 | 容器编排 | ✅ |

### 1.2 数据库依赖

| 服务 | 版本 | 默认端口 | 说明 |
|------|------|----------|------|
| PostgreSQL | >= 15 | 5433 (映射端口) | 业务数据库 |
| MinIO | >= 2024 | 9000 | 对象存储 |
| Milvus | >= 2.6 | 19530 | 向量数据库 |
| Redis | >= 7.0 | 6379 | 缓存服务 |

### 1.3 资源要求

| 资源 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | 4核 | 8核 |
| 内存 | 8GB | 16GB |
| 磁盘 | 50GB | 100GB+ |

---

## 2. 开发/生产环境分离

### 2.1 环境分离策略

项目采用**开发/生产环境完全分离**策略：

| 特性 | 开发环境 | 生产环境 |
|------|---------|---------|
| **Docker 配置** | `docker-compose.dev.yml` | `docker-compose.yml` |
| **数据卷** | `*_dev`（如 `postgres_data_dev`） | `*_prod`（如 `postgres_data_prod`） |
| **容器名称** | `*-dev`（如 `postgres-dev`） | `*-prod`（如 `postgres-prod`） |
| **数据库** | `app_dev` | `app_prod` |
| **重启策略** | `no` | `unless-stopped` |
| **服务完整性** | 核心基础设施 | 完整服务（含前端后端） |
| **启动脚本** | `scripts/start-dev.bat` | `scripts/start-prod.bat` |

### 2.2 核心优势

- ✅ **数据隔离**: 开发测试数据不会影响生产数据
- ✅ **安全可控**: 生产环境必须手动配置强密码
- ✅ **灵活切换**: 一键启动脚本自动切换环境
- ⚠️ **端口冲突**: 两个环境使用相同端口（5433/8000/8080 等），**同一时间只能运行一个环境**

### 2.3 环境切换流程

```bash
# 停止当前环境
# 如果是开发环境：
docker-compose -f docker-compose.dev.yml down

# 如果是生产环境：
docker-compose down

# 切换到目标环境
# 启动开发环境：
.\scripts\start-dev.bat

# 启动生产环境：
.\scripts\start-prod.bat
```

---

## 3. 开发环境部署

### 3.1 快速启动（推荐）

使用一键启动脚本：

```bash
# 进入项目目录
cd langchain_rag_demo

# 一键启动开发环境
.\scripts\start-dev.bat

# 或手动执行
copy .env.dev .env
docker-compose -f docker-compose.dev.yml up -d
```

### 3.2 启动依赖服务

```bash
# 进入项目目录
cd langchain_rag_demo

# 启动所有开发服务（含后端、前端、数据库、监控等）
docker-compose -f docker-compose.dev.yml up -d

# 检查服务状态
docker-compose -f docker-compose.dev.yml ps

# 等待服务就绪（Milvus 首次启动需要 1-2 分钟）
docker-compose -f docker-compose.dev.yml logs milvus-standalone | findstr "Proxy is ready"
```

### 3.3 后端本地开发

```bash
cd backend

# 使用 Poetry 创建虚拟环境
poetry env use python

# 安装依赖（包括开发依赖；poetry.lock 为后端唯一依赖描述）
poetry install

# 安装 Ollama（必须）
# 下载地址: https://ollama.com/download

# 拉取模型（首次运行需要下载，耗时较长）
ollama pull deepseek-r1:7b-qwen-distill-q4_K_M
ollama pull qwen2.5:7b
ollama pull bge-m3:latest
ollama pull qllama/bge-reranker-v2-m3:latest

# 启动服务（推荐方式）
poetry run python start.py
```

### 3.4 前端本地开发

```bash
cd frontend

# 安装依赖
pnpm install

# 开发模式启动（热更新）
pnpm dev
```

### 3.5 访问地址

| 服务 | 地址 | 说明 |
|------|------|------|
| 前端（本地开发） | http://localhost:5173 | Vue3 开发服务器 |
| 后端 API | http://localhost:8000 | REST API |
| API 文档 | http://localhost:8000/docs | Swagger UI |
| PostgreSQL | localhost:5433 | 数据库 |
| MinIO 控制台 | http://localhost:9001 | 对象存储管理 |
| Milvus | localhost:19530 | 向量数据库 |
| Redis | localhost:6379 | 缓存服务 |
| SearXNG | http://localhost:8080 | 聚合搜索引擎 |
| Prometheus | http://localhost:9090 | 监控指标 |
| Grafana | http://localhost:3000 | 监控面板 |

### 3.6 停止开发环境

```bash
# 停止服务（保留数据）
docker-compose -f docker-compose.dev.yml down

# 停止服务并删除数据卷（谨慎！数据将丢失）
docker-compose -f docker-compose.dev.yml down -v
```

---

## 4. 生产环境部署

### 4.1 快速启动（推荐）

```bash
# 进入项目目录
cd langchain_rag_demo

# 一键启动生产环境（会提示确认）
.\scripts\start-prod.bat

# 或手动执行
copy .env.prod .env
docker-compose up -d
```

### 4.2 首次部署准备

#### 4.2.1 配置强密码

生产环境部署前**必须**配置强密码：

编辑 `.env.prod` 文件：

```env
# 必须修改以下密码！
POSTGRES_PASSWORD=your_secure_postgres_password_here
MINIO_ROOT_USER=prod_minio
MINIO_ROOT_PASSWORD=your_secure_minio_password_here
MINIO_ACCESS_KEY=your_secure_access_key_here
MINIO_SECRET_KEY=your_secure_secret_key_here
MINIO_BUCKET_NAME=documents
REDIS_PASSWORD=your_secure_redis_password_here
GF_SECURITY_ADMIN_PASSWORD=your_secure_grafana_password_here

# 安全密钥（必须设置为强密钥，否则后端启动失败）
SECRET_KEY=your_very_long_random_secret_key_here_min_32_chars

# 联网搜索配置（使用内置 SearXNG 时保持默认即可）
SEARCH_PROVIDER=searxng
SEARXNG_BASE_URL=http://searxng:8080
```

#### 4.2.2 验证配置

```bash
# 确保 .env.prod 已正确配置
cat .env.prod | findstr PASSWORD
```

### 4.3 完整服务启动

```bash
# 启动所有服务（后端、前端、数据库等）
docker-compose up -d

# 检查服务状态
docker-compose ps

# 查看所有服务日志
docker-compose logs -f
```

### 4.4 访问地址

| 服务 | 地址 | 用户名/密码 |
|------|------|-------------|
| 前端界面 | http://localhost | - |
| 后端 API | http://localhost:8000 | - |
| API 文档 | http://localhost:8000/docs | - |
| PostgreSQL | localhost:5433 | 见 .env.prod |
| MinIO 控制台 | http://localhost:9001 | 见 .env.prod |
| Milvus | localhost:19530 | - |
| Redis | localhost:6379 | 见 .env.prod |
| SearXNG | http://localhost:8080 | - |
| Prometheus | http://localhost:9090 | - |
| Alertmanager | http://localhost:9093 | - |
| Grafana | http://localhost:3000 | admin / 见 .env.prod |
| PostgreSQL Exporter | localhost:9187（容器内） | 仅 Prometheus 通过 Docker 网络访问 |

### 4.5 停止生产环境

```bash
# 停止服务（保留数据）
docker-compose down

# 停止服务并删除数据卷（谨慎！数据将丢失）
docker-compose down -v
```

---

## 5. Docker 部署

### 5.1 开发环境 Docker Compose 服务说明

`docker-compose.dev.yml` 包含以下服务：

| 服务 | 镜像 | 端口 | 数据卷 | 说明 |
|------|------|------|--------|------|
| postgres-dev | postgres:16.14 | 5433:5432 | postgres_data_dev | 开发数据库 |
| minio-dev | minio/minio:latest | 9000-9001 | minio_data_dev | 开发对象存储 |
| milvus-standalone-dev | milvusdb/milvus:v2.6.17 | 19530 | milvus_data_dev | 开发向量数据库 |
| etcd-dev | quay.io/coreos/etcd:v3.5.30 | - | etcd_data_dev | Milvus 元数据 |
| redis-dev | redis:7.2-alpine | 6379 | redis_data_dev | 开发缓存服务 |
| searxng-dev | searxng/searxng:latest | 8080 | - | 开发聚合搜索引擎 |
| prometheus-dev | prom/prometheus:v3.5.0 | 9090 | prometheus_data_dev | 监控指标采集 |
| grafana-dev | grafana/grafana:12.0.2 | 3000 | grafana_data_dev | 监控面板 |
| backend-dev | - | 8000 | - | 后端 API 服务 |
| frontend-dev | - | 5173 | - | 前端界面 |
| postgres-exporter-dev | prometheuscommunity/postgres-exporter:latest | 9187 | - | PostgreSQL 指标导出 |

### 5.2 生产环境 Docker Compose 服务说明

`docker-compose.yml` 包含以下服务：

| 服务 | 镜像 | 端口 | 数据卷 | 说明 |
|------|------|------|--------|------|
| backend-prod | - | 8000 | - | 后端 API 服务 |
| frontend-prod | - | 80 | - | 前端界面 |
| postgres-prod | postgres:16.14 | 5433:5432 | postgres_data_prod | 生产数据库 |
| minio-prod | minio/minio:latest | 9000-9001 | minio_data_prod | 生产对象存储 |
| milvus-standalone-prod | milvusdb/milvus:v2.6.17 | 19530 | milvus_data_prod | 生产向量数据库 |
| etcd-prod | quay.io/coreos/etcd:v3.5.30 | - | etcd_data_prod | Milvus 元数据 |
| redis-prod | redis:7.2-alpine | 6379 | redis_data_prod | 生产缓存服务 |
| searxng-prod | searxng/searxng:latest | 8080 | - | 生产聚合搜索引擎 |
| prometheus-prod | prom/prometheus:v3.5.0 | 9090 | prometheus_data_prod | 监控指标采集 |
| grafana-prod | grafana/grafana:12.0.2 | 3000 | grafana_data_prod | 监控面板 |
| alertmanager-prod | prom/alertmanager:v0.29.0 | 9093 | alertmanager_data_prod | 告警管理 |
| postgres-exporter-prod | prometheuscommunity/postgres-exporter:latest | 9187 | - | PostgreSQL 指标导出 |

### 5.3 常用命令

```bash
# ===========================================
# 开发环境命令
# ===========================================

# 启动开发环境
docker-compose -f docker-compose.dev.yml up -d

# 查看开发环境状态
docker-compose -f docker-compose.dev.yml ps

# 查看开发环境日志
docker-compose -f docker-compose.dev.yml logs -f

# 查看特定服务日志
docker-compose -f docker-compose.dev.yml logs -f postgres

# 停止开发环境（保留数据）
docker-compose -f docker-compose.dev.yml down

# 停止开发环境（删除数据）
docker-compose -f docker-compose.dev.yml down -v

# 重启特定服务（使用 Compose 服务名，非容器名）
docker-compose -f docker-compose.dev.yml restart postgres

# 进入容器（使用容器名）
docker exec -it postgres-dev bash

# ===========================================
# 生产环境命令
# ===========================================

# 启动生产环境
docker-compose up -d

# 查看生产环境状态
docker-compose ps

# 查看生产环境日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f backend

# 停止生产环境（保留数据）
docker-compose down

# 停止生产环境（删除数据）
docker-compose down -v

# 重启特定服务（使用 Compose 服务名，非容器名）
docker-compose restart backend

# 进入容器（使用容器名）
docker exec -it postgres-prod bash
```

### 5.4 内存限制配置

在 `docker-compose.yml` 中可以添加资源限制：

```yaml
services:
  milvus-standalone-prod:
    deploy:
      resources:
        limits:
          memory: 8G
          cpus: "4.0"
        reservations:
          memory: 4G
          cpus: "2.0"
```

---

## 6. 配置说明

### 6.1 配置文件层次

项目包含多个环境变量配置文件，用于不同层次的配置管理：

| 文件 | 用途 | 优先级 | 使用场景 |
|------|------|--------|---------|
| **Docker Compose** | 生产环境配置 | 最高 | 生产环境启动时注入 |
| **.env.dev / .env.prod** | 项目级环境配置 | 中 | 开发/生产环境启动时复制为 `.env` |
| **.env** | 后端本地开发配置 | 最低 | 本地开发时提供默认值 |

### 6.2 后端配置加载机制

后端配置加载逻辑（`backend/src/config.py`）：

**1. Docker 环境**（`IN_DOCKER=true`）：
- 后端 `src/config.py` 读取项目根目录的 `.env` 文件作为兜底
- 优先使用 Docker Compose `environment` 中显式注入的环境变量
- **确保生产环境使用正确的配置**

**2. 本地开发环境**（`IN_DOCKER` 未设置或为 `false`）：
- 读取项目根目录的 `.env` 文件
- 允许通过系统环境变量覆盖配置文件中的值
- **允许通过系统环境变量覆盖默认值**

### 6.3 环境变量配置文件说明

| 文件 | 用途 | 使用场景 |
|------|------|---------|
| `.env.dev` | 项目级开发环境配置 | 启动开发环境时复制为 `.env` |
| `.env.prod` | 项目级生产环境配置 | 启动生产环境前必须配置 |
| `.env` | 项目级运行时配置 | Docker Compose 实际读取；本地开发时提供默认值 |

### 6.4 核心配置参数对比

| 参数 | 开发环境 (.env.dev) | 生产环境 (.env.prod) | 敏感 |
|------|---------------------|----------------------|------|
| `POSTGRES_DB` | `app_dev` | `app_prod` | 否 |
| `POSTGRES_USER` | `dev_user` | `prod_user` | 否 |
| `POSTGRES_PASSWORD` | `dev_password` | **必须手动设置** | ✅ |
| `MINIO_ROOT_USER` | `dev_minio` | `prod_minio` | 否 |
| `MINIO_ROOT_PASSWORD` | `dev_minio_password` | **必须手动设置** | ✅ |
| `MINIO_ACCESS_KEY` | `dev_access_key` | **必须手动设置** | ✅ |
| `MINIO_SECRET_KEY` | `dev_secret_key` | **必须手动设置** | ✅ |
| `MINIO_BUCKET_NAME` | `documents` | `documents` | 否 |
| `MINIO_SECURE` | `false` | `true` | 否 |
| `REDIS_DB` | `0` | `0` | 否 |
| `REDIS_PASSWORD` | `dev_redis_password` | **必须手动设置** | ✅ |
| `SECRET_KEY` | 不设置（启动自动生成临时密钥） | **必须手动设置强密钥** | ✅ |
| `APP_ENV` | 不设置 | Docker 部署不设置；非 Docker 生产部署必设 `production` | 否 |
| `MILVUS_REBUILD_ON_MISMATCH` | `false` | `false`（除非已确认可丢弃向量数据） | 否 |
| `ADMIN_KEY` | 不设置（管理接口仅限开发模式） | 建议设置；未设置时管理接口一律 403 | ✅ |
| `SEARCH_PROVIDER` | `searxng` | `searxng` | 否 |
| `SEARXNG_BASE_URL` | `http://localhost:8080` | `http://searxng:8080` | 否 |
| `GF_SECURITY_ADMIN_PASSWORD` | `admin` | **必须手动设置** | ✅ |
| `TITLE_GENERATION_ENABLED` | `true` | `true` | 否 |
| `TITLE_MAX_LENGTH` | `30` | `30` | 否 |
| `TITLE_FALLBACK_LENGTH` | `30` | `30` | 否 |
| `TITLE_GENERATION_MODEL` | 空 | 空 | 否 |

> **说明**：
> - `SEARXNG_BASE_URL`：本地（非 Docker）开发使用 `http://localhost:8080`（见 `.env.dev` / `.env.example`）；Docker 模式下后端容器通过 `docker-compose.dev.yml` / `docker-compose.yml` 硬编码为 `http://searxng:8080`，`.env` 中的值不会生效。
> - SearXNG 的 `redis: url: false`（见 `configs/searxng/settings.yml`）显式关闭 SearXNG 自身的 Redis 限流缓存，与项目 Redis 缓存用途相互独立，无需修改。

### 6.4.1 存量库升级：sessions.messages 列迁移 JSONB

消息保存已改为 `jsonb ||` 原子追加（并发安全），要求 `sessions.messages` 列为
JSONB 类型。**新建库无需处理**（建表即为 JSONB）；从旧版本升级的存量库必须在
启动新版后端前执行一次迁移脚本（幂等，可重复运行）：

```bash
cd backend
python scripts/migrate_session_messages_jsonb.py
```

未迁移直接启动新版时，消息写入会报 `operator does not exist: json || jsonb`。

### 6.4.2 启动强校验（生产模式）

满足以下任一条件即按生产标准执行启动校验，校验失败将拒绝启动：

- Docker 部署（`IN_DOCKER=true`）；
- 显式设置 `APP_ENV=production`（用于非 Docker 的生产部署，如直接 uvicorn/systemd 运行）。

校验项：`SECRET_KEY` 强度（≥32 字符、≥3 种字符类型、不在弱密钥黑名单），
`POSTGRES_PASSWORD` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` 非空。
开发模式（非生产）下 `SECRET_KEY` 缺失时会自动生成临时随机密钥并告警
（重启后旧签名失效）。

**管理操作鉴权**：`PUT /config/processing`、`POST /config/reset`、
`POST /metrics/reset` 为管理操作。配置了 `ADMIN_KEY` 时请求须携带匹配的
`X-Admin-Key` 头（常量时间比较）；生产模式未配置时一律 403；开发模式放行。

**基础设施端口**：生产编排（`docker-compose.yml`）中 Redis/PostgreSQL/MinIO/
Milvus/SearXNG/Prometheus/Alertmanager/Grafana 的宿主机端口仅绑定
`127.0.0.1`，对外只暴露 backend(8000) 与 frontend(80)；远程运维走 SSH
隧道或反向代理。

### 6.5 配置检查清单

启动前建议执行以下检查：

```bash
# ===========================================
# 开发环境检查
# ===========================================

# 1. 检查环境变量文件
ls -la .env.dev

# 2. 启动开发环境
.\scripts\start-dev.bat

# 3. 检查开发服务状态
docker-compose -f docker-compose.dev.yml ps

# 4. 检查 Ollama 模型
ollama list

# ===========================================
# 生产环境检查
# ===========================================

# 1. 检查生产环境配置
cat .env.prod | findstr PASSWORD

# 2. 确保密码已配置（不能为空）
# 如果密码为空，请编辑 .env.prod 文件

# 3. 启动生产环境
.\scripts\start-prod.bat

# 4. 检查生产服务状态
docker-compose ps

# 5. 检查所有密码是否已设置
docker-compose exec postgres psql -U prod_user -d app_prod -c "SELECT 1"
```

### 6.6 搜索服务配置

联网搜索相关配置位于 `.env.dev` / `.env.prod`：

```env
# 搜索引擎：duckduckgo / searxng / tavily
SEARCH_PROVIDER=searxng

# SearXNG 私有化部署地址（使用 searxng 时必填）
SEARXNG_BASE_URL=http://searxng:8080
SEARXNG_TIMEOUT=10

# 搜索结果数量与抓取
SEARCH_MAX_RESULTS=10
SEARCH_MAX_FETCH=5
SEARCH_FETCH_TIMEOUT=10
SEARCH_MIN_CONTENT_LENGTH=100

# LLM Query 改写与重排
SEARCH_ENABLE_MULTI_QUERY=true
SEARCH_NUM_QUERIES=3
SEARCH_ENABLE_RERANK=true
SEARCH_RERANK_MODEL=qllama/bge-reranker-v2-m3:latest
SEARCH_RERANK_PROVIDER=ollama
SEARCH_RERANK_TOP_K=5

# Redis 缓存
SEARCH_CACHE_TTL=3600
SEARCH_CONTENT_CACHE_TTL=86400

# Function Calling / ReAct Agent（默认关闭）
SEARCH_ENABLE_FUNCTION_CALLING=false
SEARCH_ENABLE_REACT=false
SEARCH_REACT_MAX_STEPS=3
SEARCH_AGENT_FALLBACK_TO_PHASE2=true
```

**说明**：
- 启用 `SEARCH_ENABLE_RERANK=true` 后，默认通过 Ollama 本地调用 `qllama/bge-reranker-v2-m3:latest`；若切换为 `SEARCH_RERANK_PROVIDER=sentence_transformers`，则需联网下载对应 HuggingFace 模型
- 使用 SearXNG 可避免 DuckDuckGo 的反爬限制，项目已内置 `searxng-dev` / `searxng-prod` 服务
- Function Calling / ReAct 对本地模型的指令遵循能力要求较高，建议充分测试后再开启

### 6.7 会话标题生成配置

会话标题生成相关配置位于 `.env.dev` / `.env.prod`：

```env
# 会话标题自动生成（基于 LLM）
TITLE_GENERATION_ENABLED=true
TITLE_MAX_LENGTH=30
TITLE_FALLBACK_LENGTH=30
# 留空则默认使用 FAST_LLM_MODEL_NAME
TITLE_GENERATION_MODEL=
```

**说明**：
- 启用后，新建会话发送第一条消息时，后端会自动调用本地 LLM 生成会话标题
- 生成失败时会降级为截取用户问题前 `TITLE_FALLBACK_LENGTH` 个字作为标题
- 如需使用更轻量的模型专门生成标题，可设置 `TITLE_GENERATION_MODEL`（如 `qwen2.5:7b`）

### 6.8 安全配置建议

**生产环境安全建议**:

1. **修改默认密码**: 不要使用默认密码，修改 `.env.prod` 中的所有密码
2. **设置强 SECRET_KEY**: `SECRET_KEY` 必须设置为长随机字符串，否则后端容器会拒绝启动
3. **限制网络访问**: 仅允许必要的端口对外访问
4. **启用 HTTPS**: 使用 HTTPS 加密传输
5. **配置防火墙**: 使用 ufw 或 iptables 限制访问
6. **定期备份**: 定期备份数据库和 MinIO 数据
7. **最小权限原则**: 为数据库用户分配最小必要权限

---

## 7. 监控告警

### 7.1 Prometheus 配置

Prometheus 配置文件位于 `configs/prometheus/prometheus.yml`（开发环境使用 `configs/prometheus/prometheus.dev.yml`），包含：

- **scrape_configs**: 配置监控目标
- **alerting**: 配置告警规则
- **rule_files**: 告警规则文件

**配置示例**:

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

rule_files:
  - "alerts.yml"

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'fastapi-app'
    static_configs:
      - targets: ['backend:8000']

  - job_name: 'postgresql'
    static_configs:
      - targets: ['postgres-exporter:9187']
```

### 7.2 Grafana 配置

Grafana 数据源已自动配置，访问地址：http://localhost:3000

**登录凭证**:
- 用户名: `admin`
- 密码: 见 `.env.dev` (开发) 或 `.env.prod` (生产)

**仪表盘**:
- 当前未预配置仪表盘（`configs/grafana/` 仅包含数据源 provisioning）
- 如需可视化监控指标，请在 Grafana 中手动导入或创建仪表盘（可参考 7.3 节的指标列表）

### 7.3 监控指标

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

### 7.4 告警规则

告警规则定义在 `configs/prometheus/alerts.yml`：

```yaml
groups:
  - name: fastapi_alerts
    rules:
      - alert: FastAPIHighErrorRate
        expr: rate(rag_request_errors_total[1m]) > 0.05
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "FastAPI 错误率过高"
          description: "错误率超过 5%，当前值 {{ $value }}"

      - alert: FastAPILatencyHigh
        expr: histogram_quantile(0.95, sum(rate(rag_request_duration_seconds_bucket[5m])) by (le)) > 5
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "FastAPI 响应延迟过高"
          description: "P95 延迟超过 5 秒，当前值 {{ $value }}s"

      - alert: FastAPIDown
        expr: up{job="fastapi-app"} == 0
        for: 30s
        labels:
          severity: critical
        annotations:
          summary: "FastAPI 服务不可用"
          description: "FastAPI 服务已停止响应"
```

### 7.5 Alertmanager 配置

Alertmanager 配置文件位于 `configs/alertmanager/alertmanager.yml`：

```yaml
global:
  resolve_timeout: 5m

route:
  group_by: ['alertname']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 1h
  receiver: 'webhook'

receivers:
  - name: 'webhook'
    # 默认未配置真实 webhook 接收端，告警仅在 Alertmanager UI 中展示。
    # 如需启用 webhook 通知，取消下方注释并填写真实 URL，建议设置 send_resolved: true
    # webhook_configs:
    #   - url: 'http://your-webhook-endpoint/alert'
    #     send_resolved: true

inhibit_rules:
  - source_match:
      severity: 'critical'
    target_match:
      severity: 'warning'
    equal: ['alertname', 'dev', 'instance']
```

> **说明**：
> - receiver 名称与 `configs/alertmanager/alertmanager.yml` 保持一致（`webhook`）。
> - `inhibit_rules.equal` 包含 `dev` 标签，用于按环境隔离抑制告警。
> - 启用 webhook 时建议设置 `send_resolved: true`，以便告警恢复时通知接收端。

---

## 8. 常见问题

### 8.1 Ollama 服务未启动

**问题**: `ollama list` 命令失败

**解决方案**:

```bash
# 启动 Ollama 服务
ollama serve

# 后台启动（Linux）
nohup ollama serve > /var/log/ollama.log 2>&1 &

# 设置自启（Linux）
systemctl enable ollama
systemctl start ollama

# 检查服务状态
ollama list
```

### 8.2 PostgreSQL 连接失败

**问题**: 后端无法连接到数据库

**检查事项**:

| 检查项 | 开发环境命令 | 生产环境命令 |
|--------|-------------|-------------|
| 服务状态 | `docker-compose -f docker-compose.dev.yml ps postgres` | `docker-compose ps postgres` |
| 连接参数 | 检查 `.env.dev` | 检查 `.env.prod` |
| 端口占用 | `netstat -tlnp` 过滤 5433 | `netstat -tlnp` 过滤 5433 |
| 密码验证 | `docker exec -it postgres-dev psql -U dev_user -d app_dev` | `docker exec -it postgres-prod psql -U prod_user -d app_prod` |

**解决方案**:

```bash
# 开发环境修改数据库密码
docker exec -it postgres-dev bash
psql -U dev_user -d app_dev
ALTER USER dev_user WITH PASSWORD 'new_password';

# 生产环境修改数据库密码
docker exec -it postgres-prod bash
psql -U prod_user -d app_prod
ALTER USER prod_user WITH PASSWORD 'new_password';
```

### 8.3 前端无法连接后端

**问题**: 前端页面无法访问 API

**检查事项**:

| 检查项 | 说明 |
|--------|------|
| 后端服务状态 | 检查 `start.py` 是否运行 |
| 代理配置 | `vite.config.js` 中的 proxy 配置 |
| CORS 配置 | 后端是否允许前端域名 |
| 端口访问 | 8000 端口是否可访问 |

**解决方案**:

```bash
# 检查后端服务
curl http://localhost:8000/health

# 检查前端代理配置
cat frontend/vite.config.js
```

### 8.4 模型拉取失败

**问题**: `ollama pull` 失败

**解决方案**:

```bash
# 手动拉取模型
ollama pull deepseek-r1:7b-qwen-distill-q4_K_M
ollama pull qwen2.5:7b
ollama pull bge-m3:latest
ollama pull qllama/bge-reranker-v2-m3:latest

# 查看已安装模型
ollama list
```

### 8.5 Milvus 连接失败

**错误信息**: `"service unavailable: internal: Milvus Proxy is not ready yet"`

**解决方案**:

```bash
# 等待 Milvus 服务完全启动
sleep 60

# 开发环境检查 Milvus 状态
docker-compose -f docker-compose.dev.yml logs milvus-standalone | findstr "Proxy is ready"

# 生产环境检查 Milvus 状态
docker-compose logs milvus-standalone | findstr "Proxy is ready"

# 重启 Milvus 服务（使用 Compose 服务名）
docker-compose -f docker-compose.dev.yml restart milvus-standalone

# 检查 MinIO 日志（使用 Compose 服务名）
docker-compose -f docker-compose.dev.yml logs minio
```

### 8.6 Redis 连接失败

**问题**: 后端日志显示无法连接 Redis，联网搜索缓存失效

**解决方案**:

```bash
# 检查 Redis 容器状态
docker-compose -f docker-compose.dev.yml ps redis
docker-compose ps redis

# 查看 Redis 日志（使用 Compose 服务名）
docker-compose -f docker-compose.dev.yml logs redis

# 测试 Redis 连接
docker-compose -f docker-compose.dev.yml exec redis redis-cli ping
```

**注意**：Redis 连接失败不会阻塞主流程，系统会回退到无缓存模式运行。

### 8.7 配置不一致

**问题**: 环境变量配置为空或不一致

**解决方案**:

```bash
# 检查开发环境配置
cat .env.dev

# 检查生产环境配置
cat .env.prod

# 确保 .env 文件存在
ls -la .env

# 如果 .env 不存在，复制对应的环境文件
# 开发环境：
copy .env.dev .env

# 生产环境：
copy .env.prod .env
```

### 8.8 环境切换失败

**问题**: 切换环境后服务无法启动

**解决方案**:

```bash
# 1. 确保当前环境已完全停止
# 开发环境：
docker-compose -f docker-compose.dev.yml down

# 生产环境：
docker-compose down

# 2. 检查端口占用
netstat -tlnp | findstr "5433 6379 8000 9000 9001 19530"

# 3. 重新启动目标环境
.\scripts\start-dev.bat
# 或
.\scripts\start-prod.bat

# 4. 检查服务状态
docker-compose ps
# 或
docker-compose -f docker-compose.dev.yml ps
```

### 8.9 内存不足

**问题**: 模型运行时内存不足

**建议配置**:

| 模型 | 建议内存 |
|------|----------|
| 7B 模型 | 8GB+ |
| 13B 模型 | 16GB+ |
| 33B 模型 | 32GB+ |

**优化方案**:
- 使用量化模型（如 `q4_K_M`）
- 调整 uvicorn `--workers` 数量
- 增加交换空间

```bash
# 创建交换空间
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
```

### 8.10 文档上传失败

**问题**: 上传文档时返回 500 错误

**检查事项**:
- MinIO 服务状态
- 文件大小限制
- 文档格式支持

**解决方案**:

```bash
# 开发环境检查 MinIO 状态
docker-compose -f docker-compose.dev.yml logs minio

# 生产环境检查 MinIO 状态
docker-compose logs minio

# 检查后端日志
docker-compose logs backend

# 检查文件大小限制
cat backend/src/config.py | findstr MAX_FILE_SIZE
```

---

## 9. 故障排查

### 9.1 日志排查

```bash
# ===========================================
# 开发环境日志
# ===========================================

# 查看所有服务日志
docker-compose -f docker-compose.dev.yml logs -f

# 查看特定服务日志（使用 Compose 服务名）
docker-compose -f docker-compose.dev.yml logs -f postgres
docker-compose -f docker-compose.dev.yml logs -f minio
docker-compose -f docker-compose.dev.yml logs -f milvus-standalone
docker-compose -f docker-compose.dev.yml logs -f redis

# 查看最近日志
docker-compose -f docker-compose.dev.yml logs --tail=100 postgres

# 搜索错误日志
docker-compose -f docker-compose.dev.yml logs postgres | findstr -i error
docker-compose -f docker-compose.dev.yml logs redis | findstr -i error

# ===========================================
# 生产环境日志
# ===========================================

# 查看所有服务日志
docker-compose logs -f

# 查看特定服务日志（使用 Compose 服务名）
docker-compose logs -f backend
docker-compose logs -f postgres

# 查看最近日志
docker-compose logs --tail=100 backend

# 搜索错误日志
docker-compose logs backend | findstr -i error
```

### 9.2 健康检查

```bash
# 检查后端服务
curl http://localhost:8000/health

# 检查数据库连接（开发环境）
docker exec -it postgres-dev psql -U dev_user -d app_dev -c "SELECT 1"

# 检查数据库连接（生产环境）
docker exec -it postgres-prod psql -U prod_user -d app_prod -c "SELECT 1"

# 检查 Milvus 连接
python -c "from pymilvus import connections; connections.connect('default', 'localhost', 19530); print('Connected')"

# 检查 MinIO 连接
python -c "from minio import Minio; c = Minio('localhost:9000', access_key='dev_minio', secret_key='dev_minio_password'); print('Connected')"

# 测试 Redis 连接
docker-compose -f docker-compose.dev.yml exec redis redis-cli ping
```

### 9.3 性能排查

```bash
# 查看 CPU 使用率
top
htop

# 查看内存使用
free -h

# 查看磁盘使用
df -h

# 查看网络连接
netstat -tlnp
ss -tlnp

# 查看 Docker 资源使用
docker stats

# 开发环境查看特定容器资源
docker stats postgres-dev minio-dev

# 生产环境查看特定容器资源
docker stats backend-prod postgres-prod
```

### 9.4 端口测试

```bash
# 测试端口是否可访问
nc -zv localhost 5433
nc -zv localhost 8000
nc -zv localhost 9000
nc -zv localhost 19530

# 使用 curl 测试
curl -I http://localhost:8000/health
curl -I http://localhost:9090
```

---

## 附录：端口汇总

| 服务 | 端口 | 说明 | 环境 |
|------|------|------|------|
| 前端（开发） | 5173 | Vue3 开发服务器 | 开发 |
| 前端（生产） | 80/443 | Nginx 代理 | 生产 |
| 后端 API | 8000 | FastAPI 服务 | 两者 |
| PostgreSQL | 5433 | 业务数据库 | 两者 |
| MinIO | 9000 | 对象存储 | 两者 |
| MinIO 控制台 | 9001 | 管理界面 | 两者 |
| Milvus | 19530 | 向量数据库 | 两者 |
| Redis | 6379 | 搜索结果与网页内容缓存 | 两者 |
| SearXNG | 8080 | 私有化聚合搜索引擎 | 两者 |
| Prometheus | 9090 | 监控指标 | 两者 |
| Alertmanager | 9093 | 告警管理 | 生产 |
| Grafana | 3000 | 监控面板 | 两者 |
| PostgreSQL Exporter | 9187 | 指标导出 | 生产 |

**注意**: 开发/生产环境使用相同的端口，但数据完全隔离。

---

## 附录：命令速查

```bash
# ===========================================
# 快速启动
# ===========================================

# 开发环境
.\scripts\start-dev.bat

# 生产环境
.\scripts\start-prod.bat

# ===========================================
# 开发环境命令
# ===========================================

docker-compose -f docker-compose.dev.yml up -d
docker-compose -f docker-compose.dev.yml ps
docker-compose -f docker-compose.dev.yml logs -f
docker-compose -f docker-compose.dev.yml down
docker-compose -f docker-compose.dev.yml down -v
docker-compose -f docker-compose.dev.yml restart postgres
docker-compose -f docker-compose.dev.yml restart redis

# ===========================================
# 生产环境命令
# ===========================================

docker-compose up -d
docker-compose ps
docker-compose logs -f
docker-compose down
docker-compose down -v
docker-compose restart backend

# ===========================================
# 环境切换
# ===========================================

# 停止当前环境
docker-compose down
# 或
docker-compose -f docker-compose.dev.yml down

# 切换到新环境
.\scripts\start-dev.bat
# 或
.\scripts\start-prod.bat
```
