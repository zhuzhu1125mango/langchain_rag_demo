# 对抗性检查报告（2026-11-07）

> 方法：**假设项目自述都是错的**，从代码与运行现场去证伪。
> 不读文档的结论，只读实现、执行计划、日志与真实响应。
> 检查范围：生产栈（11 容器）、当前代码（`08ee1eb`）、CI 最近运行。

## 0. 结论摘要

| 判定 | 项 |
|---|---|
| **证伪失败**（声称成立） | owner 隔离、开放注册关闭、凭证不入日志、XSS 防护、SQL 注入面、schema 与迁移一致、三方对账一致 |
| **发现真问题** | ① 会话表**无索引** + 前端**无分页** ② 删除链只有逻辑层证据 ③ 「错误可判定」从未被真实事故验证 ④ `health/ready` 不含 Ollama ⑤ 依赖豁免无到期提醒 ⑥ 生产模式靠 `IN_DOCKER` 推断 |

**一句话**：这个项目在「组件层」的证据非常扎实（4 个演练脚本、三方对账、93 例异常分级测试），
但在「**在线请求路径的系统层**」几乎是空白——规模、索引、分页、故障注入都没验过。
RTO 证书只覆盖了「把数据搬回来」，没覆盖「搬回来之后 100 个用户同时用」。

## 1. 证伪失败的声称（真的成立）

| 声称 | 对抗手段 | 结果 |
|---|---|---|
| 资源按 owner 隔离 | 用非管理员账号撞别人的会话：读 / 导出 / 删除 | **403 / 403 / 403**，数据未被破坏 |
| 搜索不跨用户 | 另一账号用同一关键词搜索 | 返回 **0 条** |
| 语义缓存不串答案 | 读 `scope_key()` 构造 | `rag:semcache:{user_id}:{kb_scope}`，按用户 + KB 集合隔离 |
| 生产默认关注册 | 直接 POST 注册 | **403** |
| 日志不泄凭证 | grep 生产日志的 JWT / `rag_` Key / `password=` 模式 | **0 命中** |
| 无 XSS | 查 `v-html` 的两个使用点 | `markdown-it({html:false})` + **DOMPurify 白名单**；另一处先 `escapeHtml` |
| 无 SQL 注入 | 查 `text(f"...")` 拼接 | **0 处** |
| schema 与代码一致 | `alembic current` vs 迁移链 head | 生产 `d5e6f7a8b9c0` = 唯一 head（11 个迁移，单链） |
| 三方一致 | `consistency_audit.py --sample 5`（容器内，只读） | Milvus 532 / MinIO 4 / Redis 0，**exit 0** |
| 代码卫生 | 裸 `except: pass` / 裸 print / TODO | **0 / 0 / 0**；6 处 `except Exception: pass` 全在清理路径且有注释 |

## 2. 发现的不足

### P0-1 `sessions` 表只有主键索引，且前端没有分页

**证据**（生产库 `EXPLAIN ANALYZE`）：

```
Limit -> Sort (updated_at DESC) -> Seq Scan on sessions
  Filter: user_id = 'x' AND (messages)::text ~~* '%kw%'
```

`pg_indexes` 里 `sessions` 只有 `sessions_pkey`。这意味着：

- **「列出我的会话」本身就在全表扫描 + 全表排序**（不只是搜索）；
- 搜索还要逐行把 JSONB 转成文本（成本随**消息总量**增长，不是会话数）；
- 后端 `GET /api/sessions/` 有 `skip`/`limit`（默认 100、上限 500），但**前端完全没接**——
  实测 `HistoryView.vue` 与 `chat.ts` 里 `skip`/`limit`/`loadMore`/`分页` 零匹配。
  **超过 100 条会话的用户看不到第 101 条，也没有任何提示。**

**并且**：`docs/api.md` 里那句「无索引，万级会话下可接受」是我上一轮写的，
**没有任何测量支撑**——属于典型的「听起来合理的判断」被写成事实。

**方案（按成本递增）**：

```sql
-- ① 最小改动，解决列表分页（必做）
--    注意用 CONCURRENTLY，生产 4 条数据无所谓，但脚本要能复用到有数据的环境
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sessions_user_updated
  ON sessions (user_id, updated_at DESC);
```

② 前端接分页（`limit` 固定 50 + 「加载更多」按钮，或游标分页）——**没有这一步，索引只解决一半问题**。

③ 搜索加速二选一：
- **推荐**：把消息搜索改成「标题 + 会话摘要」匹配，摘要列可建 GIN；
- 或建独立表 `session_message_index(session_id, user_id, tsvector)`，
  在写入消息时同步，用 `to_tsvector('simple', content)` + GIN（注意中文需要分词扩展，`simple` 只切空格）；
- `pg_trgm` 对 `(messages)::text` 做表达式 GIN 索引**技术上可行但体积大**，且要求转换函数 IMMUTABLE，
  建议先用 `EXPLAIN` 在副本上验证再上。

> 验收：`EXPLAIN` 从 `Seq Scan` 变成 `Index Scan`；前端在 150 条会话时能看到第 101 条。

### P0-2 删除链的证据只到「逻辑层」

`test_consistency_delete_chain.py`（13 例）用 `_FakeVectorStore` / `_Minio` 替身，
断言的是**顺序与失败语义**——这部分做得很好（例如「向量未删成功就不能先删 DB 记录」）。
但它证明不了「真实 Milvus / MinIO 里的东西真的没了」。

现有 `consistency_audit.py` 证明的是**当前状态一致**，不是**删除动作有效**。
两者差别在事故里很关键：一个删除漏掉向量，只要还没跑对账，审计也是绿的。

**方案**：新增 `scripts/delete_drill.sh`（与 `restore_drill.sh` 同思路，约 60 行）：

1. 建临时 KB → 上传固定内容的小文档（带唯一标记）→ 等 chunk 完成；
2. 记录基线：`documents` 行数、Milvus `count(*)`（用 `query` 而非 `num_entities`，它含软删标记）、
   MinIO 对象数、Redis `rag:semcache:{user}:*` 键数；
3. 删除文档与 KB；
4. 断言：四处**全部回到基线**（尤其 Milvus 用 `count(*)` 查，别用 `num_entities`）；
5. 失败时打印残留的 id 列表，退出码非 0。

这条做完，「删得干净」才从「代码逻辑对」升级为「现场验证过」。

### P1-3 「错误可判定」从未被真实事故验证

生产日志里 `severity=` 出现 **0 次**——6 条 WARNING 全是启动配置警告，没有一条走 `log_exception`。
也就是说，这个项目的第二大卖点目前只有**单测断言**在支撑（93 例），
没有一次真实故障证明「日志真的能判定能否重试」。

**方案**：`scripts/fault_drill.sh`，在隔离栈里注入三类故障，检查日志与用户可见行为：

| 注入 | 期望日志 | 期望用户侧 |
|---|---|---|
| Milvus 不可达 | `severity=reportable` + `error=MilvusException`（可重试） | 503 或明确错误，不返回「没有相关内容」 |
| 打分模型抛异常 | `severity=reportable`，**不得**降级为 0 分 | 不出现「检索无结果」这种误导 |
| 代码 bug（`KeyError`） | `severity=programming` + 堆栈 | 500 + `rid`，可凭 rid 定位 |

关键判据：**「分数为 0」与「打分失败」在日志里必须可区分**——这是最容易混的一对。

### P1-4 `health/ready` 不含 Ollama，「healthy」不等于「能回答」

`main.py:801`：`_collect_health_checks(include_ollama=False)`。
刻意的设计（Ollama 在 compose 外，纳入会导致容器重启风暴），但有副作用：
**Ollama 挂掉时 11/11 容器仍全绿**，而所有问答都会失败。

**方案**（不破坏容器语义）：
- 保留 `/health/ready` 四项连通性；
- 新增 `/health/e2e`（管理员 + 限流）：真实问一句固定问题，返回端到端耗时与是否命中；
- 把它接进现有的夜间任务（`nightly.yml`），红了就说明「容器健康但业务不可用」。

### P2-5 依赖豁免 2026-11-30 到期，没有自动提醒

`.trivyignore` 登记了 4 条 CVE（filelock 1 条、transformers 3 条），
每条都有原因、攻击面评估和复核日期——这份清单的质量很高，
但**日期写在注释里，没有任何机制在读它**。过期后扫描仍然会静默通过。

**方案**：在 `security-scan.yml` 加一步 10 行的检查：
解析 `.trivyignore` 的「复核窗口」，若早于今天则 `exit 1`，提示「豁免已过期，需复核或升级」。

### P2-6 生产模式靠 `IN_DOCKER` 推断

`IS_PRODUCTION = (APP_ENV == "prod") or IN_DOCKER`。两个方向都有坑：

- Docker 里的**开发栈**也被当成生产（强校验要求强密钥，靠 `start-dev.sh` 生成随机值绕过）；
- 裸跑（非 Docker）**忘设 `APP_ENV=prod` 就没有强校验**——文档写了这个坑，但靠人记。

**方案**：启动日志第一行打印判据（`APP_ENV=... IN_DOCKER=... IS_PRODUCTION=... 强校验=开/关`），
并在 `prod` 意图下（`APP_ENV` 缺失但 `IN_DOCKER=false` 且存在 `.env.prod`）打印显式警告。
成本 10 行，收益是「以后不会再猜」。

### P2-7 测试的真实覆盖边界

1371 处 `monkeypatch`/`Mock`/`patch`，120 个测试文件。CI 里另有真 PG 的集成测试（1784 passed），
所以**数据库层是真的**；但 **Milvus / MinIO 的真实行为仍然只有演练脚本覆盖**（不是每次 CI 都跑）。

这不是缺陷，是边界。要做的是**在文档里把这条边界写清楚**（现在 README 的「怎么验证」已列了演练脚本，
但没说「CI 不跑这些」）。

### P2-8 文档里的「判断句」与「实测句」混在一起

「万级会话下可接受」是一个**未测量的判断**，却和「hit@5 = 0.869」这种实测数字并列。
读者无法区分。这是元问题：项目的可信度建立在「可验证」上，一旦判断句混进去，整体可信度就被稀释。

**方案**：约定两条写法——
- 实测数字后必须带口径（命令或报告链接）；
- 未测的判断必须显式标注 `（未测，待验证）`。
可选：CI 里加一条 grep，禁止 `docs/api.md`、`docs/guide/*.md` 出现无标注的「可接受 / 性能足够 / 不会成为瓶颈」等词。

## 3. 项目的真实长板与短板

**长板（不是自我评价，是有证据的）**

1. **静默失败的治理深度**：删除四类落点 + 三方对账 + 三级异常分级 + 断连续传，
   这四件事的组合在个人项目里很少见。
2. **证据链文化**：`docs/benchmarks/` 里连「加了重排反而更差」这种对自己不利的结论都留着。
3. **运维演练的工程化程度**：4 个演练脚本能在一条命令内起隔离实例、验完自动清理，
   这件事比「写一份备份文档」难一个量级。

**短板（同一套标准下的）**

1. **在线路径的规模从未被测过**：备份恢复测到 915MB，但「100 个用户同时查 1 万条会话」
   一次都没测。索引缺失就是这个空白的直接后果。
2. **证据分布不均**：组件层（Milvus/MinIO/PG 各自）很强，系统层（整条链路）只有手工冒烟。
3. **单机形态锁死**：9 处进程内状态决定了它只能是单副本；这不是缺陷，是定位，
   但它意味着「容量」这条路基本走不通——而「企业级」三门槛里差的正是容量。

## 4. 建议的执行顺序

| 顺序 | 动作 | 成本 | 为什么先做 |
|---|---|---|---|
| 1 | 加 `(user_id, updated_at DESC)` 索引 + 前端分页 | 半天 | 唯一影响真实使用的功能缺口，且改完立刻可验 |
| 2 | `delete_drill.sh` | 半天 | 补第一卖点的端到端证据，与已有 4 个演练脚本同构 |
| 3 | 修正 `docs/api.md` 里「万级可接受」这句话 | 10 分钟 | 先把已知的假声称改成「未测」 |
| 4 | `fault_drill.sh` | 一天 | 补第二卖点的真实性验证 |
| 5 | `/health/e2e` + 接夜间任务 | 半天 | 解决「绿着但不能用」 |
| 6 | 豁免到期检查、启动判据日志 | 各 10 行 | 低成本防回归 |

1 和 2 做完，这份报告里的 P0 就清零了。3 是可以现在就改的。

---

## 附录：本报告的复现命令

```bash
# 索引与执行计划（只读）
docker exec postgres-prod psql -U prod_user -d app_prod -c \
  "EXPLAIN (ANALYZE, COSTS OFF) SELECT id FROM sessions
   WHERE user_id='x' AND CAST(messages AS TEXT) ILIKE '%k%' ORDER BY updated_at DESC LIMIT 100;"
docker exec postgres-prod psql -U prod_user -d app_prod -tAc \
  "select indexname from pg_indexes where tablename='sessions'"

# 三方对账（只读，退出码 0/1/2）
docker exec backend-prod python scripts/consistency_audit.py --sample 5

# 日志里的凭证与分级标记
docker logs backend-prod 2>&1 | grep -cE "eyJ[A-Za-z0-9_-]{20,}|rag_[A-Za-z0-9]{20,}|password="
docker logs backend-prod 2>&1 | grep -c "severity="

# 前端是否分页（0 = 没有）
grep -rn "skip\|limit\|loadMore" frontend/src/views/HistoryView.vue frontend/src/queries/chat.ts
```
