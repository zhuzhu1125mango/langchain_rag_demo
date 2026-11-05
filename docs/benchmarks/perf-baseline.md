# 性能基线与夜间回归（2026-11-05）

> OPS-4 的最后一项。此前 1659 例功能测试守着正确性、`run_eval` 守着检索质量，
> 但**没有任何性能基线**：`run_eval` 每跑一次都在算 `latency_p50/p95`，只是没人比对，
> 于是「某次改融合窗口让每题多花 40ms」这种退化只能靠人偶然发现。
>
> 这一页说明**量什么、怎么判、判错了怎么查**。基线数字在
> [`perf-baseline.json`](perf-baseline.json)，判定逻辑在 `backend/scripts/perf_suite.py`。

## 1. 当前基线（profile = local，本机 i5-12500H / 16 核 / NVMe）

| 测项 | min | median | p95 | 说明 |
|---|---|---|---|---|
| `rrf_fusion` | **0.072ms** | 0.073ms | 0.088ms | 三路 × 50 条（含重叠）融合一次 |
| `markdown_chunking` | **3.915ms** | 5.222ms | 12.868ms | 40KB markdown 切 792 块 |
| `bm25_query` | **0.268ms** | 0.327ms | 0.344ms | 单题打分（78 篇语料，不含建索引） |

计数器（确定性，必须逐位相等）：

| 计数器 | 值 |
|---|---|
| `rrf_channel_items` | 150 |
| `rrf_fused_items` | 60 |
| `chunk_input_chars` | 40321 |
| `chunk_count` | 792 |
| `bm25_questions` | 90 |
| `bm25_docs_scored_per_query` | 78 |

`profile = ci` 的档位待首次夜间运行后填入；在此之前该档位走「记录模式」（只打印、不卡点）。

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
但 runner 之间能差 2~3 倍，所以**跨 profile 不比绝对值**，每个 profile 只跟自己比。

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
