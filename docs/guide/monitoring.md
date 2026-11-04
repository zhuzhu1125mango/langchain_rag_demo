# 监控运维指南

> 定位：监控栈的使用入口——看什么面板、指标含义、告警规则与处置思路。
> 配置文件：`configs/prometheus/`、`configs/alertmanager/`、`configs/grafana/`
> 更新时间：2026-10-26

## 1. 访问入口

| 组件 | 生产栈地址 | 开发栈地址 | 用途 |
|---|---|---|---|
| Prometheus | http://127.0.0.1:**9094** | http://localhost:9090 | 指标查询、告警规则状态 |
| Grafana | http://127.0.0.1:**3001** | http://localhost:3000 | 可视化面板 |
| Alertmanager | http://127.0.0.1:**9095** | —（dev 栈未部署） | 告警分组/抑制/分发 |

> 生产环境监控端口仅绑定 `127.0.0.1`，远程访问请走 SSH 隧道或反向代理。
> 生产端口与开发栈**全部错开**（详见 [deployment.md](deployment.md) §2.2.1）。
>
> ✅ **Grafana 已 provision「RAG Overview」面板**：`configs/grafana/dashboards/rag_overview.json`
> （经 `provisioning/dashboards/dashboards.yml` 自动加载），覆盖请求量/延迟/错误率、
> LLM 调用、检索、语义缓存与 Wiki 编译指标；如需新增面板，补充该目录下的 JSON 即可。

## 2. 采集目标（prometheus.yml）

| Job | 目标 | 说明 |
|---|---|---|
| fastapi-app | `backend:8000/metrics` | 后端业务指标（10s 间隔） |
| milvus | `milvus-standalone:9091` | Milvus 自带指标端点 |
| postgresql | `postgres-exporter:9187` | PostgreSQL 指标（凭据来自 POSTGRES_USER/PASSWORD 环境变量） |
| prometheus | `localhost:9090` | 自监控 |

抓取/评估间隔 15s，fastapi/milvus 为 10s。容器间通信用服务名（非 host.docker.internal）。

## 3. 核心业务指标（backend/src/middleware/prometheus.py）

| 指标 | 类型 | 含义 |
|---|---|---|
| `rag_request_total` | Counter | 请求总数 |
| `rag_request_duration_seconds` | Histogram | 请求延迟（P95 告警基于此） |
| `rag_request_errors_total` | Counter | 请求错误数 |
| `rag_active_requests` | Gauge | 当前并发请求数 |
| `rag_llm_calls_total` / `rag_llm_call_errors_total` | Counter | LLM 调用总数/失败数 |
| `rag_llm_call_duration_seconds` | Histogram | LLM 调用延迟 |
| `rag_vector_search_duration_seconds` | Histogram | 向量检索延迟 |
| `rag_vector_search_total` | Counter | 向量检索次数 |
| `rag_retrieval_errors_total` | Counter | 知识库检索失败数（混合失败且 dense 兜底也失败，P2-5） |
| `rag_sse_interrupted_total` | Counter | SSE 流中断数（客户端中途断连，P2-5） |
| `rag_documents_processed_total` / `rag_document_process_duration_seconds` | Counter/Histogram | 文档处理数/耗时（按 file_type） |
| `rag_kb_queries_total` | Counter | 知识库问答数（按 answer_type） |
| `rag_strategy_decisions_total` | Counter | 策略决策数（strategy_name × result） |
| `semantic_cache_hits_total` / `misses_total` / `stores_total` / `lookup_seconds` | Counter/Histogram | 语义缓存命中/未命中/写入/查找延迟 |
| `wiki_compilations_total` 等 wiki_* 指标族 | Counter/Histogram | Wiki 编译次数/产出页数/耗时/事实保留率/锁获取/矛盾发现 |

> 完整定义以 `backend/src/middleware/prometheus.py` 为准（指标较多，此处列核心族）。

### 3.1 模型路由与 Provider 观测（P1/P2.1）

- 按 `model_name` 维度的旧指标保留（`rag_llm_calls_total` / `..._errors_total` / `..._duration_seconds`）；P2.1 新增按 Provider 与任务归因：
  - `rag_model_provider_calls_total{provider_id, model_name, task, result}`：调用数/失败数（result=success/error）
  - `rag_model_tokens_total{provider_id, model_name, direction}`：input/output Token（流式取决于 Provider 是否在末尾返回用量）
  - `rag_model_circuit_state{provider_id}`：1=open、0.5=half_open、0=closed；连续失败 3 次打开 60s 熔断
- 切换模型后指标标签会新增/更替对应 `provider_id` / `model_name` 序列；Grafana「RAG Overview」按模型聚合，不会因切换出现断点。
- 熔断期间该 Provider 的调用会被直接跳过并计入应用日志 warning（`Provider 熔断中，跳过`）；用量与熔断状态可用 `GET /api/admin/models/usage?days=7` 查看。
- 用量增量每 `MODEL_USAGE_FLUSH_INTERVAL_S`（默认 60s）写入 `model_usage_daily`，应用关闭时最后 flush；查询接口返回最近 N 天持久化聚合 + 当前进程未落库增量，重启只可能丢失最后一个 flush 周期的增量。
- 成本估算按模型配置的每 1K tokens 单价计算；未配置价格的调用 `estimated_cost=0` 且计入 `unpriced_calls`，需结合该字段判断成本数据是否完整。
- 外部 Provider 的失败与超时同样计入 `rag_llm_call_errors_total` 与熔断计数；排查时结合 `/api/config` 的 `registry_source` 与 `route_models` 确认真实路由。
- **当前仍未实现**：按租户维度的配额/成本上限与 429 告警（属于 MODEL-P4）；设置页已提供用量/成本展示与模型单价配置。
- **索引迁移（P3）**：`GET /api/admin/models/index/status` 查看当前索引画像与目标 Embedding 配置；重建期间 `status=rebuilding`，失败会记录 `last_error` 并保留线上索引。重建/回滚会全量失效语义缓存（`rag:semcache:*`），命中率短时下降属预期。
- **健康探针（SEC-12）**：`/health/live` 仅判断进程存活；`/health/ready` 深探 DB/Redis/Milvus/MinIO，任一失败返回 503；`/health/detail`（管理员）含依赖错误与 Ollama。
- **认证与隐私（SEC-10/14）**：登出/refresh 轮换通过 `rag:jwt:revoked:*` 吊销 jti；日志中用户问题已替换为 `<redacted:hash:len>`，排查时用 trace 表而非日志还原原文。
- **结构化日志（OPS-3）**：每条日志自动携带 `request_id` / `user_id` / `stage` 三个上下文维度。
  - `LOG_LEVEL`（默认 `INFO`）、`LOG_FORMAT`（`text` 默认 / `json`）；
  - text 格式追加 `[rid=... user=... stage=...]`，json 输出单行对象（`ts/level/logger/message/request_id/user_id/stage`，异常含 `exc_info`）；
  - `user_id` 在认证依赖通过后绑定（匿名/API Key 全局用户为 `api_key_user`，未认证为 `-`）；
  - `stage` 覆盖管道阶段：`enhance_context` / `intent` / `tool_first` / `decide` / `semantic_cache` / `kb_retrieval` / `generation` / `finalize`；
  - 排查慢请求时可按 `request_id` 过滤全链路日志，按 `stage` 聚合定位耗时阶段。
- 切换 Embedding 模型后，历史向量仍由旧模型生成；在重建索引前不要以检索指标下降判断新模型质量。

## 4. 告警规则（configs/prometheus/alerts.yml）

共 12 条告警，分四组：

| 组 | 告警 | 表达式（摘要） | 级别 | 含义 |
|---|---|---|---|---|
| scrape | ScrapeTargetDown（2026-11-02） | `up == 0` 持续 2m | critical | 任一抓取目标不可达；**`/metrics` 返回 5xx 也算 up=0**，指标会整页消失 |
| scrape | PostgresDatabaseDown（2026-11-02） | `pg_up == 0` 持续 1m | critical | exporter 活着但连不上库 |
| fastapi | FastAPIHighErrorRate | `rate(errors_total[1m]) / clamp_min(rate(request_total[1m]), 1e-9) > 0.05` | warning | 接口错误率 > 5%（比值口径） |
| fastapi | FastAPILatencyHigh | P95 延迟 > 5s 持续 1m | critical | 接口整体变慢 |
| fastapi | FastAPIDown | `up{job="fastapi-app"} == 0` 持续 30s | critical | 后端挂了/未启动 |
| fastapi | SseStreamInterruptRate（P2-5） | `rate(rag_sse_interrupted_total[5m]) > 1` 持续 3m | warning | SSE 流式中断过多（网络不稳/前端提前取消） |
| milvus | MilvusDown | `up{job="milvus"} == 0` 持续 30s | critical | 向量库不可用 |
| milvus | RetrievalFailureRate（P2-5） | `rate(rag_retrieval_errors_total[5m]) > 0` 持续 2m | warning | 检索持续失败（混合失败且 dense 兜底也失败；Milvus 连接失败由 MilvusDown 覆盖） |
| milvus | MilvusHighQueryLatency | `milvus_proxy_sq_latency_milliseconds` 均值 > 2000 持续 1m | warning | 检索变慢（指标名已实测修正为毫秒口径） |
| llm | OllamaTimeoutRate（P2-5） | 按模型分组失败率 > 10% 持续 2m | critical | Ollama 请求超时/失败率过高（含 Tavily，经 model_manager 归并） |
| llm | LLMCallHighLatency | P95 > 30s 持续 1m | warning | 模型推理变慢（Ollama 过载/排队） |
| llm | LLMCallErrors | 错误率 > 10% 持续 1m | critical | 模型调用大量失败 |

> 规则文件经 `test_prometheus_alerts.py` 校验（YAML 合法、severity 在 labels 下、alert 名唯一、
> 指标名与 prometheus.py 定义一致）。Alertmanager webhook URL 留空（通知渠道留待部署，见 §5）。

### 处置思路速查

- **FastAPIDown / MilvusDown**：`docker compose ps` 看容器状态 → `logs backend / milvus-standalone`。Milvus 依赖 etcd + MinIO，先确认两者健康
- **FastAPILatencyHigh**：结合 `rag_active_requests`（并发过高）与 `rag_llm_call_duration_seconds`（多数延迟来自 LLM）定位瓶颈层
- **FastAPIHighErrorRate**：按 `endpoint`/`error_type` 标签拆分 `rag_request_errors_total` 定位具体接口
- **RetrievalFailureRate**：查看 backend 日志中 Milvus 查询/混合检索异常；确认 Milvus 存活（MilvusDown 未触发则多为查询层错误，如集合 schema 不匹配）
- **SseStreamInterruptRate**：多为网络不稳或前端提前取消（用户主动停止生成）；持续高位需排查网关超时配置
- **OllamaTimeoutRate / LLMCallErrors**：确认 Ollama 进程存活与模型已拉取；常见原因是模型未加载或显存/内存不足（按 `model_name` 标签定位具体模型）
- **MilvusHighQueryLatency**：检查集合数据量增长、是否有 rebuild 任务在后台执行
- **ScrapeTargetDown**：`curl <target>/metrics` 看是否 200。**别只看容器 `healthy`**——postgres-exporter 曾因挂载的 legacy `queries.yaml`（v0.20 已废弃该格式）让 `/metrics` 整页 500 数小时，容器一直「在跑」、PG 指标全无、也没有任何告警（当时没有 `up == 0` 规则）。排查顺序：`docker exec <exporter> wget -qO- localhost:9187/metrics | head` → 500 就去掉 `--extend.query-path` 再看

> 修改 `alerts.yml` / `prometheus.yml` 后需 **重启 Prometheus** 生效（未开启 `--web.enable-lifecycle`，
> `POST /-/reload` 返回 403）：`docker restart prometheus-prod`，随后 `/api/v1/rules` 核对规则数。
> 改前先校验：`docker exec prometheus-prod /bin/promtool check rules /etc/prometheus/alerts.yml`。

## 5. 告警通知（Alertmanager）

**当前状态：仅 webhook 占位，告警只在 Alertmanager UI 展示，不会主动推送。**

启用通知：编辑 `configs/alertmanager/alertmanager.yml`，取消 `webhook_configs`（或 email 段）注释并填入真实配置，随后重启 alertmanager 容器。默认路由按 `alertname` 分组，重复通知间隔 1h；critical 告警会抑制同名的 warning。

## 6. 常用操作

> **监控栈现在是 opt-in（P0-3）**：默认 `docker compose up -d` 只起 7 个核心服务。
> 需要指标/告警时，两种打开方式：
> - 开发栈：`INCLUDE_MONITORING=1 ./scripts/start-dev.sh`
> - 手工：`docker compose -f docker-compose.yml --env-file .env.prod --profile monitoring up -d`
> - 生产脚本 `start-prod.*` 已内置该 profile，不受影响。


```bash
# 查看监控栈状态
docker compose ps prometheus grafana alertmanager

# 修改告警规则后生效（规则文件挂载进容器，重启即可）
docker compose restart prometheus

# Grafana 数据源
# configs/grafana/datasources/prometheus.yml 已预置 Prometheus 数据源（指向 prometheus:9090）
```

### 6.1 每日一致性巡检（只读）

指标只能反映「有请求经过的地方」，而「元数据已删、向量/对象/缓存还在」这类问题
**不会触发任何异常**——没有代码路径可以挂日志，只能主动对账。巡检覆盖：

| 链路 | 检查项 |
|---|---|
| Milvus ↔ DB | 孤儿向量、文档下架后仍有向量、移库后 kb_id 未更新、kb_id 为空/指向已删库、已发布却检索不到 |
| MinIO ↔ DB | 无主对象、DB 引用但文件已丢失 |
| Redis ↔ DB | 缓存条目引用已删/未发布文档、所属库已删、过 TTL 未清、索引指向不存在的 scope |

```bash
# 手动跑一次（一次性容器，不碰正在服务的 backend 进程）
./scripts/nightly_consistency.sh
AUDIT_DIR=/var/log/rag-audit ./scripts/nightly_consistency.sh --skip-minio

# cron：每日 04:10（错开 02:30 备份）
10 4 * * * cd /opt/langchain_rag_demo && /usr/bin/env bash scripts/nightly_consistency.sh >> /var/log/rag-audit.log 2>&1
```

Windows 宿主机（Docker Desktop）**优先用下面这条「进程内调度」**，不要先考虑计划任务：

#### 方案 A（推荐）：进程内周期调度，不依赖宿主机

巡检跑在 backend 进程里（`CONSISTENCY_AUDIT_INTERVAL_HOURS`），容器在跑巡检就在跑。
这样做的原因很实际：`schtasks` 在受限机器上会被安全策略直接拦死（不可绕过），
一旦「每天巡检」挂在某台机器的任务计划里，换台机器、换个人就静默消失，
而台账上还写着「脚本已验证」——本项目在 2026-11-04 的自检里就撞到了这个坑
（`backups/audit/` 最新产物停在两天前，没人发现）。

```bash
# .env.prod（或 .env.dev）开一次即可；0 = 关闭，默认关闭
CONSISTENCY_AUDIT_INTERVAL_HOURS=24      # 间隔小时
CONSISTENCY_AUDIT_OUTPUT_DIR=            # 留空 = backend/data/consistency（生产已挂载到宿主机）
CONSISTENCY_AUDIT_KEEP_FILES=30          # 保留最近 30 份
CONSISTENCY_AUDIT_TIMEOUT_SECONDS=1800   # 单轮超时，防止扫描卡住占死循环

# 生效
docker compose up -d backend
```

三个行为要点（都在单测里钉住了）：

- **启动不无条件补跑**：先读 Redis 的 `rag:audit:last_run`，间距未到只等剩余时间；
  没有记录才在 60s 后跑一次。否则「容器每天重启一次」会让每轮都从头计时，永远等不到窗口。
- **有差异记 WARNING**：后台循环没有退出码，日志是唯一能被告警抓到的出口，
  所以 WARNING 行里带 `issues=` 与各存储的 `sections=`。
- **报告落在 `backend/data/consistency/`**（容器内 `/app/data` 已挂载），按文件名轮转。

验证（同样三步）：

```bash
# ① 启动日志里应有「一致性巡检周期调度已启动」与「下次执行将在 N 分钟后」
docker logs backend-prod 2>&1 | grep -E "一致性巡检"

# ② 触发一轮：删掉上次执行记录 + 重启，60s 后应出现「一致性巡检通过/发现差异」
docker exec redis-prod redis-cli DEL rag:audit:last_run
docker compose restart backend

# ③ 产物：宿主机 backend/data/consistency/ 下应有当天的 consistency_*.json
docker exec backend-prod ls -l /app/data/consistency | tail -3
```

> 进程内版本的**已知边界**：多副本部署时每个副本都会跑一遍（只读对账，重复无害但浪费）；
> 需要严格单实例请关掉该开关，改用下面的 cron/计划任务方案。

#### 方案 B（备选）：宿主机计划任务

Windows 宿主机（Docker Desktop）用 PowerShell 版本 + 计划任务：

```powershell
# 手动跑一次
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\nightly_consistency.ps1

# 注册每日 04:10 计划任务（需允许 schtasks；Docker Desktop 需保持运行）
schtasks /Create /SC DAILY /ST 04:10 /TN "rag-consistency-audit" `
  /TR 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\MyCode\langchain_rag_demo\scripts\nightly_consistency.ps1"' /F
```

> 若 `schtasks` 被组策略/终端安全软件拦截，改用「任务计划程序」图形界面新建任务：
> 操作 = `powershell.exe`，参数 = `-NoProfile -ExecutionPolicy Bypass -File <仓库>\scripts\nightly_consistency.ps1`，
> 起始于 = 仓库根目录。

**注册后必须验证（三步，缺一步都不算接好）**：

```powershell
# ① 手动跑一次：应 exit 0，且 backups\audit\ 下出现当天的 .json 与 .log
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\nightly_consistency.ps1; $LASTEXITCODE
# ② 让计划任务立即触发一次，而不是等第二天
schtasks /Run /TN "rag-consistency-audit"
# ③ 次日核对：任务上次运行结果应为 0x0；日志时间戳是当天 04:10 前后
schtasks /Query /TN "rag-consistency-audit" /V /FO LIST | Select-String "上次运行|上次结果"
```

前置条件与坑（2026-11-02 实测补充）：

- **Docker Desktop 必须处于运行状态**，否则一次性容器创建失败 → 退出码 2；
- 脚本文件必须是 **UTF-8 with BOM**：Windows PowerShell 5.1 对无 BOM 的 UTF-8 按 ANSI 解析，
  中文会变成乱码（`涓€鑷存€у贰妫€`）。仓库里的 `nightly_consistency.ps1` 已是 BOM 版本，
  用编辑器另存时别把 BOM 去掉；
- 容器里的 Python 输出是 **LF 换行**，而 PS 5.1 的 `Get-Content` 只按 CRLF 切分，
  整份日志会被当成一行回显（行全粘在一起）。脚本已改用 `[IO.File]::ReadAllLines` 读日志；
  **日志文件本身是好的**，只是回显问题，别误判成巡检输出损坏。

产物：`backups/audit/consistency_<日期>.json`（结构化，`kind` 稳定可直接做告警规则）与同名 `.log`。

退出码：`0` 一致 / `1` 发现差异 / `2` 巡检未完整执行（连不上某侧存储）。
**`2` 要当故障处理**：它说明这次巡检没看到全貌，不能当成「没问题」。

> 报告出来后的清理是破坏性操作：脚本故意没有 `--fix`，先按 `subject` 人工确认，
> 再走 backup-restore.md 里的删除流程。

## 相关文档

- 部署与端口规划：[deployment.md](deployment.md)
- 备份 / 恢复 / 回滚 / 容量与演练：[backup-restore.md](backup-restore.md)
- 架构总览：[../architecture.md](../architecture.md)
