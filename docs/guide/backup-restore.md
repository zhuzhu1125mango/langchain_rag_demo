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

**两种执行模式，脚本自动选择**：

| 你的宿主机 | 模式 | 条件 |
|---|---|---|
| 装了 `pg_dump` / `mc`（Linux 生产机） | host | 直连数据库与对象存储 |
| **没装**（Windows Git Bash 默认如此） | docker | 借用 `postgres-prod` / `minio-prod` 容器里的客户端 |

> **2026-11-04 发现**：本机 `pg_dump` 与 `mc` **都不存在**，`backup.sh` 按老版本根本跑不起来——
> 这就是 `backups/` 里连一份 `.sql.gz` 都没有的真正原因（历史备份全是部署时手工 `pg_dump -Fc`）。
> 现在没客户端会走容器模式，凭据从容器的 `/run/secrets/MINIO_ROOT_*` 读（生产是 `*_FILE` 注入，
> 环境变量里并没有 `MINIO_ROOT_*`）。

```bash
# 最简：容器模式，默认 postgres-prod / minio-prod / bucket=documents
./scripts/backup.sh

# 显式指定（Linux 生产机，host 模式）
export POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5434 POSTGRES_DB=app_prod POSTGRES_USER=prod_user
export POSTGRES_PASSWORD="$(cat secrets/postgres_password)"   # 由 Docker Secrets 提供，勿写入 shell 历史
export MINIO_ALIAS=local MINIO_BUCKET=documents BACKUP_DIR=/backup
mc alias set local http://127.0.0.1:9002 \
  "$(cat secrets/minio_root_user)" "$(cat secrets/minio_root_password)"
./scripts/backup.sh
```

可覆盖项：`PG_CONTAINER`、`MINIO_CONTAINER`、`MINIO_INTERNAL_URL`（容器内是 `127.0.0.1:9000`，
不是宿主机映射的 9002）、`BACKUP_DIR`、`MINIO_BUCKET`。

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

# PG_FILE 两种格式都行，脚本按文件头自动识别（不需要你区分）：
#   .sql.gz  = backup.sh 产出（纯文本 + gzip）
#   .dump    = pg_dump -Fc 产出（custom 格式）
export PG_FILE=/backup/postgres_<stamp>.sql.gz
export MINIO_DIR=/backup/minio_<stamp>     # 只需恢复 PG 时可不设
./scripts/restore.sh

$COMPOSE stop backend && $COMPOSE up -d backend
curl -fsS http://127.0.0.1:8001/health/ready
```

> **2026-11-04 演练暴露的坑（脚本已修）**：仓库 `backups/` 里的 4 份备份全部是部署时
> 手工 `pg_dump -Fc` 的 `.dump`，而 `restore.sh` 当时只会 `gunzip | psql`——按本文档恢复
> 会立刻报 `gzip: not in gzip format`，极易被误判成「备份损坏」。
> 现在脚本按**文件头魔数**（`PGDMP`）选择 `pg_restore` / `psql`，并默认拒绝往非空库导入
> （非空库导入会中途失败，留下「一半有数据」的状态，比不恢复更难排查）。
> 另外：`backup.sh` 的 `.sql.gz` 产物在本机**一份都没有**，说明这条脚本化备份链路
> 从未真正跑过——见 §7.3 演练记录。

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
- **实测（2026-11-04 回滚演练，§7.3）**：上一版镜像（`rag-backend:rollback-20261101b`）装在
  已迁到 `d5e6f7a8b9c0` 的库上，`/health/ready` 四项 ok（冷启动 27~48s）、登录与知识库问答均正常
  ——**加列式迁移的向后兼容成立**，回滚不需要先 `downgrade`，§5.3 的「恢复备份优先」也顺带被验证。
- **但旧镜像里没有新迁移文件**：任何 `alembic` 命令都会直接报
  `Can't locate revision identified by 'd5e6f7a8b9c0'`（连 `alembic current` 都不行）。
  也就是说**回滚后不能靠旧镜像做任何迁移操作**，要迁移只能用新镜像——这条以前没写进文档。
- **回滚会丢掉 P0-4 的优雅停机**：旧镜像没有 `--timeout-graceful-shutdown`，`docker stop`
  时正在生成的 SSE 会被硬杀（ExitCode 137、客户端收不到 `end` 帧、答案截断）。回滚窗口内
  请选低峰，并接受「重启会打断在途回答」。

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

> 已自动化，四条命令各管一段：
>
> - `./scripts/restore_drill.sh [备份文件]` —— **PostgreSQL**：起隔离 PG（端口 5547）、调用真实
>   `restore.sh`、比对表数/行数与生产、输出 RTO，结束自动销毁 `drill-*` 容器与卷。
>   加 `HOST_RESTORE=1` 则在**宿主机**执行 `restore.sh`（本机无 `psql`/`pg_restore` →
>   走容器降级分支，也就是 runbook 让运维实际走的那条路）。
> - `./scripts/restore_drill_minio.sh` —— **MinIO 整实例**：在源实例造种子对象 → `backup.sh`
>   产出镜像 → 起一台**全新** MinIO（独立数据卷、独立凭据）→ `restore.sh MINIO_ONLY=1` 回灌 →
>   校验文件数/文件名集合/内容哈希集合 → 用业务 SDK 复读 → 自动清理。
> - `./scripts/restore_drill_milvus.sh` —— **Milvus 整实例**：源实例造 1000 条演练集合 →
>   官方 `milvus-backup` 备份 → 起全新一套 etcd + MinIO + Milvus → `restore --rebuild_index` →
>   校验条数/索引/抽样哈希/ANN 结果 → 自动清理。
> - `./scripts/rollback_drill.sh` —— **回滚（§7.2）**：现打一份备份 → 隔离 PG 恢复 →
>   把**上一版镜像**装上去（连 drill 的 PG/Redis、只读生产 Milvus/MinIO）→ 验 ready/登录/问答 →
>   验「生成中被停」的行为 → 自动清理。`ROLLBACK_IMAGE=` 指定要回滚到的镜像。
>
> 上述三条验证的是「**数据能恢复**」。还有一件它们都不覆盖：**恢复完整套服务能不能起来、
> 能不能答对问题**——需要六个容器（PG/Redis/MinIO/etcd/Milvus/backend）一起才能验。
> 这条为手工编排，步骤见 §7.4，2026-11-04 实测通过（未脚本化）。

### 7.1 恢复演练

1. 隔离环境起空 PG/MinIO/Milvus（不要指向生产卷）；
2. 取最近一次日备 + 周备，按 §4.1 全流程恢复；
3. 启动应用，`/health/ready` 200；抽查 3 个知识库问答 + 1 个权限隔离用例；
4. 记录实际 RTO、缺失数据范围、失败步骤；
5. 演练中发现的问题**必须回写本文档或对应 runbook**，否则视为演练未完成。

### 7.2 回滚演练

```bash
./scripts/rollback_drill.sh                    # 现打备份 → 隔离恢复 → 装上一版镜像 → 验功能与停机
ROLLBACK_IMAGE=rag-backend:rollback-20261101 ./scripts/rollback_drill.sh
PG_FILE=backups/xxx.sql.gz ./scripts/rollback_drill.sh   # 省一次 dump
KEEP=1 ./scripts/rollback_drill.sh             # 保留现场排查
```

脚本做四件事，缺一件这次演练就不算数：

1. **现打一份备份**并恢复进**隔离** PG（不是复制数据目录），顺带再验一次备份链路；
2. 把**上一版镜像**装到这份**当前 schema** 的数据上（模拟「升级完发现不对」），
   连 drill 的 PG/Redis，Milvus 与 MinIO 只读生产；
3. 验 `health/ready`、登录、一次知识库问答——**能启动 ≠ 能用**，旧代码要能读新表；
4. 验「生成中被停」：用 curl 拿到首帧后立刻 `docker stop`，看进程退出码与客户端有没有 `end` 帧。

判定口径（2026-11-04 实测值见 §7.3）：第 1~3 条必须全绿；第 4 条只记录行为差异，
因为「回滚会丢掉优雅停机」是**能力缺失**，不是每次都能撞上生成中的流。

### 7.3 演练记录| 日期 | 演练类型 | 数据规模 | PG 恢复 | MinIO 恢复 | Milvus 恢复 | 总耗时（RTO） | 结论 | 改进项 |
|---|---|---|---|---|---|---|---|---|
| 2026-10-27 | 首次基线（文档化） | — | — | — | — | — | — | 待首次执行 |
| **2026-11-04** | **PG 恢复（隔离库 `drill-pg`，端口 5547）** | **29 表 / 136KB / 0 文档 / 3 用户 / 82 trace** | **1s** | 未验（无镜像产物） | 未验（0 向量，可重建） | **6s**（含容器启动 4s） | **通过，但需补一步** | 见下 |
| **2026-11-04** | **MinIO 整实例恢复（全新 `drill-minio` + 新卷 + 新凭据）** | **3 对象 / 1MB（含中文名与嵌套路径）** | 不适用 | **3/3 对象，哈希集合一致** | 未验（0 向量，可重建） | **4s**（含实例启动 2s） | **通过** | 见下 |
| **2026-11-04** | **Milvus 整实例恢复（全新 etcd+MinIO+Milvus）** | **1000 条 128 维 + HNSW（备份 477KiB）** | 不适用 | 不适用 | **1000/1000，索引与 ANN 一致** | **29s**（备份 2s + 恢复 17s） | **通过，但默认参数会漏索引** | 见下 |
| **2026-11-04** | **端到端：三套全恢复 + 起服务 + 问答** | **4 文档 / 532 chunk / 4 用户 / 111 trace / 4 对象** | **4 文档 532 chunk** | **4 对象，md5 与原文件一致** | **532 实体，检索命中** | **手工编排，未计时**（Milvus 恢复 15.7s、backend 到 ready 16s） | **通过** | 见下 |
| **2026-11-04** | **回滚：上一版镜像（11-01）+ 当前 schema 数据** | **4 用户 / 2 KB / 4 文档 / 532 chunk** | **2s 恢复，库 schema=`d5e6f7a8b9c0`** | 只读（未写入） | 只读（532 实体，问答命中） | **ready 27~48s**（冷启动加载模型），全流程 **77~101s**（含现打备份时约 101s） | **通过（回滚可行，但有两条代价）** | 见下 |

**2026-11-04 首次演练详情**（脚本：`scripts/restore_drill.sh`，可重复执行）：

用了最新的 `backups/app_prod_pre_p0deploy_20261102_1058.dump`，在隔离 PG 上调用**真实的
`scripts/restore.sh`** 恢复（不是演练脚本里另写一套，否则验证的是演练脚本）。

结果：

- **PG 恢复 1s、29 张表全部导入**；总 RTO **6s**（含临时容器启动 4s）。数据量小，
  这个数字**不能外推到真实规模**——它证明的是「链路通、dump 可恢复」，不是「1 万文档也 6 秒」。
- **行数对得上，差额可解释**：还原 `users=3 / kb=1 / docs=0 / traces=82`，
  生产 `4 / 1 / 0 / 107`。差额来自备份之后的新增（`ops-admin` 与 25 条 trace），属预期。
- **schema 落后一个迁移，必须补 `alembic upgrade head`**：还原库 `alembic_version=c4d5e6f7a8b9`，
  生产是 `d5e6f7a8b9c0`；逐列比对后差异**只有 `users.is_admin` 一列**。
  也就是说：**恢复完直接起应用会在管理接口上报错**，§4.1 第 3 步不是可选项。
- **发现并修掉 3 个真问题**：
  1. `restore.sh` 只会 `gunzip | psql`，而 `backups/` 里 4 份备份全是 `pg_dump -Fc` 的 `.dump`
     → 按 runbook 恢复会报 `gzip: not in gzip format`，极易误判成备份损坏。已改为按魔数
     （`PGDMP`）自动选 `pg_restore`，并默认拒绝往非空库导入。
  2. `backup.sh` 的 `.sql.gz` 产物在本机**一份都没有**（`ls backups/*.sql.gz | wc -l` = 0），
     说明脚本化备份链路从未真正跑过，历史备份都是部署时手工打的。
  3. 演练脚本在 Git Bash 下把容器内 `/tmp/restore.sh` 改写成 Windows 路径而失败
     （runbook 指定的正是 Git Bash）→ 已加 `MSYS_NO_PATHCONV=1`。

**2026-11-04 第二批：备份链路与 MinIO 往返也跑通了**

- **`backup.sh` 首次真正执行**（此前 `ls backups/*.sql.gz` 为 0，脚本化备份从未跑过）。
  第一次就撞上「宿主机没有 `pg_dump`/`mc`」——runbook 指定的 Git Bash 环境两项都不具备。
  已给 `backup.sh` / `restore.sh` 加**容器模式降级**：没客户端就借用 `postgres-prod` / `minio-prod`
  里的二进制，凭据从 `/run/secrets/MINIO_ROOT_*` 读。
- **MinIO 往返实测**（专用 bucket，不用 `documents`，避免碰生产数据）：
  放 2 个对象（1MB 随机二进制 + 中文名文本）→ `backup.sh` 镜像 → `restore.sh MINIO_ONLY=1`
  回灌到另一个 bucket → **逐对象 sha256 完全一致**（含中文文件名）。
- **PG 的 `.sql.gz` 分支也验了**：用刚生成的 dump 再走一次隔离恢复，
  **29 表 / 1s / 行数 4|1|0|107 与生产完全一致**（这次备份是几分钟前的，所以没有迁移差）。
- 顺带暴露的两个脚本缺陷：`restore.sh` 强制要求 `MINIO_DIR`（只想恢复 PG 时没法用）→ 改为可选
  并新增 `MINIO_ONLY=1`；MinIO 镜像里没有 `grep`/`sed`，脚本里不能依赖这些工具。

**2026-11-04 第三批：MinIO 整实例恢复通过**（脚本：`scripts/restore_drill_minio.sh`）

第二批只是在**同一个 MinIO 实例内换了 bucket**——凭据、版本、配置都还在，最坏的情况
（实例连数据卷一起丢）没被验证过。这一批补上：

- 源实例造 3 个对象（**中文名** `报告 2026.txt`、1MB 随机二进制、**嵌套路径** `nested/子目录/深一层.txt`）
  → `backup.sh` 产出镜像 → 起**全新实例**（独立数据卷 `drill_minio_data`、独立凭据 `drill_minio`，
  与生产不同）→ 确认恢复前 bucket 数为 0 → `restore.sh MINIO_ONLY=1` 回灌到同名 `documents`。
- **校验方式不只看文件数**：把两边各自 `mc mirror` 到宿主机，比「文件数 / 文件名集合哈希 /
  内容哈希集合」三项，全部一致；再用 **业务侧 minio SDK**（不是 `mc`）列出并读取对象，
  确认应用层能拿到内容——`mc` 能看不等于应用能读。
- **RTO 4s**（含实例启动 2s，3 个对象 / 1MB）。同样**不能外推**。

**本批暴露的问题（都已修）**：

1. **MinIO 镜像的工具集极小**：连 `sed`/`wc`/`sha256sum` 都没有，不只是没有 `grep`。
   规则改成「容器内只跑 `mc`」，文本处理与哈希一律回宿主机做。
2. **`xargs -0 sha256sum` 在 MSYS 上崩溃**（`assertion "bc_ctl.arg_max >= LINE_MAX" failed`），
   管道输出为空，于是两侧的 `CONTENT_SHA` 都退化成**空串的 sha256**——**看起来一致，其实什么都没比**。
   这是这类比对的通病：校验值会退化成常量。已换成 `read -d ''` 循环，并加一道断言
   「文件数为 0 或哈希等于空串哈希就判校验无效」。
3. **`MSYS_NO_PATHCONV=1` 会被子脚本继承**：演练脚本为保住容器内 `/tmp` 路径设了它，
   结果 `backup.sh` 里 `docker cp ... /c/MyCode/...` 被当成容器路径，报
   `directory "C:\c\MyCode\..." does not exist`。已让 `backup.sh` / `restore.sh` 自己用
   `cygpath -w` 转宿主机路径，不再依赖调用方的环境变量。
4. 顺带两个小坑：S3 bucket 名不允许下划线（`drill-src-20261104_152853` 被拒）；
   `pipefail` 下 `tr ... | head -c 24` 会让赋值返回 141，`set -e` 在打出第一行之前就终止脚本
   （表现为「exit 0、输出全空」）。

**2026-11-04 第四批：Milvus 整实例恢复通过**（脚本：`scripts/restore_drill_milvus.sh`）

一直写着「Milvus 未验，0 向量可重建」。但「可重建」意味着重新灌库 + 重新算 embedding，
成本远高于 PG/MinIO，不能靠这句话糊过去。这批用**官方 `milvus-backup`**（不是自研导出，
自研只能导「我想得到的字段」）跑通整条链：

- 源实例（就是生产 `milvus-standalone-prod`，真实事故也是它）建临时集合 `drill_docs`：
  1000 条 128 维随机向量 + HNSW/COSINE 索引；
- `create` 备份 → 477KiB / 8 对象，**2s**；
- 起**全新一套**：新 etcd（独立卷）+ 新 MinIO（独立卷、独立凭据）+ 新 Milvus（同镜像
  `v2.6.17`），确认恢复前集合数为 0；
- `restore --rebuild_index` → **17s**；RTO（起实例→恢复完成）**29s**；
- 五项校验全过：条数 1000/1000、索引 `vec:HNSW/COSINE` 一致、抽样 100 条的
  标量+向量哈希一致（`5c802f2f871cd96c`）、ANN top-5 id 与分数**逐位一致**。

**这批最有价值的发现：默认参数恢复会丢索引，而日志说「成功」。**

```
restore backup success        # 日志如此
indexes: []                   # 实际
collection.load() -> MilvusException(code=700, message=index not found[collection=drill_docs])
```

也就是说：恢复完集合在、数据在、`num_entities` 也正确，但**应用连不上**——`load()` 直接失败。
必须带 `--rebuild_index`（脚本里已经固定）。这条以前没有任何文档提过。

其余踩到的坑（都写进脚本注释）：

1. **不要给新 Milvus 设 `COMMON_STORAGETYPE=minio`**：2.6.17 会 panic，
   `Failed to init arrow filesystem: Invalid: Unsupported storage type: minio`（Exit 134）。
   默认本来就是 minio，设了反而错。
2. **`enable_dynamic_field` 的集合需要 etcd 段才会被完整备份**：create 时若 config 没配
   `milvus.etcd`，日志会打 `skip backup collection dynamic field, etcd client is not initialized`。
   本项目 `documents` 与 `drill_docs` 的 `enable_dynamic_field` 都是 `false`，所以无影响；
   以后若启用 dynamic field，必须补 etcd 段。
3. 备份名与集合名同规则（字母数字下划线），`drill-20261104-155448` 会被拒
   （`Error: invalid backup name`）。
4. 官方文档给的镜像是 `ghcr.io/zilliz/milvus-backup`，**本机 ghcr.io 返回 denied**；
   `docker.io/milvusdb/milvus-backup` 可用（本次用的 v0.6.1）。

**本次仍没验到的**：应用启动 + `/health/ready` + 问答抽查（需完整依赖栈，未在隔离环境起后端）；
生产 `documents` 里的真实对象（当前 0 文档）。Milvus 只验了 dense 索引，
**`documents` 还有 `sparse_embedding` 字段，稀疏索引未验**——灌入真实文档后应重跑一次本脚本
（脚本默认只跑 dense 的 drill 集合，若加 sparse 字段需同步扩展）。

**2026-11-04 第五批：端到端（三套恢复 + 起服务 + 问答）通过**

前三批各自证明「数据能恢复」，但都没回答一个问题：**恢复完之后，服务能不能起来、能不能答对？**
这批把六个容器一起拉起来验证（步骤见 §7.4）。

准备：在生产灌 4 份真实文档（`architecture.md` / `deployment.md` / `monitoring.md` /
`backup-restore.md`）→ 4 文档 / **532 chunk** / Milvus 532 实体（含 `HNSW` 与
`SPARSE_INVERTED_INDEX`）/ MinIO 4 对象。备份 PG 124KB + MinIO 4 对象 + Milvus 备份。

恢复与结果（全部指向全新容器与卷，凭据从同一份 secrets 挂载）：

| 项 | 结果 |
|---|---|
| PostgreSQL | 4 文档 / 532 chunk / 4 用户 / 111 trace，与源一致 |
| MinIO | 4 对象；`GET /api/documents/{id}/file` 返回 **200 / 19903 B / md5 `f44065a8…`**，与本地原文件**完全一致** |
| Milvus | 532 实体；`restore --rebuild_index` **15.7s** |
| backend | 起容器到 `/health/ready` **16s**，四项 `database/redis/milvus/minio` 全 `ok` |
| 问答（同一个问题） | 源 `knowledge_base` / 3 引用 / 293 字；恢复后 `knowledge_base` / 3 引用 / 367 字，内容一致（措辞不同属 LLM 非确定性） |

**这批抓到的两个真问题：**

1. **`restore.sh` 缺 psql 的容器降级。** `backup.sh` 早就支持「宿主机没 pg_dump 就借容器」，
   但 `restore.sh` 一直直接用宿主机 `psql`/`pg_restore`——而 runbook 指定的 Git Bash 环境
   两者都没有。也就是说：**按文档恢复，在这台机器上根本跑不起来**（和当初 `backup.sh`
   一模一样的坑，只是当时只修了一半）。已补容器分支（`PG_CONTAINER`，默认 `postgres-prod`）。
   ✅ **已在第六批补齐实测**（此前只做了语法校验）：`HOST_RESTORE=1 ./scripts/restore_drill.sh`
   在宿主机（无 `psql`/`pg_restore`/`mc`）上分别跑 `.dump` 与 `.sql.gz` 两种产物，
   两条容器分支都命中并通过（29 表 / 2s / 行数与生产一致）；回滚演练也走了同一条分支。
   顺带修掉两个只在真跑时才暴露的缺陷（见第六批）。

2. **Windows 挂载 secrets 的畸形路径（极隐蔽）。** 在 Git Bash 里未设 `MSYS_NO_PATHCONV=1` 时，
   `-v "$PWD/secrets/redis_password:/run/secrets/REDIS_PASSWORD"` 会被改写成
   `C:\...\redis_password;C -> \Users\...\run\secrets\REDIS_PASSWORD`。**容器照常启动**，
   但 `/run/secrets/REDIS_PASSWORD` 不存在 → `redis-server --requirepass "$(cat …)"` 拿到空串
   → **Redis 以无密码启动**，而 backend 带密码连 → 报的是
   `Redis 连接超时（3.0s），请检查 REDIS_HOST=… 和端口 6379 是否可达`。
   这条报错会把排查引向网络，而真因是凭据文件根本没挂上。**挂载后用 `docker inspect` 核对
   `Source` 是硬要求**，或用 `cygpath -w` 显式转路径。

另附一条观察（与灾备无关但同批发现）：问「ScrapeTargetDown 是什么意思」时，检索**召回了**
正确章节（`monitoring.md > 4. 告警规则`），但 rerank 分只有 **0.066**（另两块 0.0004 / 0.00004），
低于 `KB_RELEVANCE_SCORE_THRESHOLD=0.3` → 判为 `kb_miss`。这与
[stage-probe 报告](../benchmarks/stage-probe-2026-11-03.md) 的结论一致：
**重排分不可跨题比较，阈值判据本身有问题**。不影响本次灾备结论。

**2026-11-04 第六批：回滚演练通过（脚本 `scripts/rollback_drill.sh`，可重复执行）**

备份恢复都验过，但**回滚**没验过——而事故里更常见的是「升级完发现不对，想退回去」。
这批把「上一版镜像装在当前 schema 的数据上」跑了一遍。演练编排：现打一份生产备份 →
隔离 PG 恢复 → 起 drill Redis → 装 `rag-backend:rollback-20261101b`（11-01 构建）→
Milvus/MinIO 只读生产 → 验 ready / 登录 / 问答 / 停机。写操作只落在 `drill-rb-*` 的 PG 与 Redis。

| 项 | 实测 |
|---|---|
| schema | 还原库 `d5e6f7a8b9c0`；回滚镜像内置 head 只到 `c4d5e6f7a8b9` → **库比镜像新**（回滚的常态） |
| 启动 | `/health/ready` 200，**27s（热）/ 48s（冷）**，四项 `database/redis/milvus/minio` 全 ok |
| 登录 | 200，token 272 字符（旧代码能读带 `is_admin` 列的新用户表） |
| 问答 | `knowledge_base` / 3 引用 / 341~1159 字；多次跑答案长度波动属模型非确定性，与回滚无关（问题固定为「备份怎么恢复？恢复后要确认什么？」） |
| 停机（生成中） | `docker stop` **3~4s**、**ExitCode 137**、客户端收到 **11~15KB 无 `end` 帧**、日志停在 `Waiting for connections to close` |

**两条代价（已回写 §5.2）**：

1. **旧镜像跑不了 alembic**：`Can't locate revision identified by 'd5e6f7a8b9c0'`，连
   `alembic current` 都失败。回滚后要动迁移只能用新镜像，不能按习惯在旧镜像里 `upgrade head`。
2. **丢掉 P0-4 的优雅停机**：旧镜像启动命令是 `uvicorn src.main:app --host 0.0.0.0 --port 8000`，
   没有 `--timeout-graceful-shutdown`；生成中被停就是硬杀 + 残答。

**这批抓到的三个缺陷（都已修）**：

1. **`restore.sh` 的 `.sql.gz` 容器分支会导错库**（最危险的一条）：脚本把
   `psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"` 写在**单引号**里，这句实际由**容器的** `sh`
   去展开，取到的是容器自己的 `POSTGRES_DB`。调用方指定另一个库名时，数据被静默导进容器默认库，
   `psql` 照样 exit 0 —— 「恢复成功、目标库却是空的」。已改为 `docker exec -e PGU=… -e PGD=…`
   显式传入。（`.dump` 分支一直是对的，所以只有纯文本产物会中招。）
2. **读魔数会在恢复现场打一行吓人的警告**：`head -c 5` 读 gzip 时第 4 字节是 `0x00`，
   bash 报 `ignored null byte in input`。改成走管道 + `tr -d '\000'`。
3. **`pg_dump` 里的 `SELECT set_config(...)` 会把结果表打到屏幕**，恢复现场看到一张空表容易
   误判成报错 → 导入时加 `-q -o /dev/null`。

**一条方法学结论**：停机测试不能 `sleep 固定秒数` 再停。这道题 2~4 秒就答完，早先版本
`sleep 3/4` 秒再 `docker stop`，测到的是「没有活跃连接」，结论在 `ExitCode 0` 与 `137` 之间
摇摆（第一版就得到过 2s/0 的假结论）。改成 **curl 轮询到首帧后立刻 stop** 才稳定复现。

**本次仍没验到的**：Milvus 的 sparse 索引单独验证（问答走 dense+sparse 混合，间接覆盖）；
真实规模下的 RTO（532 chunk 与 1 万文档不是一回事）；**前端**镜像的回滚（本次只验后端，
§5.2 要求前后端同批回退，前端回滚目前只有构建与 `nginx.conf` 对比，没有端到端演练）。

### 7.4 端到端演练步骤（手工编排，约 20 分钟）

前提：生产已有数据；`docker` 可用；本机 Ollama 在跑（backend 要调 embedding 与 LLM）。

```bash
export MSYS_NO_PATHCONV=1                 # 必须：否则 -e /run/secrets/... 会被改写成 Windows 路径
SECW=$(cygpath -w "$PWD/secrets")         # -v 的宿主路径用 winpath，避免 C:\...;C 畸形挂载

# 1. 备份三套
BACKUP_DIR=backups/drill-e2e MINIO_BUCKET=documents ./scripts/backup.sh
#    Milvus 用 milvus-backup create（见 §7.3 第四批的 backup.yaml 写法，指向生产 Milvus + 生产 MinIO）

# 2. 起新栈（全部独立卷、独立容器名，网络复用 rag-prod_infra-net）
#    postgres:16.14 / redis:7.2-alpine / pgsty/minio / etcd v3.5.30 / milvus v2.6.17
#    密码统一从 secrets 挂到 /run/secrets/，minio 用 *_FILE 且 **必须置空 MINIO_ACCESS_KEY_FILE 等**

# 3. 恢复
#    PG    : docker cp dump -> drill-pg，容器内 gunzip | psql（宿主机无 psql）
#    MinIO : MINIO_ONLY=1 MINIO_CONTAINER=drill-minio MINIO_DIR=<镜像目录> ./scripts/restore.sh
#    Milvus: milvus-backup restore -n <备份名> --rebuild_index（**不能省 --rebuild_index**）

# 4. 起 backend：环境变量从生产导出后替换四个 HOST
#    docker inspect backend-prod --format '{{range .Config.Env}}{{println .}}{{end}}' > backend.env
#    POSTGRES_HOST=drill-pg / REDIS_HOST=drill-redis / MILVUS_HOST=drill-milvus
#    MINIO_ENDPOINT=drill-minio:9000；/app/data 挂到独立目录；端口 8002

# 5. 验收
curl -s localhost:8002/health/ready          # 四项 ok
# 同一个问题分别问 8001/8002，比较 answer_type / sources / 关键结论
# 下载原件比对 md5：GET /api/documents/{id}/file
```

**验收判据**（不要只看 `restore success`）：`/health/ready` 四项 ok、同一问题的
`answer_type` 与引用数与源一致、原件下载 md5 与本地文件一致。

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
