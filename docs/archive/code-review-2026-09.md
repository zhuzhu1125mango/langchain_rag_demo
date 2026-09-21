# 代码审查报告（2026-09）

> ⚠️ 弃用注记（2026-09）：报告中的 SearXNG 组件已迁移至 Tavily，下述 SearXNG / `SEARXNG_*` 复盘内容存留为历史。
> 状态：一次性审查报告，归档封存
> 后续：§1.3 的 6 项中 1、4、5、3 已修复并验证（见 **§10**）；文档漂移已全量回写（见 **§11**）；
> P0-2 与 P0-6 待处理
> 审查日期：2026-09-16
> 审查范围：`backend/src/`、`backend/tests/`、`frontend/src/`、`docs/`、`scripts/`、`docker-compose*.yml`、CI 与部署配置（不含 `frontend/dist`、`.venv`、`node_modules`）
> 审查基线：工作区当前状态（HEAD = `d20ec93`，含 18 个已修改文件 + 24 个未跟踪文件）
> 审查方式：静态工具实测 + 全量测试执行 + 逐文件读码，所有结论均标注证据来源

---

## 0. 阅读须知：结论的证据等级

本报告对每条结论标注证据等级，避免"看起来像问题"被当作问题：

| 标记 | 含义 |
|---|---|
| **[实测]** | 已由本机命令运行复现，附命令与输出 |
| **[读码]** | 已定位到具体 `file:line` 并逐行确认逻辑，未运行动态复现 |
| **[推断]** | 基于读码的合理推断，未完全验证，需人工确认 |

凡未标注者均不写入本报告。

---

## 1. 执行摘要

### 1.1 项目概况

| 项 | 数值 |
|---|---|
| 后端源码 | 32,163 行（`backend/src/`，约 150 个模块） |
| 后端测试 | 15,641 行 / 82 个测试文件 / 982 个用例 |
| 前端源码 | 11,587 行（`.vue` + `.ts`） |
| 文档 | 8,269 行 / 21 篇 Markdown |
| 后端路由 | 106 条，分布于 16 个路由模块 |
| 技术栈 | FastAPI + LangChain + Milvus + PostgreSQL + Redis + MinIO + SearXNG + Ollama；前端 Vue 3 + TS + Pinia + vue-query |

这是一个**功能覆盖面远超同类 Demo、工程化投入明显高于平均水平**的项目：双环境（dev/prod）容器编排隔离、Prometheus/Grafana/Alertmanager 监控栈、多阶段 CI、JSONB 会话存储、Alembic 迁移、Prometheus 指标埋点、完整的 RAG 决策管线与意图路由、LLM-Wiki 编译层、语义缓存、Agent 有界循环、A/B 实验框架。

但本次审查发现的**问题密度同样很高**，且呈现出一个清晰的模式：

> **项目在"横向功能铺开"上投入巨大，但在"纵向一致性收口"上明显欠账。**
> 同一件事往往有 2~4 种实现（分页、错误处理、确认弹窗、脚本、测试写法）；新子系统上线后旧的横切关注点（鉴权、错误码、文档、测试基线）没有回头统一。

### 1.2 总体评分

| 维度 | 评分 | 一句话结论 |
|---|---|---|
| 功能完整度 | ★★★★☆ | 覆盖面广，但存在"看起来能用、实际失效"的功能（见 P0-1/P0-2/P1-F1） |
| 架构设计 | ★★★★☆ | 分层清晰、降级链完整；`rag_chain.py` 已越过合理体量边界 |
| 安全 | ★★☆☆☆ | **对象级授权存在系统性失效，3 处跨租户越权** |
| 代码规范 | ★★☆☆☆ | lint 三件套（black/isort/flake8）全部不过，1888 项告警 |
| 命名规范 | ★★★★☆ | 后端整体克制统一；API 路径命名与响应结构不统一 |
| 接口规范 | ★★☆☆☆ | 分页四套并存、路径风格混用、错误码契约形同虚设 |
| 文档规范 | ★★★☆☆ | 文档量惊人且索引规范，但**与代码大面积脱节，含"照做必错"内容** |
| 测试体系 | ★★★☆☆ | 982 用例、覆盖广；但**测试用 mock 掩盖了真实缺陷**，且有卡死用例 |
| 部署运维 | ★★★☆☆ | 编排设计专业；**但生产栈存在 2 个阻断级缺陷，告警链路完全静默** |
| 前端 | ★★★☆☆ | 类型纪律优秀、流式实现扎实；**功能级失效与内存泄漏并存** |

### 1.3 必须优先处理的 6 件事

按 **影响 × 修复成本** 排序，前 3 项建议当日修复：

| # | 问题 | 影响 | 修复成本 |
|---|---|---|---|
| 1 | `validate_kb_ownership` 归属校验形同虚设 | **任何登录用户可检索他人知识库全文** | 改 1 行 SQL |
| 2 | `experiments` 表无 `owner_id` + 端点零归属校验 | **任意用户可增删改他人实验** | 模型+迁移+manager（约 1 天） |
| 3 | `/knowledge_bases/recommend`、`/knowledge_graph` 无归属校验 | **任意用户可读取他人知识库内容与图谱** | 加参数 + 复用校验（约 2 小时） |
| 4 | `.env.prod` 有一行键值被注释吞掉 | **`./scripts/start-prod.sh` 直接中止，生产栈无法启动** | 改 1 行 |
| 5 | `frontend/nginx.conf` 无 `client_max_body_size` | **生产环境 >1MB 文件上传必然 413** | 加 1 行 |
| 6 | `wiki_compiler` 向量与页面对齐错位 | **增量编译把内容写进错误页面（静默数据污染）** | 改 1 行 |

---

## 2. 实测数据（可复现）

所有命令在项目根目录或 `backend/`、`frontend/` 下执行，Windows 11 + Git Bash + Python 3.12.10。

### 2.1 后端

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试 | `pytest -q --ignore=tests/test_structured_chunking.py` | ✅ **868 passed, 103 skipped, 0 failed**（75.05s） |
| 单元测试（完整） | `pytest -q` | ⚠️ **卡死**：停在 `test_structured_chunking.py::test_word_elements_loading_and_split`，>5 分钟无输出，`timeout 120s` 强制终止 |
| 格式检查 | `black --check src tests` | ❌ **194 files would be reformatted, 20 unchanged** |
| 导入排序 | `isort --check-only src tests` | ❌ 25+ 文件报 "Imports are incorrectly sorted" |
| 静态检查 | `flake8 src --count --statistics` | ❌ **1888 项**（明细见下） |

`flake8` 明细（按类型）：

| 计数 | 规则 | 说明 |
|---|---|---|
| 1202 | E501 | 行过长（>79，但项目实际按 black 的 88 执行，属规则未对齐） |
| 477 | W293 | 空行含空白字符 |
| 68 | F401 | 导入未使用 |
| 45 | E402 | 模块级 import 不在文件顶部 |
| 23 | W292 | 文件末尾缺换行 |
| 19 | E302 | 类/函数定义前空行不足 |
| **1** | **F821** | **未定义名称（真实 Bug，见 P1-B1）** |
| **1** | **E722** | **裸 `except:`** |
| 2 | F811 | 重复导入 |
| 8 | F841 | 局部变量赋值未使用 |

> 注：项目 `pyproject.toml` 声明了 `black==26.5.1` / `isort==8.0.1` / `flake8==7.3.0` 作为 dev 依赖，但**三者均未配置到任何检查流程中**（无 `[tool.black]` 段、无 `setup.cfg`/`.flake8`、CI 不执行）。即：格式规范"写在依赖里，没有落在流程里"。

### 2.2 前端

| 检查项 | 命令 | 结果 |
|---|---|---|
| 类型检查 | `npx vue-tsc -b` | ✅ exit 0 |
| ESLint | `npx eslint .` | ❌ **exit 1**：1 error（`ChatView.vue:174` 未使用变量）+ 1 warning |
| 单元测试 | `npx vitest run` | ✅ **9 files / 43 tests passed**（3.95s） |

构建产物（`frontend/dist/assets`，2.8MB）：

| 文件 | 大小 |
|---|---|
| `ChatView-*.js` | **1,100,364 B（1.10 MB）** |
| `index-*.js` | **1,094,233 B（1.09 MB）** |
| `index-*.css` | **423,719 B（424 KB）** |
| `KnowledgeBaseView-*.js` | 62,877 B |

`vite.config.ts` 无 `build.rollupOptions.output.manualChunks`，Element Plus 全量引入 + `highlight.js` 全语言注册（`MarkdownRenderer.vue:26`），首屏需下载 2 个 1MB 级 chunk。

---

## 3. P0 严重问题

### P0-1 对象级授权完全失效：`validate_kb_ownership` 从不校验 owner **[读码，已逐行确认]**

**证据**

```python
# backend/src/utils/validators.py:45-65
async def validate_kb_ownership(
    db: AsyncSession, kb_ids: List[str], current_user: CurrentUser
) -> None:
    """校验知识库 ID 列表全部属于当前用户。"""       # ← 文档字符串如此声称
    if not kb_ids:
        return
    result = await db.execute(
        select(KnowledgeBase.id).filter(
            KnowledgeBase.id.in_([uuid.UUID(k) for k in kb_ids])   # ← 没有 owner_id 过滤
        )
    )
    owned = {str(row) for row in result.scalars()}
    if len(owned) < len(kb_ids):
        raise HTTPException(status_code=403, detail="无权访问部分知识库")
```

`current_user` 参数在整个函数体内**从未被引用**。查询只判断"知识库是否存在"，第 64 行的比较等价于存在性检查。函数名、docstring、模块 docstring（`validators.py:5` "拒绝访问他人知识库（对象级授权）"）三处声称的语义与实现完全不符。

**调用点（6 处，覆盖问答主链路）**

| 位置 | 用途 |
|---|---|
| `src/api/chat.py:94` | `POST /api/chat/messages` 非流式问答 |
| `src/api/chat.py:236` | `POST /api/chat/stream` 流式问答 |
| `src/api/chat.py:537` | 快捷问题建议 |
| `src/api/chat.py:795` | 文档/知识库相关查询 |
| `src/api/document.py:1274` | 更新文档时校验目标 KB |

下游无兜底：`milvus_service.py:688/745` 的过滤表达式只有 `kb_id in [...]`，Milvus 层面无 owner 概念；`KnowledgeBase` 表的 `owner_id` 只在 API 层的 `_get_owned_kb`（`wiki.py:78`）等个别位置校验。

**影响**：任何已登录用户，只要拿到（或猜到/遍历到）他人的知识库 UUID，即可对该知识库提问、检索全文、获取引用原文。这是**跨租户数据泄露**，且发生在本系统的核心功能路径上。

**为什么测试没发现**：`tests/test_kb_id_validation.py:79-88`

```python
owned = SimpleNamespace(scalars=lambda: [uuid.UUID(VALID_ID), uuid.UUID(VALID_ID_2)])
db = AsyncMock()
db.execute = AsyncMock(return_value=owned)      # ← 直接伪造查询结果
await validate_kb_ownership(db, [VALID_ID, VALID_ID_2], user)   # 必然通过
```

测试断言的是 mock 的返回值，不是真实 SQL 的行为。CI 全绿。

**修复建议**

```python
select(KnowledgeBase.id).filter(
    KnowledgeBase.id.in_([uuid.UUID(k) for k in kb_ids]),
    KnowledgeBase.owner_id == current_user.user_id,        # 补这一行
)
...
if set(owned) != set(kb_ids):                              # 同时改用集合比较
    raise HTTPException(status_code=403, detail="无权访问部分知识库")
```

并补一条**真实数据库**的回归测试（标记 `@pytest.mark.integration`），断言 A 用户查 B 用户 KB 返回 403。

---

### P0-2 `experiments` 表无 `owner_id`，实验模块零归属校验 **[读码]**

**证据**

- `backend/src/models/experiment.py`：`Experiment` / `ExperimentVariant` / `TrafficAllocation` / `ExperimentMetric` / `ExperimentResult` 五张表，**全文无 `owner_id` 列**（已 grep 确认）。
- `backend/src/api/experiment.py`：全部 11 个端点只挂 `dependencies=[Depends(get_current_user)]`（`main.py:371`），无任何资源归属校验。
- `backend/src/services/experiment_manager.py:25/120/152/180`：`create_experiment` / `get_experiment` / `list_experiments` / `delete_experiments` 等函数签名中**没有 owner 参数**。
- `backend/alembic/versions/8cb6e5b283ec_align_schema_with_models_owner_id_not_.py`：只给 `documents` / `feedbacks` / `knowledge_bases` 补了 `owner_id NOT NULL`，**未处理 experiments**。

值得注意的是，该迁移对应的提交信息为「fix(backend): 实验表模型对齐 JSONB 并补 owner_id 非空/实验外键约束」——commit message 声称补了 owner_id，实际补的是 `documents`/`feedbacks`/`knowledge_bases` 三表，实验表本身被漏掉。

**影响**：任意登录用户可列出、读取、启动、停止、删除、调整流量分配的**全部**他人实验。`GET /api/experiments` 直接返回全量数据。

**修复建议**：`Experiment` 加 `owner_id`（FK → `users.id`，NOT NULL）+ 新迁移（含存量回填）+ `experiment_manager` 全链路加 owner 过滤 + API 层复用 `require_owner`。

---

### P0-3 知识库推荐与图谱接口无归属校验 **[读码]**（已于 2026-09-16 修复，见 §10.5）

**证据**

```python
# backend/src/api/knowledge_base.py:653（修复前）
@router.post("/recommend", response_model=List[KBRecommendationResponse])
async def recommend_knowledge_bases(request: KBRecommendationRequest):     # ← 无 current_user
    ...
    recommendations = await rag_chain.recommend_knowledge_bases(request.question, request.top_k)

# backend/src/api/knowledge_base.py:679（修复前）
@router.post("/knowledge_graph", response_model=KnowledgeGraphResponse)
async def generate_knowledge_graph(request: KnowledgeGraphRequest = Body(...)):   # ← 无 current_user
    ...
    result = await rag_chain.generate_knowledge_graph(request.kb_ids)      # ← kb_ids 原样透传
```

**两个端点的失效方式不同，需分别说明**（初版本报告曾把两者混为一谈为"都接受客户端 kb_ids"，此处更正）：

- `/recommend`：请求体 `KBRecommendationRequest` **只有 `question` 和 `top_k`，不含 kb_ids**。真正的问题是 `KBRecommender.recommend_knowledge_bases` 调用 `_retrieve_documents(question, kb_ids=None)`（`kb_recommender.py:56`），而检索层把 `None` 当作"不限定范围"——于是推荐结果是在**全部用户**的知识库上检索得出的，泄露他人知识库的 `kb_id`、相关性分数与命中片段数。
- `/knowledge_graph`：`kb_ids` 确为客户端可控，原样下传给 `generate_knowledge_graph`（`knowledge_graph_generator.py:40`），**无任何归属校验**；不传时同样走全量检索分支。

**影响**：任何登录用户可获取他人知识库的推荐结果与知识图谱（节点标题、文档名、关联关系），属内容级泄露。

**修复时的关键陷阱（已踩过并规避）**：检索层的判断是

```python
# kb_retrieval_service.py:78 与 milvus_service.py:687 同构
if kb_ids and len(kb_ids) > 0:
    search_kwargs["kb_ids"] = kb_ids
```

**空列表是假值**，会被跳过，最终 `_build_filter_expr` 返回 `None` → Milvus 全量检索。因此"用户没有知识库时传空列表"这种看似自然的写法**反而会造成全量泄露**，必须在 API 层短路返回。


---

### P0-4 `.env.prod` 单行键值被注释吞掉，`start-prod.sh` 启动即中止 **[实测]**

**证据**

```
# .env.prod:96
SEARXNG_SECRET_KEY=# SearXNG 会话加密密钥（必填强随机字符串）
```

该行缺少换行，注释被拼进了值里。bash 将 `VAR=# 注释文字` 解析为「临时赋值 + 执行命令 `SearXNG`」。

实测复现：

```bash
$ printf 'SEARXNG_SECRET_KEY=# SearXNG 会话加密密钥\n' > /tmp/src.env
$ bash -c 'set -euo pipefail; source /tmp/src.env; echo "REACHED"'
/tmp/src.env: line 1: SearXNG: command not found
$ echo $?
127
```

`scripts/start-prod.sh:11` 有 `set -euo pipefail`，且 `:187`（`run_security_checks`）与 `:223`（`load_env_vars`）两处 `source "$ENV_FILE"` → **脚本在「2/7 安全检查」阶段即退出，永远不会执行到 `docker compose up`**。

PowerShell 侧表现相反但同样错误：`start-prod.ps1:195/236` 的正则 `(.*)$` 会把整段注释文字设为环境变量；而 compose 的变量插值优先取 shell 环境（高于 `--env-file`），导致 searxng 容器把这串中文注释当作会话加密密钥。

**修复建议**：清空该行注释、填入真实随机值；并把 `SEARXNG_SECRET_KEY`、`REDIS_PASSWORD` 加入 `start-prod.sh:57` 的 `REQUIRED_VARS`（当前只校验 5 项，正是漏检根因）。

---

### P0-5 生产前端上传必然 413：nginx 未设 `client_max_body_size` **[读码/实测]**

**证据**

- `frontend/nginx.conf`：全文件**无 `client_max_body_size`**（已 grep 确认），nginx 默认上限 `1m`。
- `frontend/src/utils/axios.ts:13`：`baseURL: '/api'` → 上传请求经 `/api/` location 反代到后端。
- `frontend/src/components/knowledge-base/UploadDocumentDialog.vue:188`：`api.post('/documents/upload', formData)`。
- 后端 `backend/src/config.py:133`：`MAX_UPLOAD_SIZE_MB: int = 100`。

即前后端约定 100MB，中间 nginx 卡在 1MB。**绝大多数 PDF/Word 文档都超过 1MB**，生产环境上传功能实际不可用；且返回的是 nginx 的 413 HTML 页面，不是后端 JSON 错误结构，前端错误处理会失配。

**修复建议**：`client_max_body_size 100m;`（与 `MAX_UPLOAD_SIZE_MB` 对齐）+ `proxy_request_buffering off;`。

---

### P0-6 `ContextEnhancer` 摘要为进程级共享可变状态 → 跨用户串话 **[读码]**

**证据**

```python
# rag_chain.py:352（单例 _async_init 内）
self.context_enhancer = ContextEnhancer(...)          # RAGChain 是进程级单例

# rag_chain.py:1678（每次请求的 _finalize）
await self._update_conversation_summary(history)      # 写入 context_enhancer 的实例字段

# context_enhancer.py:32/47/62
self.conversation_summary = ...                        # 实例属性，被所有请求共享
if not history:
    return self.conversation_summary                   # 无历史时直接注入"别人的"摘要
```

`RAGChain` 经 `AsyncSingleton` 保证进程内唯一，`ContextEnhancer` 作为其属性同样唯一。并发请求会互相覆盖 `conversation_summary`，而 `context_enhancer.py:47/62` 会把它注入**其他会话**的提示词。

项目已用 `ContextVar` 修过同类问题——`rag_chain.py:63-72` 有明确注释：「RAGChain 是进程级单例，此前将决策结果挂在 self 上，并发请求会互相覆盖（串号）」。此处是同一类问题的遗漏。

**影响**：A 用户会话摘要进入 B 用户上下文（信息泄露 + 答非所问）；且每轮请求在流末尾多一次 LLM 往返，拖慢 SSE 收尾。

**修复建议**：按 `session_id` 隔离（`dict[session_id, summary]` + LRU 上限），或直接移除该机制。

---

### P0-7 Wiki 增量编译把内容写进错误页面 **[读码]**

**证据**

```python
# backend/src/services/wiki_compiler.py:306-313
for pos, i in enumerate(pending_idx):
    best_score, best = 0.0, None
    for j, page in enumerate(list(remaining)):        # ← j 是 remaining 的实时下标
        score = _cosine(cand_vecs[pos], page_vecs[j]) # ← page_vecs 是循环前算好的定长列表
        ...
    if best is not None and best_score >= TITLE_MATCH_THRESHOLD:
        matched[i] = best
        remaining.remove(best)                        # ← 删除后，j 与 page_vecs 索引错位
```

`page_vecs` 在循环外一次性算出，与 `remaining` 的**初始**顺序对齐；循环内 `remaining.remove(best)` 改变了列表，但 `page_vecs` 不变。第一次命中后，后续候选都在与"错位的向量"比较相似度 → 匹配到错误既有页并静默合并，全程无日志。

**影响**：知识库增量编译时，新文档内容可能被合并进无关页面，造成**静默的数据污染**（不报错、不告警）。

**修复建议**：改为配对消费，保持向量与页面同步：

```python
pairs = list(zip(list(remaining), page_vecs))
for pos, i in enumerate(pending_idx):
    best_score, best = 0.0, None
    for page, vec in pairs:
        score = _cosine(cand_vecs[pos], vec)
        ...
    if best is not None and best_score >= TITLE_MATCH_THRESHOLD:
        matched[i] = best
        pairs = [(p, v) for p, v in pairs if p is not best]
```

---

### P0-8 计算器误路由并短路整条决策管线 **[读码]**

**证据**

```python
# intent_router/constants.py:31-34
CALCULATION_KEYWORDS = {
    "计算", "等于", "是多少", "+", "-", "*", "×", "÷", "/", "%",
    "换算", "兑换", "汇率", "天后", "天前",
}

# intent_router/__init__.py:99-101
@staticmethod
def is_calculation_question(question: str) -> bool:
    return any(kw in question for kw in CALCULATION_KEYWORDS)   # 裸子串匹配

# intent_router/__init__.py:299-307（分支顺序早于 312 行的规则冲突检测）
if self.is_calculation_question(question):
    return IntentDecision(primary_mode=PrimaryMode.TOOL_FIRST,
                          suggested_tools=["calculator"], rule_hit=True, ...)

# tools/plugins/calculator_tool.py:93
cleaned = re.sub(r"[^\d+\-*/().%\s]", "", cleaned)   # 中文被整段删除
```

触发条件极宽：`"-"`、`"%"`、`"是多少"` 任一出现即命中。`_safe_eval` 的清洗会删掉所有非数学字符，使剩余片段仍能成功求值。

**示例推演**：「100美元是多少人民币」→ 命中 `"是多少"`（早于汇率工具规则）→ `TOOL_FIRST['calculator']` → 清洗后得 `"100"` → 返回 `计算结果：100`，`success=True`。`rag_chain._stage_tool_first`（`1019-1040`）据此直接流式作答并置 `finished=True`，知识库检索与 `exchange_rate` 工具都不会执行。

**影响**：用户拿到看似正确、实为截断的无意义数值。这是**面向用户的答案正确性缺陷**，且因为 `rule_hit=True` 绕过了 LLM 裁决，没有二次拦截。

**修复建议**：计算类关键词收紧为「必须同时含数字与运算符」，或把 `is_calculation_question` 改为按 token 判定的正则；`_safe_eval` 对含中文/字母的原串直接拒绝而非静默清洗；把该分支移到规则冲突检测之后。

---

### P0-9 四个端点把 403/404 吞成 500，鉴权语义丢失 **[读码]**

**证据**

`src/api/document.py` 中 `_get_owned_document`（:90，会抛 400/404/403）被直接放在 `try` 内，但 except 子句**没有** `except HTTPException: raise`：

| 端点 | 行号 | except 子句 |
|---|---|---|
| `POST /{doc_id}/reprocess` | 1082（调用）/ 1128 | `except ValueError` + `except Exception` ❌ |
| `POST /{doc_id}/classify` | 1151（调用）/ 1194 | 同上 ❌ |
| `POST /{doc_id}/quality` | 1217（调用）/ 1225 | 同上 ❌ |
| `POST /documents/duplicate-detect` | 1466（调用） | 同上 ❌ |

对照：`upload`（:618）、`batch_upload`（:717）、`update_status`（:1280）**是**写对了的——都有 `except HTTPException: raise`。说明这是四处遗漏，不是设计取舍。

**影响**：越权访问返回 `500 "…失败，请稍后重试"` 而非 `403`；不存在的文档返回 500 而非 404。前端无法区分「无权限」「不存在」「服务故障」三种情况，且 500 会污染错误率监控。

**修复建议**：四个端点统一补 `except HTTPException: raise`（建议抽成装饰器或中间件，避免第六次遗漏）。

---

### P0-10 内部异常细节回显给客户端（绕过全局脱敏） **[读码]**

**证据**

`src/main.py:315-329` 的全局异常处理器刻意只返回 `"Internal Server Error"`，并注释说明详情仅进日志。但以下端点自行 `raise HTTPException(500, detail=str(e))`，**绕过了这层脱敏**：

- `src/api/learning.py:47, 73, 84, 109, 128, 158`（6 处）
- `src/api/experiment.py:44, 57, 70, 88, 106, 124, 148, 169, 182, 195, 213`（11 处）

`str(e)` 可能包含 SQL 片段、MinIO 对象路径、数据库连接串、内部堆栈信息。同一批文件中还有 `except Exception: raise` 的补丁式写法（`learning.py:85/103/121/145/210`），说明作者已意识到问题但未覆盖全部。

**修复建议**：统一改为「日志记录 `str(e)` + 返回固定文案」，或将 `detail` 收敛为错误码枚举。这也正好是 `AppException` 层次（`src/exceptions.py`，见 P2-4）本该承担的职责。

---

## 4. P1 中等问题

### 4.A 安全与授权

| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| P1-A1 | 反馈越权写入（先写后校验） | `chat.py:675-677` 先 `db.add(feedback)` + `commit()`，`:689` 才 `require_owner(session.user_id, current_user)` | 可对他人会话写入反馈；属主不符时数据已落库、响应却是 403（部分写入） |
| P1-A2 | WebSocket 频道无用户维度 | `notification.py:99` 频道名由客户端任意指定；`notification_service.subscribe`（`:66`）是全局 `channel → {ws}` 集合，无按连接过滤 | JWT 用户订阅 `doc:{他人kb_id}` 即可收到他人变更通知 |
| P1-A3 | 上传进度接口无归属校验 | `document.py:1394` `GET /upload/progress/{upload_id}` | 任意登录用户可查询他人任务的文件名与进度 |
| P1-A4 | JWT 无法撤销 | `auth.py:101-119` 注释「不查库（无会话表，删号后 token 自然过期失效）」 | 删号/改密后 token 仍有效至过期；`ACCESS_TOKEN_EXPIRE_MINUTES` 默认 1440（24h）且无 refresh 机制 |
| P1-A5 | 遗留数据放行 | `auth.py:274` `if owner_id and owner_id != current_user.user_id` | `owner_id` 为 NULL 的资源对**任意**登录用户开放（docstring 称为向后兼容） |
| P1-A6 | 名称唯一性无 owner 维度 | `tag.py:54`、`category.py:58` | 跨用户抢占同一标签/分类名；`category.py:63` 的 `uuid.UUID(data.parent_id)` 不在 try 内 → 非法 parent_id 返回 500 |
| P1-A7 | 全局配置权限尺度不一 | `learning.py:76/131/175/184` 改全局学习引擎只需登录；而 `config.py:89/154` 同类全局配置用 `require_admin` | 普通用户可篡改全局学习引擎配置 |

### 4.B 架构与并发

| # | 问题 | 证据 |
|---|---|---|
| P1-B1 | **未定义名称（真实运行时 Bug）** | `chat.py:292` `return generated_title` —— `generated_title` 只在 `send_message`（`:173`）定义，不在 `stream_answer` 作用域。会话被并发删除时抛 `NameError`，被 `:474` 的 `except Exception` 吞掉 |
| P1-B2 | 串行化了本可并行的 IO | `milvus_service.py:582-583` dense/sparse 两路 `await` 串行（同文件 `:633` 的 `search_hybrid_multi` 却用了 `gather`）；`_convert_sparse_embeddings`（`:364`）未走 `to_thread`，CPU 密集操作在事件循环内执行 |
| P1-B3 | 事件循环内同步加载模型 | `web_search_service.py:302` `CrossEncoder(...)` 由 `:355` 直接调用（`hybrid_search.py:129` 用了 `to_thread`）→ 首次搜索阻塞整个服务数秒至数十秒；`_load_model` 无锁，并发首调重复加载 |
| P1-B4 | 每次请求新建 `httpx.AsyncClient` | `web_search_service.py:516/611`、`_weather_impl.py:75/115`、`exchange_rate_tool.py:264/298`、`gold_price_tool.py:323/358` —— 无连接复用、重复 TLS 握手 |
| P1-B5 | `AsyncSingleton` 初始化失败后复用半成品 | `async_singleton.py:70-76`：`_async_init()` 抛错时实例已存入 `_instances` 且 `_initialized=False`，下次复用**同一**未完成实例重跑 init → `milvus_service.py:58` 重新 `AsyncMilvusClient(...)` 而不关闭旧 client（连接泄漏） |
| P1-B6 | 客户端断连丢失全部收尾 | `rag_chain.py:1671-1679` `_finalize`（trace 落盘、语义缓存写入、Agent 记忆写入、摘要更新）只在生成器被消费到底时执行；SSE 消费方提前关闭则全部跳过 |
| P1-B7 | fire-and-forget 任务未持引用 | `trace_collector.py:148` `asyncio.create_task(self.save_async())` 无引用保留；同仓库 `rag_chain.py:75/1155` 的 `_background_store_tasks` 是正确写法 —— 两种写法并存 |
| P1-B8 | 上下文预算 `break` 而非 `continue` | `context_builder.py:236`：一个装不下的来源直接终止，高 rank 长片段可饿死其余全部来源 |
| P1-B9 | `rag_chain.py` 体量失控 | 1991 行 / 60+ 方法；混合了管线编排、模板常量、缓存、Agent、评估等 6 类职责 |

### 4.C 接口与一致性

| # | 问题 | 证据 |
|---|---|---|
| P1-C1 | **`AppException` 是死代码** | `src/exceptions.py` 定义了 5 个异常类 + `main.py:298` 有处理器，但 `grep -rn AppException src/api/` **零命中**。全部端点用裸 `HTTPException`，处理器一律回 `error_code: "HTTP_ERROR"` → 错误码契约形同虚设 |
| P1-C2 | 分页四套并存 | `skip/limit`（`feedback.py:126`、`badcase.py:135`）、`page/page_size`（`knowledge_base.py:203`、`document.py:953`）、`limit/offset`（`trace.py:64`）、仅 `limit`（`document.py:774`）；`document.py:863` 与 `session.py:153` **完全不分页**（全量返回 + joinedload） |
| P1-C3 | 路径/方法风格混用 | `trace.py:59` `@router.get("")` → `/api/traces`，其余集合端点用 `"/"`；因 `redirect_slashes=False`（`main.py:282`），`/api/traces/` 与 `/api/documents` 均 404。批量删除三种写法：`POST /documents/batch/delete`、`POST /knowledge_bases/batch-delete`、`DELETE /experiments/batch`。路径 snake/kebab 混用（`knowledge_bases` vs `batch-delete`、`duplicate-detect`）。所有 DELETE 返回 200 + `{"message"}` 而非 204 |
| P1-C4 | 裸 dict body + 死模型 | `document.py:1336` `body: dict = Body(...)`，无 schema、无 `ids` 类型校验；同文件 `:147` 定义的 `BatchDeleteRequest` 从未被引用 |
| P1-C5 | 单文档详情回显 ORM 对象 | `document.py:1232-1239` `return doc` 直接返回 SQLAlchemy 对象，无 `response_model` → 回显 `file_path`（`minio://…`）、`owner_id`、`quality_details` 等内部列，与同文件 `DocumentResponse` 不一致 |
| P1-C6 | 流式/非流式错误码不一致 | `chat.py:254-256` 会话不存在时返回 **200 + SSE error 事件**；同场景非流式 `:105` 返回 404 |
| P1-C7 | 分页参数静默钳制 | `document.py:953-963` `page_size < 10` 被静默改为 500，而非 `Query(ge=, le=)` 校验 |
| P1-C8 | 健康检查与指标匿名可访问 | `main.py:427` `/metrics`、`:394` `/health/detail` 无鉴权，暴露内部指标与依赖拓扑 |

### 4.D 前端

| # | 问题 | 证据 |
|---|---|---|
| P1-D1 | **"猜你想问"永不触发（功能失效）** | `ChatView.vue:321-322` 先调 `fetchSuggestions()` 再调 `fetchKBRecommendations()`，两者**共用同一个 `debounceTimer` 变量**（`:170`）；后者无条件 `clearTimeout`（`:327-328`）并注册自己的定时器 → `/api/chat/suggestions` 从不发出。旁证：`e2e/fixtures.ts:231` 为该接口写了 mock 却从未被调用 |
| P1-D2 | **乐观删除写入无人读取的缓存键** | `KnowledgeBaseView.vue:473` `setQueryData(['documents'], ...)`，而实际键为 `['documents', effectiveParams]`（`queries/kb.ts:166`）→ 删除后表格仍显示该文档 |
| P1-D3 | **DOMPurify 全局 hook 泄漏** | `MarkdownRenderer.vue:55/64` 在 `onMounted` 内 `DOMPurify.addHook(...)`，全局单例、**从不 remove**（未导入 `onUnmounted`）→ 每条助手消息挂载一次，N 条消息 = N 个重复 hook，每次 sanitize 跑 N 遍且永不回收 |
| P1-D4 | SSE 流写入目标用"最后一条消息" | `ChatView.vue:469` 每条事件取 `messages[length-1]`；流未结束时切换会话会把后续内容追加到最后一条历史消息；组件无 `onBeforeUnmount` abort |
| P1-D5 | WebSocket 卸载后仍自动重连 | `useNotifications.ts:110-123/135-145`：`disconnect()` 清 timer 后 `ws.close()`，但 `onclose` 仍会 `scheduleReconnect()`（非 1008）→ 3s 后重新建连 |
| P1-D6 | "取消上传"不取消 | `UploadDocumentDialog.vue:305-323` 只关 WS + 置标志，for 循环无取消判断，仍继续上传并在结束覆盖状态；`api.post` 无 AbortController |
| P1-D7 | 请求重试实际只有 1 次 | `utils/axios.ts:65-77`：`shouldRetry` 含 `!config._retryCount`，首次失败写入 1 后第二次直接 reject，`MAX_RETRIES=3` 永不可达（注释与实现不符） |
| P1-D8 | 设置页三个开关是假的 | `GeneralView.vue:229-259` 把 `typingEffect/autoScroll/maxInputLength` 存 `localStorage['rag-settings']`，**全仓仅此文件出现该 key**；`:250` `JSON.parse` 无 try/catch |
| P1-D9 | spinner 永远转 | `LearningView.vue:136` `:class="{'animate-spin': triggerMutation.isPending}"` 漏 `.value`（模板拿到 ref 对象恒真）；同文件其他地方都写了 `.isPending.value` |
| P1-D10 | 知识图谱重复请求 | `KnowledgeBaseView.vue:177` 每次渲染新建 `[currentKB.id]` 数组 + `KnowledgeGraph.vue:501-503` 对该 prop `deep:true` watch → 父组件每次重渲染都打一次接口 |
| P1-D11 | 事件回调中调用 `useQuery` | `ExperimentView.vue:495` 在方法内调用 `useExperimentResult()`，创建无 scope 回收的 observer |
| P1-D12 | 分组标题渲染出 UUID | `KnowledgeBaseSelector.vue:133-144` 用 `kb.group_id` 同时做 key 和显示名；`queries/kb.ts:81` 本有 `group_name` |

### 4.E 部署与工程化

| # | 问题 | 证据 |
|---|---|---|
| P1-E1 | **告警链路完全静默** | `configs/alertmanager/alertmanager.yml:19` `receiver: 'webhook'` 但 `:27-29` 的 `webhook_configs` **全部被注释** → 所有告警被丢弃 |
| P1-E2 | 告警规则无效 | `configs/prometheus/alerts.yml:60` 引用的 `milvus_query_latency_seconds_*` 不是合法 Milvus 指标名（实际为 `milvus_proxy_sq_latency_*`）→ 规则恒不触发；`:17` `rate(...) > 0.05` 语义是「每秒错误数」而非注释所称「错误率 5%」 |
| P1-E3 | Grafana 无 dashboard | `configs/grafana/` 只 provision 了 datasource |
| P1-E4 | CI 无 lint / 无镜像构建 / 无漏洞扫描 | `.github/workflows/ci.yml`：不跑 ruff/mypy；两个 Dockerfile 从未在 CI 构建（P0-5、P1-E6 类问题不会被拦住）；无 pip-audit/pnpm audit，无 `.github/dependabot.yml`；无 `concurrency:`；无 `permissions:` 收紧 |
| P1-E5 | 配置一致性校验存在系统性缺口 | `scripts/check_env_consistency.py:61-104` 只校验「`.env.example` ⊇ (`.env.dev` ∪ `.env.prod`)」与「compose `${}` ⊆ env 文件」，**完全不读 `config.py`** → 23 个字段在 `.env.example` 中零提及（`ALLOWED_ORIGINS`、`ANSWER_*`、`CITATION_*`、`INTENT_ROUTER_*EMBEDDING*`、`OLLAMA_NUM_CTX`、`QUERY_REWRITE_*`、`MILVUS_FLUSH_*`、`MODEL_TASK_ROLES` 等），CI 全绿 |
| P1-E6 | 镜像非最小化、以 root 运行 | `backend/Dockerfile.prod:7-45` 单阶段、无 `USER`，`build-essential/gcc/g++` 留在运行镜像；`COPY --from=ghcr.io/astral-sh/uv:latest` 用 `latest` 漂移（其余镜像均钉日期 tag） |
| P1-E7 | `IS_PRODUCTION` 把"在容器里"等同于"生产" | `config.py:403-408` 未设 `APP_ENV` 时回退 `IN_DOCKER`，而 `docker-compose.dev.yml:174` 设了 `IN_DOCKER:"true"` → **开发容器按生产校验**：`auth.py:297` 使 dev 容器 `/config/reset`、`/metrics/reset` 一律 403；`auth.py:276` 的 default 用户兼容路径失效 |
| P1-E8 | `ALLOWED_ORIGINS` 无法按 .env 惯例覆盖 | 该字段为 `List[str]`，pydantic-settings 对复杂类型走 JSON 解析：设 `ALLOWED_ORIGINS=http://a.com,http://b.com` 会抛 `SettingsError` → CORS 生产环境实际只能用 `config.py:98` 的 localhost 默认值 |
| P1-E9 | 迁移存在生产风险 | `8cb6e5b283ec...py:30/45/48` 对 `documents/feedbacks/knowledge_bases.owner_id` 直接 `SET NOT NULL`，只清理了 4 张 experiment 表的孤儿行（`:24-28`），**未回填 NULL 的 owner_id** → 存量库有 NULL 行时迁移中途失败；且部分 DDL 已生效后重跑会报「已存在」 |
| P1-E10 | 迁移执行时机与文档矛盾 | `docker-compose.yml:10` 文档写直接 `docker compose up -d`，而迁移在 `start-prod.sh:332` 于 compose 启动**之后**手动 exec → 按文档走则永不跑迁移，只靠 `init_db(create_all)` 建表（与 Alembic 形成双写） |
| P1-E11 | 三套启动脚本行为漂移 | `start-dev.sh:332` 在 `set -e` 下 `wait_for_health` 返回 1 会立即退出，使 `:335-342` 的「部分服务未就绪」提示成为不可达代码（`.ps1:347` 无此问题）；`start-dev.ps1:314/317` **明文回显** `POSTGRES_PASSWORD`/`MINIO_ROOT_PASSWORD`（sh 版只回显库名）；`start-dev.ps1:150` 正则不剥离行内注释，而 `.env.dev` 有 6 行带行内注释 → PowerShell 进程内环境变量值会带上注释尾巴 |
| P1-E12 | `stop-*.sh` / `logs-*.sh` 未带 `--env-file` | 与 `start-*.sh` 不一致，可能操作到错误的 compose 项目 |

### 4.F 数据与算法正确性

| # | 问题 | 证据 |
|---|---|---|
| P1-F1 | 天气城市名正则吞掉时间前缀 | `_weather_impl.py:47-49` 首条正则 `(.{2,10}?)` 惰性展开把时间词并入城市名（`今天上海天气` → `今天上海`）；`intent_router/__init__.py:266` 只用"非空"判定 → `服务器温度过高怎么办` 被判为 `tool_first['weather_query']` |
| P1-F2 | 数值指标推断传全文导致大面积误报 | `numerical_validator.py:205` 传 `content`（全文）而非 `window` 给 `_infer_metric`；`_infer_metric`（`:159-165`）取**第一个**命中指标 → 同一来源所有数字贴同一 metric，`:252` 按 `(metric, unit)` 分组把无关数值凑成一组 → 大量假冲突 |
| P1-F3 | 指代消解破坏问题文本 | `conversation_context.py:141/151-155` 对单字代词（这/那/该/其/此）用 `replace(p, topic, 1)` **无边界**替换 → `应该怎么处理` → `应北京天气怎么处理`，且被 `rag_chain.py:978-985` 用作实际检索 query |
| P1-F4 | 指标基数无界 | `middleware/metrics.py:51/95` 用 `request.url.path` 原样作 label 与字典键 → `/api/documents/{uuid}` 每个 UUID 生成一条时间序列，`endpoint_stats` 无限增长 |
| P1-F5 | 临时文件泄漏 | `document_processor.py:510-514` `download_file` 抛错时 `delete=False` 的临时文件永不清理（清理逻辑在其后的 `load_document` finally 内） |
| P1-F6 | 过期硬编码年份 | `web_search_service.py:217` 返回 `"2024 OR 2025"` 拼进查询（当前 2026 年，反而偏向旧结果） |
| P1-F7 | DB 连接池硬编码且无 pre_ping | `database.py:17-24` `pool_size/max_overflow/pool_timeout/pool_recycle` 全为字面量；缺 `pool_pre_ping=True`（PG 重启后首个请求拿死连接） |
| P1-F8 | 主搜索路径无结果级缓存 | `web_search_service.py:812` `build_search_context_enhanced` 不读写 context 缓存（只有旧接口 `:752-756` 有）→ 相同问题每次重跑 LLM 改写 + N 路搜索 + N 次抓取 |
| P1-F9 | 缓存服务一次异常即永久降级 | `cache_service.py:140-190` 任一异常即置 `_available=False`，仅 `/health/detail` 能恢复 |

---

## 5. P2 轻微问题

### 5.1 死代码与死配置

| 位置 | 说明 |
|---|---|
| `utils/validators.py:137`（`auth.py:137`）`validate_uuid_user_id` | 定义后**全仓零引用**（已验证） |
| `milvus_service.py:834` `reconnect` | 零引用；且不关闭旧 client |
| `notification_service.py:78` `send_personal_message`、`:87` `ConnectionManager.broadcast`（空实现 `pass`） | 零引用 |
| `minio_service.py:142` `get_file_url_async`、`:152` `file_exists_async`、`:164` `upload_text`、`:176` `download_text` | 零引用 |
| `document_processor.py:717` `save_uploaded_file` | 无调用方，且 async 内同步调 MinIO；`document.py:38` 还导入未使用（F401） |
| `rag_chain.py:245` `last_reasoning` | 只初始化从不赋值 → `get_last_reasoning()` 恒返回空 |
| `rag_chain.py:426` `_get_retriever` | 恒返回 None |
| `config.py:148` `KB_RELEVANCE_SCORE_THRESHOLD`、`:158` `CONTEXT_COMPRESSION_ENABLED` | 配置项全项目零引用，而 `rag_chain.py:243` 硬编码 `similarity_threshold = 0.4` |
| `document.py:147` `BatchDeleteRequest` | 定义后从未被引用 |
| `queries/chat.ts:188/208` | 以 `use*` 命名却返回普通函数（误导） |
| `stores/chat.ts:37` `isLoadingQuickQuestions` | 只声明从不赋值 |
| `ChatView.vue:174` `pendingReasoningUpdate` / `reasoningRafId` | 死代码：`requestAnimationFrame` 从未调用，注释声称的"同帧合并 reasoning"不存在（**这正是 ESLint error 的来源**） |
| `llm_router.py:81-90` | 手写单例，`_lock` 为死代码 |

### 5.2 规范与一致性（后端）

- **重复导入**：`document.py:31` 与 `:59` 重复导入 `settings`；`:33` 与 `:167` 重复导入 `_schedule_semantic_cache_invalidation`（F811×2）。
- **裸 `except:`**：`knowledge_graph_generator.py:112` `except: continue`（E722）。
- **未使用变量**：`document.py:882/976/998`、`knowledge_base.py:670/696`、`middleware/metrics.py:73`、`rule_learner.py:389`（F841×8）。
- **`== True` 比较**：`document.py:538/651`、`knowledge_base.py:287`（E712）。
- **`e` 变量未使用**：多处 `except Exception as e:` 后未引用 `e`。
- **魔法数**：`tool_manager.py:179 timeout=30.0`、`document_processor.py:220 max(chunk_size, 2000)`、`context_enhancer.py:154 history[-5:]`、`output_sanitizer.py:239 tolerance=low_confidence_threshold/12`、`ChatView.vue:340 top_k:3`、防抖 500/800/3000ms。
- **`output_sanitizer.py:119-122`** 用带符号 `closest` 作分母（`:471` 才是 `abs(closest)`，0 值判 inf）。
- **`gold_price_tool.py:218`** `_extract_currency` 未 lower → 大写货币码静默回落 CNY。
- **`exchange_rate_tool.py:93`** `Decimal("0")` 为假值 → amount 从 0 变 1。
- **`_stream_with_retry`**（`rag_chain.py:1734-1739`）切到 `llm_direct` 后仍上报 `OLLAMA_MODEL_NAME` 指标（`:1773`）→ Prometheus 归因错误。
- **分层倒置**：`intent_router/__init__.py:47` 直接 import 工具插件私有实现 `_extract_city_name` / `is_datetime_question`，而 `tool_manager.py:242` 又反向懒 import `intent_router` → 双向依赖。

### 5.3 规范与一致性（前端）

- 重复实现：`WikiDrawer.vue:376 formatDate` 与 `utils/format.ts:12`；`ReasoningPanel.vue:139` 与 `utils/reasoning.ts:11` 同逻辑两份；`ChatView.vue:585` 用裸 `fetch` 绕开 axios 并重复实现鉴权头注入（`:417-425`）。
- 交互三套并存：原生 `confirm()`（`HistoryView.vue:163/173`、`KnowledgeBaseView.vue:459/517/686`）、`ElMessageBox`、`useCrudModal`；提示两套并存：`ElMessage` 与 `useToast`。
- 非空断言：`ExperimentView.vue:411-421`（4 处 `variants!`）、`WikiDrawer.vue:267/235/372`、`CompareAnswerPanel.vue:81/141`、`DocumentSourceModal.vue:141`。
- `v-for key=index`：`ChatInputArea.vue:88`、`CompareAnswerPanel.vue:81/122/141`、`TraceView.vue:122/141/153`、`DocumentAnalysisDialogs.vue:124`。
- 错误态缺失或误导：`KnowledgeGraph.vue:460-462` 失败时落入空态分支显示"暂无图谱数据，请先上传文档"；`ExperimentView` 未用 `isLoading` → 加载中即显示"暂无实验"。
- 边界 bug：`DocumentSourceModal.vue:145` 用真值判 `highlight_offset`，`offset=0` 时不显示高亮；`HistoryView.vue:164/176` 删除 `mutate()` 无 `onError` → 静默失败。
- 硬编码外链：`MessageSources.vue:156` 硬编码 `google.com/s2/favicons`（外网依赖 + 域名外泄）。
- 设置项：`eslint.config.ts:22` 用 `flat/essential`（非 `recommended`）；`no-console` 未开 → 源码 9 处 `console.log` 会进生产。

### 5.4 仓库卫生

- `backend/.ab_check.py`、`backend/_debug_router.py`、`backend/agent_ab_*.json`、`backend/run_B_plan.json` 等 24 个未跟踪文件（不含本报告）既未提交也未加入 `.gitignore`，易误提交；根目录另有 9 个 `agent_sse_*.log` 调试残留（`.gitignore` 的 `*.log` 已覆盖）。
- 无 `CHANGELOG` / `CONTRIBUTING` / API 变更记录。

---

## 6. 分维度深度评价

### 6.1 架构设计 ★★★★☆

**做得好的**：

- **分层清晰**：`api/`（路由，不写业务）→ `services/`（业务）→ `models/`；`utils/`、`middleware/` 职责单一。
- **RAG 管线已做阶段化重构**：`rag_chain.py` 把编排拆为 `_stage_intent` / `_stage_decide` / `_stage_kb_retrieval` / `_stage_generate` / `_finalize` 等 stage 方法 + `_PipelineState` 数据类 + `_StageTimer` 计时器，比早期的大函数可读得多。
- **降级链完整**：混合检索失败回退 dense；联网搜索失败不影响 KB 回答；工具失败降级 LLM 直答；语义缓存 Redis 不可用 fail-open；wiki 编译失败不阻断上传。
- **并发正确性的自觉**：`rag_chain.py:63-72` 明确记录了"曾用 `self` 挂决策结果导致并发串号"并改用 `ContextVar`；`_background_store_tasks`（`:75/1155`）持引用防 fire-and-forget 被 GC —— 这是很多项目会踩的坑。
- **统一单例抽象**：`AsyncSingleton` 双重检查锁 + 子类维度隔离 + `reset_instance` 测试友好。

**不足**：

- `rag_chain.py` 1991 行已越过合理边界，混合了管线编排、提示词模板常量、缓存写入、Agent 调用、评估五类职责；且 `__init__` 未声明全部属性（`context_builder`/`query_rewriter` 仅在 `_async_init` 赋值），中途失败会 `AttributeError`。
- 横切关注点没有统一收口：鉴权（P0-1/2/3）、错误处理（P0-9/10）、响应结构（P1-C5）各写各的，导致同一类缺陷反复出现。
- `AppException` 层次设计出来了却没落地（P1-C1），说明"设计了但没执行"是这个项目的典型模式。

### 6.2 代码规范 ★★☆☆☆

工具链齐全（black/isort/flake8 都在 `pyproject.toml` 的 dev 依赖里），**但没有一个被接进流程**：无配置文件、无 pre-commit、CI 不执行。结果是 1888 项 flake8 告警、194 个文件不符合 black 格式，同时夹杂 1 个真实 Bug（F821）、1 个裸 except、2 个重复导入——**真实缺陷被淹没在格式噪声里**，这正是"有工具不用"的代价。

好消息是代码里 **0 处 `print`**、**0 处 TODO/FIXME/HACK**、`except Exception: pass` 仅 8 处且多数有明确注释说明是有意的（如 `cache_service.py:98` "忽略关闭过程中的异常"）——说明开发者本人是有纪律的，缺的是自动化门禁。

### 6.3 命名规范 ★★★★☆

- 后端模块/函数/变量命名整体克制、语义清晰，无中英混用（注释全中文、标识符全英文，符合惯例）。
- 前端 `<script setup lang="ts">` 100% 统一，无 Options API 混用；0 处 `any`、0 处 `@ts-ignore`、仅 1 处 double cast。
- **扣分项集中在 API 层**：路径 snake/kebab 混用（`knowledge_bases` vs `batch-delete` vs `duplicate-detect`）；`POST /api/evaluate/...` 用动词作资源前缀；批量删除三种命名法。

### 6.4 接口规范 ★★☆☆☆

106 条路由中，**分页 4 套、路径尾斜杠 2 套、批量操作 3 套、DELETE 状态码 1 套（全 200）、错误体 1 套（但错误码枚举与文档不符）**。文档承诺的错误码（`UNAUTHORIZED`/`DATABASE_ERROR`/`SERVICE_ERROR`/`TIMEOUT`/`SEARCH_ENGINE_ERROR`）在代码中全部不存在。

亮点是**边界校验的收敛**：`utils/validators.py` 的 `parse_uuid_list` 显式消除了 Milvus 过滤表达式注入面；`document.py:90` 的 `_get_owned_document` 与 `wiki.py:78` 的 `_get_owned_kb` 把「400 非法 ID / 404 不存在 / 403 非 owner」统一到一处，思路正确——只是被 P0-9 的 except 子句破坏。

### 6.5 文档规范 ★★★☆☆

**形式上堪称模板级**：`docs/README.md` 定义了目录职责、命名规则（小写 kebab-case）、设计文档状态枚举（草稿/待确认/部分实施/已实施/已归档）、双向同步要求；21 篇文档 8269 行，`design/` 每篇都有 `> 状态：` 头部；`archive/` 有归档规则。

**内容上大面积脱节**（以下均已交叉验证）：

| 文档 | 问题 |
|---|---|
| `api.md:1035` | 自称"14 个路由模块、核对至 2026-09-01" → 实际 **16 个模块 / 106 条路由**；auth（3 条）、trace（2 条）、wiki（4 条）共 **9 条接口全文零记录** |
| `api.md:864-867` | **WS 鉴权写反**：文档称"握手 URL 以 `?api_key=` 携带"，代码（`auth.py:191-221`）明确握手不校验、由首帧 `{"type":"auth"}` 认证——正是为了"凭据不落 URL/日志" |
| `api.md:161-174` | SSE 帧描述与实际不符：代码只发 `data:` 无 `event:` 行；漏 `reasoning`/`thinking`/`title` 三类事件 |
| `api.md:649` | 记录 `GET /api/learning/status`，代码中不存在 |
| `api.md:935-967` | 错误体写 `{detail, code, timestamp}`，实际 `{error_code, detail, request_id}`；6 个错误码枚举全部不存在 |
| `architecture.md:37` | "MinIO/Milvus 连接失败超过 30 秒则暂停启动（fail-fast）" —— **代码中无此逻辑**（`main.py` lifespan 只预热 CacheService/VectorStore/Chain，不检查 MinIO/Milvus） |
| `architecture.md:17` | 模型清单写 `deepseek-r1:7b` + `qwen2.5:7b`，实际 `.env.dev` 为 `qwen3:4b` + `bge-m3` + `bge-reranker-v2-m3` |
| `architecture.md:91` | "auth.py：API Key（主模式）+ JWT 占位" —— JWT 已完整落地（users 表 + register/login/me） |
| `architecture.md:82/87` | `api/` 清单缺 auth/trace/wiki/config/category/tag；工具插件清单缺 `kb_search`/`wiki_lookup`（实际 9 个） |
| `architecture.md` 全文 | 完全未提已落地的**语义缓存**、**LLM-Wiki 编译层**、**Trace** 三个子系统 |
| `deployment.md:70` | "两环境使用相同端口（5433/8000/8080 等），同一时间只能运行一个环境" —— **与实际相反**，`docker-compose.yml` 端口已全部错开（backend 8001、prometheus 9094、alertmanager 9095、grafana 3001、postgres 5434、redis 6380、minio 9002/9003、milvus 19531），注释明写"与开发栈错开" |
| `deployment.md:254-263` | 生产访问地址写 8000/5433/9001/19530/9090/9093/3000，实际全部不同 → **照做必配错** |
| `deployment.md:106/406/413` | 教用户 `copy .env.dev .env` —— 该文件不存在（compose 走 `env_file: .env.dev/.env.prod`） |
| `deployment.md:286-315` | 把容器名（`backend-prod`）填进"服务"列 → `docker compose restart backend-prod` 不可执行 |
| `README.md:411` | 列 `.env`"环境变量配置（自动切换）"，该文件不存在 |
| `README.md:448` | `src/dependencies.py` 不存在 |
| `README.md:35-49` | 技术栈版本全部过期（FastAPI ^0.115.0 → `==0.136.3`、Ollama ^0.2.10 → 0.6.2、Redis ^7.0 → 8.0.0 等） |
| `README.md:640-676` | `CHUNK_SIZE` 512/`TOP_K` 1-10 等默认值错误（实际 500、1-20）；`SEARCH_ENABLE_FUNCTION_CALLING` 默认值写反（false → 实际 True）；同文 `:651` 与 `:564/:629` 对 `APP_ENV` 的取值自相矛盾 |
| `docs/README.md:38-48/55-67` | 漏收 `agent-ab-evaluation.md`、`agent-evolution.md`（文件实际存在） |
| `docs/README.md:61`、`price-trustworthiness.md:7` | 指向 `backend/src/tools/`，**该路径不存在**（实际 `backend/src/services/tools/plugins/`） |
| `design/search-optimization.md:5` | 状态"待确认（未实施）"，但 `query_rewriter.py`、`citation_backfiller.py`、`search_postprocessor` 均已落地并接入 `rag_chain.py:344` |
| `design/agent-evolution.md:3` | 状态"设计稿（未实施）"，但 `agent_orchestrator.py`、`kb_retrieval_service.py` 已落地，文档 §9 已有实施记录；`:396` "L1-a 待实施" 实际已实施（`config.py:289-291`、`rag_chain.py:450/475`） |
| 多篇 design 文档 | 引用了 10+ 个不存在的文件路径（`backend/prompts/intent_router_examples.jsonl`、`backend/scripts/diag_latency.py`、`scripts/migrate_wiki_pages.py` 等） |

**测试基线三处互斥**：`development.md:31` 写 "552 passed/97 skipped"，`archive/code-review-2026-08.md:189` 写 "552/13/84"，`llm-wiki-compile.md:633` 写 "828 passed/101 skipped"，而**本次实测为 868 passed / 103 skipped**（排除一个卡死用例）。

> 这一节的密度说明：文档不是"写得少"，而是"写完之后没有回写机制"。`docs/README.md` 反复强调"代码变更时必须同步更新"，但没有任何自动化手段去保证——这正是 CI 里缺一个"文档漂移检查"的地方。

### 6.6 测试体系 ★★★☆☆

**规模可观**：82 个测试文件 / 982 用例 / 15641 行，测试代码与源码比约 **0.49:1**，对个人项目而言是相当高的投入。覆盖了意图路由、混合检索、语义缓存、wiki 编译、JWT 鉴权、SSE 重试、并发、编码、Milvus 重建守卫等硬骨头。

**结构规范**：`conftest.py` 用 `--run-integration` / `--run-e2e` 显式区分需外部服务的用例，CI 默认跳过；`integration_client` 用进程级共享 TestClient 解决「异步单例绑定事件循环」问题（有详细注释）；`_reset_async_engine` 处理连接池跨循环污染；Windows 下切 SelectorEventLoop。这些都是踩过坑才写得出来的。

**但存在三个结构性问题**：

1. **Mock 掩盖真实缺陷（最严重）**。`test_kb_id_validation.py:79-88` 直接伪造 `db.execute` 的返回，使 P0-1 这个跨租户越权漏洞在 CI 中完全不可见。同类问题在 `test_kb_id_validation`、`test_update_document_fk` 等归属校验测试中重复出现——**结论：所有断言"归属校验"的单测都应改为真实 DB 的集成测试**。

2. **测试套件会卡死**。`test_structured_chunking.py::test_word_elements_loading_and_split` 调用 `load_document()` → `UnstructuredWordDocumentLoader` → `unstructured` 在首次使用时联网下载 NLTK 数据。本机实测：`timeout 120` 强制终止，进程内 21 个 `SYN_SENT` 连接。**该文件被排除后，全量套件 75 秒跑完并 0 失败**——即卡死完全由这一个用例造成。CI 环境有外网所以侥幸通过，但开发者本地或受限网络下会无限挂起。建议：为该用例加 `@pytest.mark.integration`，或在 conftest 中预热/固定 NLTK 数据目录。

3. **覆盖率未被度量**。`pytest-cov` 在依赖里，但 `pyproject.toml` 无 `[tool.coverage]` 配置，CI 不跑 `--cov`，无阈值门禁。因此"哪些模块没被测"是未知的——例如 `save_assistant_message`（P1-B1 的所在地）就完全没有测试。

### 6.7 功能完善度

**已完成且质量较高的子系统**（读码确认存在且接线）：

意图路由（规则 + embedding + LLM 三层）· 混合检索（BM25 + dense + RRF）· 重排序（Ollama / sentence-transformers 双 provider）· 查询改写 · 引用补全 · 数值幻觉校验 · 输出清洗 · 语义缓存 · LLM-Wiki 编译（5 个 Phase）· 有界 Agent 循环（DECIDE-ACT-OBSERVE）· 工具插件（9 个）· A/B 实验框架 · 学习引擎 · 请求 trace · JWT 多用户认证 · 监控栈 · OCR 深度解析（双后端可选依赖）

**功能层面的缺口**：

| 缺口 | 说明 |
|---|---|
| 无速率限制 | `pyproject.toml` 有 `aiolimiter` 依赖，但未用于 API 层；登录接口无防爆破 |
| 无 refresh token | access token 24h 且不可撤销（P1-A4） |
| 无用户管理界面 | 有 `users` 表与注册/登录，但无用户增删改查端点 |
| 无软删除 | 删除为物理删除，`document.py:433-440` 先删 MinIO 再删库，中途失败不原子 |
| 无 API 版本化 | 所有路由硬编码 `/api`，无 `/api/v1` |
| 无健康检查分层 | `/health` 恒返回 healthy（不检查依赖），`/health/detail` 是真实检查但无鉴权 |
| Grafana 无看板 | 只 provision 了 datasource（P1-E3） |
| 无 CHANGELOG | 有 21 篇文档，无版本变更记录 |

### 6.8 安全

**做得好的**：

- `milvus_service._safe_id`（`:660-677`）UUID 白名单 + `_build_filter_expr` 对 `source_kind` 的字符集校验，双层防 Milvus 表达式注入。
- `calculator_tool` 用 AST 白名单而非 `eval`。
- `fetch_webpage_tool.py:36` 有 SSRF 校验（覆盖云元数据地址/私网/回环）。
- 上传大小按**实测字节数**校验（`document.py:69` 的 `_ensure_upload_size`），不信任客户端声明的 size，并有 2 例单测覆盖（`tests/test_security_hardening.py:119` 正常通过并 seek 回起点、`:126` 超限返回 413）。
- 敏感词过滤在流式/非流式两个入口都前置。
- 生产启动强制 `SECRET_KEY` 强度校验 + 关键凭据非空校验（`main.py:206-223`）。
- 调试脚本已收敛：根目录 24 个 A/B 测试脚本无 `os.getenv("MILVUS_HOST")` 类生产端点硬编码。
- `require_owner`（`auth.py:255`）在 document/kb/wiki/chat/session/feedback 中**确实被调用了 32 处**——P0-1/2/3 是"该调的地方没调"，不是"机制不存在"。

**结构性问题**：鉴权是**逐端点手工挂载**的，没有统一入口。106 条路由 × (是否需要登录) × (是否需要归属校验) 的组合靠人记，必然遗漏——本次发现的 3 个越权点全是这类遗漏。**建议引入 FastAPI 依赖注入的统一切面**（如 `OwnedResource` 依赖类），把"查资源 + 校验归属"变成声明式的一行。

---

## 7. 改进路线图

### 第 1 天（阻断级 + 一行修复）

| 序 | 动作 | 对应 |
|---|---|---|
| 1 | `validators.py:59-64` 补 `owner_id` 过滤 + 集合比较；补真实 DB 回归测试 | P0-1 |
| 2 | `.env.prod:96` 修复键值行；`REQUIRED_VARS` 补 `SEARXNG_SECRET_KEY`/`REDIS_PASSWORD` | P0-4 |
| 3 | `nginx.conf` 加 `client_max_body_size 100m;` + `proxy_request_buffering off;` | P0-5 |
| 4 | `wiki_compiler.py:299-313` 改配对消费 | P0-7 |
| 5 | 4 个端点补 `except HTTPException: raise` | P0-9 |
| 6 | `CALCULATION_KEYWORDS` 收紧 + `_safe_eval` 拒绝含中文原串 | P0-8 |
| 7 | `chat.py:292` 修 `NameError`；`chat.py:675-689` 把 `require_owner` 提到 `commit` 之前 | P1-B1 / P1-A1 |

### 第 1 周

| 序 | 动作 | 对应 |
|---|---|---|
| 8 | `Experiment` 加 `owner_id` + 迁移（含回填）+ manager 全链路过滤 | P0-2 |
| 9 | `recommend` / `knowledge_graph` 注入 `current_user` 并校验 kb 归属 | P0-3 |
| 10 | 清掉 `learning.py` / `experiment.py` 的全部 `detail=str(e)`；让 `AppException` 真正落地 | P0-10 / P1-C1 |
| 11 | `ContextEnhancer` 摘要按 session 隔离或移除 | P0-6 |
| 12 | **接入 lint 门禁**：补 `[tool.black]` / `[tool.isort]` / `.flake8`（E501 调至 88），一次性 `black . && isort .`，CI 加 `--check` | 6.2 |
| 13 | 修 ESLint error（`ChatView.vue:174`）；CI 加 `eslint` 与 `vue-tsc` 门禁 | 2.2 |
| 14 | `alertmanager.yml` 打开 `webhook_configs`；修 `alerts.yml:60` 指标名 | P1-E1/E2 |
| 15 | CI 增加镜像构建验证（两个 Dockerfile） | P1-E4 |

### 第 1 个月

16. 接口规范统一：分页统一为 `page/page_size` + 统一响应包装；路径风格统一；DELETE 改 204。
17. 归属校验切面化：用依赖注入替代逐端点手工挂载（根治 P0-1/2/3 类问题）。
18. `rag_chain.py` 拆分：抽出 `pipeline/`（编排）、`prompts/`（已存在）、`agent/`，目标单文件 < 600 行。
19. 前端：`ChatView` 拆出 `useSSEStream` / `useDebouncedSuggest`；查询键统一常量；DOMPurify hook 提到模块顶层；引入 `manualChunks` + `highlight.js/lib/core` 按需注册。
20. 文档回写机制：把本次报告 §6.5 的差异表作为 TODO 逐条回写；`api.md` 改为从 OpenAPI schema 自动生成（FastAPI 已提供 `/openapi.json`）；CI 加文档漂移检查。
21. 测试补强：归属校验全部改真实 DB 集成测试；修 `test_word_elements_loading_and_split` 的联网依赖；接入 `pytest-cov` + 覆盖率门禁（建议先设 55% 不阻断、只报告）。
22. 可观测性：Grafana 补 dashboard；`middleware/metrics.py` 的 label 改路由模板（`request.scope["route"].path`）而非原始 path。

---

## 8. 做得好的地方（客观记录）

审查不应只列问题。以下均为读码确认：

1. **工程化投入远超同类项目**：双环境容器编排（端口全域错开、`name:` 隔离、`deploy.resources.limits`、`condition: service_healthy`、searxng `cap_drop: ALL`）、前后端 `.dockerignore` 排除密钥、生产栈基础设施端口全绑 `127.0.0.1`。
2. **CI 有真知灼见**：integration job 的注释把"为什么 minio/etcd/milvus 不能用 GHA services"逐条讲清（含镜像 CMD 的核实），并手工 `docker run` 解决；检索评估带阈值卡点；全流程无 `continue-on-error`。
3. **并发正确性的系统性思考**：`ContextVar` 修串号、`_background_store_tasks` 持引用、`AsyncSingleton` 双重检查锁、SSE 重试恢复，都有明确注释说明动机。
4. **降级与容错设计完整**：见 6.1。
5. **配置治理规范**：全量 `pydantic-settings` + `.env.{APP_ENV}` 分层；模型名无内置默认（避免硬编码）；`MILVUS_REBUILD_ON_MISMATCH` 默认 False 以避免静默清空向量库——这个默认值的选择体现了对数据安全的敬畏。
6. **前端类型纪律优秀**：`noUncheckedIndexedAccess` 下 `vue-tsc -b` 零错误；0 `any`、0 `@ts-ignore`；每个组件带完整 JSDoc。
7. **前端流式实现扎实**：用 `fetch` + `POST` 替代 `EventSource`（问题不进 URL/日志）、`TextDecoder` 增量缓冲按 `\n\n` 切事件、`end` 后等 `title` 再关流、后端 `message_id` 回填。
8. **前端安全意识到位**：markdown-it `html:false` + DOMPurify 标签白名单 + 链接 scheme 校验；`utils/url.ts` 校验 http(s) 才 `window.open`；WS 鉴权走首帧而非 URL query。
9. **代码卫生习惯好**：0 处 `print`、0 处 TODO/FIXME、`except: pass` 仅 8 处且多数有注释说明为有意为之。
10. **文档形式规范**：`docs/README.md` 的目录职责/命名/状态枚举/双向同步规则，是很多团队做不到的。

---

## 9. 结论

这个项目的能力上限很高，实现质量在**"已经想到的地方"是扎实的**——降级链、并发、SSRF、注入防护、容器化隔离，这些往往要踩过坑才会做的东西都做到了。

问题集中在另外一侧：**横切关注点靠人记，没靠机制**。

- 鉴权靠逐端点手工挂载 → 3 处跨租户越权（P0-1/2/3）
- 错误处理靠逐端点写 except → 4 处 403 变 500、17 处异常细节泄露（P0-9/10）
- 格式规范靠依赖声明 → 1888 项 flake8 告警淹没 1 个真实 Bug
- 文档靠手工同步 → 与代码大面积脱节，含"照做必错"的部署端口
- 测试靠 mock → 断言 mock 而非行为，越权漏洞 CI 全绿

因此本报告的首要建议不是"多写代码"，而是**把已经写好的规范接进自动化流程**：lint 门禁、归属校验切面、文档从 OpenAPI 生成、归属校验改真实 DB 测试。这四件事做完，同类缺陷的复发率会显著下降。

同时必须指出：**P0-1 / P0-2 / P0-3 是三个真实的跨租户越权漏洞，在多用户部署下等同于数据泄露事故，应视为最高优先级，先于任何功能迭代处理。**

---

## 10. 本次会话已修复项（2026-09-16）

审查后立即修复了 §1.3 中 3 项一行级阻断问题及 1 项配套加固，**每项均已实测验证**。

### 10.1 已修复清单

| 编号 | 文件 | 改动 | 验证方式与结果 |
|---|---|---|---|
| P0-1 | `backend/src/utils/validators.py:56-72` | SQL 补 `KnowledgeBase.owner_id == current_user.user_id`；改为集合比较；复用 `parse_uuid_list` 归一化 | 见 10.2 |
| P0-4 | `.env.prod:95-97`、`scripts/start-prod.sh:56-67` | 拆出注释行 + 生成 64 字符强随机密钥；`REQUIRED_VARS` 补 `SEARXNG_SECRET_KEY`/`REDIS_PASSWORD` | 见 10.3 |
| P0-5 | `frontend/nginx.conf:24-39` | `location /api/` 内补 `client_max_body_size 110m` + `proxy_request_buffering off` | 见 10.4 |
| P0-3 | `backend/src/api/knowledge_base.py`、`src/services/rag_chain.py`、`src/services/kb_recommender.py` | 推荐/图谱的候选范围改由服务端按 owner 解析；图谱显式 kb_ids 走归属校验；空范围短路 | 见 10.5 |

### 10.2 P0-1 授权修复与回归守卫

修复后的函数：

```python
normalized = set(parse_uuid_list(kb_ids))          # 归一化为小写去重集合
result = await db.execute(
    select(KnowledgeBase.id).filter(
        KnowledgeBase.id.in_([uuid.UUID(k) for k in normalized]),
        KnowledgeBase.owner_id == current_user.user_id,   # ← 新增：对象级授权
    )
)
owned = {str(row) for row in result.scalars()}
if owned != normalized:                            # 集合比较，替代长度比较
    raise HTTPException(status_code=403, detail="无权访问部分知识库")
```

两处附带修正：

- **大写 UUID 误判**：DB 中 `str(UUID)` 为小写，若调用方（如 `document.py:1274` 传 `data.kb_id`）传入大写 UUID，单纯的集合比较会把「自己的知识库」判为无权限。故先经 `parse_uuid_list` 归一化。
- **重复 ID 误判**：原 `len(owned) < len(kb_ids)` 在调用方传入重复 ID 时会误判，改为集合相等比较。

**新增回归守卫**：`tests/test_kb_id_validation.py::TestValidateKbOwnershipSql`（2 例）。与原有基于 `AsyncMock` 的用例不同，它捕获真实语句并断言编译后的 SQL 含 `owner_id` 与当前用户 ID——**正是原有 mock 写法遗漏的那一层**。

并已验证该守卫**确实会失败**：临时移除 owner 过滤条件后重跑，得到

```
AssertionError: SQL 未限定 owner_id，对象级授权被绕过:
  SELECT knowledge_bases.id FROM knowledge_bases
  WHERE knowledge_bases.id IN ('123e4567e89b12d3a456426614174000')
1 failed, 1 passed
```

这条 SQL 即修复前的越权查询。随后已恢复修复并确认通过。

**回归结果**：全量套件 **870 passed / 103 skipped / 0 failed**（此前 868 passed，+2 为新增守卫用例），`tests/test_update_document_fk.py` 等复用该函数的测试均未受影响。

### 10.3 P0-4 环境文件修复

```
- SEARXNG_SECRET_KEY=# SearXNG 会话加密密钥（必填强随机字符串）
+ # SearXNG 会话加密密钥（必填强随机字符串）
+ SEARXNG_SECRET_KEY=<新生成的 64 字符 token_urlsafe(48)>
```

验证：

| 检查 | 修复前 | 修复后 |
|---|---|---|
| `bash -c 'set -euo pipefail; source .env.prod'` | **exit 127**（`SearXNG: command not found`） | **exit 0** |
| 7 项必填变量非空校验 | 6/7（`SEARXNG_SECRET_KEY` 值为 `#`，`REDIS_PASSWORD` 未被校验） | **7/7 全部非空** ✓ |
| `bash -n scripts/start-prod.sh` | — | 语法无错 ✓ |
| `python scripts/check_env_consistency.py`（CI 作业） | 通过 | 通过 ✓ |

> 注：生成的是新的随机密钥。若该项目已有 searxng 实例在跑且依赖旧密钥加密的会话，重启后既有会话会失效（SearXNG 侧影响仅为需重新建立会话）。如需沿用旧值请自行替换该行。

### 10.4 P0-5 nginx 上传上限

在 `location /api/` 内新增：

```nginx
client_max_body_size 110m;      # 取 110m 而非 100m：本指令限制整个 multipart 请求体，
                                # 比后端校验的文件字节数多出边界与表单字段开销
proxy_request_buffering off;    # 大文件透传，不在 nginx 落盘缓冲
```

**验证限制（如实记录）**：本机无本地 nginx 镜像，`docker pull nginx:1.29-alpine` 因网络受限停滞（与 §2.1 中测试卡死同源的网络问题），**因此未能执行 `nginx -t`**。已完成的检查仅为：配置文件花括号配对（5/5）、指令字面正确、缩进层级与所在 `location` 块一致。**建议在有 Docker 网络的环境补跑一次 `nginx -t` 再上生产。**

### 10.5 P0-3 知识库范围限定

**改动（4 个文件）**

| 文件 | 改动 |
|---|---|
| `src/api/knowledge_base.py` | 新增 `_owned_kb_ids()` 辅助函数；两个端点注入 `current_user` + `db`；`/recommend` 用服务端解析的 owner 候选范围；`/knowledge_graph` 显式 kb_ids 走 `validate_kb_ownership`、未传时取本人全部 KB；两处空结果均短路返回 |
| `src/services/rag_chain.py` | `recommend_knowledge_bases` 增加 `kb_ids` 透传参数 |
| `src/services/kb_recommender.py` | 增加 `kb_ids` 参数并在**显式空列表**时短路返回 |
| `tests/test_kb_scope_ownership.py` | 新增 6 例回归测试 |

关键实现：

```python
async def _owned_kb_ids(db, current_user) -> List[str]:
    result = await db.execute(
        select(KnowledgeBase.id).filter(KnowledgeBase.owner_id == current_user.user_id)
    )
    return [str(row) for row in result.scalars()]

# /recommend：候选范围由服务端解析，不接受客户端指定
owned_kb_ids = await _owned_kb_ids(db, current_user)
if not owned_kb_ids:
    return []          # ← 必须短路：空列表下传等于全量检索（见 P0-3 陷阱说明）
```

候选知识库由**服务端按 owner 解析**而非过滤客户端输入，因此不存在"过滤遗漏某个字段"的绕过面。

**为什么测试不依赖数据库**：`test_kb_scope_ownership.py` 直接调用路由协程并注入假会话，绕开 TestClient 与 app lifespan，因此无需 PostgreSQL/Milvus/Redis，可随单元测试在 CI 全量运行。既有的 `test_api_knowledge_base.py` 整体标记为 `integration`（本地与 CI 单测均跳过），无法承担这道守卫。

**回归守卫已双向验证**——两项各自撤销后重跑，均按预期失败：

| 撤销的改动 | 失败用例 | 报错 |
|---|---|---|
| `/recommend` 的 `if not owned_kb_ids: return []` | `TestRecommendScope::test_no_owned_kb_short_circuits_without_retrieval` | `AssertionError: 不应在无可访问知识库时发起推荐检索` |
| `/knowledge_graph` 的 `validate_kb_ownership` 调用 | `TestKnowledgeGraphScope::test_foreign_kb_id_rejected` | 未抛 403（用例期望 `HTTPException(403)`） |

随后恢复并确认通过。

**行为变更提示**：`/knowledge_graph` 未传 `kb_ids` 时，原先返回全量知识库图谱，现在返回**当前用户**的图谱；显式传入非本人 `kb_id` 由"返回他人数据"变为 **403**。前者是修复目标，后者可能影响混用 JWT 与 API Key 两种认证方式的部署（同一知识库由 JWT 用户创建、再用 API Key 访问时，`owner_id` 为 UUID 而当前用户为 `api_key_user`，会被拒绝）。该语义与代码中既有 32 处 `require_owner` 调用一致，属向既有约定收敛，但从混用模式迁移的部署需留意。

**回归结果**：全量套件 **876 passed / 103 skipped / 0 failed**（此前 870，+6 为本次新增用例）。前端无需改动——`KnowledgeGraph.vue:457` 与 `queries/kb.ts:446` 的请求体与响应结构均未变化。

### 10.6 第二批修复（2026-09-18）：P0-7 / P0-9 / P1-B1 / P1-A1

| 编号 | 文件 | 改动 | 验证 |
|---|---|---|---|
| P0-7 | `services/wiki_compiler.py:298-318` | `_match_by_embedding` 改用 `(page, vec)` 配对列表 `pairs`，命中后同步 `pairs.pop(best_idx)`，消除 `remaining.remove` 导致的向量下标错位 | 新增 `test_embedding_match_multi_candidate_no_misalignment`（2 候选 + 2 既有页），已验证还原到原始 bug 时该用例失败 |
| P0-9 | `api/document.py` ×4 处 | `reprocess` / `classify` / `quality` / `duplicate-detect` 四个端点补 `except HTTPException: raise`，避免 `_get_owned_document` 的 403/404 被吞成 500 | 语法检查 + 全量回归 |
| P1-B1 | `api/chat.py:292` | `return generated_title` → `return False`（`generated_title` 在 `stream_answer` 作用域不存在，会话被并发删除时抛 `NameError` 被 `except Exception` 吞掉） | 全量回归 |
| P1-A1 | `api/chat.py:666-725` | 反馈端点把会话归属校验 + `require_owner` 提到 `db.add(feedback)` / `commit` 之前，消除"先写入后校验导致的部分写入" | 全量回归 |

**P0-7 修复的关键教训**：第一次尝试用 `zip(remaining, page_vecs)` 修，但 `zip` 在 `remaining.remove` 后仍会错位（`page_vecs` 未同步删除），新增的回归测试立即抓住了这个"修复中的 bug"。正确做法是用独立的 `pairs = list(zip(remaining, page_vecs))` 配对列表，命中后 `pairs.pop(best_idx)` 同步删除。这验证了"修复本身也需要回归守卫"。

### 10.7 尚未修复

§1.3 的 6 项中已有 4 项完成（1、4、5、3），**剩余 2 项**：

- **P0-2** `experiments` 表加 `owner_id` + 迁移（含存量回填）+ `experiment_manager` 全链路过滤 —— 涉及模型、迁移、服务三层，约需 1 天。
- **P0-6** `ContextEnhancer` 摘要按会话隔离 —— 需调整 `RAGChain` 的调用方式，有设计选择（按 session_id 隔离 or 移除该机制）。

§3 中其余 P0（**P0-8** 计算器误路由、**P0-10** 异常细节回显）与 §4/§5 的全部问题**均未改动**。

---

## 11. 文档回写（2026-09-16）

§6.5 的差异表已逐条回写。所有修正均**先取证再落笔**——凡涉及"是否已实现"的判断，均以读码核实为准，未核实的明确标注为待确认而非直接改写。

### 11.1 改动清单

| 文档 | 改动 |
|---|---|
| `docs/api.md` | 附录补齐 9 条缺失接口（认证 ×3 / Trace ×2 / Wiki ×4），并修正模块数与日期；新增「0. 认证接口」「15. Wiki 编译层接口」「16. 请求追踪接口」三章；新增「鉴权」章节（此前全文无鉴权说明）；修正 SSE 事件格式、错误响应格式与错误码表、WS 鉴权方式、文档列表/知识库列表/会话详情/非流式响应的参数与字段 |
| `docs/architecture.md` | 修正模型清单（原为已废弃的 deepseek-r1/qwen2.5）、删除不存在的「MinIO/Milvus 30 秒 fail-fast」约束、JWT 由"占位"改为已落地、补全 `api/` 模块与工具清单、补入语义缓存/Wiki 编译/Trace 三个子系统、`run_in_executor` → `asyncio.to_thread`、补充 lifespan 实际启动顺序 |
| `docs/guide/deployment.md` | **生产端口全部改正**（原文档所载端口无一正确）；删除"两环境端口相同、只能跑一个"的错误结论；新增端口对照表；修正 `.env` 复制流程（该文件不存在）；两张容器表改用 Compose 服务名并补容器名列；修正"对外只暴露 backend(8000)" |
| `docs/guide/monitoring.md` | 生产监控端口改正（9090/3000/9093 → 9094/3001/9095）；标注 Grafana 无 dashboard；修正 `FastAPIHighErrorRate` 的语义描述（实为"每秒错误数"，非"错误率 5%"）；标注 `MilvusHighQueryLatency` 的指标名待实测确认 |
| `README.md` | 技术栈版本全部更新为 `pyproject.toml`/`package.json` 实际值；项目结构中删除不存在的 `.env` 与 `src/dependencies.py`，补全 `docs/` 与 `api/` 清单；模型分工表改为配置驱动并标注无内置默认；`CHUNK_SIZE`/`CHUNK_OVERLAP`/`TOP_K` 默认值与取值范围修正；`APP_ENV` 取值统一为 `prod`；`SEARCH_ENABLE_FUNCTION_CALLING` 默认值 false → true；FC 工具清单修正；功能特性表补入 JWT/OCR/混合检索/语义缓存/Wiki/追踪等已落地能力 |
| `docs/design/search-optimization.md` | 状态 待确认 → **已实施**，并在 §5.1 附逐项落地核对表（含 1 项未实现的 `search_types.py` 字段扩展） |
| `docs/design/agent-evolution.md` | 状态 设计稿（未实施）→ **部分实施**（Phase 1/2 已落地，Phase 3 按 §9.5 暂不启动）；§11 的 L1-a 由"待实施"改为已实施并附核实锚点 |
| `docs/design/rag-enhancement.md` | 16 个勾选框逐项回填（14 项已完成），§9「下一步」中过期的"本方案为设计稿"结论改写为剩余 2 项待办 |
| `docs/README.md` | 索引补入 agent-evolution / agent-ab-evaluation 两篇；修正 `backend/src/tools/` 为实际路径；同步状态表 |

### 11.2 权威性验证

API 附录已用脚本与代码逐一比对：**代码 111 条路由（api/ 106 + main.py 5）= 附录 111 条，缺失 0、虚构 0**。比对脚本用后已删除。

### 11.3 对本报告的两处自我更正

**其一：P1-E1 的描述过重。** 本报告称告警链路"形同虚设"，但 `docs/guide/monitoring.md` §5 早已明确写着「当前状态：仅 webhook 占位，告警只在 Alertmanager UI 展示，不会主动推送」并给出启用方法。这是**已知且有文档记录的设计选择**，不是隐瞒的缺陷。严重度应下调为"需在产品层面决策是否接入通知渠道"。（`configs/prometheus/alerts.yml` 的规则语义问题仍成立。）

**其二：P0-3 的原始描述有误**，已在 §3 更正——`/recommend` 的请求体根本不含 `kb_ids`，两者失效方式不同。

**其三：回写过程中新引入并已修正的错误。** 回写 `api.md` 的 SSE 章节时，我把事件类型列成了 7 种（含一个 `sources` 事件）。复查 `chat.py` 的实际发射点后确认**不存在该事件**——来源信息随 `end` 事件下发，实际只有 6 种。已在 `api.md` 与 `architecture.md` 两处修正。

记录此条是因为它正是本报告批评的模式：**凭印象补全而非逐一核对**。回写过程本身也适用同一条纪律。

### 11.4 未回写的部分

以下差异**未处理**，因涉及判断或需实测，不宜由本次审查单方面改写：

- `docs/design/intent-router-upgrade.md` 等篇内引用的历史文件路径（如 `backend/prompts/intent_router_examples.jsonl`）——多为记录当时状态的实施记录，改写会失真，建议就地加注而非删除
- 各文档中的历史测试基线（"552 passed"等）——属当期快照，本次实测为 **876 passed / 103 skipped**（排除一个卡死用例），差异源于后续迭代，无需回改历史记录
- `MilvusHighQueryLatency` 的实际指标名——需连接 Milvus 的 `/metrics` 才能确认
- `.env.example` 与 `config.py` 的 23 个未文档化字段（P1-E5）——属配置治理问题，需连同 `check_env_consistency.py` 一并改造

---

## 附录 A：审查执行记录

| 命令 | 工作目录 | 结果 |
|---|---|---|
| `pytest -q --ignore=tests/test_structured_chunking.py` | `backend/` | 868 passed, 103 skipped, 0 failed, 75.05s |
| `pytest -q` | `backend/` | 卡死于 `test_word_elements_loading_and_split`，强制终止 |
| `pytest -v tests/test_structured_chunking.py` | `backend/` | 7 passed 后卡死，`timeout 120s` 终止 |
| `black --check src tests` | `backend/` | 194 files would be reformatted, 20 unchanged |
| `isort --check-only src tests` | `backend/` | 25+ 文件报错 |
| `flake8 src --count --statistics` | `backend/` | 1888 项（明细见 §2.1） |
| `npx vue-tsc -b` | `frontend/` | exit 0 |
| `npx eslint .` | `frontend/` | exit 1（1 error, 1 warning） |
| `npx vitest run` | `frontend/` | 9 files / 43 tests passed, 3.95s |
| `bash -c 'set -euo pipefail; source /tmp/src.env'` | — | exit 127，复现 P0-4 |

**环境限制说明**：本机无 Docker 守护进程与 Redis/Milvus/PostgreSQL 服务运行，因此**未执行** `--run-integration` / `--run-e2e` 标记的用例，也**未实际启动**应用访问 `/docs` 或进行端到端问答验证。所有涉及运行时行为的结论均基于代码逻辑与静态复现，已按 §0 的等级标注；部署类结论（P0-5）经 compose/nginx 配置交叉核对而非实际部署验证。

## 附录 B：未验证项（供后续确认）

| 项 | 说明 |
|---|---|
| `/metrics`、`/health/detail` 是否由网关层限制访问 | 本次未检查反向代理/云安全组配置（P1-C8） |
| `deployment.md:161-172` 之外的生产访问地址 | 文档中可能还有未列举的端口错误 |
| `semantic_cache_service._trim` 读-改-写竞态 | 来自子审查标注为未逐行复核（P1-F9 同类） |
| `embedding_classifier` 换模型不失效示例向量缓存 | 同上，标注置信度较低 |
| 前端 E2E（3 条链路）实际执行结果 | 需 `pnpm test:e2e` 与运行中的前后端 |
| `.env.dev` / `.env.prod` 中 `SECRET_KEY` 的实际强度 | 已由启动校验覆盖，未人工复核具体值 |
