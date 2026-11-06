# 知澜 · 一个把「可验证」做进 RAG 的知识库

> **结项版 README（2026-11-07）**。结论与遗留风险见 [docs/FINAL-REPORT.md](docs/FINAL-REPORT.md)；
> 开发过程与逐项验收见 [归档台账](docs/archive/project-status-2026-11-07.md)；
> 这份 README 之前的完整开发版（含三种部署方式逐步说明）保留在
> [docs/archive/README-dev-2026-11-07.md](docs/archive/README-dev-2026-11-07.md)。

**知澜**是一个可私有化部署的 RAG 知识问答系统：LangChain + Ollama + Milvus，默认完全离线，
数据不出内网。它不是一个「接上大模型就能问答」的 demo——它关心的是另外三件事：

> **删得干净、错得明白、断得回来。**

| 难点 | 常见做法（以及它的后果） | 这里的做法 | 证据 |
|---|---|---|---|
| **删除真的删干净了吗** | 删 DB 行就算删完。向量、对象存储、语义缓存里仍留着内容——用户以为删了，检索还能命中 | 删除按 **DB / 向量 / 对象 / 缓存** 四类数据落点逐一对齐，失败带 `phase` 可定位；另有**只读对账工具**主动比对 Milvus↔DB、MinIO↔DB、Redis↔DB | `backend/src/services/consistency_audit.py`<br>`backend/scripts/consistency_audit.py`<br>`backend/tests/test_consistency_delete_chain.py`（13 例）<br>`backend/tests/test_consistency_audit.py`（29 例） |
| **出错了，日志能判定能不能重试吗** | 一律 `except: logger.error(...)`，或把异常压成默认值（打分失败 → 0 分 → 被当成「不相关」） | 三级严重度（`RECOVERABLE` / `REPORTABLE` / `PROGRAMMING`），**异常类型优先于文案**；「后果优先」——底层是连接错误但会导致结论错误的，显式抬级 | `backend/src/utils/error_handling.py`<br>**360 处 `log_exception()` 调用**、198 处显式分级<br>`backend/tests/test_error_handling.py`（93 例） |
| **回答生成到一半断网了怎么办** | 生成器就是响应体，客户端一断连 `CancelledError` 直接杀掉生成，只落半截答案；切网/刷新=重问一次 | 运行与连接解耦：事件进带序号的缓冲，断连只结束订阅；凭 `run_id` + 游标续传，可显式停止并保留已生成部分 | `backend/src/services/chat_run.py`<br>`backend/src/api/chat/runs.py`<br>`backend/tests/test_chat_run.py`（35 例） |

三件事都有一个共同点：**它们的失败是静默的**。不报错、不告警、看起来一切正常，直到有人发现
「删掉的东西还能搜到」或者「知识库明明有内容却答没有」。这个项目的大部分工程量，花在把这些
静默失败变成可判定的信号上。

---

## 📌 结项状态（2026-11-07）

| 维度 | 状态 |
|---|---|
| 定型 | **工程化参考实现**，不是产品：无真实用户、无 SLA、无采购方 |
| 测试 | 后端 **1691 passed / 117 skipped**；前端 154 passed；CI **7/7 绿** |
| 生产 | 11 容器全 healthy、`/health/ready` 四项 ok、OpenAPI **117** 条路由 |
| 运维链路 | 备份/恢复/回滚四类演练 + 规模 RTO + 夜间性能门禁，全部有脚本、有实测数字 |
| 未做 | 多租户/SSO、真 HA、桌面端、企业级压测（详见「边界」与结项说明 §3） |

**「企业级」一词仍然冻结**：三条门槛只过了两条（恢复演练 ✅、≥50 题标注集 ✅ 90 题），
**容量实测（X 并发下 P95 与成功率）没做**。在此之前它就是个打折的形容词。

---

## 🚀 上手（拉过一次镜像后约 10 分钟）

```bash
# 0. 前置：Docker / Docker Compose，以及 Ollama（不在 compose 内，需单独安装）
ollama pull qwen2.5:3b-instruct   # 直答模型
ollama pull qwen3:4b              # 深度思考（按需）
ollama pull bge-m3                # 嵌入模型（1024 维）

# 1. 一键起开发栈（后端起 8000，前端起 5173）
git clone https://github.com/zhuzhu1125mango/langchain_rag_demo.git
cd langchain_rag_demo
./scripts/start-dev.sh        # Windows: .\scripts\start-dev.ps1

# 2. 打开 http://localhost:5173 → 注册 → 建知识库 → 上传文档 → 提问
#    空库时首个注册用户自动成为管理员（管理页/模型配置用它进）；
#    生产环境默认关注册，改用：python scripts/create_admin.py --username admin
```

首次运行会自动从 `.env.example` 生成 `.env.dev`（189 个键）并随机生成 `SECRET_KEY`——
**只有开发环境这么干**，生产必须由人显式配置强密码。启动前还会检查 Ollama 的
三个模型是否已拉取，缺哪个就把 `ollama pull` 命令直接打出来，而不是让你对着一堆
`unhealthy` 猜原因。只想先校验配置、不起容器：

```bash
CHECK_ONLY=1 ./scripts/start-dev.sh        # Windows: $env:CHECK_ONLY="1"; .\scripts\start-dev.ps1
```

默认是**精简模式**：只起 7 个容器（后端 / 前端 / Postgres / Redis / Milvus / etcd / MinIO），镜像合计约 **5.2 GB**，另需 **5.6 GB** 模型。监控栈（Prometheus / Grafana / Alertmanager）挂在 `monitoring` profile 下，需要时加 `INCLUDE_MONITORING=1`，镜像合计约 6.7 GB。

所以「10 分钟」指的是**已经拉过一次之后**。文档标称最低 8 GB 内存 / 4 核；compose 资源上限合计约 9.9 GB（含监控栈），实测基线尚未补齐——见下方「边界」。

生产部署、环境分离、离线语料、升级与关机语义见 [docs/guide/deployment.md](docs/guide/deployment.md)；
环境变量与端口见 [docs/guide/configuration.md](docs/guide/configuration.md)。

## 🔍 怎么验证上面那些说法

不要求你相信 README，跑这几条：

```bash
cd backend
IN_DOCKER=false uv run pytest -q                       # 后端全量：1691 passed / 117 skipped（2026-11-06）
uv run pytest tests/test_error_handling.py -q          # 异常分级：93 例
uv run pytest tests/test_chat_run.py -q                # 续传/停止：35 例
uv run python scripts/run_eval.py --show-misses         # 检索基线：hit@5 0.869 / MRR 0.739（90 题 / 78 篇）
uv run python scripts/consistency_audit.py --sample 5   # 三方对账，只出报告不删（退出码 0/1/2）

# 运维链路：任一条约 1 分钟，起隔离实例、跑真实脚本、自动清理
cd .. && ./scripts/restore_drill.sh          # PostgreSQL 恢复（29 表 / 6s）
./scripts/restore_drill_milvus.sh             # Milvus 恢复（dense+sparse 双索引）
./scripts/rollback_drill.sh                   # 回滚到上一版镜像（含前端与「生成中停机」）
./scripts/scale_rto_drill.sh MODE=pg          # 真实规模 RTO（默认 1 万文档 / 20 万 trace）
```

`consistency_audit.py` 的退出码是刻意的：**0=一致、1=有差异、2=巡检没跑成**。
某一侧采集失败时它不会打印「未发现差异」——假阴性比不巡检更危险。

## 📊 做到了什么：一页数字

每条都能指到脚本或报告，不是估计值。

| 维度 | 结论 | 出处 |
|---|---|---|
| 检索质量 | 90 题 **hit@5 0.869 / MRR 0.739**；未命中的 11 题**全是口语化问法**，制度题与参数题 100% 命中 | [benchmarks/retrieval-baseline](docs/benchmarks/retrieval-baseline-2026-11-02.md) |
| 真实文档探针 | 24 题：概览题 0.598 / 细节题 0.792；加宽重排窗口**反而更低**（0.598→0.501） | [benchmarks/stage-probe](docs/benchmarks/stage-probe-2026-11-03.md) |
| 延迟 | 知识库问答整链 wall **0.9~1.5s**（全本地，零 API 费） | trace stage detail |
| 恢复 RTO | PG 915MB **22s** / Milvus 2 万向量 **29~33s** / MinIO 195MB **18s** | [benchmarks/rto-scale](docs/benchmarks/rto-scale-2026-11-05.md) |
| 性能门禁 | 计数器硬卡点（切分块数 792、融合条数 60）+ 耗时按 profile 卡 1.6× | [benchmarks/perf-baseline](docs/benchmarks/perf-baseline.md) |
| 回滚 | 上一版镜像可在当前 schema 上跑通（ready 27~48s）；代价是**跑不了 alembic** 且**丢掉优雅停机** | [guide/backup-restore §7](docs/guide/backup-restore.md) |

**一个诚实的负面结论**：在同一份语料上，稠密单通道 hit@5 = 0.953、RRF 混合 = 0.891、
混合+重排 = 0.922 —— **生产默认的混合检索打不过不融合的稠密单通道**。原因是 78 篇语料太小、
BM25 引入噪声。没改成默认稠密，因为换语料后结论可能反转，但这条一直留在文档里。

## 🚧 边界（有意不做的事）

写清楚不做什么，比写清楚做了什么更能说明一个项目的取舍：

| 不做 | 原因 |
|---|---|
| 多租户 / 组织层级 / SSO | 现有隔离按 `owner` 做，够单个组织内网使用；加租户层是另一个量级的改造 |
| 高可用 / 多副本 | 9 处进程内状态（运行缓冲、Wiki 去抖、进度表、限流桶…）会互相打架；单机是当前唯一自洽的形态 |
| 桌面端 / 免登录单用户版 | 需要第二套存储抽象（SQLite + 嵌入式向量），成本是重写而不是优化 |
| 「企业级」这个词 | 三条可验证门槛：恢复演练 ✅、质量标注集 ✅（90 题）、**容量实测 ❌**。在此之前它就是打折的形容词 |

这是个人的实践项目，没有真实用户，因此也没有 SLA。它的价值主张是**可复现的严谨**：
每一条声称都能指到文件、指到测试、指到一条能跑的命令。

---

## 📋 能力清单（完整）

上面是三个关键点，这里是全部能力。

| 功能模块 | 特性描述 | 状态 |
|---------|---------|------|
| 🔐 **多用户认证** | API Key（单实例）+ JWT（注册/登录，滑动窗口续期）双模式；用户级 API Key 自助签发/吊销（E.6，MCP/脚本/CI 集成前置）；资源按 `owner_id` 隔离，越权返回 403 | ✅ |
| 📁 **多格式文档支持** | TXT, PDF, DOCX, XLSX, PPTX, MD, CSV, JSON, HTML, EPUB | ✅ |
| 🖼️ **扫描版 PDF OCR** | 扫描版检测 + 双后端（MinerU / PaddleOCR）可选依赖分流，失败回退内置解析 | ✅ |
| 🔍 **智能检索** | 混合检索（BM25 稀疏 + 向量稠密 + RRF 融合）+ Cross-Encoder 重排 | ✅ |
| 🧩 **结构化分块** | 按类型路由分块策略（Markdown / HTML / Word / 代码 / 表格行），保留标题层级 | ✅ |
| ⚡ **语义缓存** | 高相似度问题直接回放答案，Redis 不可用时 fail-open；按 KB 索引失效 | ✅ |
| 📖 **LLM-Wiki 编译层** | 将文档预编译为结构化页面（增量编译 / 交叉链接 / 一致性检查） | ✅ |
| 🌐 **联网搜索** | 支持 Tavily（默认）/ DuckDuckGo / SearXNG，Query 改写 + 重排 + Redis 缓存 | ✅ |
| 🤖 **搜索 Agent** | 有界 Agent 编排（DECIDE-ACT-OBSERVE，默认开启）＋ 原生 Function Calling / ReAct 作为回退路径 | ✅ |
| 🛡️ **Agent 安全护栏** | 输入拦截、工具参数与 URL 校验、输出凭证/系统提示脱敏；高危工具可开人工审批（SSE 审批卡片 + 回执 API）；检索/网页内容按不可信上下文隔离、域名/DLP 策略、`fetch_webpage` 默认关闭 | ✅ |
| 🔐 **隔离、安全与配额（owner 级）** | SEC-1~21：owner 隔离、生产默认关注册、聊天 RPM/并发/日额度 + 上传配额、受信代理 IP + Redis 滑动窗口限流、SSRF fail-closed + 连接层固定 IP、JWT 登出吊销、账号导出/注销与日志脱敏、CSP/HSTS | ✅ |
| 📊 **Agent 评测** | 轨迹级四维确定性评测 + 答案级判分器 + A/B 门槛与离线 harness（`AGENT_TRAJ_EVAL_ENABLED` 默认关；在线抽样未做） | ✅ |
| 💬 **流式输出** | 实时打字效果；**生成与连接解耦**：断网/刷新不丢回答，重连按 `run_id` 续传、可显式停止并保留已生成部分 | ✅ |
| 🏷️ **智能会话命名** | 首条消息发送后自动调用 LLM 生成会话标题 | ✅ |
| 🧹 **问候过滤** | 你好 / hi / 在吗 等日常对话直接走 LLM，跳过知识库检索 | ✅ |
| 🧭 **产品身份口径** | “你是谁 / 你是什么模型”等身份问题统一以知澜回答，避免底层模型自曝 | ✅ |
| 🔄 **多模型 Provider** | Ollama 原生 + OpenAI-Compatible（OpenAI/DeepSeek/通义兼容/Kimi/vLLM/LM Studio）；按任务路由、能力校验、fallback 与热切换；DB 配置 + 加密 Key + 管理 API + 设置页 | ✅ |
| 🔗 **来源展示** | 前端同时展示知识库片段与网页搜索来源（标题/URL） | ✅ |
| 🔒 **完全离线（默认）** | 本地 Ollama 模型，无需付费 API；外部 Provider 需管理员显式启用，本地→外部降级默认禁止 | ✅ |
| 🖥️ **界面支持** | Vue3 + Tailwind/Element Plus 设计系统：语义 token、组件 primitive、三态组件、响应式布局与暗色模式 | ✅ |
| 🧭 **系统能力状态** | 设置页只读展示默认关闭/需运维开启的能力及风险与开启方式 | ✅ |
| 📊 **实验框架** | A/B 实验管理 API + chat 链路分流（变体白名单覆盖 decision/检索参数 + response_time 回收），`AB_EXPERIMENT_CHAT_ENABLED` 默认关 | ✅ |
| 🧠 **动态学习** | 基于反馈的规则学习引擎；写回闭环已接入（单例权重回载 + KB 短路样本补采），开关默认关 | ✅ |
| 🔎 **答案可信度** | 引用补全（embedding 匹配补 `[n]`）+ 数值幻觉校验 + 分档置信度提示 | ✅ |
| 🧭 **意图路由** | 规则 + Embedding 分类 + LLM 路由三层，置信度门控与歧义澄清 | ✅ |
| 📈 **监控告警** | Prometheus + Alertmanager + Grafana（rag_overview 仪表盘已 provision） | ✅ |
| 🧾 **请求追踪** | 每次问答的完整链路耗时记录，经 `/api/traces` 查询 | ✅ |
| 🏷️ **标签分类 / ⭐ 反馈 / ⚙️ 系统配置 / 🔔 实时通知** | 文档分类与标签、评价反馈收集、在线调参、WebSocket 推送变更 | ✅ |
| 🔎 **历史会话** | 关键词搜索（标题 + 消息正文）、按知识库/日期筛选、导出 Markdown/JSON（UX-5，2026-11-07 上线） | ✅ |

---

## 🛠️ 技术栈

版本以 `backend/pyproject.toml`（uv 锁定）、`frontend/package.json` 与 compose 镜像 tag 为准。

| 分类 | 技术 | 版本 | 说明 |
|------|------|------|------|
| 后端框架 | FastAPI | 0.136.3 | 现代高性能 API 框架 |
| ORM | SQLAlchemy | 2.0.50 | 异步数据库操作 |
| 向量数据库 | Milvus（服务端） | v2.6.17（镜像 tag） | 分布式向量存储 |
| 向量数据库客户端 | pymilvus | 3.0.2 | 与上者版本号不同步，注意区分 |
| LLM 集成 | Ollama（Python 客户端） | 0.6.3 | 本地模型运行 |
| 嵌入模型 | bge-m3 | latest | 离线多语言向量嵌入，1024 维 |
| 对象存储 | MinIO（pgsty/minio 社区 fork） | RELEASE.2026-06-18（镜像 tag） | 官方 2025-10 停发镜像并归档仓库，改用仍在维护的 drop-in 兼容 fork |
| 前端框架 | Vue 3 | ^3.5.44 | 现代前端框架 |
| 状态管理 | Pinia | ^3.0.4 | 状态管理 |
| 数据缓存 | Vue Query | ^5.104.5 | 客户端数据缓存 |
| UI 组件 | Element Plus | ^2.14.7 | 企业级组件库 |
| 缓存服务 | Redis（Python 客户端） | 8.1.0 | 缓存与语义缓存 |
| 监控系统 | Prometheus / Grafana | v3.5.0 / v12.0.2 | 指标采集与仪表盘 |

**支持的文档格式**：TXT / MD / PDF / DOCX / DOC / XLSX / XLS / PPTX / PPT / CSV / JSON / HTML / EPUB
（扫描版 PDF 走 OCR 双后端；各格式的分块策略不同，见 [guide/development.md](docs/guide/development.md)）。

### 模型分工

模型名没有内置默认值（去硬编码），必须由 `.env` 提供；启动时经 `/api/tags` 校验存在性，
缺失则**拒绝启动**。下表是开发栈 legacy 取值，用 registry 后以任务路由为准：

| 任务 | 配置项 | 开发栈取值 | 说明 |
|------|--------|-----------|------|
| 主生成模型 | `OLLAMA_MODEL_NAME` | `qwen3:4b` | 复杂推理、RAG 最终答案、Agent / Function Calling |
| 非思考模型 | `OLLAMA_DIRECT_MODEL_NAME` | `qwen2.5:3b-instruct` | 关闭深度思考时使用（直答） |
| 轻量任务模型 | `FAST_LLM_MODEL_NAME` | `qwen2.5:3b-instruct` | 意图路由、标题生成、Query 改写；须与上一项一致以避免同请求内换载 |
| Embedding | `EMBEDDING_MODEL_NAME` | `bge-m3:latest` | 1024 维，须与 `EMBEDDING_DIMENSION` 一致 |
| 重排序 | `KB_RERANK_MODEL` / `SEARCH_RERANK_MODEL` | `BAAI/bge-reranker-base` | 精排；provider 可选 `sentence_transformers` / `ollama` |

**为什么 4B 只有一个入口**：4GB 显存下 4B（3.9GiB）驻留会驱逐 3B 直答模型，形成换载拉锯。
所以 **4B 唯一入口 = `deep_thinking=on` 的主回答链**，辅助任务全部归 `fast_llm`。

模型可热切换、无需重启（DB → `MODEL_REGISTRY_FILE` → `.env` 三级优先级），
Provider Key 在 DB 中加密、只写不读；同 Provider 连续失败 3 次熔断 60s。
细节见 [design/model-provider-registry-2026-10.md](docs/design/model-provider-registry-2026-10.md)。

---

## 📁 项目结构（顶层）

```
langchain_rag_demo/
├── docker-compose.yml / .dev.yml   # 生产 / 开发编排
├── .env.example                    # 全量配置模板（189 键，含中文注释）
├── scripts/                        # 启停、备份恢复、演练、验收（见下表）
├── backend/
│   ├── start.py                    # 入口（环境检查 + FastAPI 启动）
│   ├── pyproject.toml / uv.lock    # 依赖唯一事实来源
│   ├── alembic/versions/           # 11 个迁移
│   ├── scripts/                    # 评测、对账、探针、性能、迁移
│   ├── tests/                      # 134 个测试文件
│   └── src/
│       ├── config.py               # 配置中心（pydantic-settings，按 APP_ENV 选 .env.{env}）
│       ├── auth.py                 # API Key / JWT / owner 归属校验
│       ├── main.py                 # 装配与 lifespan（巡检调度等）
│       ├── api/                    # REST 路由（17 个模块）
│       ├── services/               # 业务实现（检索、缓存、编译、评测、审计…）
│       └── utils/                  # 异常分级、日志、限流等
├── frontend/src/                   # Vue 3（47 个 .vue + 62 个 .ts）
├── docs/                           # 见「文档地图」
└── monitoring/                     # Prometheus / Grafana / Alertmanager 配置
```

### 常用脚本

| 类别 | 脚本 | 说明 |
|---|---|---|
| 启停 | `start-dev.* / start-prod.* / stop-*.{ps1,bat,sh}` | 开发/生产启停；生产含安全检查与 alembic 迁移 |
| 日志 | `logs-dev.* / logs-prod.*` | 实时日志，可指定服务 |
| 备份恢复 | `backup.sh` / `restore.sh` | PG + MinIO；容器模式降级（宿主机无客户端时借容器） |
| 演练 | `restore_drill*.sh` / `rollback_drill.sh` | 恢复（PG / MinIO / Milvus）与回滚，隔离实例、自动清理 |
| 规模 | `scale_rto_drill.sh MODE=pg\|milvus\|minio\|all` | 万级数据量 RTO |
| 评测 | `backend/scripts/run_eval.py`、`perf_suite.py`、`probe_retrieval_stages.py` | 检索质量、性能门禁、逐级探针 |
| 对账 | `backend/scripts/consistency_audit.py` | 三方一致性，只读 |
| 验收 | `check_env_consistency.py`（CI 跑）、`scripts/e2e_verify_chat.py`、`scripts/ask_once.py` | 配置键一致性、端到端问答验收、单次问答（报帧数与是否收到 `end`） |

脚本参数（`-SkipWait` / `-Force` / `-Clean`、`SKIP_WAIT=1` / `FORCE_START=1`）见
[归档 README §配套脚本](docs/archive/README-dev-2026-11-07.md)。

---

## 📖 文档地图

| 想知道 | 读 |
|---|---|
| **结项结论**：做到了什么 / 没做什么 / 遗留风险 | [docs/FINAL-REPORT.md](docs/FINAL-REPORT.md) |
| 系统怎么组织的、代码在哪 | [docs/architecture.md](docs/architecture.md) |
| 接口有哪些、参数是什么 | [docs/api.md](docs/api.md) |
| 部署、环境分离、升级与关机语义、踩坑 | [docs/guide/deployment.md](docs/guide/deployment.md) |
| 环境变量、端口、配置加载机制 | [docs/guide/configuration.md](docs/guide/configuration.md) |
| 本地开发、测试、CI、已知踩坑 | [docs/guide/development.md](docs/guide/development.md) |
| 备份 / 恢复 / 回滚 / 容量与扩容 | [docs/guide/backup-restore.md](docs/guide/backup-restore.md) |
| 监控栈、指标、告警处置 | [docs/guide/monitoring.md](docs/guide/monitoring.md) |
| 三个静默失败的完整叙事（长文） | [docs/articles/silent-failures.md](docs/articles/silent-failures.md) |
| 所有量化结论 | [docs/benchmarks/](docs/benchmarks/) |
| 设计文档（一文一事） | [docs/design/](docs/design/) |
| 过程账本（417 行，已冻结） | [docs/archive/project-status-2026-11-07.md](docs/archive/project-status-2026-11-07.md) |

文档索引与命名规范见 [docs/README.md](docs/README.md)。

---

## 📄 许可证

MIT License

## 🤝 贡献

这是个人实践项目，没有对外贡献流程。如果要用它：先读「边界」与结项说明 §4 的负面结论，
别在已经被证否的方向上重做一遍。分支与合并、代码约定的历史记录见
[docs/archive/README-dev-2026-11-07.md](docs/archive/README-dev-2026-11-07.md) 与
[docs/guide/development.md](docs/guide/development.md)。
