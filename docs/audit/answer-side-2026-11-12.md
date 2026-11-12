# 答案侧诊断：一个藏了 4 个月的空格 bug

> 2026-11-12。起因是「答级评测该怎么做」这个问题，第一个样本就撞上了更严重的东西。

## 0. 一句话

为了验证「检索到的内容有没有被写进答案」，我先跑了一道题，发现答案里
`docker logs backend-prod 2>&1 | grep -E` 变成了 `dockerlogsbackend-prod2>&1|grep-E`。
根因是 `OutputSanitizer.sanitize()` 结尾的 `strip()` 被**逐 chunk** 调用，
而流式 chunk 的边界经常正好落在空格上。**自 2026-07-02 引入起，所有知识库答案的
空格与结尾换行都在丢失。**

## 1. 根因

`_stream_with_retry` 每产出一个 chunk 就调一次 `OutputSanitizer.sanitize(chunk)`，
而 `sanitize()` 的两条规则是**整段语义**的：

```python
cleaned = cls.remove_thinking_tags(text)      # 内部 return cleaned.strip()
cleaned = cls.remove_answer_prefix(cleaned)   # 内部 return cleaned.strip()
return cleaned.strip(), was_polluted          # 再来一次
```

逐片 strip 的效果（容器内实测）：

```
'docker logs'      -> 'docker logs'
' backend-prod'    -> 'backend-prod'      ← 前导空格被吃
' 2>&1 '           -> '2>&1'
拼接结果: '请执行docker logsbackend-prod2>&1| grep -E"一致性"'
整段调用: '请执行 docker logs backend-prod 2>&1 | grep -E "一致性"'
```

还有一个连带问题：`"\n".join(text.splitlines())` 会吃掉片段结尾的换行，
所以流式输出的换行也在丢。

**影响面**：所有 `knowledge_base` / `hybrid_search` / `tool_first` 答案；落库的
`final_answer`、会话历史、导出结果全部带着粘连文本。中文看不出问题，
代码块和英文完全不可复制——而「真人浏览器验证」这一项一直挂着没做，所以没人发现。

**存在时长**：`strip()` 与逐 chunk 调用同日引入（`175c7c6`，2026-07-02），
至修复约 **4 个月**。

## 2. 修复

`strip_edges` 开关：整段调用保持原行为，流式调用传 `False`。

- `sanitize(..., strip_edges=True)` / `remove_thinking_tags` / `remove_answer_prefix` 同步加参数
- 换行：只在确实删掉「工具调用行」时才重建文本，否则原文不动（`splitlines(keepends=True)`）
- 三处流式调用改为 `strip_edges=False`：`generation_stages.py` ×2、`pre_stages.py`、`agent_stream.py`
- `tests/test_output_sanitizer_streaming.py` 11 例：拼接后逐字节等于原文、结尾换行保留、
  缩进保留、纯空格 chunk 保留、工具调用行仍被过滤

## 3. 修复效果：答案侧指标

新增 `scripts/probe_answer_quality.py`，在生产 606 块语料上跑完整链路（24 题），
比较「上下文事实覆盖率」与「**答案文本**事实覆盖率」：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 概览 上下文 / 答案 / gap | 0.500 / 0.194 / **+0.306** | 0.500 / **0.319** / **+0.181** |
| 细节 上下文 / 答案 / gap | 0.708 / 0.375 / **+0.333** | 0.708 / **0.542** / **+0.167** |
| 自信漏答（有料却没写） | 10/24 | **7/24** |
| 拒答（诚实说不知道） | 3/24 | 3/24 |

**5 道题的答案覆盖率直接从 0.00 跳到 0.50~1.00**（`一致性巡检`、`数据库迁移怎么执行`、
`数据库迁移怎么回滚`、`恢复的顺序`、`迁移出错对比`）——它们的"漏答"其实是空格 bug 造成的
假象，答案一直是对的。

一个反例：「改完告警规则怎么生效」1.00 → 0.50（同一事实串这次没匹配上），需要单独看，
不能假定所有变化都是改善。

## 4. 这个指标暴露了什么（不是 bug，是真问题）

修复后仍有 **gap ≈ 0.17**，且 7 题「自信漏答」：上下文里有事实，答案没写，而且
`answer_type = knowledge_base`（系统认为答上了）。三题最典型：

| 题 | 上下文 | 答案 | 现象 |
|---|---|---|---|
| 监控哪些指标、有哪些告警？ | 0.00 | 0.00 | 检索只拿到指标表，却仍标 `knowledge_base` |
| 换 Embedding 模型需要做什么？ | 0.00 | 0.00 | 同上（融合名次 32，进不了窗口） |
| 备份恢复后要确认哪些事？ | 0.00 | 0.00 | 同上 |

这三题的模式一致：**上下文零覆盖，系统却以知识库身份作答**，不复述、不拒答。
这正是「自信的错答案」最纯粹的形态，也解释了 11-09 那次 `kb_miss`
（0.027/0/0）为什么让用户困惑——它当时其实做出了正确的判断，只是没有告知依据。

## 5. 结论

1. **答案侧必须单独测**。上下文覆盖率 0.68 不代表答案覆盖率 0.68，两者差 0.17~0.33，
   而这个差额此前没有任何指标在看。
2. **自动化指标测不出渲染问题**。空格 bug 藏了 4 个月，所有单测、探针、门禁都是绿的——
   因为它们看的都是「事实串在不在文本里」，而事实串本身带空格，匹配失败又被当成"漏答"，
   于是一个渲染 bug 伪装成了检索质量问题。
3. **评测顺序要反过来**：先做一次「上下文 vs 答案」的对照，再谈调检索。
   今天前半段所有检索优化，都建立在"答案没问题"这个未经验证的假设上。

## 6. 复现

```bash
docker cp backend/data/eval_questions.json backend-prod:/app/data/
docker cp backend/scripts/probe_answer_quality.py backend-prod:/app/scripts/
docker exec backend-prod python scripts/probe_answer_quality.py \
    --kb b3bad7b2-998c-47ec-8398-f70732081d3a --flush-cache \
    --report /app/data/answer-quality-20261112.json
```

`--flush-cache` 不是可选项：语义缓存会让重跑直接命中旧答案，检索根本不走，
gap 会被算成 0（第一次跑就踩了，日志里有 `semantic_cache_hit`）。

---

## 7. 补记（同日傍晚）：这份报告的数字全部落在噪声带内

同一天傍晚接回填时做了重复实验：**同一配置、同一上下文，连跑两遍**，
概览答案覆盖率 0.340 / 0.493（差 15.3pp），细节 0.542 / 0.583。
而上下文覆盖率两遍**逐位相同**——抖动全部来自生成采样，不是检索。

根因：模型注册表为空、走 legacy 路径、`temperature` 未配置 ⇒
`langchain_ollama` 用默认 **0.8**。固定 `KB_ANSWER_TEMPERATURE=0.1` 后，
同配置两跑逐位一致（11/30、9/18）。

对本报告的影响：

| 结论 | 是否成立 |
|---|---|
| 空格 bug 是真的、影响面判断 | **成立**（渲染问题，与采样无关） |
| 「修复后 gap 0.181 / 0.167」 | **方向成立，数值不可当验收数字**（噪声 ±15pp） |
| 「自信漏答 10/24 → 7/24」 | **方向成立，具体题数在噪声内** |
| 「5 题从 0.00 跳到 0.5~1.0」 | **成立**（同题同上下文，跳变幅度远大于噪声） |
| 「7 题自信漏答，3 题上下文零覆盖」 | **成立**（上下文零覆盖是确定性的） |

教训：**答案侧要下结论，先固定温度、再谈重复**。否则一个 ±15pp 的抖动会把
「+5pp 的改善」和「−5pp 的回归」都变成看起来有据的结论。
详见 [benchmarks/neighbor-expand-2026-11-12.md](../benchmarks/neighbor-expand-2026-11-12.md) §5。
