# 备份 / 恢复 / 回滚与容量 Runbook（SEC-13 / OPS-1）

> 定位：生产运维的「数据保护 + 故障恢复 + 容量规划」单一口径。
> 配套脚本：`scripts/backup.sh`、`scripts/restore.sh`（bash；Windows 用 Git Bash / WSL 执行）。
> 更新时间：2026-10-27（OPS-1 补全：新增回滚矩阵、容量模型与扩容阈值）

## 1. 目标与 RPO / RTO

| 对象 | 备份方式 | RPO | RTO 目标 | 可重建 |
|---|---|---|---|---|
| PostgreSQL | `pg_dump` 每日 | 24h | ≤1h | 否（元数据唯一来源） |
| MinIO（原始文件） | `mc mirror` 每日 | 24h | ≤1h | 否（原件唯一来源） |
| Milvus（向量/索引） | `milvus-backup` 每周 + 变更前 | 7d | 数小时~1 天 | 是（PG+MinIO 可重放） |
| 配置 / 密钥 | 密钥管理系统版本化 | 变更即备份 | ≤15min | 否（必须单独保管） |
| Redis（缓存/限流/吊销名单） | 不备份 | — | 分钟级 | 是（重放后为空，见 §5.4） |
| Prometheus / Grafana | 数据卷 | 可选 | — | 是（面板由 provisioning 重建） |

> Milvus 可重建，因此其 RPO/RTO 明显宽于 PG/MinIO；真正不可丢的是 PG 与 MinIO。
> 恢复演练每季度一次（§7），验收以「RTO 实测值」为准。

## 2. 备份范围

- **PostgreSQL**：知识库、文档与分块元数据、会话/消息、`request_traces`、审计、学习样本、`model_usage_daily`、模型注册表。
- **MinIO**：上传原件、文档附件/图片等对象（bucket 名默认 `documents`）。
- **Milvus**：`documents` 集合（dense HNSW + 可选 BM25 sparse）。
- **不纳入**：`node_modules`、构建产物、`backups/` 自身、Milvus 中间 WAL。

保留策略（GFS）：

| 产物 | 日备保留 | 周备保留 | 月备保留 |
|---|---|---|---|
| PG dump（`.sql.gz`） | 30 天 | 12 周 | 12 月 |
| MinIO 镜像目录 | 30 天 | 12 周 | 12 月 |
| Milvus 快照 | — | 8 周 | 6 月 |

## 3. 执行备份

### 3.1 一次性备份

生产栈的 compose 调用必须带项目文件与 env 文件（凭据来自 `secrets/`，不在 `.env.prod`）：

```bash
COMPOSE="docker compose -f docker-compose.yml --env-file .env.prod"

export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5434 POSTGRES_DB=app_prod POSTGRES_USER=prod_user
export POSTGRES_PASSWORD="$(cat secrets/postgres_password)"   # 由 Docker Secrets 提供，勿写入 shell 历史
export MINIO_ALIAS=local MINIO_BUCKET=documents BACKUP_DIR=/backup

mc alias set local http://127.0.0.1:9002 \
  "$(cat secrets/minio_root_user)" "$(cat secrets/minio_root_password)"
./scripts/backup.sh
```

Milvus（`milvus-backup` 版本需与 Milvus 2.6.x 兼容）：

```bash
milvus-backup create -n "backup_$(date +%Y%m%d_%H%M%S)"
```

### 3.2 校验（缺一不可）

```bash
gzip -t /backup/postgres_<stamp>.sql.gz           # 压缩包完整性
gunzip -c /backup/postgres_<stamp>.sql.gz | head  # 应为 SQL 头，不是空文件
ls /backup/minio_<stamp>/ | wc -l                 # 与 bucket 对象数量级一致
milvus-backup list                                # 快照存在且 size > 0
```

备份产物必须**异地存放**（对象存储 / 离线盘），落盘权限 600，含凭据的 dump 建议 `age`/`gpg` 加密后再外传。
未通过 §3.2 校验的备份按「无备份」处理。

### 3.3 定时执行

Linux（cron，prod 栈）：

```cron
# 每日 02:30 备份；日志追加到 /var/log/rag-backup.log
30 2 * * * cd /opt/langchain_rag_demo && /usr/bin/env bash scripts/backup.sh >> /var/log/rag-backup.log 2>&1
# 每周日 03:30 Milvus 快照
30 3 * * 0 cd /opt/langchain_rag_demo && milvus-backup create -n "weekly_$(date +%Y%m%d)" >> /var/log/rag-backup.log 2>&1
```

Windows：用「任务计划程序」调用 Git Bash 执行同一脚本；失败告警需自行接入（脚本退出码非 0 即失败）。

> cron 不继承交互 shell 的环境：把 §3.1 的 `export` 与 `mc alias set` 写入 `/etc/rag-backup.env`（权限 600），
> 并在 cron 行前 `. /etc/rag-backup.env &&`；否则备份会以「凭据为空」静默失败。

## 4. 恢复

### 4.1 恢复顺序（不可打乱）

1. **停写**：`$COMPOSE stop backend`（前端可保留只读页，避免新请求写入）；
2. **PG**：目标库必须为空（`DROP SCHEMA public CASCADE; CREATE SCHEMA public;` 或新建库后切换 `POSTGRES_DB`），再执行 `restore.sh` 导入 dump；非空库直接导入会因对象已存在而中断；
3. **schema 对齐**：`cd backend && uv run alembic current` 与 `alembic heads` 比对，必要时 `alembic upgrade head`；
4. **MinIO**：`mc mirror` 回灌原件；
5. **Milvus**：`milvus-backup restore -n <name>`；若无快照，走 §4.3 重建；
6. **Redis**：清空应用缓存键（§5.4），避免旧答案/旧限流被复用；
7. **启动**：`$COMPOSE up -d backend`，确认 `/health/ready` 返回 200；
8. **验收**：抽查 3 个知识库问答（引用来源、权限隔离）+ 1 个 Agent 工具调用，记录耗时。

```bash
COMPOSE="docker compose -f docker-compose.yml --env-file .env.prod"
export PG_FILE=/backup/postgres_<stamp>.sql.gz MINIO_DIR=/backup/minio_<stamp>
./scripts/restore.sh
$COMPOSE stop backend && $COMPOSE up -d backend
curl -fsS http://127.0.0.1:8001/health/ready
```

### 4.2 恢复后必做

- 语义缓存失效：`docker exec redis-prod redis-cli --scan --pattern 'rag:semcache:*' | xargs -r docker exec -i redis-prod redis-cli del`（重建/回滚索引会自动失效，手工恢复不会）；
- 核对 `EMBEDDING_DIMENSION` 与集合 schema 一致（维度不符时启动会在 `ensure_collection` 阶段报错）；
- 核对 `GET /api/admin/models/index/status`：`status` 与 `registry` 中 Embedding 路由是否一致；
- 观察 10 分钟错误率与 P95（`monitoring.md` §3），异常则进入 §5 回滚判断。

### 4.3 部分恢复（推荐用于单知识库/单文档）

只丢单个知识库时，不必整库恢复 Milvus：从 MinIO 取回原件 → 前端重新上传/重新解析 → 新切片自动写入向量库。
优点：不影响其他租户数据；缺点：Embedding 模型必须与当前线上一致，否则新老向量不可比。

## 5. 回滚

### 5.1 决策矩阵

| 症状 | 首选动作 | 禁止动作 |
|---|---|---|
| 新版本启动失败 / 接口 5xx 飙升 | 回退应用版本（§5.2），DB 保持不动 | 直接 `downgrade` 试探 |
| 迁移报错 / `alembic check` 漂移 | 恢复迁移前 PG 备份（§4） | 手工改表结构 |
| 检索结果骤降 / 维度不匹配 | 索引回滚或重建（§5.4） | 删除集合后盲目重灌 |
| 配置改错（模型/限额/安全开关） | 回滚 `.env.prod` 并重启 backend | 只改容器内文件（重启即丢） |
| 凭据泄露 | 轮换密钥 + 吊销 JWT（§5.4） | 仅重启不轮换 |

**回滚前置**：先做一次 §3.1 备份。回滚是「用已知状态覆盖未知状态」，没有当前备份的回滚等于二次事故。

### 5.2 应用版本回滚

```bash
COMPOSE="docker compose -f docker-compose.yml --env-file .env.prod"
# 1) 备份当前库
./scripts/backup.sh
# 2) 切回上一个可用版本（tag 或 commit）
git checkout <last-good-tag>
$COMPOSE build backend frontend
$COMPOSE up -d backend frontend
# 3) 确认
curl -fsS http://127.0.0.1:8001/health/ready
```

- 后端与前端**同批回退**（SSE 事件协议、审批卡片等跨端契约需版本匹配）；
- 回退后若旧代码不认识新表字段，以 PG 备份恢复为准，不要保留「新 schema + 旧代码」组合长期运行。

### 5.3 数据库迁移回滚

- 优先「恢复备份」，而不是 `alembic downgrade`：本项目多数迁移的 `downgrade` 只做 `drop`，无法还原已回填数据；
- 确需 downgrade 时，先确认目标版本：`cd backend && uv run alembic history --verbose`，再 `uv run alembic downgrade <rev>`；
- 回滚后必须 `uv run alembic upgrade head` 回到主线，并跑 `uv run alembic check` 确认无漂移（CI 同款卡点）。

### 5.4 索引与缓存回滚

- 向量索引：设置页「向量索引 / Embedding」→「回滚」调用 `POST /api/admin/models/index/rollback`（恢复上一次重建前的备份集合 `{collection}__backup_*`）；确认稳定后人工清理备份集合；
- 语义缓存：重建/回滚会自动失效 `rag:semcache:*`；手工恢复场景须按 §4.2 清理；
- JWT 吊销名单：`rag:jwt:revoked:*` 存于 Redis。**Redis 数据丢失意味着已登出 token 可再次使用**，恢复后如怀疑凭据风险，应轮换 `SECRET_KEY` 使全部 token 失效并强制重新登录；
- 限流计数（分钟/日配额）随 Redis 清空重置，属预期，不需要回滚。

## 6. 容量规划

### 6.1 容量模型

> 变量均取自 `.env.prod`：`CHUNK_SIZE=512`、`CHUNK_OVERLAP=64`、`EMBEDDING_DIMENSION=1024`、`MILVUS_HNSW_M=16`、`MILVUS_EF_CONSTRUCTION=128`。

1. **切片数**：`N_chunks ≈ 文本字符数 / (CHUNK_SIZE − CHUNK_OVERLAP) = 字符数 / 448`
2. **Milvus 单切片**（经验值，含 20% 增长余量）：
   - dense float32：`4 × dim = 4 KB`
   - HNSW 图：`≈ 8 × M ≈ 128 B`
   - sparse（BM25 开启时）：`100~300 B`
   - 标量（`content` 512 字符 UTF-8 ≈1.5 KB + id/source/heading ≈0.2 KB）
   - 合计 **≈ 6~7.5 KB / 切片**
3. **MinIO**：约等于原件体积（PDF/图片按实际字节，不压缩）
4. **PG**：元数据 ≈ Milvus 切片数的 0.3~0.6 KB/行（含索引），另加运行数据：
   - 每轮问答（messages + trace）≈ 2~10 KB；按 `TRACE_RETENTION_DAYS=30` 滚动淘汰；
   - 审计按 `AUDIT_RETENTION_DAYS=365` 保留；应用日志按 `LOG_RETENTION_DAYS=30` 滚动。
5. **备份盘**：`PG_gz × 30 + MinIO × 30 + Milvus 快照 × 8`，其中 `PG_gz ≈ 原库 10%~25%`。

### 6.2 规模参考（中文文本为主）

| 原始文本 | 切片数 | Milvus | MinIO | PG（含索引） | 备份盘（全量日备 30 + 周备 8） |
|---|---|---|---|---|---|
| 1 GB | ≈74 万 | ≈6 GB | 1 GB | 0.5~1 GB | ≈85 GB |
| 10 GB | ≈740 万 | ≈55 GB | 10 GB | 3~6 GB | ≈0.8 TB |
| 50 GB | ≈3700 万 | ≈280 GB | 50 GB | 15~30 GB | ≈3.9 TB |

> Milvus 经验倍率：**约为原始文本体积的 5 倍**。若关闭混合检索（`KB_ENABLE_HYBRID_SEARCH=false`），可省去 sparse 部分（约 5%）。
> 若切换更高维 Embedding（如 1536/3072 维），dense 部分按 `4×dim` 线性增长，需重新核算。
> **备份盘是全量拷贝口径**（备份盘容量常被低估，通常是线上数据盘的 8~10 倍）。改用对象存储版本化、硬链接快照或增量镜像后可降到 1/5~1/10，但须先验证可恢复性。

### 6.3 扩容触发阈值

| 指标 | 预警 | 必须处理 | 处理手段 |
|---|---|---|---|
| 宿主机磁盘使用率 | >70% | >85% | 迁移数据卷到大盘 / 清理备份与旧镜像 `docker system prune` |
| PG 库 + WAL 体积 | >50% 磁盘 | >70% 磁盘 | 调低 `TRACE_RETENTION_DAYS` / 归档审计 / 扩容盘 |
| Milvus P95 检索延迟 | >500ms | >2s（`MilvusHighQueryLatency`） | 加内存 / 提升 `MILVUS_EF` 前先评估 / 拆分集合 |
| 宿主机内存 | >75% | >90%（OOM 风险） | 提高内存或限制 Milvus（`deploy.resources.limits`，见 deployment.md §5.4） |
| Redis 内存 | >60% `maxmemory` | 触发淘汰 | 调 TTL / 缩 `SEMANTIC_CACHE_MAX_ENTRIES`（默认 200） |

> **监控缺口**：当前 Prometheus 未采集宿主机磁盘/内存（无 node-exporter），容量类指标需按 §6.4 手工巡检，或后续接入 node-exporter 补告警。

### 6.4 巡检清单

| 周期 | 检查项 | 命令 / 入口 |
|---|---|---|
| 每日 | 磁盘余量、备份是否成功、备份文件大小是否异常 | `df -h`、`ls -lh /backup`、cron 日志 |
| 每周 | 容器资源占用、PG 表膨胀、Milvus 延迟趋势 | `docker stats`、`monitoring.md` §3、Grafana「RAG Overview」 |
| 每月 | 副本可用性抽样、备份保留策略执行、日志/审计保留期生效 | 抽查 3 份备份可解压、`retention` 日志、`/api/audit` |
| 每季度 | 恢复演练 + 回滚演练（§7）、容量模型复核 | 见 §7 记录表 |

### 6.5 容量相关配置（调优前先评估影响）

| 配置 | 默认 | 影响 |
|---|---|---|
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 512 / 64 | 切片数（≈线性反比），直接影响 Milvus 体积与 LLM 上下文长度 |
| `MILVUS_HNSW_M` | 16 | 内存/构建时间↑、召回↑；>32 后收益递减 |
| `MILVUS_EF_CONSTRUCTION` | 128 | 仅影响建索引耗时与内存峰值 |
| `KB_HYBRID_SEARCH_TOP_K` / `KB_HYBRID_RERANK_TOP_K` | 20 / 5 | CPU（rerank 是主要开销）与延迟 |
| `CHAT_DAILY_MESSAGE_QUOTA` / `UPLOAD_DAILY_MB_PER_USER` | 200 / 500 | 用户级配额，防止单租户打满容量 |
| `MAX_UPLOAD_SIZE_MB` / `UPLOAD_MAX_BATCH_MB` | 100 / 200 | 单文件与单批上限，直接决定解析内存峰值 |
| `SEMANTIC_CACHE_TTL_HOURS` / `SEMANTIC_CACHE_MAX_ENTRIES` | 24 / 200 | Redis 内存占用 |

### 6.6 扩容操作

1. **磁盘扩容（推荐）**：停栈 → `docker volume inspect` 确认卷路径 → 迁到新盘（`docker run --rm -v <vol>:/from -v /mnt/new:/to alpine cp -av /from/. /to/`，或用 `local` 驱动的 bind 挂载）→ 校验文件数 → 起栈；
2. **Milvus 加内存**：先看 `docker stats` 是否接近上限制，再调整 compose `deploy.resources.limits`（deployment.md §5.4）；Milvus 的 `queryNode` 内存不足会直接 OOM 而非降级；
3. **PG 调优**：`shared_buffers` ≈ 25% 内存、`work_mem` 4~16MB（trace 聚合查询较多）、`maintenance_work_mem` 建索引时临时提高；
4. **后端并发**：`CHAT_MAX_CONCURRENT_STREAMS_PER_USER` 控制单用户流式并发；多 worker 时注意后台任务（学习/保留期清扫/用量 flush）会随 worker 复制，非必要不要盲目加 worker。

## 7. 演练（每季度）

### 7.1 恢复演练

1. 隔离环境起空 PG/MinIO/Milvus（不要指向生产卷）；
2. 取最近一次日备 + 周备，按 §4.1 全流程恢复；
3. 启动应用，`/health/ready` 200；抽查 3 个知识库问答 + 1 个权限隔离用例；
4. 记录实际 RTO、缺失数据范围、失败步骤；
5. 演练中发现的问题**必须回写本文档或对应 runbook**，否则视为演练未完成。

### 7.2 回滚演练

1. 在隔离环境部署「上一版本」镜像与当前版本数据（模拟升级后回退）；
2. 按 §5.2 执行应用回退，确认旧版本可启动、SSE 正常、登录/知识库问答可用；
3. 按 §5.3 验证「备份恢复优先于 downgrade」的路径可行；
4. 记录回退耗时与数据一致性检查结果。

### 7.3 演练记录模板

| 日期 | 演练类型 | 数据规模 | PG 恢复 | MinIO 恢复 | Milvus 恢复 | 总耗时（RTO） | 结论 | 改进项 |
|---|---|---|---|---|---|---|---|---|
| 2026-10-27 | 首次基线（文档化） | — | — | — | — | — | — | 待首次执行 |

## 8. 应急处置速查

| 场景 | 动作 |
|---|---|
| 磁盘满导致写入失败 | 先删旧备份/悬空镜像释放空间 → 停写 → 迁卷（§6.6），勿删数据卷 |
| PG 连接池耗尽 | 查 `pg_stat_activity` 长事务 → 重启 backend → 调小 `pool_size`/加超时 |
| Milvus 不可用 | 依赖 `MilvusDown` 告警；先确认 etcd + MinIO 健康；短期降级为纯 PG 元数据浏览 |
| 误删知识库/文档 | 从 MinIO 找回原件并重新入库（§4.3）；元数据从最近 PG 备份提取 |
| 密钥/Token 泄露 | 轮换 `SECRET_KEY`（使全部 token 失效）+ `API_KEY`/`ADMIN_KEY` → 重启 backend → 全员重新登录 |
| 备份脚本失败 | 看 cron 日志退出码；确认 `mc alias` 与 `pg_dump` 客户端版本；修复后**立即补一次全量备份** |

## 相关文档

- 部署与端口/资源：[deployment.md](deployment.md)（§1.3 资源要求、§5.4 内存限制）
- 监控指标与告警处置：[monitoring.md](monitoring.md)
- 模型注册表与索引重建/回滚：[model-provider-registry-2026-10.md](../design/model-provider-registry-2026-10.md) §12.7
- 安全与保留期策略：[security-multitenancy-hardening-2026-10.md](../design/security-multitenancy-hardening-2026-10.md)（SEC-13/14）
