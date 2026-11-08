# 故障注入怎么跑（`scripts/fault_drill.sh`）

> 为什么有这一页：`severity=` 分级在单测里一直是绿的，但生产日志里它**一次都没出现过**。
> 单测能证明「代码调用了 `log_exception`」，证明不了「真实事故发生时，日志能让人分清
> 是外部依赖挂了、还是代码坏了」。这个演练补的就是这一段。

## 前提

| 需要 | 说明 |
|---|---|
| 开发栈在跑 | `./scripts/start-dev.sh`（PG / Milvus / MinIO / Redis / etcd） |
| Docker 可用 | 脚本会 `docker pause` / 重建容器 |
| 宿主 Ollama | 上传文档与问答都要 embedding；模型缺失时脚本以退出码 3 跳过 |
| 专用后端 | 脚本自动起 `fault-backend`（`docker-compose.fault.yml`，宿主端口 **18001** → 容器 8000） |

> 端口为什么是 18001：Windows 上 8000 段常被别的进程占着（实测 `bind: address already in use`），
> 而容器内仍是 8000 不变；脚本里用 `API_BASE=http://127.0.0.1:18001/api` 指过去。

**不要对着生产跑**：`docker stop milvus-standalone-prod` 会中断所有正在进行的问答，
而且重建后端容器意味着一次真实停机。这个脚本的所有注入都是有破坏性的。

## 跑

```bash
./scripts/fault_drill.sh
# 失败时保留现场（容器不删、临时目录不删）
KEEP=1 ./scripts/fault_drill.sh
```

退出码：`0` 三类均符合预期 · `1` 有不符合（会指出是哪一条） · `3` 环境不具备，**没跑**。
`3` 不是通过——这是刻意的：把「跳过」当绿灯，比没有这条演练更危险。

## 三类注入与判据

### A. 依赖不可达（`docker stop milvus-standalone-dev`）

| 观察点 | 期望 |
|---|---|
| 用户侧 | 明确失败（SSE `error` 事件 / 请求失败），**不能**是「没有找到相关内容」 |
| 日志 | `action=kb.retrieval.dense_fallback` + `severity=reportable` |
| 恢复后 | `docker start` 再问一次，正常出答案 |

为什么用 `stop` 而不是 `pause`：`pause` 在 Windows/Docker Desktop 上把进程冻住后，
pymilvus 的连接处于「半死」状态，恢复时既不报错也不返回，`unpause` 之后还要等
gRPC 自己超时；`stop` 是干净的连接拒绝，判据稳定得多（实测 2026-11-08）。

为什么盯着「没有相关内容」：这是最容易被误读的一类——基础设施挂了，用户以为是自己
问的东西不存在，运维看到的是「一切正常」（因为没报错）。

### B. 打分失败（provider 改 ollama + `KB_RERANK_MODEL` 指向不存在的模型）

**两个变量必须一起改**。只改模型名、provider 仍是 `sentence_transformers` 时，失败发生在
**加载阶段**（`action=hybrid_search.load_cross_encoder`），进程随后把 `_model_failed` 置真、
永久走 RRF 截断——打分路径一次都不会进入，`hybrid_search.rerank` 一条日志都不会有。
2026-11-08 01:33 那次演练就是这么「假通过」的：三次问答全 0 退出、断言找不到日志才暴露。

改成 ollama 后，坏模型名是「每次打分都 404」（实测 0.04s 返回），正好打中打分路径。

| 观察点 | 期望 |
|---|---|
| 前两次失败 | `action=hybrid_search.rerank` + `severity=recoverable`（降级到 RRF 仍在工作） |
| 连续第 3 次 | 升级为 `severity=reportable`，带 `consecutive_failures=3` |
| 底层原因 | `action=rerank.ollama.score`（证明是打分失败，不是别的原因没跑） |
| 分数 | 回退成 RRF 分，**不能是 0**（0 分会被 `KB_RERANK_MIN_SCORE` 滤空 → 假「没有内容」） |
| 用户侧 | 仍有答案，只是排序质量下降 |

分两档的原因：单次超时是抖动，每次报警等于制造噪音；模型名配错是不可自愈的，
只记 warning 会永久沉底。

### C. 代码 bug（临时在 `stream_answer` 首行注入未捕获异常）

| 观察点 | 期望 |
|---|---|
| HTTP | 500 |
| 响应体 | 带 `request_id`（用户报障时能对上日志） |
| 日志 | `action=request.unhandled` + `severity=programming` + 完整堆栈 |

注入是临时的：脚本先备份 `backend/src/api/chat/stream.py`，`trap` 里无论如何都会还原。
**用 `docker restart` 让改动生效，不依赖 `--reload`**：Windows 挂载卷上 watchfiles 收不到
inotify 事件（实测改完文件请求照样 200，注入等于没做）。

## 演练数据必须是「全新的、且已落盘的」

脚本每次新建一个 `fault-kb-<时分秒>`，上传文档、等处理完成、**显式 flush 向量**，然后才开跑。
这两步都不是多余的：

| 不做会怎样 | 实测（2026-11-08） |
|---|---|
| 复用已有的知识库 | PG 里 `chunks_count > 0`、Milvus 里该库 0 条向量（多轮演练留下的漂移）。检索 0 条候选 → 没有 pair 可打分 → **注入 B 静默失效**，问答照样「成功」 |
| 上传后不 flush | Milvus 的插入先进内存 growing segment。基线能检索到 3 条来源，A 步停一次 Milvus 之后 B 步候选变 0 —— 测试对象自己把数据弄丢了 |

所以基线断言是「退出码 0 **且** 来源条数 > 0」，不是「有答案就行」。
0 条来源时脚本以退出码 3 结束，并提示先跑 `python scripts/consistency_audit.py`。

## 这个演练抓到过什么

2026-11-08 首次真跑时抓到一条真实缺陷：**重排打分失败 → 回退 RRF → 上下文被清空**。
`rerank_score` 在回退时是 RRF 分（≈0.016），而相关性门禁拿它跟阈值比，全部判为不相关，
最终用户看到的是「知识库里没有相关内容」。现已修：回退结果带 `rerank_fallback` 标记，
门禁看到标记就改走语义相似度兜底。

结论：演练的价值不在于「跑通」，而在于**它能把「日志分级正确、用户侧却是另一种错误」**
这种组合暴露出来——这是单测和 200 健康检查都看不见的。

## 已知限制

- **只用开发栈验证过**：生产上的日志采集、告警路由没在演练里走过。
- **分级判据依赖日志文本**（`grep severity=`）：如果改成 JSON 日志格式，脚本里的
  grep 需要跟着改。
- **底层逐条日志可能被节流**：`ollama_reranker` 对单条打分失败只记前 3 次（之后每 50 次一条），
  而启动预热就会用掉这 3 次额度。所以断言看的是 batch 级结论（「整批 N/N 条打分失败」），
  不是逐条日志。
- **`ollama_unreachable` 不在这里测**：它由 `/health/e2e` 覆盖（探针会先探 Ollama），
  单独再注入一次属于重复。
- **注入 B 只覆盖「打分失败」，不覆盖「模型加载失败」**：加载失败走
  `action=hybrid_search.load_cross_encoder`，且会把进程置为永久 RRF 截断，不再重试。
  那条分支由单测 `tests/test_rerank_grading.py` 与 `tests/test_error_handling.py` 覆盖。
- 演练会创建临时知识库并在结束时删除；如果中途被 Ctrl-C，下次运行会看到一个
  `fault-kb-*` 残留，手工删掉即可。
- **别在演练运行时改这个脚本**：bash 是边读边执行的，改动会让它读到错位内容
  （实测报 `unexpected EOF`）。要改就等它跑完。
