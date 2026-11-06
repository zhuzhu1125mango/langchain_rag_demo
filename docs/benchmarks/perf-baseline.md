# 性能基线与夜间回归（2026-11-05）

> OPS-4 的最后一项。此前 1659 例功能测试守着正确性、`run_eval` 守着检索质量，
> 但**没有任何性能基线**：`run_eval` 每跑一次都在算 `latency_p50/p95`，只是没人比对，
> 于是「某次改融合窗口让每题多花 40ms」这种退化只能靠人偶然发现。
>
> 这一页说明**量什么、怎么判、判错了怎么查**。基线数字在
> [`perf-baseline.json`](perf-baseline.json)，判定逻辑在 `backend/scripts/perf_suite.py`。

## 1. 当前基线

两个档位各自独立（同一段代码在两台机器上的绝对值不可比，见 §3）。

| 测项 | local（i5-12500H / 16 核） | ci（ubuntu-latest / 4 vCPU） | ci ÷ local |
|---|---|---|---|
| `rrf_fusion` | **0.072ms** | **0.077ms** | 1.07× |
| `markdown_chunking` | **3.915ms** | **3.490ms** | 0.89× |
| `bm25_query` | **0.268ms** | **0.212ms** | 0.79× |

local 的中位数/p95（3.915 / 5.222 / 12.868ms 那组）只做留档，不参与卡点——理由见 §2。

**一个有反直觉的结果**：4 核 runner 在这三个测项上**不比 16 核笔记本慢**，甚至更快。
这三个负载都是单线程纯 Python（BM25 打分、正则切分、字典累加），核数帮不上忙，
而笔记本在省电策略下会被降频。所以「CI 一定比本机慢」是个想当然的假设——
正因为如此，基线才必须**实测**而不是估。

计数器（确定性，与机器无关，两个档位共用一套）：

| 计数器 | 值 |
|---|---|
| `rrf_channel_items` | 150 |
| `rrf_fused_items` | 60 |
| `chunk_input_chars` | 40321 |
| `chunk_count` | 792 |
| `bm25_questions` | 90 |
| `bm25_docs_scored_per_query` | 78 |

ci 档取自首次手动触发的夜间运行（Actions run `42172489784`），**从下一次起开始卡点**。

**实测抖动**（同一份代码连跑 4 次，`markdown_chunking` 的 min）：

| 运行 | chunking min | 相对基线 |
|---|---|---|
| #1（基线） | 3.490ms | 1.00× |
| #2 | 2.458ms | **0.70×** |
| #3 | 3.563ms | 1.02× |
| #4 | 3.483ms | 1.00× |

三次落在 ±2%，一次快了 30%（更像是那台物理机当时更空/未降频，不是代码变了）。
**目前没有观察到「慢」的离群点**，所以 1.6× 的余量在现有证据下够用；
真遇到慢离群，先按 §5 看 runner 信息，再决定是否临时放宽（`PERF_MAX_RATIO` 可覆盖），
不要直接改基线。

## 2. 两个刻意的选择

**① 为什么卡「计数器」而不是只卡时间。**
时间受机器影响，计数器不受：每题打分 78 篇、40KB 切出 792 块、三路 150 条融合成 60 条。
改算法（加宽重排窗口、多跑一遍融合、换切分策略）会立刻让这些数字变，而且**不用等它变慢**。
这是唯一能在共享 runner 上稳定生效的卡点。

**② 为什么用「最小值」而不是均值/中位数。**
同一台机器上连跑三次，取每次的最小值：

| 统计量 | 三次取值 | 抖动 |
|---|---|---|
| `min`（采用） | 4.111 / 4.155 / 4.188ms（chunking） | **1.9%** |
| `median` | 5.083 / 6.318 / 8.145ms | **60%** |

最小值不是「最快的一次运气好」，而是「没有被打断时的耗时」——它与机器负载的相关性最低。
CI runner 是共享的，用均值会让门禁在红与绿之间随机跳，最后被人注释掉。

## 3. 怎么判

| 情形 | 结果 |
|---|---|
| 计数器任一不符 | **红**（确定性，没有解释空间） |
| 耗时 > 基线 × 1.6（`PERF_MAX_RATIO` 可调） | **红** |
| 耗时在阈值内 | 绿，并在 summary 里给出 `当前 / 基线 = X.XX×` |
| 基线里没有该 profile | **记录模式**（绿），提示把本次数字填进基线 |
| 出现新测项 / 新计数器 | 绿，标注「基线里没有，已记录」 |

阈值取 1.6× 是「能抓住真退化、又不被 runner 抖动搞红」的折中：本机同代码抖动 <2%，
实测 local 与 ci 在这三个测项上只差 0.79~1.07×（§1），但**共享 runner 的抖动无法事前约束**
（同一 runner 不同时段、不同物理机都不同），所以仍然**跨 profile 不比绝对值**，
每个 profile 只跟自己比。

## 4. 怎么看

- **每晚 02:30（CST）自动跑**：`.github/workflows/nightly.yml`，也可以在 Actions 页
  `Nightly Perf → Run workflow` 手动触发。
- 结果写在**那次运行的 Summary 页**（一张表 + 判定），artifact `nightly-perf` 保留 90 天
  （`perf_report.json` + `eval_report.json`）。
- 报告里同时带 `run_eval` 的 `hit_rate / mrr / p50 / p95`，这样「质量没退但慢了」
  能在一份报告里看出来，不用来回翻。

## 5. 红了怎么查（按顺序，别跳步）

1. **先看 runner**：summary 顶部有 `nproc` / `cpuinfo` / `uptime` / `free -m`。
   托管 runner 是共享的，`uptime` 高说明邻居在抢 CPU。
2. **看是哪种红**：计数器红 = 算法变了（真的要看代码）；耗时红但计数器没变 = 要么环境，
   要么常数变化（比如多了一次拷贝）。
3. **本地复跑**：`cd backend && uv run python scripts/perf_suite.py --profile local`，
   连跑三次看 `min` 是否稳定。
4. **确认是代码退化就先修代码**。要改基线，先在目标机器上 `--update-baseline`，
   并在提交说明里写清「为什么这次变慢是可接受的」——顺序反了就成了「改基线让红灯变绿」。

## 6. 不覆盖什么（别拿它当 SLA）

- **端到端问答延迟**：要 Ollama + embedding + 重排，CI 里没有。那份数字在
  [`quality-and-cost-2026-11-04.md`](quality-and-cost-2026-11-04.md)（平均 4.0s，本机）。
- **真实 Milvus 检索**：CI 的 `rag-eval` 是纯 BM25 离线栈，向量库与网络都不在内。
- **并发能力**（X 并发下 P95 与成功率）：这是「企业级三门槛」里的容量实测，未做，
  且与这里的单线程微基准不是一回事。
- **备份/恢复耗时**：由 `scripts/scale_rto_drill.sh` 与 runbook §7 的四个演练脚本覆盖，
  见 [`rto-scale-2026-11-05.md`](rto-scale-2026-11-05.md)。

## 7. 复现

```bash
cd backend
uv run python scripts/perf_suite.py                              # 对比 local 基线
uv run python scripts/perf_suite.py --profile ci --report perf.json --summary s.md
uv run python scripts/perf_suite.py --max-ratio 2.0              # 临时放宽阈值（排查用）
uv run python scripts/perf_suite.py --update-baseline            # 重写基线（需人确认）
```
