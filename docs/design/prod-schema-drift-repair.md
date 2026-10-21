# 生产库 schema 漂移修复方案

> 状态：已实施（**代码侧 ①②③ + prod 窗口执行均已完成，2026-10-19**；`alembic check` 无漂移）
> 关联：[project-status.md](../project-status.md)（§二 SCHEMA 行）、[archive/project-readthrough-2026-09-30.md](../archive/project-readthrough-2026-09-30.md)（P0-1）、[guide/deployment.md](../guide/deployment.md)
> 日期：2026-10-18
> 说明：遵循「设计先行 → 确认后实施」。**本方案仅针对 prod**；dev 已于 2026-10-18 修复至 head。

## 1. 问题与证据

prod 库 `alembic_version = 820bc90565d8(=head)`，但 `alembic check` 与直接查 `information_schema` 显示**实际 schema 落后**：

| 缺失对象 | 说明 | 迁移来源 |
|---|---|---|
| 表 `audit_logs`（+4 索引） | 审计日志写入目标 | **无迁移**（模型 `audit_log.py` 存在但从未生成迁移） |
| 表 `user_api_keys`（+3 索引，含 `key_hash` 唯一） | E.6 用户级 API Key | **无迁移**（同上） |
| 列 `experiments.owner_id` | 实验对象级授权 | 迁移 `820bc90565d8`（未实际执行） |
| 列 `wiki_pages.links` | wiki 互链结构化存储 | 迁移 `b7d2c94a1f30`（未实际执行） |
| `experiments.variants/metrics`、`experiment_variants.config`、`experiment_results.analysis_data` 类型 `json`→`jsonb` | 与模型不一致 | 迁移链中的类型对齐（未执行） |

**生产实际影响**：审计写入无表、E.6 Key 表不存在、**实验创建因缺 `owner_id` 会 500**、wiki links 缺失。

**根因**：[scripts/start-prod.sh](../../scripts/start-prod.sh) L369-390 的 `run_database_migration`——`alembic upgrade head` 失败时**回退 `alembic stamp head`**（注释称"历史遗留库 create_all 建表撞 DuplicateTable，stamp 即可"）。该兜底把 prod 版本号强行标到 head，掩盖了实际未执行的 DDL；此后新增的表/列（audit_logs、user_api_keys 及后续迁移）从未物化。

## 2. 修复目标

- prod schema 与 `Base.metadata` 一致（`alembic check` 报「No new upgrade operations detected」）。
- 补齐 `audit_logs` / `user_api_keys` 的**正式迁移**（同时修复"任何纯迁移构建的库"缺这两表的隐患）。
- 堵住复发：去掉 start-prod 的盲目 `stamp` 兜底 + CI 增漂移卡点。
- 全程**可备份、可回滚**、在维护窗口内低风险执行。

## 3. 方案（三部分）

### 3.1 新增「对账」迁移（幂等）

新增 `backend/alembic/versions/<rev>_reconcile_missing_schema.py`，`down_revision = 820bc90565d8`（成为新 head），`upgrade()` 幂等补齐：

- `audit_logs`：`op.create_table`（含 `ix_audit_logs_user_id/action/resource_type/created_at`），**仅当表不存在**时创建。
- `user_api_keys`：`op.create_table`（`user_id` FK→`users.id`；`ix_user_api_keys_user_id` + `ix_user_api_keys_key_hash`(unique)），仅当表不存在。
- `experiments.owner_id`：`op.add_column(varchar, server_default='', nullable=False)` → `op.alter_column(drop server_default)`；**仅当列不存在**。
- `wiki_pages.links`：`op.add_column(jsonb, server_default='[]'::jsonb, nullable=False)` → drop server_default；**仅当列不存在**。
- 类型对齐：对 4 个 experiment 列，**仅当当前类型为 `json`** 时 `ALTER ... TYPE jsonb USING <col>::jsonb`。

幂等 helper（沿用 `8cb6e5b283ec` 的 `_constraint_exists` 风格）：`_table_exists` / `_column_exists` / `_column_is_json`（查 `information_schema`）。

> **幂等是硬要求**：dev 已在 head 且 schema 一致 → 该迁移在 dev 上必须 **no-op**；同时它也是"纯迁移构建的新库"补齐 `audit_logs`/`user_api_keys` 的正解。`downgrade()` 反向（drop 新增对象 + 类型转回 json），仅作应急，优先用备份恢复。

### 3.2 修 `start-prod.sh`（防复发）

`run_database_migration` 去掉盲目 `stamp head` 兜底：`upgrade head` 失败即 `log_error` + `exit 1`（给出排查指引），**不再自动 stamp**。若确需兼容"全空库"，改为**仅当库内零业务表**时才 stamp（显式判定），否则一律失败暴露问题。

### 3.3 CI 漂移卡点

在 `.github/workflows/ci.yml` 的 `backend-integration`（已有 postgres service）增一步：
`uv run alembic upgrade head` → `uv run alembic check`；`check` 报差异即非零退出。等价于"迁移链必须能重建出与模型一致的 schema"，从源头拦住"模型改了但没写迁移"（如本次 `audit_logs`/`user_api_keys`）。

## 3.4 prod 执行步骤（确认后按序）

1. **备份**：`docker compose -f docker-compose.yml --env-file .env.prod exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > backups/app_prod_2026xxxx.sql`（异地留存）。
2. **窗口**：选低峰；期间可短暂停 backend 写入。
3. **构建含新迁移的镜像**：prod backend 为**镜像构建**（无源码挂载）→ `docker compose -f docker-compose.yml --env-file .env.prod build backend`。
4. **应用迁移**：`docker compose ... exec -T backend alembic upgrade head`（或直接走修好的 `./scripts/start-prod.sh`）。
5. **验证**：
   - `alembic current` = 新 head；`alembic check` = No new upgrade operations detected；
   - 表/列存在性（`audit_logs`、`user_api_keys`、`experiments.owner_id`、`wiki_pages.links`）；
   - 冒烟：建实验（`POST /api/experiments/` 应 200）、审计写入、`/auth/api-keys`（若开启）。
6. **回滚**：优先**恢复步骤 1 备份**；次选 `alembic downgrade 820bc90565d8`（应急）。

## 4. 验证（实施前）

- dev 上跑一遍新迁移：预期 **no-op**（`alembic check` 仍无漂移）。
- 临时全新库（`ALEMBIC_DATABASE_URL` 指向空库）跑 `upgrade head` + `alembic check`：预期一致（证明迁移链自足，含新表）。

## 5. 风险与回退

| 风险 | 处置 |
|---|---|
| JSON→JSONB 转换短暂 `ACCESS EXCLUSIVE` 锁 | 维护窗口执行；experiment 表数据量小，耗时短 |
| 幂等 guard 写错导致误删/重复 | 先 dev + 空库验证；迁移只做"补齐/转换"，不做删除 |
| `downgrade` 生产不可靠 | 回滚以 pg_dump 恢复为主 |
| 去掉 stamp 后 start-prod 在异常库上直接失败 | 属预期：暴露问题优于掩盖；文档同步更新 |

## 6. 建议实施序

3.1（迁移）→ dev/空库验证 → 3.2（脚本）+ 3.3（CI）→ 提交 → **prod 窗口执行 3.4**。

---

## 7. 实施记录（2026-10-19，代码侧）

### 已落地
- **① 对账迁移**：[`d4e5f6a7b8c9_reconcile_missing_schema.py`](../../backend/alembic/versions/d4e5f6a7b8c9_reconcile_missing_schema.py)（`down_revision=820bc90565d8`，成为新 head）——幂等补齐 `audit_logs`/`user_api_keys` 两表、`experiments.owner_id`/`wiki_pages.links` 两列、4 个 experiment 列 `json→jsonb`（`USING <col>::jsonb`）。helper 用 `information_schema`（`data_type` 区分 json/jsonb）。
- **② 脚本去 stamp**：[start-prod.sh](../../scripts/start-prod.sh) + [start-prod.ps1](../../scripts/start-prod.ps1) 删除「upgrade 失败即 stamp head」兜底，失败一律报错退出（复发根因堵死）。
- **③ CI 卡点**：[.github/workflows/ci.yml](../../.github/workflows/ci.yml) `backend-integration` 在跑测试前增 `alembic upgrade head` + `alembic check`（全新库 → 迁移链必须重建出与模型一致的 schema）。

### 附带发现并修复（新缺陷）
- 迁移 [`8cb6e5b283ec`](../../backend/alembic/versions/8cb6e5b283ec_align_schema_with_models_owner_id_not_.py) 的 `_constraint_exists` 用 `conrelid = :table::regclass`，在 **asyncpg** 下 SQLAlchemy 无法解析 `:table` 后的 `::` → `PostgresSyntaxError`（该迁移此前从未在空库上真正执行过；dev/prod 均靠 create_all + stamp 绕过）→ `alembic upgrade head` 在空库必失败。**已改为 pg_class 关联查询**，空库整链跑通。

### 验证
- **dev**（已在 head 且一致）：`upgrade head`（应用 d4e5…，体 no-op）→ `current=head`、**`alembic check` 无漂移**。
- **全新库**（临时 `app_fresh`）：`upgrade head` 整链（baseline→…→d4e5…）跑通 → `check` 无漂移；`audit_logs`/`user_api_keys`/`experiments.owner_id`/`variants=jsonb` 均就位 → 证明迁移链自足。

### prod 执行（2026-10-19 完成）
- **备份**：`docker exec postgres-prod pg_dump …` → `C:\Users\zhuzhu\.cache\rag_backups\app_prod_20261019.sql`（522 KB，exit 0）。
- **走最小面**（改用 `docker cp` 投放迁移，**未重建镜像**）：避免把本次未提交的 LEARN 代码带上 prod。
- **应用**：容器内 `alembic upgrade head`（`820bc90565d8 → d4e5f6a7b8c9`）→ `current=head`、**`alembic check` = No new upgrade operations detected**；`audit_logs`/`user_api_keys`/`experiments.owner_id`/`wiki_pages.links`/`variants=jsonb` 均就位。
- **待补**：功能冒烟（建实验 API）需 prod 凭据（生产强制鉴权、无全局 Key/匿名），暂留；DDL 为增量，运行中的 backend 无需重启。

### 待办
- 提交（本会话改动未提交）。
