# 结项说明（2026-11-07）

> 这份文件回答三个问题：**这个项目是什么、做到了什么、哪些没做**。
> 其余文档是过程账本与专题细节，本页是结论。
>
> 归档：[project-status-2026-11-07.md](archive/project-status-2026-11-07.md)（417 行完整台账，冻结保留）

## 0. 一句话

**知澜**是一个可私有化部署的 RAG 知识问答系统（LangChain + Ollama + Milvus，默认全离线），
2026-11-07 结项。它是**工程化参考实现，不是产品**：没有真实用户，没有 SLA，没有采购方。

它的价值主张是「**可复现的严谨**」——每一条声称都能指到文件、指到测试、或指到一条能跑的命令。
本页所有数字都来自仓库里的脚本或报告，不是估计。

## 1. 结项判据

2026-11-04 把「做完」定义成三件交付物，加上台账清零与真实上线：

| # | 判据 | 状态 | 证据 |
|---|---|---|---|
| 1 | 技术长文：三个静默失败 | ✅ 3446 汉字 | [articles/silent-failures.md](articles/silent-failures.md) |
| 2 | 基准页：检索与成本结论汇成一页 | ✅ | [benchmarks/quality-and-cost-2026-11-04.md](benchmarks/quality-and-cost-2026-11-04.md) |
| 3 | 干净目录复现：全新 clone 按 README 能起来 | ✅ 修掉 4 处阻断启动的问题 | [guide/quickstart-verification-2026-11-04.md](guide/quickstart-verification-2026-11-04.md) |
| 4 | 台账 P0-1 ~ P2-3 清零 | ✅ 无「未完成」项 | 归档台账 §三 |
| 5 | 真实部署并在生产冒烟 | ✅ DEPLOY-5（2026-11-07） | 归档台账 DEPLOY 段 |

## 2. 做到了什么

### 2.1 正确性：测试与 CI

| 项 | 数字 | 说明 |
|---|---|---|
| 后端用例 | **1691 passed / 117 skipped** | `IN_DOCKER=false uv run pytest -q`，约 2 分钟 |
| 集成用例 | 1784 passed / 24 skipped | CI `Backend Integration Tests`，真 PostgreSQL |
| 前端用例 | 154 passed | `pnpm test`（vitest） |
| CI | **7/7 绿** | lint / 单测 / 集成 / 前端 / 构建 / 安全扫描 / 镜像 |
| 代码规模 | 后端 229 文件 / 50444 行；前端 47 个 `.vue` + 62 个 `.ts` | 11 个 alembic 迁移 |

### 2.2 三个静默失败（项目的真正主题）

这三件事的失败都**不报错**，看起来一切正常，直到有人发现「删掉的还能搜到」或「库里有内容却答没有」。
项目的大部分工程量花在把它们变成可判定的信号上。

| 难点 | 做法 | 证据 |
|---|---|---|
| **删除真的删干净了吗** | 删除按 DB / 向量 / 对象 / 缓存四类落点逐一对齐，失败带 `phase` 可定位；另有只读对账工具主动比对 Milvus↔DB、MinIO↔DB、Redis↔DB | `consistency_audit.py`（退出码 0=一致/1=有差异/2=没跑成）、删除链 13 例 + 对账 29 例 |
| **出错了能判定能不能重试吗** | 三级严重度（`RECOVERABLE`/`REPORTABLE`/`PROGRAMMING`），异常类型优先于文案；「后果优先」——底层是连接错误但会导致结论错误的显式抬级 | `error_handling.py`，**369 处调用点**，93 例单测 |
| **生成到一半断网怎么办** | 运行与连接解耦：事件进带序号缓冲，断连只结束订阅；凭 `run_id` + 游标续传，可显式停止并保留已生成部分 | `chat_run.py` + `api/chat/runs.py`，35 例单测 |

### 2.3 检索质量（量出来的，不是形容词）

| 项 | 数字 |
|---|---|
| 90 题标注集 | **hit@5 = 0.869，MRR = 0.739**（78 篇语料） |
| 未命中的 11 题 | **全部是口语化问法**（「出去玩几天」vs 文档里的「请假」）；制度题与参数题 100% 命中 |
| 24 题真实文档探针 | 概览题 0.598 / 细节题 0.792 |
| 通道对比 | 稠密单通道 0.953 / RRF 混合 0.891 / 混合+重排 0.922 |

### 2.4 延迟与成本

- 知识库问答整链 wall **0.9~1.5s**（bge-m3 + bge-reranker-base + qwen2.5:3b-instruct，全本地，零 API 费）。
- 模型分工是**显存治理**的结果，不是配置洁癖：4GB 显存下 4B 驻留会驱逐 3B 直答模型，
  所以 **4B 的唯一入口是 `deep_thinking=on`**，辅助任务（意图路由/标题/Query 改写）全部归 `fast_llm`。
- reranker 换型（`bge-reranker-v2-m3` → `bge-reranker-base`）：质量零差异，**推理快 3.3 倍**，
  kb_retrieval 热身 1.3~1.9s → 441~560ms。发现过程见 `guide/deployment.md` 的踩坑记录。

### 2.5 可运维：备份 / 恢复 / 回滚，全都有脚本与实测

四个演练脚本，都能一条命令跑完并自动清理：

| 脚本 | 验什么 | 实测 |
|---|---|---|
| `restore_drill.sh` | PostgreSQL（起隔离 PG、调真实 `restore.sh`、比表数/行数） | 29 表 / 6s（两份备份格式 `.dump` 与 `.sql.gz` 都验） |
| `restore_drill_minio.sh` | MinIO 整实例（全新实例 + 独立凭据 + sha256 比对） | 4s / 3 对象 / 哈希一致 |
| `restore_drill_milvus.sh` | Milvus 整实例（dense + **sparse 双索引**） | 29s / 1000 向量 / 双索引类型与 top-k 一致 |
| `rollback_drill.sh` | 回滚（上一版镜像装当前 schema + 前端回滚 + 生成中停机） | 全流程 77~101s，条件见下 |

**规模 RTO**（`scale_rto_drill.sh`，合成数据）：

| 组件 | 规模 | 备份 | 恢复 |
|---|---|---|---|
| PostgreSQL | 915MB / 3 万文档 / 60 万 trace | 46s → 61MB | **22s** |
| Milvus | 2 万向量（1024 维 dense + sparse） | 3s → 71MiB | **29~33s** |
| MinIO | 1000 对象 / 195MB | 5s | **18s** |

结论与**五条边界**（合成数据可压缩 15:1、单机同盘无网络、不含后端冷启动 27~48s、每组样本少、非并发压测）
见 [benchmarks/rto-scale-2026-11-05.md](benchmarks/rto-scale-2026-11-05.md)。

**回滚的两条代价**（演练才发现，已写进 runbook §5.2）：旧镜像跑不了任何 alembic 命令
（`Can't locate revision`，连 `current` 都失败）；且丢掉 P0-4 的优雅停机——
生成中 `docker stop` 会得到 ExitCode 137 + 客户端拿不到 `end` 帧。

### 2.6 性能门禁（不是「以后再说」）

`backend/scripts/perf_suite.py` + `.github/workflows/nightly.yml`（每天 02:30 CST）：

- **计数器硬卡点**（与机器无关，逐位相等）：每题打分文档数 78、40KB markdown 切 792 块、三路 150 条融合成 60 条。
  改算法（加宽窗口、多跑一遍融合）会立刻被抓，不用等它变慢。
- **耗时按 profile 卡**：取多次迭代的 **min**（本机抖动 1.9%，median 能抖 60%）。
  local 基线 rrf 0.072ms / chunking 3.915ms / bm25 0.268ms；ci 档已实测填入并开始卡点。
  （2026-11-09 修订：判据从「仅 1.6× 比例」改为「1.6× 比例 **且** 绝对增量 ≥ 5ms」——
  0.2ms 级的微基准靠噪声就能凑出 1.76×，实测把夜间任务假红过一次。详见
  [benchmarks/perf-baseline.md §3](benchmarks/perf-baseline.md)。）
- 反直觉结果：4 核 GitHub runner 在这三个单线程微基准上**不比 16 核笔记本慢**（0.79~1.07×）。
  所以「CI 一定比本机慢」是想当然，基线必须实测。

### 2.7 安全与界面

- SEC-1~21 全部完成：owner 级隔离、生产默认关注册、RPM/并发/日额度、受信代理 + Redis 滑动窗口、
  SSRF fail-closed + 连接层固定 IP、JWT 登出吊销、CSP/HSTS、工具域名白黑名单与 DLP。
- 前端 47 个 Vue 组件：语义 token 设计系统、三态组件（加载/空/错）、暗色模式、响应式。

### 2.8 生产现场

11 个容器全 healthy、`/health/ready` 四项 ok、OpenAPI **117** 条路由、4 篇文档 / 532 chunk、
三方一致性巡检 `issues 0`（进程内调度，每日自动跑）。

## 3. 没做什么（以及为什么）

写清楚不做什么，比写清楚做了什么更能说明取舍。

| 不做 | 原因 | 什么前提才该做 |
|---|---|---|
| 多租户 / 组织层级 / SSO / RBAC | 现有隔离按 `owner` 做，够单个组织内网使用；加租户层是另一个量级的改造 | 出现第二个组织要共用一套实例 |
| 高可用 / 多副本 / 主从 | **9 处进程内状态**（运行缓冲、Wiki 去抖、进度表、限流桶…）会互相打架；单机是当前唯一自洽的形态 | 有第二台机器，且先把 9 处状态外置 |
| 桌面端 / 免登录单用户版 | 需要第二套存储抽象（SQLite + 嵌入式向量），成本是重写而非优化 | 真的要打包分发 |
| 「企业级」这个词 | 三条门槛只过了两条：恢复演练 ✅、≥50 题标注集 ✅（90 题）、**容量实测（X 并发下 P95 与成功率）❌** | 压测做完并有数字 |
| AGT-2 在线抽样 → 失败转回归 | 没有真实流量，抽不到样本，做出来是空转 | 有外部用户 |
| AGT-4 Artifacts / AGT-5 MCP / AGT-6 记忆 / AGT-7 多模态 | 都是「另一量级」的改造，不是补漏 | 有明确要接的外部工具或场景 |
| 评测集扩到 150 题 | 90 题已能稳定区分通道差异；扩题不改变结论 | 要对外发布质量报告 |

## 4. 诚实的负面结论与遗留风险

**这几条是不好看但必须写的**：

> 本节是自述。独立视角的对抗性复查（假设所有声称都是假的去证伪）另见
> [audit/adversarial-2026-11-07.md](audit/adversarial-2026-11-07.md)——
> 它查出了本文没写的 P0：`sessions` 表无索引、会话列表前端无分页。

1. **生产默认的混合检索，在这份语料上打不过不融合的稠密单通道**（0.891 vs 0.953）。
   混合+重排 0.922 也低于稠密单通道。原因是 78 篇语料规模太小、BM25 稀疏通道引入噪声。
   没有改成默认稠密，因为换语料后结论可能反转——但这条结论一直留在 README 里。
2. **概览题的检索是弱项**（24 题探针：0.598）。试过六种修法，加宽重排窗口**反而更低**（0.598→0.501），
   去冗余 −0.146；唯一非负的是邻块回填（+0.09，p=0.25，不显著，**未上生产**）。
   已明确停止改进：章节级召回的预期收益 <5pp，且要答级评测才能验收。
3. **4B 模型的能力边界**：`hybrid_04`（制度冲突题）实测**不再拒答**，但只复述两条制度、不回答「是否冲突」。
   这是模型能力限制，不是链接口的问题。已记录，不打算用工程手段绕。
4. **RTO 数字来自合成数据**：PG 的是脚本插入的规整行、Milvus 是随机向量，可压缩比 15:1，
   真实文档语料不会这么幸运。且是单机同盘（无网络传输），不含后端冷启动 27~48s。
5. **单机单副本**：任何一次 `docker stop` 都是停服。P0-4 的优雅停机只保证「生成中的回答不被硬杀」，
   不保证可用性。
6. **owner 手动项仍有三个没做**（都不是代码问题）：
   - `ops-admin` 密码仍是建号时的随机值（文档写着首次登录必改）；
   - `test2` / `smoke_deploy` 两个验证账号仍可登录（非管理员但能进业务）；
   - Tavily API Key 未轮换。
   前两项是真实的敞口。全库只有 `ops-admin` 一个管理员账号。
7. `request_traces.tool_calls` 存的其实是**工具输出**而非工具名，字段名易误读。已记录，未改（改名要动表）。

## 5. 怎么接手

```bash
# 1. 起来（拉过一次镜像后约 10 分钟）
ollama pull qwen2.5:3b-instruct && ollama pull qwen3:4b && ollama pull bge-m3
./scripts/start-dev.sh          # Windows: .\scripts\start-dev.ps1

# 2. 验证它没坏（不要求你相信本文档）
cd backend
IN_DOCKER=false uv run pytest -q                       # 1691 passed / 117 skipped
uv run python scripts/run_eval.py --show-misses        # hit@5 0.869 / MRR 0.739
uv run python scripts/consistency_audit.py --sample 5  # 三方对账，只出报告不删

# 3. 验证运维链路（任一脚本，约 1 分钟，自动清理）
./scripts/restore_drill.sh
./scripts/rollback_drill.sh
```

**读哪一份**：

| 想知道 | 读 |
|---|---|
| 系统怎么组织的、代码在哪 | [architecture.md](architecture.md) |
| 接口有哪些、参数是什么 | [api.md](api.md) |
| 怎么部署、踩过什么坑 | [guide/deployment.md](guide/deployment.md) |
| 环境变量与端口 | [guide/configuration.md](guide/configuration.md) |
| 本地开发、测试、CI | [guide/development.md](guide/development.md) |
| 备份 / 恢复 / 回滚 / 容量 | [guide/backup-restore.md](guide/backup-restore.md) |
| 监控与告警处置 | [guide/monitoring.md](guide/monitoring.md) |
| 三个静默失败的完整叙事 | [articles/silent-failures.md](articles/silent-failures.md) |
| 所有量化结论 | [benchmarks/](benchmarks/) |
| 过程账本（417 行，冻结） | [archive/project-status-2026-11-07.md](archive/project-status-2026-11-07.md) |
| 旧版开发向 README（三种启动方式逐步说明、脚本参数、完整结构树） | [archive/README-dev-2026-11-07.md](archive/README-dev-2026-11-07.md) |

## 6. 冻结清单

以下内容**不再排期**，除非前提发生变化：

- 检索改进（概览题、通道融合调优）——要答级评测才能验收，收益预期 <5pp；
- 评测集扩容；
- 多租户 / SSO / RBAC、真 HA、桌面端单机版、企业级压测与 SLA；
- AGT-2 / AGT-4 / AGT-5 / AGT-6 / AGT-7（在线抽样、Artifacts、MCP 客户端、记忆、多模态）。

结项后如果有人要继续：先读第 4 节的负面结论，别在已经被证否的方向上重做一遍。

---

**结项日期**：2026-11-07 · 最终提交见 `git log` · 生产快照：11 容器 healthy / 117 路由 / 4 文档 532 chunk
