# 监控运维指南

> 定位：监控栈的使用入口——看什么面板、指标含义、告警规则与处置思路。
> 配置文件：`configs/prometheus/`、`configs/alertmanager/`、`configs/grafana/`
> 更新时间：2026-09-01

## 1. 访问入口

| 组件 | 地址（生产绑定回环） | 用途 |
|---|---|---|
| Prometheus | http://127.0.0.1:9090 | 指标查询、告警规则状态 |
| Grafana | http://127.0.0.1:3000 | 可视化面板 |
| Alertmanager | http://127.0.0.1:9093 | 告警分组/抑制/分发 |

> 生产环境监控端口仅绑定 `127.0.0.1`，远程访问请走 SSH 隧道或反向代理。开发环境使用 `prometheus.dev.yml`，且不含 alertmanager 服务。

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
| `rag_documents_processed_total` | Counter | 已处理文档数 |

## 4. 告警规则（configs/prometheus/alerts.yml）

| 告警 | 表达式（摘要） | 级别 | 含义 |
|---|---|---|---|
| FastAPIHighErrorRate | 错误率 > 5% 持续 1m | warning | 接口大量报错 |
| FastAPILatencyHigh | P95 延迟 > 5s 持续 1m | critical | 接口整体变慢 |
| FastAPIDown | up == 0 持续 30s | critical | 后端挂了/未启动 |
| MilvusDown | up == 0 持续 30s | critical | 向量库不可用 |
| MilvusHighQueryLatency | 平均查询延迟 > 2s 持续 1m | warning | 检索变慢 |
| LLMCallHighLatency | P95 > 30s 持续 1m | warning | 模型推理变慢（Ollama 过载/排队） |
| LLMCallErrors | 错误率 > 10% 持续 1m | critical | 模型调用大量失败 |

### 处置思路速查

- **FastAPIDown / MilvusDown**：`docker compose ps` 看容器状态 → `logs backend / milvus-standalone`。Milvus 依赖 etcd + MinIO，先确认两者健康
- **FastAPILatencyHigh**：结合 `rag_active_requests`（并发过高）与 `rag_llm_call_duration_seconds`（多数延迟来自 LLM）定位瓶颈层
- **LLMCallErrors**：确认 Ollama 进程存活与模型已拉取；常见原因是模型未加载或显存/内存不足
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
