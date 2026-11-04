# 项目自检报告（2026-11-04）

> 目的：在「参考实现收尾」交付之后做一次**对抗性自检**——不读文档下结论，而是逐项验证
> 「文档声称的状态」与「现场实际状态」是否一致。只读为主，发现的问题当场修；需要 owner
> 动作的单列。
>
> 结论先行：**功能面是干净的，流程面有洞**。最严重的一条是 CI 在最新提交上已经红了两小时
> 而没人发现（见 A1）——这恰好是「参考实现」最不该出的问题：所有数字都可复现，但复现它的
> 流水线本身断了。

---

## 1. 怎么查的

| 维度 | 方法 | 不信任什么 |
|---|---|---|
| 版本与同步 | `git log` / `ls-remote` 对比 HEAD | 「已推送」的口头结论 |
| 测试 | 全量 pytest + 前端 vitest + eslint + typecheck | 文档里的 passed 数字 |
| 静态检查 | 用**与 CI 完全相同的命令**跑 ruff | 「本地跑过了」 |
| 运行态 | 容器状态、`/health/ready`、Prometheus targets | README 里的拓扑描述 |
| 数据 | 生产库四张表计数、管理员标志、巡检产物时间 | 台账里的完成标记 |
| 文档 | 内部链接、被引用脚本是否存在、索引覆盖 | 文档自身的完整性 |

---

## 2. 发现（按后果排序）

### A1 🔴 CI 在最新提交上失败，且与「收尾完成」的结论冲突

`gh run list` 显示最近一次 `main` 的 CI（run 41762555753）**failure**，
失败作业是 `Lint & Typecheck` → `Backend ruff`：

```
backend/scripts/probe_retrieval_stages.py:291:36: F821 Undefined name `Optional`
```

- **为什么本地没发现**：本机 venv 里没有 ruff（项目用 `uv` 管理，未把 ruff 装进 venv），
  而 CI 用的是 `uvx ruff@0.9.3 check --select E9,F63,F7,F82 backend`。这条命令我从未在本地跑过。
- **为什么不影响运行**：文件有 `from __future__ import annotations`，注解不求值，
  所以脚本能正常执行——这是一个**只有静态检查能抓到的**错误。
- **已修**：改为 `str | None`（与文件内其它注解风格一致），并用 CI 同款命令复验
  `All checks passed!`。提交 `99fd498` 推送后 **CI 7/7 全绿**（run 41767968709，
  Lint & Typecheck 28s）。
- **流程教训**：自查清单里必须包含「跑一遍 CI 的 lint 命令」，否则本地绿灯没有意义。
  已把 `uvx ruff@0.9.3 check --select E9,F63,F7,F82 backend` 写进本文第 5 节。

### A2 🟡 8 例测试未隔离宿主环境（`IN_DOCKER=1`）

现象：全量跑出 `8 failed, 1614 passed`。换 `env -u APP_ENV` 仍失败，换 `env -u IN_DOCKER`
后 **5/5 通过**。

根因：`Settings.IS_PRODUCTION` 在未设 `APP_ENV` 时**按 `IN_DOCKER` 推导**；宿主只要导出
`IN_DOCKER=1`（在容器里跑测试、或 shell 残留），配置暴露、`/metrics` 重置、JWT 开发态兜底、
配额等用例就会走到生产分支。CI 不设该变量，所以这条只在开发者机器上出现——**看着像功能回归，
实际是环境差异**。此前两次我都按「既有失败、与改动无关」记过账，等于放弃定位。

已修：`tests/conftest.py` 显式 `os.environ["IN_DOCKER"] = "false"`
（**不能用 `setdefault`**，宿主已设时它不生效），需要容器语义的用例继续在用例内 monkeypatch
（`test_health_check` / `test_websocket_auth` 已这样做）。复跑：**1622 passed / 105 skipped**，
且在 `IN_DOCKER=1` 仍在环境里的情况下全过。

### A3 🟡 生产环境没有管理员（台账未记）

P0-2 把「身份最小闭环」标成 ✅，但现场是：

| 检查 | 结果 |
|---|---|
| `select username, is_admin from users` | admin / test2 / smoke_deploy，**全为 false** |
| `ADMIN_API_KEY` | 空 |
| `GET /api/admin/models/providers` | **403**（无凭据与带假 key 都是 403） |

也就是说模型 Provider/路由管理页在真实环境里**进不去**。实现是对的，缺的是「谁来当管理员」
这一步。台账只记了「能力已实现」，没记「生产上没人能用」。

处理：未擅自改生产数据，已在台账新增 **P0-5 ◐**，附一条 owner 命令与验收口径（见第 4 节）。

### A4 🟢 README 与启动脚本的等待时间不一致

脚本改成 240s（首次冷启动实测 3–4 分钟），README 仍写「最多 120s」。已改。

### A5 🟢 文档数字漂移

| 位置 | 原值 | 实际 |
|---|---|---|
| README 后端用例数 | 1610 | **1622** |
| 复现报告用例数 | 1610 | **1622** |
| 台账 P1-8 探针单测 | 13 例 | **21 例** |

### A6 🟢 文档索引缺 5 份新文档

`docs/README.md` 是文档索引，但本轮收尾的三件交付物（`articles/silent-failures.md`、
`benchmarks/quality-and-cost-2026-11-04.md`、`guide/quickstart-verification-2026-11-04.md`）
以及 `stage-probe`、`e2e-verification` 都没进目录树。已补 `articles/`、`benchmarks/` 两个节点
与三份文件条目。

### A7 ⚪ 历史设计文档引用已拆分的文件（不修）

`chat.py` / `document.py` / `knowledge_base.py` 已拆成同名包，但 `design/` 与 `archive/`
里仍有按旧路径的引用（共 40 处文本引用、12 处 markdown 链接）。**刻意不改**：这些是当时的
快照，改路径等于篡改历史记录；真正会被人照抄的命令（`guide/`、`project-status.md`）已核对，
全是 `cd backend && ...` 的相对路径，可用。

---

## 3. 验证通过的项（正面清单）

| 项 | 实测 |
|---|---|
| 后端单测 | **1622 passed / 105 skipped**（`IN_DOCKER=1` 环境下也通过） |
| 前端 | typecheck 通过 / ESLint 0 / **146 passed（20 文件）** |
| 静态检查 | `uvx ruff@0.9.3 check --select E9,F63,F7,F82 backend` → All checks passed |
| 环境一致性 | `check_env_consistency.py` 通过（106 键 / 7+2 插值 / 224 字段） |
| 运行态 | 11 容器全 healthy；`/health/ready` 四项 ok（db/redis/milvus/minio） |
| 监控 | Prometheus 4 个 target 全 up（fastapi-app / milvus / postgresql / prometheus） |
| 生产数据 | 0 文档 / 1 知识库 / 3 用户 / 107 trace / **0 反馈** |
| 仓库卫生 | 无 `.env`/`secrets/`/日志/`__pycache__` 入库；0 TODO / 0 裸 except |
| 死链 | 仅历史文档的旧路径（A7），活跃文档无坏链 |

另：`except ...: pass` 共 10 处，逐处看过，均为「取消除、可选依赖探测、无效 JSON 跳过、
最佳努力清理」，不是吞掉关键错误。

---

## 4. 需要 owner 动作（3 项，都不需要写代码）

| # | 项 | 命令 / 动作 | 验收 |
|---|---|---|---|
| 1 | **建生产管理员**（A3，新） | `docker exec backend-prod python scripts/create_admin.py --username admin --promote` | 带该身份 GET `/api/admin/models/providers` → 200 |
| 2 | 注册巡检计划任务（P1-3） | `schtasks /Create`（本机 Command Security 拦截，不可绕过） | `schtasks /Query` 可见；次日 `backups/audit/` 有新产物 |
| 3 | 确认是否保留 `smoke_deploy` 账号 | 视需要停用 | 用户列表只剩预期账号 |

> 顺带一条观察：`backups/audit/` 里最新的产物是 **11-02**，11-03/11-04 没有新文件——
> 与「计划任务未注册」一致，不是脚本坏了。

---

## 5. 复现本文所有结论

```bash
# 1) 测试（宿主 IN_DOCKER=1 也应为全绿）
cd backend && .venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/integration

# 2) 与 CI 完全一致的静态检查（本地没装 ruff 就用 uvx）
cd .. && uvx ruff@0.9.3 check --select E9,F63,F7,F82 backend

# 3) 前端
cd frontend && npm run typecheck && npm run lint && npx vitest run --maxWorkers=1

# 4) 环境一致性
cd .. && python scripts/check_env_consistency.py

# 5) 运行态
docker ps --format '{{.Names}}\t{{.Status}}'
docker exec backend-prod sh -c 'curl -s http://localhost:8000/health/ready'

# 6) 生产管理员现状（A3）
docker exec postgres-prod psql -U prod_user -d app_prod -tAc \
  "select username, is_admin from users order by created_at;"
docker exec backend-prod sh -c 'curl -s -o /dev/null -w "%{http_code}\n" \
  http://localhost:8000/api/admin/models/providers'

# 7) CI 最近状态
gh run list --limit 3
```

---

## 6. 这次自检暴露的方法论问题

1. **「本地全绿」不等于「CI 全绿」**：本地缺的工具（ruff）正好是唯一能抓到 A1 的工具。
   自检必须用**对方的命令**跑，而不是「等价地跑一下」。
2. **「已知失败」是负债**：A2 那 8 例我曾两次写成「既有失败、与改动无关」——
   这句话听起来严谨，实际是**放弃定位**。真正的结论是「宿主环境未隔离」，一句话能说清。
3. **完成标记要有运行态对照**：P0-2 的能力实现是真的，但「生产上没人能用」也是真的。
   能力 ✅ 与可用 ✅ 是两件事，台账应分别记。
