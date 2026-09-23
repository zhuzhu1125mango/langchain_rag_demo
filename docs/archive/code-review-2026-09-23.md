# 代码审查报告（2026-09-23）

> 审查范围：backend/（全量 Python 源码、依赖、配置）、frontend/、docker-compose.{yml,dev.yml}、configs/、scripts/、.gitignore 与 git 历史、docs/。
> 方法：静态审查 + git 历史核查（`git log --all` / `git ls-files` / `git check-ignore`）。
> 关联：上一轮审查见 [code-review-2026-09.md](code-review-2026-09.md)（2026-09-16）。

## 总评

项目整体工程质量**明显高于"演示项目"水准**：鉴权体系完整（JWT + API Key 双模式、资源 owner 隔离、WebSocket 首帧鉴权）、上传有白名单 + 字节级大小校验、SQL 全量参数化、无 eval/exec/pickle、CORS 白名单、生产栈端口全部回环绑定、镜像固定版本 tag、git 历史无任何密钥泄露、文档与代码高度一致。

主要风险集中在**凭据治理**与**开发栈网络暴露**两类，共 1 项 P0、5 项 P1、若干 P2。

---

## P0（立即处理）

### 1. 真实 Tavily API Key 明文落盘，且 dev/prod 复用同一把
- 位置：`.env.dev:120`、`.env.prod:102`（`SEARCH_API_KEY=tvly-dev-...`）
- 说明：已核实**未提交进 git、历史中也从未出现**（非泄露事故），但真实付费凭据明文存放于多个文件（另见 `backups/.env-20260920`），dev/prod 共用一把，误打包/误截图/同步盘上传即泄露。
- 修复：
  1. 立即在 Tavily 后台轮换该 Key；
  2. dev / prod 分 Key；
  3. 生产改用 Docker secret / 部署时环境变量注入；
  4. 建议加装 pre-commit secret 扫描（gitleaks）作为最后防线。

---

## P1（尽快处理）

### 2. `.env.prod` 全套生产凭据明文存盘
- 位置：`.env.prod:13-96`（PostgreSQL / MinIO / Redis 密码、SECRET_KEY、Grafana 密码均为真实强密码明文；SECRET_KEY 长期静态）
- 修复：迁移到 Docker secrets 或部署时注入；定期轮换；`backups/` 中 env 备份加密。

### 3. 开发栈所有基础设施端口绑定 0.0.0.0
- 位置：`docker-compose.dev.yml`（redis 6379 / postgres 5433 / minio 9000-9001 / milvus 19530,9091 / prometheus 9090 / grafana 3000）
- 说明：办公网/公共 Wi-Fi 下，Milvus、MinIO 控制台、PostgreSQL 对整个局域网开放；叠加 dev Grafana 密码回退 `admin`，即为 admin/admin。
- 修复：与生产栈对齐，改为 `127.0.0.1:6379:6379` 等回环绑定（一行改动，收益最大）。

### 4. dev Prometheus 开启无鉴权热重载且对外暴露
- 位置：`docker-compose.dev.yml:212,225`（`--web.enable-lifecycle`）
- 说明：任何能访问 9090 的人可 `POST /-/reload`；生产栈已正确移除该参数并注释了原因。
- 修复：与生产一致移除该参数。

### 5. SSRF 防护不一致：搜索内容抓取未走 `validate_url_safe`
- 位置：`backend/src/services/web_search_service.py:670-688`
- 说明：`fetch_content()` 直接抓取搜索引擎返回的任意 URL，未调用 `src/utils/security.py:155` 的 `validate_url_safe()`；而 `tools/plugins/fetch_webpage_tool.py:36` 同类操作有校验。搜索结果/重定向被污染为内网地址（如 `169.254.169.254`）可打穿内网。
- 修复：`fetch_content` 入口统一调用 `validate_url_safe(url)`。

### 6. `VITE_API_KEY` 未文档化且会打进客户端包
- 位置：`frontend/src/utils/axios.ts:33`、`router/index.ts:107`、`composables/useNotifications.ts:72`、`views/ChatView.vue:420`、`components/knowledge-base/WikiDrawer.vue:312`、`UploadDocumentDialog.vue:254`
- 说明：6 处读取 `import.meta.env.VITE_API_KEY`，但 `.env.example` 无任何 `VITE_` 变量说明。Vite 会将 `VITE_` 前缀变量打包进产物，等于公开该 Key。
- 修复：在 `.env.example` 显式注释该变量（标注"仅开发用，任何值都会暴露在浏览器端"）；生产完全依赖 JWT 登录，考虑移除该回退逻辑。

---

## P2（日常迭代消化）

| # | 位置 | 问题 |
|---|---|---|
| 7 | `backend/src/main.py:429` | `/metrics` 匿名可读（对比 `/metrics/reset` 有 `require_admin`），暴露接口拓扑与调用量 |
| 8 | `backend/scripts/prepare_ab_kb.py:20` | 硬编码密码 `ab_eval_2026` 已入 git；配合默认开放注册可被抢注 |
| 9 | `backend/src/auth.py:181-182` | 裸跑生产漏设 `APP_ENV=prod` 时完全匿名放行；建议绑定非 localhost 且无认证配置时启动告警/拒绝 |
| 10 | `frontend/src/utils/axios.ts:62-76` | 对所有 5xx 自动重试 3 次，POST 上传等非幂等请求可能重复写入 |
| 11 | `frontend/src/utils/axios.ts:57` | 401 时 `window.location.href='/login'` 丢失回跳上下文 |
| 12 | `frontend/Dockerfile.prod:12` | `pnpm install` 未加 `--frozen-lockfile`，构建不可重现 |
| 13 | `docker-compose.dev.yml` | 开发栈全部服务无资源限制（生产栈均有 limits） |
| 14 | 两份 compose 的 redis 健康检查 | 密码出现在命令行（`redis-cli -a ...`），`docker inspect` 可见；改用 `REDISCLI_AUTH` |
| 15 | `docker-compose.yml:62` | 生产 `MINIO_SECURE=false` 静默覆盖 env 的 true，与注释自相矛盾；容器内网络的折衷应显式注明 |
| 16 | `configs/grafana/datasources/prometheus.yml:17` | 生产 Grafana 数据源 `editable: true` |
| 17 | `configs/license/minio.license` | JWT license（含账号/容量字段）经 `.gitignore` 豁免入库，需确认是否敏感 |
| 18 | `scripts/stop-prod.ps1:39` | 使用未定义的 `$Blue` 变量，INFO 前缀渲染为空 |
| 19 | `backend/pyproject.toml:88` | `duckduckgo-search==8.1.1` 已更名 `ddgs`，上游弃维护旧包名 |
| 20 | `services/rag_chain.py`（2029 行）、`api/document.py`（1674 行）、`web_search_service.py`（996 行） | 巨型文件建议按职责拆分 |
| 21 | `backend/src/config.py:435-437` | import 时即创建目录（副作用），不利于测试 |
| 22 | `backend/data/model_cache/`（5GB） | 模型缓存未入库（正确），但位于 repo 内易被 IDE/备份误打包，建议外移至 `~/.cache` 或独立数据卷 |
| 23 | `docs/README.md` | 未提及 `frontend/README.md` 子文档入口 |

---

## 做得好的（值得保持）

- **安全设计**：计算器工具 AST 白名单实现（测试专门验证拒绝 `__import__`）；上传扩展名白名单 + 字节级校验 + UUID 落 MinIO；WebSocket 首帧鉴权避免密钥进 URL；日志不记 query string；subprocess 均为固定二进制 + 超时。
- **前端渲染安全**：markdown-it + DOMPurify + 自定义 hook 过滤危险协议链接，XSS 防护完整。
- **配置管理**：pydantic-settings 分层配置，凭据无内置默认值，生产启动强制校验 SECRET_KEY 强度与凭据非空（fail-fast）；`.env.example` 为干净占位符。
- **工程化**：uv.lock / pnpm-lock 双端锁版本；OCR 重依赖隔离在可选组；生产栈端口回环绑定 + 资源限制 + 固定镜像 tag；启停脚本有危险操作二次确认与弱密码预检；git 卫生极佳（420 个跟踪文件中无 env/日志/缓存/模型权重）。
- **文档**：docs 索引规范健全，端口对照表与两份 compose 逐项一致，已有审查闭环归档机制。

---

## 建议处理顺序

1. **今天**：轮换 Tavily Key（#1）；`docker-compose.dev.yml` 端口改回环绑定 + 移除 `--web.enable-lifecycle`（#3、#4，各一行改动）。
2. **本周**：`fetch_content` 补 SSRF 校验（#5）；`.env.example` 补 `VITE_API_KEY` 说明并评估移除（#6）；`/metrics` 加鉴权（#7）。
3. **后续迭代**：凭据迁移 Docker secrets（#2）、P2 表逐项消化。
