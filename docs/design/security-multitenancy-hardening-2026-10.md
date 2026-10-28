# 安全与多租户加固（SEC）

> 状态：已实施（P1 SEC-1~5 2026-10-24；P2 SEC-6~15 与 P3 SEC-16~21 均于 2026-10-27 完成，实施记录见 §10/§11）
> 关联：[project-status.md](../project-status.md)（唯一任务台账）、[agent-guardrails-hitl.md](agent-guardrails-hitl.md)（现有输入/工具/输出护栏）、[agent-eval-trace.md](agent-eval-trace.md)（评测与回归）、[evolution-roadmap-2026-10.md](evolution-roadmap-2026-10.md)
> 定位：2026-10-24 对抗性审查后的收口方案。**公网多租户上线前必须先完成 P1（SEC-1 ~ SEC-5）**；私有化单租户/LAN 部署可按 P2/P3 排期。
> 审查方式：静态代码审查 + 测试/CI/部署配置核对，未做真实渗透测试；部分结论依赖部署方式（是否公网、是否开放注册、是否启用联网/Agent）。

## 1. 结论与上线边界

| 部署形态 | 当前结论 | 依据 |
|---|---|---|
| 私有化 / 单租户 / 内网 | 可用 | 主资源 owner 隔离、XSS/SSRF 主路径、会话原子写入等基础扎实；剩余问题多为运维与纵深防御 |
| 公网多租户 SaaS | **不建议直接开放** | 未发现未授权接管系统的 P0，但存在跨用户数据泄露、资源滥用、限流失效、状态与索引不一致等 P1 |

**一句话**：工程能力强、边界意识不均衡；先把 P1 收口，再谈「企业级公网多租户」。本方案不推翻现有架构，按「最小改动、可回归、默认安全」推进。

## 2. 威胁模型与假设

**威胁主体**

- 未认证公网访问者
- 已注册的恶意用户（可上传文档、发起问答、调用学习/实验接口）
- 被污染的网页/外部文档（提示注入载体）
- 被盗 JWT / API Key（XSS、终端失窃）
- 误配置的运维操作（公开注册、跳过启动校验、错误代理信任）

**信任边界**

- 前端 → Nginx → FastAPI：Nginx 是唯一可信代理；后端只接受来自 Docker 内网的代理头
- API → Ollama / Milvus / MinIO / Redis / PostgreSQL：内网可信，但租户数据必须按 `owner_id` / `kb_id` 隔离
- Agent → 外部网页：**完全不可信**；工具出站必须经过策略与审批
- 模型输出与检索内容：不可信；不得直接作为指令或 HTML 执行

**非目标**

- 不防主机 root / 容器逃逸 / 模型权重投毒
- 不追求「零提示注入」；目标是把可利用面收敛到可检测、可审批、可回归
- 不在本专项引入多 Agent 编排或自研评测平台

## 3. P1 方案（公网多租户前置条件）

### SEC-1 学习模块跨用户泄露与写入污染

**现状**

- [`/api/learning/misclassification`](../../backend/src/api/learning.py) 仅要求登录，内部经 [`sample_store.get_recent_executions()`](../../backend/src/services/sample_store.py) 读取**全局最近执行**，任何用户可看到他人原始问题。
- [`/api/learning/record-execution`](../../backend/src/api/learning.py) 与 `record-feedback` 允许任意登录用户写入全局策略样本，未校验 session/execution 归属，可污染学习权重。

**设计**

1. **数据层**：`strategy_executions` 增加 `user_id VARCHAR(64) NULL`（索引）；迁移时按 `session_id` 关联 `sessions.user_id` 回填，无法回填的标记 `__legacy__`。
2. **查询层**：`SampleStore` 拆成两类入口：
   - `get_recent_executions(owner_id=...)`：面向 API，必须带 owner 过滤；
   - `get_training_samples()`：仅后台学习任务使用，允许全局聚合，但**不对外返回原始问题**。
3. **接口层**：
   - `/learning/misclassification`、`/learning/stats` 改 `require_admin`；普通用户只看自己的聚合统计（可选 `/learning/my-stats`）。
   - `/learning/record-execution`、`/record-feedback` 若仅内部链路使用则**移除 HTTP 暴露**；保留则要求 admin，并校验 `session_id`/`execution_id` 归属。
4. **回填与回归**：新增双用户测试：A 不可见 B 的问题；A 不可写 B 的 execution；后台学习仍能拿到全局样本。

**验收**

- 任一普通用户调用学习分析接口，返回 403 或仅自身数据；
- 伪造 `execution_id` 写入反馈失败；
- `pytest` 覆盖越权读写各 1 例。

**规模**：M（迁移 + 服务 + API + 测试）

---

### SEC-2 注册开放与资源配额

**现状**

- [`AUTH_ALLOW_REGISTRATION` 默认 `True`](../../backend/src/config.py)；`.env.prod` 未覆盖该键。
- 聊天流式端点与批量上传没有任何用户级限流/配额；批量上传无文件数/总大小上限。

**设计**

1. **注册默认值**：开发保持 `true`；生产默认 `false`。启动时若 `IS_PRODUCTION && AUTH_ALLOW_REGISTRATION`，记录 **CRITICAL 警告**并在 `/health/detail` 标记 `registration_open=true`；是否拒绝启动由 owner 决定（建议默认拒绝，允许显式 `ALLOW_OPEN_REGISTRATION_IN_PROD=true` 覆盖）。
2. **聊天配额**（Redis 计数器，按 `user_id`）：
   - `CHAT_RATE_LIMIT_PER_MINUTE`（默认 10）
   - `CHAT_MAX_CONCURRENT_STREAMS_PER_USER`（默认 2）
   - `CHAT_DAILY_MESSAGE_QUOTA`（默认 200）
   - 超限返回 `429` + `Retry-After`；并发超限返回 `429`，不创建新流。
3. **上传配额**（Redis 日计数器 + 请求校验）：
   - `UPLOAD_MAX_FILES_PER_BATCH`（默认 20）
   - `UPLOAD_MAX_BATCH_MB`（默认 200，小于单文件上限 × 文件数）
   - `UPLOAD_DAILY_MB_PER_USER`（默认 500）
   - 校验顺序：文件数 → 单文件 → 批量总量 → 日配额；失败不落 MinIO。
4. **可观测**：配额命中写入 Prometheus `rag_quota_blocks_total{scope,reason}` 与审计日志（admin 可见）。

**验收**

- 单用户触发聊天/上传配额时稳定 429，其他用户不受影响；
- 批量上传超文件数/总量在进入 MinIO 前拒绝；
- 生产未关闭注册时启动日志/健康检查明确告警。

**规模**：M

---

### SEC-3 代理感知的限流

**现状**

- 限流键使用 `request.client.host`；生产 Nginx 设置 `X-Real-IP`，但后端未显式信任代理头，后端看到的可能是 Nginx 容器 IP。
- 限流桶在进程内，多 worker 时各进程独立计数。

**设计**

1. **代理信任**：在 compose/启动参数显式设置 `FORWARDED_ALLOW_IPS`（仅 Docker 内网网段或 Nginx 容器 IP），启用 Uvicorn `--proxy-headers`；禁止 `*` 除非后端端口绝不对外暴露。
2. **客户端标识**：`get_client_ip(request)` 统一封装：仅当 `request.client.host` 属于受信代理时读取 `X-Forwarded-For` 的**最右侧非受信地址**，否则用 socket IP；`X-Real-IP` 仅作兜底。
3. **限流存储**：认证类限流迁移到 Redis 滑动窗口（ZSET/INCR + TTL），key 为 `rl:{scope}:{client_ip}`；Redis 不可用时降级进程内桶并告警（认证接口可接受短暂降级，不因 Redis 故障锁死全部登录）。
4. **多副本**：去掉「阈值按副本数放大」的旧假设；Redis 统一计数后再按副本扩容。
5. **回归**：模拟 Nginx → 后端链路，两个不同 XFF 客户端互不影响；单个 IP 触发限流不阻塞其他 IP。

**验收**

- 同一 Nginx 后，两个真实客户端拥有独立登录桶；
- 单 IP 攻击不再造成全局 429；
- 多 worker/多副本下限流一致（集成测试或 Redis 单测）。

**规模**：M

---

### SEC-4 文档状态、KB 移动与索引一致性

**现状**

- “下架”只改 `documents.status`；Milvus 向量无状态字段，检索链路不按状态过滤 → **下架文档仍会被回答**。
- 更新文档 `kb_id` 只改 SQL，不重建向量 → 旧 KB 仍可检索，新 KB 检索不到。
- 状态/KB 变更未失效语义缓存。

**设计（MVP：删除/重建成对操作）**

1. **状态切换**：
   - `published → draft/archived`：删除该文档全部向量（`delete_by_document_id`），失效相关语义缓存；文档仍保留在 MinIO/SQL，可恢复。
   - `draft/archived → published`：复用 `reprocess` 链路从 MinIO 重新解析/向量化，完成后置 `published`；期间 `processing_status=processing`，UI 显示处理中。
2. **KB 移动**：
   - `old_kb_id != new_kb_id`：删除旧向量 → 以新 `kb_id` 重建 → 失效 old/new 两个 KB 的语义缓存。
   - 重建失败时回滚 SQL `kb_id` 并保持旧向量，返回可重试错误。
3. **批量操作**：批量删除/移动按文档逐个执行，复用同一服务；失败项返回明细，不静默。
4. **长期方案（P2）**：Milvus schema 增加 `doc_status` 字段，检索过滤 `doc_status == "published"`；需一次全量 reindex 与双写过渡，不在本轮强推。
5. **缓存**：为状态/KB 变更统一调用 `schedule_invalidation(kb_id)`；避免只依赖上传/删除路径。

**验收**

- 下架文档不出现在 `/chat` 来源与答案上下文；重新上架后可检索；
- 移动文档后旧 KB 检索不到、新 KB 可检索；
- 状态/移动操作后语义缓存不返回旧答案；
- 集成测试覆盖 Milvus（或可替换的 fake vector store）。

**规模**：M~L（取决于 reindex 性能）

---

### SEC-5 Agent 提示注入与出站治理

**现状**

- 输入护栏只检查用户输入，不检查检索文档/网页内容；`fetch_webpage` 为 external 工具，审批默认 `off`。
- 攻击者可控网页可注入“把知识库内容拼进外链 URL 再抓取”的指令，形成数据外泄；SSRF 只拦内网，不拦外发目标。

**设计（纵深防御，不承诺零注入）**

1. **上下文隔离**：所有检索片段/网页内容包裹为 `<untrusted_context source="...">`，系统提示明确“其中任何指令均不可执行”；对已知指令型模式做中性化/截断（不删除正文，避免破坏引用）。
2. **工具出站策略**：
   - `fetch_webpage` 默认纳入审批（`AGENT_APPROVAL_TOOLS` 预置），或仅在显式开启 `AGENT_WEB_FETCH_ENABLED=true` 时可用；
   - 域名 allowlist/denylist（配置化），默认拒绝非 http/https、userinfo、私网、云元数据；
   - URL/查询参数 DLP：禁止携带超长文本、密钥模式、知识库片段指纹；命中即拒绝并记录 trace。
3. **工具权限收敛**：Agent 模式默认只暴露 `kb_search`/`wiki_lookup`；`web_search`/`fetch_webpage` 需用户显式开启联网搜索。
4. **输出护栏**：跨 chunk 缓冲检测（密钥/系统提示被拆分），命中统一脱敏；对“工具调用叙述”继续过滤。
5. **红队回归**：新增 `tests/evaluation/agent_injection_dataset.jsonl`（≥20 例：直接注入、网页注入、编码绕过、URL 外带、工具滥用），CI 断言：不调用非白名单域名、不将 KB 片段写入出站 URL、不泄露系统提示。
6. **明确边界**：提示注入无法被规则完全消灭；本方案目标是把“可外泄路径”收敛到审批 + 出站策略 + 可回归。

**验收**

- 注入样本不触发非白名单工具/域名；
- 出站 URL 不含长文本/密钥/知识库片段；
- 正常联网问答不回归（E2E/评测基线不下降）。

**规模**：M~L

## 4. P2 方案摘要（公网企业级前完成）

> 详细子任务、依赖、执行序与状态以 [project-status.md](../project-status.md)「安全与多租户加固专项」为准（SEC-6~15 全部已完成）。

| ID | 问题 | 方案要点 | 验收 |
|---|---|---|---|
| SEC-6 | 实验遗留空 owner 对所有用户可见/可控 | 移除 `owner_id == ""` 放行；迁移回填或仅 admin 可见遗留实验 | ✅ 普通用户无法列出/操作空 owner 实验；新增管理员 `/experiments/admin/legacy` |
| SEC-7 | 分类/标签为全局资源 | 二选一：加 `owner_id` 做租户隔离；或明确为全局字典并仅 admin 可写 | ✅ 决策为全局字典 + admin 写；删除分类前校验文档引用（409） |
| SEC-8 | 通知全局频道泄露活动元数据 | 默认订阅 `user:{id}`；`doc:`/`task:` 订阅校验归属；`kb:*`/`doc:*` 仅 admin/内部 | ✅ 默认 user 频道；全局频道仅内部；doc/task 越权拒绝 |
| SEC-9 | SSRF DNS 失败放行 / TOCTOU / 响应体无上限 | DNS 失败 fail-closed；解析后固定 IP 连接；限制重定向跳数/响应字节/Content-Type | ✅ DNS fail-closed + 连接后 peer IP 复核 + 2MB/内容类型/跳数限制；外部抓取走 `utils/safe_http.py` 的 httpcore 固定 IP 后端（TLS 仍按原 host 校验证书），消除解析-连接 TOCTOU |
| SEC-10 | JWT 无吊销 | 增加 `jti`/session version + Redis 黑名单；登出使 token 失效；缩短 access TTL、refresh 轮换 | ✅ jti + 本地/Redis 黑名单、`POST /auth/logout`、refresh 轮换旧 token |
| SEC-11 | 语义缓存失效全库扫描 | 维护 `kb_id → scope keys` 索引（Redis Set），按 KB 精准失效 | ✅ `rag:semcache:index:*` 反向索引；存量无索引时回退 SCAN |
| SEC-12 | 健康检查浅、Redis 硬依赖 | `/health/live` 浅探活；`/health/ready` 深探 DB/Redis/Milvus/MinIO；compose 用 ready | ✅ live/ready/detail 拆分；compose healthcheck 改 ready |
| SEC-13 | 无备份/扫描/SBOM、镜像浮动 | 备份恢复演练；dependabot/trivy/SBOM；基础镜像 pin digest | ✅ 备份/恢复脚本 + runbook、dependabot、Trivy+SBOM CI；Dockerfile 基础镜像与 uv 镜像均 `tag@sha256` 固定 |
| SEC-14 | 无账号删除/导出、弱密码、日志含问题 | 账号删除/导出 API；密码 ≥10 + 弱密码库；问题日志脱敏/分级 | ✅ 导出/注销 API、密码 ≥10 + 弱密码库、问题/答案/工具参数日志摘要化；trace 30 天、审计 365 天定期清扫（`retention_service`），应用日志按 `LOG_RETENTION_DAYS` 轮转 |
| SEC-15 | 测试有效性不足 | 真实后端 E2E 烟测；安全回归套件；覆盖率提升（后端 65、前端阈值上调） | ✅ `scripts/smoke_e2e.py` 与 SEC P2 回归套件；补测后后端 67.02%（`fail_under` 60→65），前端阈值 32/35/24/31→35/40/25/34 |

## 5. P3 方案摘要

> SEC-16~21 全部已完成；详细状态见台账。

| ID | 问题 | 方案 | 状态 |
|---|---|---|---|
| SEC-16 | CSP `connect-src ws:/wss:` 过宽、无 HSTS/TLS | 收紧到同源 WS；生产 TLS/HSTS 由反向代理统一提供 | ✅ `connect-src 'self'`；HSTS 按 `X-Forwarded-Proto` 下发；部署指南补 TLS 反代片段 |
| SEC-17 | `/api/evaluate/*` 无配额 | 加登录用户 QPS/日配额；批量样本上限 | ✅ `consume_eval_quota`（RPM + 日样本单元）；`EVAL_BATCH_MAX_SAMPLES` 超限 400 |
| SEC-18 | 会话列表无分页、`DocumentUpdate.status` 未校验 | 会话分页；状态字段 Literal；批量操作上限 | ✅ 会话 `skip/limit`；会话/文档批量删除 ≤200；反馈/坏例 `limit≤200`（Literal 已于 SEC-4 完成） |
| SEC-19 | ToolGuard 不解析 DNS | 统一走 `validate_url_safe` + 固定 IP 连接；新增 URL 型工具必须声明 | ✅ 注册时强校验 `url_params`；`check_async` 线程池 DNS fail-closed |
| SEC-20 | 本地 secrets 不完整导致 start-prod 预检失败 | 文档明确 `gen_secrets` 流程；预检输出缺失清单与生成命令 | ✅ 已完成 |
| SEC-21 | 文档边界表述偏乐观 | README/部署指南明确「单租户/LAN 推荐，公网需完成 SEC P1」 | ✅ 已完成 |

## 6. 发布顺序与验证

**批次 0（公网前置，P1）**：SEC-1 → SEC-3 → SEC-2 → SEC-4 → SEC-5

理由：先堵数据泄露（SEC-1），再修限流可用性（SEC-3），随后加注册/资源配额（SEC-2）；SEC-4 涉及索引重建，单独窗口；SEC-5 最后上线，配套红队回归。

**批次 1（企业级，P2）**：SEC-6 → SEC-8 → SEC-7 → SEC-9 → SEC-10 → SEC-11 → SEC-12 → SEC-13 → SEC-14 → SEC-15

理由：先收实验/通知隔离，再定分类标签归属模型；随后网络/认证（SSRF/JWT），再做缓存/健康与运维/测试。

**批次 2（纵深与体验，P3）**：SEC-19 → SEC-16 → SEC-17 → SEC-18（SEC-20/21 已完成）

**每项完成的定义**

1. 代码 + 迁移（如需要）+ 单测/集成测试；
2. 至少 1 个越权/攻击回归用例；
3. 文档与 `project-status.md` 状态回写；
4. 本地 `pytest` / `pnpm test:unit` / `pnpm build` 全绿（涉及前端时含 E2E）；
5. 生产开关/环境变量在 `.env.example` 有注释，`env-consistency` 通过。

**回滚**

- SEC-2/3 配额与限流：配置开关可回退到宽松值，但不得回退到“无限制”；
- SEC-4 索引重建：保留旧向量快照（Milvus backup collection 或 MinIO 原文可重建），失败回滚 SQL 字段；
- SEC-1/8 数据迁移：保留 `__legacy__` 标记，admin 可查；
- SEC-5：工具审批/域名策略支持配置回退，但默认安全值不可被静默改为 `off`。

## 7. 任务清单（与 project-status 同步）

> 状态以 [project-status.md](../project-status.md)「安全与多租户加固专项」为准；本表只定义范围、依赖与验收。

### P1（公网多租户前置）

| ID | 任务 | 依赖 | 验收 | 规模 |
|---|---|---|---|---|
| SEC-1.1 | `strategy_executions.user_id` 迁移与回填 | 无 | 迁移可重复执行；历史数据有 owner 或 `__legacy__` | S |
| SEC-1.2 | SampleStore 查询拆分为 owner/内部训练两类 | SEC-1.1 | API 查询必带 owner；训练查询不暴露原文 | S |
| SEC-1.3 | 学习 API 鉴权与 record-* 收口 | SEC-1.2 | 普通用户 403/仅自身；伪造 execution 失败 | S |
| SEC-2.1 | 生产注册默认关闭 + 启动拒绝 | 无 | 未显式开启时公网不可注册 | S |
| SEC-2.2 | 聊天速率/并发/日配额 | SEC-3.2 | 超限 429；用户间互不影响 | M |
| SEC-2.3 | 批量上传文件数/总量/日配额 | SEC-3.2 | 进入 MinIO 前拒绝；明细返回 | M |
| SEC-3.1 | 受信代理与客户端 IP 解析统一 | 无 | Nginx 后真实 IP 正确；伪造 XFF 不生效 | S |
| SEC-3.2 | Redis 滑动窗口限流 + 降级 | SEC-3.1 | 多 worker 一致；单 IP 不全局锁死 | M |
| SEC-4.1 | 下架删除向量、上架重建 | 无 | 下架后检索不到；上架可检索 | M |
| SEC-4.2 | 移动 KB 删除旧向量并按新 kb 重建 | SEC-4.1 | 旧 kb 不可检索、新 kb 可检索 | M |
| SEC-4.3 | 状态/移动统一失效语义缓存 | 无 | 变更后不返回旧答案 | S |
| SEC-5.1 | 检索/网页上下文不可信隔离与注入信号检测 | 无 | 注入样本不改变工具计划 | M |
| SEC-5.2 | 工具出站域名策略 + URL DLP + fetch 审批 | SEC-5.1 | 非白名单/含敏感片段出站被拒 | M |
| SEC-5.3 | Agent 注入红队数据集与 CI 门禁 | SEC-5.1/5.2 | ≥20 例，P1 用例常驻 | M |

### P2（企业级）

> 子任务拆分、执行序与实时状态见 [project-status.md](../project-status.md)；以下为范围与验收摘要。

| ID | 任务 | 状态 | 依赖 | 验收 |
|---|---|---|---|---|
| SEC-6 | 实验 owner 条件收口 | ✅ 已完成 | 无 | 空 owner 实验仅 admin |
| SEC-7 | 分类/标签归属或 admin-only | ✅ 已完成（全局字典 + admin 写） | owner 决策 | 普通用户不可跨租户改 |
| SEC-8 | 通知频道按用户隔离 | ✅ 已完成 | 无 | 只收自身事件；伪造频道被拒 |
| SEC-9 | SSRF fail-closed + IP 固定 + 响应上限 | ✅ 已完成 | 无 | 内网不可达；大响应截断；解析后按固定 IP 建连 |
| SEC-10 | JWT 吊销/登出/refresh 轮换 | ✅ 已完成 | 无 | 登出后旧 token 401 |
| SEC-11 | 语义缓存按 KB 索引失效 | ✅ 已完成 | 无 | 不再全库扫描 |
| SEC-12 | live/ready 健康检查 | ✅ 已完成 | 无 | 依赖故障不就绪 |
| SEC-13 | 备份恢复 + 供应链扫描 + 镜像 pin | ✅ 已完成 | 无 | 备份/恢复脚本与 runbook；Trivy/SBOM CI；基础镜像 digest pin |
| SEC-14 | 账号删除/导出、密码策略、日志脱敏 | ✅ 已完成 | 无 | 隐私操作可用；日志无原文；trace/审计按保留期清扫 |
| SEC-15 | 真实后端 E2E + 安全回归 + 覆盖率 | ✅ 已完成 | SEC-1~14 | 烟测脚本/回归套件；后端 67.02%、前端阈值上调 |

### P3（纵深与体验）

| ID | 任务 | 状态 | 验收 |
|---|---|---|---|
| SEC-16 | CSP 收紧 + TLS/HSTS 部署说明 | ✅ 已完成 | 安全头符合基线 |
| SEC-17 | 评估 API 配额 | ✅ 已完成 | 超限 429 |
| SEC-18 | 会话分页 + 批量上限（含 status Literal） | ✅ 已完成 | 分页/上限生效 |
| SEC-19 | ToolGuard DNS 解析统一 | ✅ 已完成 | 新增 URL 工具默认安全 |
| SEC-20 | secrets 预检与生成文档 | ✅ 已完成 | start-prod 给出明确缺失清单 |
| SEC-21 | 文档明确上线边界 | ✅ 已完成 | README/部署指南与实现一致 |

## 8. 开放问题（需 owner 决策）

1. **已决策（SEC-2）**：生产默认关闭开放注册；确需开放必须显式 `ALLOW_OPEN_REGISTRATION_IN_PROD=true`，并配合配额。是否增加邀请码/邮箱验证，留作 P2 可选项。
2. **待决策（SEC-7）**：分类/标签是「全局字典」还是「租户私有」？当前方案默认全局 + admin 写。
3. **已决策（SEC-5）**：`fetch_webpage` 默认关闭（`AGENT_WEB_FETCH_ENABLED=false`）；开启时纳入审批名单并受域名/DLP 约束。
4. **已决策（SEC-4）**：先采用「下架删向量/上架重建」MVP；Milvus `doc_status` schema 作为 P2 优化。
5. **待校准（SEC-2）**：配额默认值需按实际模型/显存/磁盘容量校准（当前建议值仅为起点）。

## 9. 实施记录（2026-10-24）

### 9.1 已落地（P1 SEC-1~5）

- **SEC-1 学习隔离**：`strategy_executions` 增加 `user_id` + Alembic `e5f6a7b8c9d0`（按 `sessions.user_id` 回填，缺失标 `__legacy__`）；`SampleStore` 查询支持 owner 过滤；`/learning/stats`、`/learning/misclassification` 按当前用户过滤；`record-execution` / `record-feedback` 改 `require_admin`，`record-execution` 的 owner 由会话归属派生；反馈写入校验 execution 归属。
- **SEC-2 注册/配额**：生产启动时 `AUTH_ALLOW_REGISTRATION=true` 且未显式 `ALLOW_OPEN_REGISTRATION_IN_PROD=true` → 拒绝启动；新增 Redis/进程内降级的聊天配额（RPM/并发/日额度）与上传配额（文件数/单批总量/日字节），聊天配额通过路由级依赖统一进出；上传失败退回日配额。
- **SEC-3 代理限流**：新增 `TRUSTED_PROXY_CIDRS` 与 `resolve_client_ip`，仅受信代理的 `X-Forwarded-For`/`X-Real-IP` 生效；`RequestTracingMiddleware` 统一回写 `scope["client"]`；认证限流改为 Redis ZSET 滑动窗口（Lua 原子），Redis 不可用时降级进程内桶；compose 注入 Docker 内网网段。
- **SEC-4 状态/索引一致性**：`DocumentUpdate.status` 收敛为 Literal；下架/归档先删向量再改状态（失败 503 保持原状态）；上架/移动 KB 后台重建索引；状态/KB 变更统一失效语义缓存；处理中禁止状态/移动操作，避免后台覆盖。
- **SEC-5 Agent 注入/出站**：新增 `context_guard`（不可信上下文包裹 + 闭合标记转义 + 注入信号检测），KB/Agent 观察结果统一包裹并在提示词声明不可执行；`ToolGuard` 增加域名 allow/deny、URL 长度、查询参数 DLP（长文本/密钥/控制字符）与文本参数长度；`fetch_webpage` 默认关闭（`AGENT_WEB_FETCH_ENABLED=false`）并纳入审批名单；新增 21 例注入红队数据集与 `test_agent_security.py`；输出护栏增加跨 chunk 触发式缓冲脱敏。

### 9.2 验证

- 新增/相关测试：`test_learning_isolation.py`、`test_quota.py`、`test_document_state_consistency.py`、`test_agent_security.py`、`test_security_hardening.py`（扩展）全部通过。
- 后端全量：**1073 passed / 105 skipped**（排除 5 个已知 Windows `python-magic` 崩溃模块：`test_document_loaders.py`、`test_ocr_parsing.py`、`test_document_processor.py`、`test_structured_chunking.py`、`evaluation/test_structured_vs_recursive.py`；该问题为项目 `docs/guide/development.md` 已记录的 Windows 环境坑，Linux CI 不受影响）。
- `scripts/check_env_consistency.py` 通过（106 键覆盖，config 201 字段全部记载）。
- Alembic 迁移 SQL 离线生成通过（`d4e5f6a7b8c9 → e5f6a7b8c9d0`）；本地 dev 栈未启动，未执行真实升级。
- 前端本轮未改动，未重跑前端测试。

### 9.3 残留与后续

- **P2 状态（2026-10-27 完成）**：SEC-6~15 全部完成，记录见 §10。
- **P3 状态（2026-10-27 完成）**：SEC-16~21 全部完成，记录见 §11。
- **SEC-5 边界**：提示注入无法被规则完全消灭；当前为“不可信包裹 + 工具出站策略 + 审批 + 红队回归”的纵深防御，仍需持续扩充红队样本。
- **SEC-4 选型**：本轮采用“下架删向量/上架重建”MVP；Milvus `doc_status` 字段与全量 reindex 留作 P2 优化。
- **部署提醒**：生产 `.env.prod` 需设置 `AUTH_ALLOW_REGISTRATION=false`（本地已补充，其他机器需同步）；Docker 下 `TRUSTED_PROXY_CIDRS` 已由 compose 覆盖为 `172.16.0.0/12`，若后端端口直接暴露给宿主/局域网，应改为精确的 Nginx 容器 IP 或专用代理网段，避免 XFF 伪造。

## 10. 实施记录（P2，2026-10-27）

- **SEC-6**：`ExperimentManager._owner_condition` 默认严格（不再放行空 owner），所有查询/写入方法透传 `include_legacy`；新增管理员只读端点 `GET /experiments/admin/legacy`。单测 `test_experiment_manager_units.py` 覆盖归属过滤与生命周期。
- **SEC-7**：分类/标签写接口收敛为 `require_admin`；分类删除前统计引用文档数，被引用返回 409。
- **SEC-8**：通知默认频道改为 `user:{user_id}`；`kb:*`/`doc:*` 全局频道仅内部用户可用，`doc:`/`kb:`/`task:` 订阅校验归属；前端订阅不再带 `channels` 参数。
- **SEC-9**：`validate_url_safe` DNS 失败 fail-closed；外部网页抓取改走 `utils/safe_http.py`——httpcore 自定义 network backend 在连接前解析并按公开 IP 建连（TLS 仍按原 host 校验证书），彻底消除 DNS 重绑定 TOCTOU；保留响应字节/Content-Type/重定向跳数上限与连接后 peer IP 复核；SearXNG 等内部请求仍走普通客户端。
- **SEC-10**：JWT 增加 `jti`，登出/刷新轮换时写入本地 + Redis 吊销表（Redis 故障 30s 冷却）；`POST /auth/logout`、前端登出 best-effort 调用。
- **SEC-11**：语义缓存新增 `rag:semcache:index:*` 反向索引，`invalidate_kb` 精准失效（无索引时回退 SCAN），`invalidate_all` 同步清索引。
- **SEC-12**：`/health/live`（浅）、`/health/ready`（DB/Redis/Milvus/MinIO，失败 503）、`/health/detail`（管理员）；compose healthcheck 使用 ready。
- **SEC-13**：新增 `scripts/backup.sh`/`restore.sh` 与 [backup-restore.md](../guide/backup-restore.md)；`.github/dependabot.yml`、Trivy + SBOM CI；Dockerfile 基础镜像与 `ghcr.io/astral-sh/uv` 均以 `tag@sha256` 固定。
- **SEC-14**：账号导出/注销 API（密码确认 + 审计 + token 吊销 + 级联清理）；密码 ≥10 + 弱密码库 + 不得等于用户名；问题/答案/工具参数日志摘要化；新增 `retention_service` 每日清扫 trace(30d)/审计(365d)，应用日志改 `TimedRotatingFileHandler` 按 `LOG_RETENTION_DAYS` 轮转。
- **SEC-15**：`backend/scripts/smoke_e2e.py` 与 SEC P2 回归套件；本轮补测 82 例（辅助 LLM 服务、Cache/MinIO/Reranker/VectorStore、ExperimentManager、SampleStore），后端 1298 passed，覆盖率 67.02%，`fail_under` 60→65；前端 131 passed，阈值 32/35/24/31→35/40/25/34。
- **验证**：`uvx ruff check --select E9,F63,F7,F82 backend` 通过。
- **残留**：季度恢复演练需按 runbook 在真实环境执行并留档；SEC P3 已于同日完成（见 §11）。

## 11. 实施记录（P3，2026-10-27）

- **SEC-16**：`frontend/nginx.conf` 的 CSP `connect-src` 由 `'self' ws: wss:` 收敛为 `'self'`（CSP3 已覆盖同源 ws/wss）；新增 `map $http_x_forwarded_proto $hsts_header`，仅在 HTTPS 抵达时下发 HSTS（明文直连不下发），三处安全头块同步；[deployment.md](../guide/deployment.md) §6.8.1 补 TLS 终结反代片段（含 `X-Forwarded-Proto` 前提说明）。
- **SEC-17**：`utils/quota.py` 新增 `consume_eval_quota`（用户级 RPM + 日样本单元，Redis 降级进程内、生产 fail-closed 沿用既有策略）；`/api/evaluate/*` 单条计 1、批量按样本数计费，`EVAL_BATCH_MAX_SAMPLES` 超限 400；新增 `EVAL_RATE_LIMIT_PER_MINUTE=10`、`EVAL_DAILY_QUOTA=200`、`EVAL_BATCH_MAX_SAMPLES=100`。
- **SEC-18**：会话列表新增 `skip`/`limit`（默认 100、上限 500）；会话与文档批量删除统一受 `BATCH_DELETE_MAX_IDS`（默认 200）约束，超限 400；反馈/坏例列表 `limit` 上限由 1000 收紧为 200。
- **SEC-19**：`BaseTool` 新增 `url_params`，`ToolRegistry.register` 强制校验 schema 中 URL 型参数必须声明（否则拒绝注册）；`ToolGuard.check_async` 在静态规则之后于线程池执行 `validate_url_safe`（DNS fail-closed），`ToolManager.execute` 统一调用；开关 `GUARDRAIL_TOOL_DNS_CHECK_ENABLED` 默认开启。`fetch_webpage` 声明 `url_params=("url",)`，其抓取仍走 SEC-9 的固定 IP 客户端。
- **验证**：新增 `tests/test_security_p3.py`（18 例）覆盖上述四项；后端全量 **1316 passed / 105 skipped**、覆盖率 67.17%（≥65 卡点）；`uvx ruff check --select E9,F63,F7,F82 backend` 通过；`nginx -t` 校验通过；前端 typecheck/eslint/build 通过（无前端源码改动）。
- **CI 收口（同日）**：`model_usage_daily.usage_date` 补 `index=True` 与迁移索引对齐（消除 `alembic check` 漂移）；`security-scan` 的 trivy-action 改 `v0.36.0`（原 `0.28.0` 无对应 tag）。Trivy 首扫报 9 CRITICAL / 57 HIGH（均为有修复版本的依赖漏洞），暂以观察模式（`exit-code: 0` + JSON artifact）运行，待 OPS-6 依赖升级后恢复阻断。
- **OPS-6 依赖升级（同日）**：后端 aiohttp 3.14.4、certifi 2026.10.18、cryptography 50.0.0、fsspec 2026.6.0、langsmith 0.8.18、pillow 13.0.0、pyjwt 2.15.1、pypdf 6.21.2、sentence-transformers 5.6.0、unstructured 0.24.0、urllib3 2.8.0；前端 axios 1.20.0、dompurify 3.4.17、markdown-it 14.3.3、vite 8.3.5、vue 3.5.44 及传递依赖（follow-redirects/form-data/linkify-it/nanoid/postcss/source-map-js）。`security-scan` 恢复 `exit-code: 1` 阻断，仅 `.trivyignore` 豁免 filelock（unstructured 强制 <4）与 transformers（OCR 组 mineru 强制 <5）共 4 条，复核日期 2026-11-30。
