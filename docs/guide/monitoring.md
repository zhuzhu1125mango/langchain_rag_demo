# 监控运维指南

> 定位：监控栈的使用入口——看什么面板、指标含义、告警规则与处置思路。
> 配置文件：`configs/prometheus/`、`configs/alertmanager/`、`configs/grafana/`
> 更新时间：2026-09-24

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

## 4. 告警规则（configs/prometheus/alerts.yml）

共 10 条告警，分三组：

| 组 | 告警 | 表达式（摘要） | 级别 | 含义 |
|---|---|---|---|---|
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

## 5. 告警通知（Alertmanager）

**当前状态：仅 webhook 占位，告警只在 Alertmanager UI 展示，不会主动推送。**

启用通知：编辑 `configs/alertmanager/alertmanager.yml`，取消 `webhook_configs`（或 email 段）注释并填入真实配置，随后重启 alertmanager 容器。默认路由按 `alertname` 分组，重复通知间隔 1h；critical 告警会抑制同名的 warning。

## 6. 常用操作

```bash
# 查看监控栈状态
docker compose ps prometheus grafana alertmanager

# 修改告警规则后生效（规则文件挂载进容器，重启即可）
docker compose restart prometheus

# Grafana 数据源
# configs/grafana/datasources/prometheus.yml 已预置 Prometheus 数据源（指向 prometheus:9090）
```

## 相关文档

- 部署与端口规划：[deployment.md](deployment.md)
- 架构总览：[../architecture.md](../architecture.md)
