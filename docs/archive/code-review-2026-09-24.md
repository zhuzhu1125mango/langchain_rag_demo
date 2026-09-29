# 代码审查报告（2026-09-24）

> 状态：已全部修复（P0×6 + P1×22 于 W1~W5 落地，P2×78 按 W6 八个批次全部落账——实施 66 / 已核实无需处理 10 / 记录取舍 2；处理计划见 [fix-plan-2026-09-24.md](fix-plan-2026-09-24.md)）
> 审查范围：backend/、frontend/、docker-compose.{yml,dev.yml}、configs/、scripts/、.env.example、docs/、.git 跟踪文件。
> 方法：静态代码审查 + 自动化探索 + 关键路径人工复核。
> 基准：当前工作区存在大量未提交改动（`git status` 显示 13 个修改文件、9 个新增文件），本报告以工作区实际内容为准。
> 关联：上一轮审查见 [code-review-2026-09-23.md](code-review-2026-09-23.md)。

---

## 1. 执行摘要

项目整体工程质量仍处于"高于演示项目"的水准：认证体系完整、SQL 参数化、上传校验严格、CORS 白名单、生产栈端口回环绑定、镜像固定 tag、文档结构规范。但近一周工作区改动较大，引入了若干新的高优先级问题，同时前次审查的部分 P0/P1 项尚未完全收敛。

本次审查共识别问题 **约 50 项**，按严重程度分类如下：

| 级别 | 数量 | 说明 |
|------|------|------|
| P0 | 6 | 安全漏洞、部署失败风险、敏感信息入库，需立即处理 |
| P1 | 22 | 权限边界、SSRF、认证强度、架构债务、配置不一致，建议本周内处理 |
| P2 | 22+ | 代码质量、性能、测试覆盖、文档细节，建议纳入日常迭代 |

**与 2026-09-23 审查对比：**
- 已修复/改善：`.env.example` 已补充 `VITE_API_KEY` 说明；`docker-compose.dev.yml` 基础设施端口已改为 `127.0.0.1` 回环绑定；dev Prometheus 已移除 `--web.enable-lifecycle`。
- 仍未修复：`.env.dev` / `.env.prod` 真实凭据明文落盘、`/metrics` 匿名可读、`fetch_content` 的 SSRF 重定向绕过、`VITE_API_KEY` 仍被打包、生产 `frontend.depends_on` 缺少健康检查条件等。
- 新增风险：工作区新增 Wiki 导航/可导航工作空间相关代码，带来了新的 Agent 工具越权、长事务、未提交脚本依赖等风险；`index.html` 入口文件错误、`TypeScript` 版本号异常等前端构建问题需立即关注。

---

## 2. P0 严重问题（立即处理）

### P0-1 前端 `index.html` 入口文件错误
- **位置：** `frontend/index.html:14`
- **问题：** `<script type="module" src="/src/main.js"></script>`，但项目实际入口为 `src/main.ts`。开发时 Vite 可能通过容错加载，但生产构建或某些严格环境下会 404，导致页面空白。
- **修复：** 改为 `<script type="module" src="/src/main.ts"></script>`。

### P0-2 `VITE_API_KEY` 被打包进前端产物
- **位置：**
  - `frontend/src/utils/axios.ts:33`
  - `frontend/src/router/index.ts:107`
  - `frontend/src/composables/useNotifications.ts:72`
  - `frontend/src/components/knowledge-base/UploadDocumentDialog.vue:254`
  - `frontend/src/components/knowledge-base/WikiDrawer.vue:312`
  - `frontend/src/views/ChatView.vue:420`
- **问题：** 6 处读取 `import.meta.env.VITE_API_KEY`。Vite 会将 `VITE_` 前缀变量在构建时替换为字面量并打包进 `dist/`，任何用户都可在浏览器开发者工具或下载的 JS 中读取该 Key，无法实现访问控制。
- **修复：** 移除前端 `VITE_API_KEY` 回退逻辑；若需无登录访问，由后端通过短期 token / cookie 会话鉴权。`.env.example` 虽有说明，但代码未下线该机制。

### P0-3 JWT 存储在 `localStorage`，易受 XSS 窃取
- **位置：**
  - `frontend/src/stores/app.ts:80`、`frontend/src/stores/app.ts:89`、`frontend/src/stores/app.ts:111`
  - `frontend/src/utils/axios.ts:29-36`
  - `frontend/src/router/index.ts:101-107`
- **问题：** Token 明文写入 `localStorage`，一旦存在 XSS 漏洞即可被盗取；路由守卫仅检查 token 存在性，不验证是否过期或有效。
- **修复：** 优先改用 `httpOnly` Cookie（后端设置 `SameSite=Lax; Secure; HttpOnly`）；如必须前端存储，使用内存变量 + refresh token 机制，避免长期凭证落地 localStorage。

### P0-4 Agent 可越权访问其他用户的知识库
- **位置：**
  - `backend/src/services/agent_orchestrator.py:354-357`
  - `backend/src/services/tools/plugins/kb_search_tool.py:61`
- **问题：** Agent 编排层仅在 `args.get("kb_ids")` 为空时才注入当前会话的 `kb_ids`，显式允许 LLM/工具参数覆盖。`KBSearchTool.execute` 直接使用 `kwargs.get("kb_ids")`，不做归属校验。攻击者可诱导模型调用 `kb_search` 并传入任意 `kb_ids`，检索其他用户的知识库内容。
- **修复：** 在 Agent 编排层固定注入 `kb_ids` 并删除外部传入的 `kb_ids`；`KBSearchTool` 执行前强制调用 `validate_kb_ownership` 校验归属。

### P0-5 SSRF：搜索内容抓取未防御重定向绕过
- **位置：**
  - `backend/src/services/web_search_service.py:444-448`（`follow_redirects=True`）
  - `backend/src/services/web_search_service.py:671-698`（`fetch_content`）
  - `backend/src/services/tools/plugins/fetch_webpage_tool.py:27-52`
- **问题：** `fetch_content()` 已调用 `validate_url_safe(url)`，但 `httpx.AsyncClient` 配置了 `follow_redirects=True`，仅对原始 URL 校验，302/301 重定向到内网地址（如 `169.254.169.254`）可绕过校验。`web_search` 返回的搜索结果也可能包含内网 URL。
- **修复：** 禁用 `follow_redirects` 或实现重定向后的二次校验；对搜索抓取的 URL 增加白名单/黑名单；在 `fetch_webpage` 中限制只能访问已注册搜索结果的 URL。

### P0-6 `configs/license/minio.license` 敏感信息入库
- **位置：** `configs/license/minio.license:1`
- **问题：** 该文件已被 Git 跟踪，内容疑似 MinIO 订阅许可证密钥。若许可证真实有效，属于敏感信息入库，泄露后难以轮换。
- **修复：** 立即确认文件性质；若为真实许可证，轮换 key 并使用 `git-filter-repo` 或 BFG 从历史中彻底删除；改由环境变量或 Docker Secret 注入。

---

## 3. P1 重要问题（本周内处理）

### P1-1 `.env.dev` / `.env.prod` 真实凭据明文落盘
- **位置：** `.env.dev`、`.env.prod`
- **问题：** 工作区存在 `.env.dev` 和 `.env.prod`，包含真实的 `SECRET_KEY`、数据库密码、MinIO Secret、Redis 密码、Tavily API Key。虽然 `.gitignore` 已忽略，但本地磁盘明文存放仍有泄露/误截图/同步盘上传风险；当前 `.env.prod` 中的密钥一旦已用于任何环境，应视为已泄露。
- **修复：** 立即轮换这些凭据；将本地 `.env.dev` / `.env.prod` 从工作区移除，仅保留 `.env.example` 作为模板；生产环境改用 Docker Secrets / Vault / 1Password / Doppler 等 secrets 管理工具。

### P1-2 开发模式默认匿名放行，生产裸跑易误配
- **位置：** `backend/src/auth.py:179-188`
- **问题：** 当未配置 `API_KEY` 且非 Docker 时直接返回 `_DEFAULT_USER`。若生产环境以非 Docker 方式部署且未配置 `API_KEY`，会自动匿名放行所有请求。
- **修复：** 默认拒绝匿名访问；开发模式通过显式环境变量（如 `ALLOW_ANONYMOUS=true`）开启，而不是靠 `IN_DOCKER` 推断。

### P1-3 URL 安全校验存在 DNS 重绑定漏洞
- **位置：** `backend/src/utils/security.py:186-190`
- **问题：** `validate_url_safe` 只对 IP 字面量做限制，遇到域名直接放行，注释也承认存在 DNS 重绑定风险。后续抓取时实际解析可能指向云元数据或内网地址。
- **修复：** 对域名执行同步 DNS 解析并二次校验解析后的 IP；或禁止访问未在白名单的域名；结合出站防火墙规则。

### P1-4 生产环境 CORS 配置允许通配符与凭据同时存在
- **位置：** `backend/src/config.py:96-117`
- **问题：** `ALLOW_CREDENTIALS: bool = True`，且 `ALLOW_METHODS`、`ALLOW_HEADERS` 默认均为 `["*"]`。如果 `.env` 中把 `ALLOWED_ORIGINS` 也配成 `*`，浏览器会允许任意来源携带 Cookie/凭证访问，造成 CSRF/凭据泄露风险。
- **修复：** 在校验器中拒绝 `*` 与 `allow_credentials=True` 同时出现；生产环境严格限定 `ALLOWED_ORIGINS`。

### P1-5 认证接口缺少速率限制
- **位置：** `backend/src/api/auth.py:74-124`
- **问题：** `/auth/register` 和 `/auth/login` 无速率限制/验证码，可被暴力破解用户名、密码枚举、批量注册。
- **修复：** 增加基于 IP/用户名的登录失败锁定、验证码或至少引入 `slowapi` 等限流中间件。

### P1-6 JWT 有效期过长且无刷新/撤销机制
- **位置：** `backend/src/config.py:86`、`backend/src/auth.py:96`
- **问题：** `ACCESS_TOKEN_EXPIRE_MINUTES = 1440`（24 小时），无 refresh token、无黑名单/撤销能力。用户改密或账号被封后，旧 token 仍长期有效。
- **修复：** 缩短 access token 有效期（如 15-30 分钟）；增加 refresh token 机制；维护 token 黑名单或结合服务端会话表校验。

### P1-7 MinIO 对象键直接使用上传文件名
- **位置：** `backend/src/services/minio_service.py:55-77`
- **问题：** `file_key = f"documents/{file_id or uuid.uuid4()}/{file.filename}"` 未对 `file.filename` 做清洗，可能包含 `..`、`/`、空字节或不可见字符，导致对象键异常、日志注入，甚至影响 MinIO 的 bucket 结构。
- **修复：** 生成对象键时使用随机 UUID 或严格的白名单清洗文件名；不要将用户可控文件名直接拼入路径。

### P1-8 `CacheService.clear_pattern` 使用 Redis `KEYS` 命令
- **位置：** `backend/src/services/cache_service.py:217-225`
- **问题：** `KEYS pattern` 在生产 Redis 中会阻塞单线程事件循环，KEY 数量大时会导致服务卡顿甚至 OOM。
- **修复：** 改用 `SCAN` 迭代删除；或按固定前缀分桶，删除时直接 `UNLINK` 单个 key。

### P1-9 文档处理后台任务长期占用数据库连接
- **位置：** `backend/src/api/document.py:177-418`
- **问题：** `process_document_async` 在单个 `async_session_maker()` 中执行 PDF 下载、解析、向量化、LLM 分析、Wiki 编译等，多次 `commit`，整个过程可能持续数分钟，长期占用数据库连接。
- **修复：** 将解析/向量化等耗时 IO 与数据库事务解耦；只在需要持久化状态时开启短事务。

### P1-10 聊天接口在生成回答期间占用请求级数据库会话
- **位置：** `backend/src/api/chat.py:70-194`
- **问题：** `send_message` 使用 `get_db` 注入的会话，在 `rag_chain.run(...)` 调用期间一直持有连接；LLM 调用可能耗时数十秒，造成连接池耗尽。
- **修复：** 在调用 LLM/检索前释放请求级会话，持久化操作使用独立短事务或后台任务。

### P1-11 `/api/config` 运行时修改全局配置且无持久化
- **位置：** `backend/src/api/config.py:89-148`、`backend/src/api/config.py:167-191`
- **问题：** `update_processing_config` 直接修改内存中的 `settings`，影响所有并发请求；重启后丢失。虽然有审计调用，但如果审计失败会被吞掉。
- **修复：** 配置变更落库或写回环境配置文件；变更前校验对当前任务的影响；审计失败应告警而非静默。

### P1-12 文档列表接口未分页
- **位置：** `backend/src/api/document.py:912-980`
- **问题：** `list_documents` 一次性加载某用户所有文档及关联 `knowledge_base`、`category`，当文档数量大时会造成慢查询和内存峰值。
- **修复：** 增加 `skip`/`limit` 分页，并对常用查询字段加索引。

### P1-13 前端 `ChatView.vue` / `KnowledgeBaseView.vue` 职责过重
- **位置：**
  - `frontend/src/views/ChatView.vue:1-808`
  - `frontend/src/views/KnowledgeBaseView.vue:1-738`
- **问题：** 单文件超过 700-800 行，同时承担 SSE 流式、会话管理、知识库 CRUD、文档搜索、Wiki 弹窗、WebSocket 通知等职责，难以测试和维护。
- **修复：** 将 SSE 连接抽取为 `useChatStream` composable，将知识库 CRUD/文档操作抽取为 service/composable，视图层只做编排。

### P1-14 `package.json` 中 TypeScript 版本号异常
- **位置：** `frontend/package.json:50`
- **问题：** `"typescript": "^6.0.3"`。TypeScript 目前最新稳定版为 5.x，`6.0.3` 不是有效版本，可能导致 `pnpm install` 失败或解析到异常包。
- **修复：** 改为当前验证过的稳定版本，如 `^5.6.3`。

### P1-15 `DocumentSourceModal.vue` 使用 `v-html` 渲染用户可控内容
- **位置：** `frontend/src/components/DocumentSourceModal.vue:44-47`
- **问题：** `highlightedContent` 虽经过自定义 `escapeHtml`，但 `v-html` 仍是高危点；`escapeHtml` 未覆盖所有 HTML 实体与边界情况。
- **修复：** 避免 `v-html`，改为将文本分段渲染：普通文本用 `{{ }}`，高亮片段用 `<mark>` 包裹；或引入 `DOMPurify` 二次净化后再 `v-html`。

### P1-16 Docker 开发镜像 `uv` 未固定版本
- **位置：** `backend/Dockerfile.dev:23`
- **问题：** `COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv`，`latest` 会随时间漂移，构建不可复现。
- **修复：** 固定到具体版本，例如 `COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /usr/local/bin/uv`。

### P1-17 生产 `frontend.depends_on` 缺少健康检查条件
- **位置：** `docker-compose.yml:96-113`
- **问题：** 生产 `frontend` 服务 `depends_on: backend` 没有 `condition: service_healthy`，仅等待容器启动；而 `docker-compose.yml` 的 `backend` 服务未声明 `healthcheck`（仅在 Dockerfile 内声明），Nginx 启动后可能立即收到后端 502。
- **修复：** 在 `docker-compose.yml` 的 `backend` 服务中增加与 dev 一致的 `healthcheck` 段，并给 `frontend.depends_on` 添加 `condition: service_healthy`。

### P1-18 `SKIP_OLLAMA_CHECK` 在容器内可能不生效
- **位置：**
  - `docker-compose.yml:68`
  - `docker-compose.dev.yml:156`
  - `backend/Dockerfile.dev:45`
- **问题：** Compose 注入了 `SKIP_OLLAMA_CHECK=true`，但 `Dockerfile.dev` 的启动命令是 `uvicorn src.main:app`，直接走 `main.py` 的生命周期校验；`main.py` 中的模型校验可能未读取 `SKIP_OLLAMA_CHECK`，导致该环境变量在容器内实际不生效。
- **修复：** 在 `main.py` 的模型校验中增加 `os.getenv("SKIP_OLLAMA_CHECK")` 跳过逻辑，或把容器 `CMD` 改为使用 `start.py`。

### P1-19 Linux Docker 中 `host.docker.internal` 无法解析
- **位置：**
  - `docker-compose.yml:67`
  - `docker-compose.dev.yml:155`
- **问题：** 后端容器将 `OLLAMA_HOST` 硬编码为 `host.docker.internal`。在 Linux Docker Engine（非 Docker Desktop）中，该域名默认不会自动解析，后端启动时无法连接到 Ollama。
- **修复：** 为 Linux 环境增加 `extra_hosts: ["host.docker.internal:host-gateway"]`，或提供可覆盖 `OLLAMA_HOST` 的环境变量说明。

### P1-20 `init_db()` 与 `alembic upgrade head` 竞争
- **位置：**
  - `backend/src/main.py:237`
  - `scripts/start-prod.sh:333-349`
- **问题：** `main.py` 在启动生命周期中调用 `init_db()`（即 `Base.metadata.create_all`），而生产启动脚本随后又执行 `alembic upgrade head`；二者竞争，可能导致 `DuplicateTableError`，脚本 fallback 到 `alembic stamp head`，掩盖真实迁移状态。
- **修复：** 从 `main.py` 的 lifespan 中移除 `init_db()`，让数据库初始化完全由 `alembic upgrade head` 负责；或在 `init_db()` 前判断 `alembic_version` 是否存在。

### P1-21 监控告警配置存在缺陷
- **位置：**
  - `configs/prometheus/alerts.yml:118-126`（`LLMCallErrors` 除以零）
  - `configs/alertmanager/alertmanager.yml:42-47`（抑制规则 `dev` 标签不存在）
  - `configs/grafana/datasources/prometheus.yml:17`、`configs/grafana/datasources/prometheus.yml:24-25`（版本不匹配、`tlsSkipVerify` 位置错误）
- **问题：**
  - `LLMCallErrors` 在分母速率为 0 时产生 Inf/NaN；
  - `inhibit_rules.equal` 包含 `'dev'` 标签，但告警规则未生成该标签，抑制永不匹配；
  - Grafana 数据源声明 `prometheusVersion: 2.40.0`，实际镜像为 `v3.5.0`；`tlsSkipVerify` 被错误放在 `secureJsonData` 下。
- **修复：** 使用 `clamp_min(rate(...), 1e-9)` 作为分母；给告警规则增加 `env` 标签或移除 `dev` 抑制条件；将 `prometheusVersion` 改为 `3.5.0` 并将 `tlsSkipVerify` 移到 `jsonData`。

### P1-22 文档/代码/环境配置三者严重漂移
- **位置：** `README.md`、`docs/guide/deployment.md`、`.env.example`、`.env.dev`、`.env.prod`
- **问题：**
  - README 仍写"Grafana 已部署但尚无 dashboard"，但 `configs/grafana/dashboards/rag_overview.json` 与 provisioning 已存在；
  - README 推荐 reranker 模型 `qllama/bge-reranker-v2-m3:latest` + Ollama，但 `.env.example` 使用 `BAAI/bge-reranker-v2-m3` + `sentence_transformers`；
  - README 中 MinIO 版本 `RELEASE.2025-09-07...` 与实际 compose 中 `pgsty/minio:RELEASE.2026-06-18...` 不一致；
  - 部署文档中的告警规则示例与 `configs/prometheus/alerts.yml` 不一致。
- **修复：** 以 `.env.example` 和当前 `config.py` 默认值为唯一事实来源，统一并更新所有文档中的模型、provider、镜像 tag、告警规则示例；删除"尚无 dashboard"等过时描述。

---

## 4. P2 一般问题（日常迭代消化）

### 后端代码质量

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 1 | `backend/src/api/document.py:31/59`、`backend/src/api/document.py:33/174` | 重复导入 `settings` 和 `schedule_invalidation` | 整理 imports，删除重复 |
| 2 | `backend/src/api/document.py:253-271`、`backend/src/api/document.py:1217-1236` | `type_labels`/`domain_labels` 硬编码重复 | 提取到统一常量模块或配置表 |
| 3 | `backend/src/api/document.py:541-563`、`backend/src/api/document.py:656-676` | 上传与批量上传重复实现 kb_id 解析/归属校验 | 抽取 `_resolve_target_kb` 公共函数 |
| 4 | `backend/src/api/chat.py:197-510` | `stream_answer` 超过 300 行，职责混杂 | 拆分为 `_prepare_session`、`_generate_sse`、`_persist_assistant_message` 等 |
| 5 | `backend/src/services/rag_chain.py`（约 2000 行）、`backend/src/api/document.py`（约 1600 行）、`backend/src/services/web_search_service.py`（约 1000 行） | 巨型文件 | 按职责拆分为多个模块 |
| 6 | `backend/src/services/knowledge_graph_generator.py:112` | 裸 `except:` 捕获 `KeyboardInterrupt` 等 | 改为捕获具体异常 |
| 7 | `backend/src/services/vector_store.py:46-53` | 混合检索失败裸 `except Exception` 静默降级 | 仅捕获已知可恢复异常，其他异常抛出 |

### 后端安全与资源

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 8 | `backend/src/auth.py:233-236` | `get_current_user_for_ws` 裸 `except Exception` | 仅捕获 `JSONDecodeError`、`TypeError` 等具体异常 |
| 9 | `backend/src/utils/security.py:155-193` | SSRF 域名放行（同 P1-3） | 见 P1-3 |
| 10 | `backend/src/services/cache_service.py:56-63` | Redis 强制要求密码，本地无密码 Redis 无法启动 | 允许空密码并警告；生产校验强制非空 |
| 11 | `backend/src/services/minio_service.py:39-44` | `MINIO_SECURE` 默认 `false` | 生产启动校验要求 `True`；默认值改为 `True` |
| 12 | `backend/src/main.py:429` | `/metrics` 匿名可读 | 增加 `require_admin` 或 IP 白名单 |
| 13 | `backend/src/main.py:412-442` | `/health/detail` 未覆盖 Milvus/MinIO/Ollama | 增加关键依赖健康探测 |
| 14 | `backend/src/main.py:62-75` | `RotatingFileHandler` 多 worker 场景下轮转可能丢失 | 生产统一输出 stdout，由外部采集 |
| 15 | `backend/src/main.py:332-346` | 全局异常处理器日志可能记录敏感信息 | 只记录异常类型与 request_id |
| 16 | `backend/src/main.py:79-115` | 请求 `request_id` 未传递到 `services/` 日志 | 使用 `contextvars` 传递 trace_id |
| 17 | `backend/src/services/semantic_cache_service.py:100-112`、`backend/src/services/semantic_cache_service.py:135-207` | 缓存 value 明文存储用户问题与答案 | 对 value 做字段级加密；按用户隔离 Redis DB/ACL |
| 18 | `backend/src/services/ocr_parser.py:117-164` | MinerU 后端遗留临时目录未清理 | 使用 `tempfile.TemporaryDirectory` 或在 `finally` 中清理 |
| 19 | `backend/src/services/document_processor.py:533-591` | 大文件全量加载到内存 | 流式解析；增加单文档内存/时间上限 |
| 20 | `backend/src/utils/async_singleton.py:131-145` | `get_instance_sync` 在运行中的事件循环中调用会失败 | 文档明确限制使用场景 |

### 后端并发与状态

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 21 | `backend/src/services/notification_service.py:59-109` | 全局 `channel_connections` 并发修改风险 | 使用 `asyncio.Lock` 或 Redis Pub/Sub |
| 22 | `backend/src/services/progress_manager.py:14-184` | 全局字典无锁；跨线程通知可能丢失 | 加锁；使用 `asyncio.run_coroutine_threadsafe` |
| 23 | `backend/src/api/document.py:819-909` | 文档搜索先全量检索再按归属过滤 | 始终传入当前用户 `kb_ids`，让 Milvus 在数据库层过滤 |
| 24 | `backend/src/services/session_service.py:32-46` | 使用 `datetime.now()` 无时区 | 统一使用 `datetime.now(timezone.utc)` |
| 25 | `backend/src/services/output_sanitizer.py:177-249` | 未对 HTML/JS 做转义 | 增加 HTML 白名单过滤或前端使用可信 Markdown 渲染器 |
| 26 | `backend/src/services/model_manager.py:251-263` | `get_embeddings()` 在异步初始化路径中同步构造 | 将构造过程放到 `asyncio.to_thread` |
| 27 | `backend/src/database.py:9-26` | 数据库连接字符串在模块导入时构建 | 延迟到应用启动生命周期，支持动态刷新 |
| 28 | `backend/src/api/document.py:982-996`、`backend/src/api/document.py:1301-1338`、`backend/src/api/knowledge_base.py:401-403` | `status` 字段直接接收任意字符串 | 使用 `Literal[...]` 或枚举限制 |
| 29 | `backend/src/api/knowledge_base.py:504-641` | 批量删除知识库可能超时 | 改为后台任务异步删除，API 立即返回任务 ID |
| 30 | `backend/src/services/wiki_compiler.py:920-925` 等 | `json.loads` 解析 LLM 输出无 schema 校验 | 使用 Pydantic 或 `jsonschema` 做结构化校验 |

### 前端代码质量

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 31 | `frontend/src/queries/chat.ts:207-213` | `useGetQuickQuestions` 返回普通函数而非 hook | 改为 `getQuickQuestions` 或真正 `useQuery` |
| 32 | `frontend/src/queries/kb.ts:136-178` | `queryKey` 包含响应式对象，缓存稳定性差 | 将参数序列化为稳定字符串 |
| 33 | `frontend/src/queries/trace.ts:85-94` | `queryKey` 包含对象引用 | 将参数解构到 queryKey |
| 34 | `frontend/src/stores/kb.ts:11-15` | 选择状态全局共享且未在路由切换时清理 | 在 `selectKnowledgeBase` 时清理文档选择 |
| 35 | 多处 | `catch { toast.error(..., error.message) }` 重复 | 封装 `handleMutationError` 工具函数 |
| 36 | `frontend/src/components/knowledge-base/DocumentTable.vue:233-234` | 表格组件直接读写全局 store | 通过 props/events 或注入 selection context |

### 前端安全与 UX

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 37 | `frontend/src/components/MarkdownRenderer.vue:98-111` | `ALLOWED_ATTR` 未显式配置，默认允许 `style` | 显式配置白名单，移除 `style`；强制 `rel="noopener noreferrer"` |
| 38 | `frontend/src/components/chat/MessageSources.vue:30-36` | 加载任意第三方 favicon，泄露用户行为 | 移除 favicon，统一使用本地图标 |
| 39 | `frontend/src/utils/axios.ts:52-58` | 401 时使用 `window.location.href` 跳转 | 使用 Vue Router 导航；提示"登录已过期" |
| 40 | `frontend/src/stores/app.ts:100-106` | `fetchMe` 静默吞掉所有错误 | 区分 401/网络错误，主动登出并提示 |
| 41 | `frontend/src/views/ChatView.vue:578-666` | SSE 错误直接写入消息气泡 | 引入消息级错误状态，渲染专用错误卡片 |
| 42 | 全局 | 缺少全局错误边界 | 在 `App.vue` 包裹 `<ErrorBoundary>` |
| 43 | `frontend/src/views/LoginView.vue:159-160` | 直接将服务端 `detail` 展示给用户 | 错误码映射为用户友好文案 |
| 44 | `frontend/src/components/knowledge-base/UploadDocumentDialog.vue:11-17`、`frontend/src/components/knowledge-base/UploadDocumentDialog.vue:143-152` | 文件上传缺少大小/数量前端校验 | 校验文件类型、大小、数量并即时提示 |
| 45 | `frontend/src/views/HistoryView.vue:172-178` | 清空历史使用原生 `confirm` | 统一使用 `ElMessageBox.confirm` |

### 前端性能

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 46 | `frontend/src/components/chat/ChatMessageList.vue:1-260` | 消息列表未虚拟化 | 引入 `vue-virtual-scroller` |
| 47 | `frontend/src/components/KnowledgeGraph.vue:41-133` | SVG 重渲染成本高 | 使用 Canvas/WebGL；做 memoization |
| 48 | `frontend/src/views/ChatView.vue:319-377` | 输入同时触发多个防抖请求 | 合并防抖；使用 `AbortController` 取消过时请求 |
| 49 | `frontend/src/components/knowledge-base/DocumentPreviewDialog.vue:75-109` | 预览无缓存 | 使用 Vue Query 或 LRU 缓存 |
| 50 | `frontend/src/main.ts:4-8` | Element Plus 与 highlight.js 全量引入 | 按需导入；只引入需要的主题 |

### 前端类型与构建

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 51 | 多处 | 大量使用 `Record<string, unknown>` 与 `as` 断言 | 为 API 响应定义精确接口；使用 Zod/io-ts 运行时校验 |
| 52 | `frontend/src/queries/chat.ts:249-257` | `useClassifyQuestion` 返回类型断言 | 统一接口或用 Zod 校验 |
| 53 | `frontend/src/queries/kb.ts:191-192` | `as unknown as KnowledgeBase[]` | 在 API 层统一解包 |
| 54 | 多处 | 非空断言 `!` 滥用 | 用运行时检查或默认值替代 |
| 55 | `frontend/src/components/AppSidebar.vue:200`、`frontend/src/views/SettingsView.vue:46` | 图标类型为 `unknown` | 使用 `Component` 类型 |
| 56 | `frontend/src/utils/axios.ts:12-24` | 复杂类型覆盖 AxiosInstance | 封装 `request<T>` / `get<T>` 等独立函数 |
| 57 | `frontend/tsconfig.app.json:5-15` | 未启用 `strict` | 显式设置 `"strict": true` 并修复类型错误 |
| 58 | `frontend/vite.config.ts:34` | 使用 `process.env.VITE_API_BASE_URL` | 服务端代理配置使用不以前缀 `VITE_` 命名的变量 |
| 59 | `frontend/nginx.conf:9-52` | 缺少基础安全头 | 添加 `X-Frame-Options`、`X-Content-Type-Options`、`Referrer-Policy`、CSP |
| 60 | `frontend/Dockerfile.prod:5-25` | root 运行 nginx；无 HEALTHCHECK | 使用非 root 用户；添加 `HEALTHCHECK` |
| 61 | `frontend/eslint.config.ts:1-38` | 未开启严格规则 | 增加 `no-console`、`no-explicit-any` 等规则 |

### 测试与 CI

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 62 | `backend/tests/conftest.py:28-45` | session 级 `integration_client` 可能导致状态泄漏 | 对写操作使用 function 级 fixture；显式重置单例状态 |
| 63 | `frontend/src/**/__tests__/*` | 单元测试覆盖范围不足 | 为核心 composable 与关键组件补充测试 |
| 64 | `.github/workflows/ci.yml:1-281` | 无 `timeout-minutes`；缺少独立 lint/format/typecheck job | 设置超时；增加 `backend-lint`、`frontend-lint`、secrets scan job |
| 65 | `frontend/vitest.config.ts:1-15`、`backend/pyproject.toml:169-175` | 无覆盖率阈值 | 启用 coverage 并设置 fail-under |
| 66 | `backend/tests/test_prometheus_alerts.py:49-57` | 未验证 alerts.yml 中所有引用指标 | 解析 PromQL 表达式，确保所有指标已在 `prometheus.py` 注册 |

### Docker / 部署

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 67 | `docker-compose.yml:238-271` 等 | 生产环境多个基础设施服务无 `healthcheck` | 为 Milvus、Prometheus、Grafana、Alertmanager、postgres-exporter 增加 healthcheck |
| 68 | `docker-compose.yml:336-339` | 所有服务共用同一个 bridge 网络 | 拆分为 `frontend-network`、`backend-network`、`infrastructure-network` |
| 69 | `docker-compose.yml:48-63` 等 | 凭据全部通过 `env_file` 注入，无 Docker Secrets / read-only rootfs / `cap_drop` | 生产敏感项迁移到 Docker Secrets；增加容器加固 |
| 70 | `docker-compose.yml:212` | 使用第三方社区镜像 `pgsty/minio` | 评估切回官方镜像并固定 digest；或在文档中说明来源并审计 |
| 71 | `docker-compose.dev.yml:243-256` | dev `postgres-exporter` 未启用扩展查询 | 补充 `PG_EXPORTER_EXTEND_QUERIES: "true"` |
| 72 | `docker-compose.yml:111-112` | 生产前端监听 80 端口，无 HTTPS/TLS | 增加 HTTPS 反向代理或 CDN TLS；nginx 增加安全头 |

### 脚本

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 73 | `scripts/start-dev.bat:25`、`scripts/start-prod.bat:25` 等 | Batch 找不到 PS7 时回退到 PS5.1，但 `.ps1` 使用 `??` 等 PS7+ 语法 | 移除 5.1 回退，直接报错要求安装 PS7 |
| 74 | `scripts/start-prod.ps1:43-49` vs `scripts/start-prod.sh:57-65` | PowerShell 安全检查未包含 `REDIS_PASSWORD` | 补充 `REDIS_PASSWORD` |
| 75 | `scripts/start-dev.sh:277-300` | 启动脚本明文打印 Grafana 密码 | 不要打印任何密码 |
| 76 | `scripts/start-dev.sh:148-150`、`scripts/start-prod.sh:188-190` | Bash 脚本 `source` 整个 env 文件 | 仅解析非敏感键；敏感值掩码显示 |

### 数据库

| # | 位置 | 问题 | 建议 |
|---|---|---|---|
| 77 | `backend/src/database.py:17-26` | 连接池参数硬编码 | 抽出为环境变量 `DB_POOL_SIZE`、`DB_MAX_OVERFLOW` |
| 78 | `backend/src/api/chat.py:676-711` | 反馈接口 N+1 查询并加载大量消息 | 在 `SessionModel.messages` 上建立 GIN 索引，或用 JSONB 查询直接定位 |

---

## 5. 做得好的地方（保持）

- **安全设计**：计算器工具 AST 白名单、上传扩展名白名单 + 字节级校验 + UUID 落 MinIO、WebSocket 首帧鉴权避免密钥进 URL、日志不记 query string、subprocess 固定二进制 + 超时。
- **前端渲染安全**：markdown-it + DOMPurify + 自定义 hook 过滤危险协议链接，XSS 基础防护完整。
- **配置管理**：pydantic-settings 分层配置，凭据无内置默认值，生产启动强制校验 SECRET_KEY 强度与凭据非空（fail-fast）。
- **工程化**：uv.lock / pnpm-lock 双端锁版本；OCR 重依赖隔离在可选组；生产栈端口回环绑定 + 资源限制 + 固定镜像 tag；启停脚本有危险操作二次确认与弱密码预检；git 卫生极佳（仅 `configs/license/minio.license` 一处敏感文件需要处理）。
- **可观测性**：Prometheus 指标体系完整，Trace 链路已落地前端时间线展示，审计日志已接入关键操作。
- **设计流程**：重大功能（结构化分块、语义缓存、Wiki 编译）均先有设计文档和离线 A/B 评估，再决定是否合入，避免盲目堆功能。

---

## 6. 建议处理顺序

### 今天（P0）
1. 修复 `frontend/index.html` 入口为 `main.ts`（P0-1）。
2. 移除前端 `VITE_API_KEY` 打包逻辑，或至少确保生产构建不注入（P0-2）。
3. 确认 `configs/license/minio.license` 性质；若为真实许可证，立即轮换并从 Git 历史中删除（P0-6）。
4. 修复 Agent 知识库越权（P0-4）和 SSRF 重定向绕过（P0-5）。
5. 评估 JWT 从 localStorage 迁移到 httpOnly Cookie 的可行性，短期可先增加 XSS 缓解措施（P0-3）。

### 本周（P1）
1. 轮换 `.env.dev` / `.env.prod` 中的真实凭据，将本地 env 文件移出工作区（P1-1）。
2. 统一并更新 README / 部署文档 / `.env.example` 中的模型、provider、镜像 tag、告警规则（P1-22）。
3. 修复 Docker 生产 `frontend.depends_on` 健康检查条件（P1-17）。
4. 修复 `SKIP_OLLAMA_CHECK` 与 `host.docker.internal` Linux 兼容性问题（P1-18、P1-19）。
5. 修复监控告警配置缺陷（P1-21）。
6. 拆分 `ChatView.vue` / `KnowledgeBaseView.vue`（P1-13）。
7. 修复 TypeScript 版本号异常（P1-14）。
8. 修复 `init_db()` 与 alembic 竞争（P1-20）。

### 后续迭代（P2）
1. 消除后端大文件（`rag_chain.py`、`document.py`、`web_search_service.py`）。
2. 引入请求级 `trace_id`（contextvars）并统一日志格式。
3. 增强前端类型严格性（启用 `strict`、减少 `any`/`as`）。
4. 补充前后端测试覆盖与 CI lint/format/typecheck job。
5. 拆分 Docker 网络、引入 Docker Secrets、加固容器安全。
6. 对长事务、大文件处理、Redis KEYS 等做专项治理。

---

## 7. 附录：关键文件路径速查

| 类别 | 文件 |
|------|------|
| 后端入口/配置 | `backend/src/main.py`、`backend/src/config.py`、`backend/src/auth.py` |
| 后端核心服务 | `backend/src/services/rag_chain.py`、`backend/src/services/web_search_service.py`、`backend/src/services/agent_orchestrator.py`、`backend/src/services/vector_store.py` |
| 后端 API | `backend/src/api/chat.py`、`backend/src/api/document.py`、`backend/src/api/knowledge_base.py`、`backend/src/api/config.py` |
| 前端入口 | `frontend/index.html`、`frontend/src/main.ts`、`frontend/src/App.vue` |
| 前端核心 | `frontend/src/views/ChatView.vue`、`frontend/src/views/KnowledgeBaseView.vue`、`frontend/src/utils/axios.ts`、`frontend/src/router/index.ts`、`frontend/src/stores/app.ts` |
| 部署/配置 | `docker-compose.yml`、`docker-compose.dev.yml`、`backend/Dockerfile.dev`、`frontend/Dockerfile.prod`、`frontend/nginx.conf` |
| 监控 | `configs/prometheus/alerts.yml`、`configs/alertmanager/alertmanager.yml`、`configs/grafana/datasources/prometheus.yml` |
| 文档 | `README.md`、`docs/guide/deployment.md`、`docs/guide/monitoring.md` |

---

*报告生成时间：2026-09-24*  
*基于工作区提交前状态，后续如有提交请以最新代码为准。*
