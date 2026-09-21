# 📡 RAG 知识库问答系统 API 文档

## 基础信息

- **API 地址**: `http://localhost:8000/api`
- **文档地址**: `http://localhost:8000/docs` (Swagger UI)
- **健康检查**: `GET /health`
- **版本**: v1.0
- **依赖管理**: uv（uv.lock 锁定版本）

### 鉴权

除 `/api/auth/register`、`/api/auth/login`、`/health`、`/health/detail`、`/metrics`、`/` 外，
全部接口都需要认证。支持两种凭据，按以下顺序判定：

| 方式 | 携带位置 | 说明 |
|------|----------|------|
| API Key | `X-API-Key: <API_KEY>` | 自托管单实例模式；身份记为 `api_key_user` |
| JWT | `Authorization: Bearer <access_token>` | 多用户模式；身份为 `users` 表主键。经 `POST /api/auth/login` 获取 |

```bash
# JWT 方式：先登录
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "alice", "password": "..."}' | jq -r .access_token)

curl -X POST http://localhost:8000/api/chat/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"question": "什么是 RAG？"}'
```

补充说明：

- **开发模式放行**：未配置 `API_KEY` 且**非 Docker** 时允许匿名访问，身份记为 `default`
- **管理操作**：`POST /api/config/*`、`POST /metrics/reset` 等还需携带 `X-Admin-Key: <ADMIN_KEY>`；
  生产模式未配置 `ADMIN_KEY` 时这些接口一律 403
- **对象级隔离**：资源（知识库/文档/会话/反馈）按 `owner_id` 隔离，非所有者访问返回 **403**；
  `owner_id` 为空的遗留数据对任意登录用户放行
- **JWT 有效期为 `ACCESS_TOKEN_EXPIRE_MINUTES`（默认 1440 分钟）**，无 refresh token 机制，
  且服务端不查库校验，因此删号/改密后 token 在过期前仍然有效
- WebSocket 走首帧鉴权，见 §11

### API 使用示例

```bash
# 发送消息（非流式）
curl -X POST http://localhost:8000/api/chat/messages \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是 RAG？"}'

# 发送消息并启用联网搜索
curl -X POST http://localhost:8000/api/chat/messages \
  -H "Content-Type: application/json" \
  -d '{"question": "2025 年最新的大模型进展", "use_web_search": true, "search_mode": "simple"}'

# 流式回答（SSE，POST body 传参）
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是RAG"}'

# 流式回答并启用联网搜索
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "2025年AI趋势", "use_web_search": true, "search_mode": "simple"}'

# 创建知识库
curl -X POST http://localhost:8000/api/knowledge_bases \
  -H "Content-Type: application/json" \
  -d '{"name": "我的知识库", "description": "测试知识库"}'

# 上传文档
curl -X POST http://localhost:8000/api/documents/upload \
  -F "file=@document.pdf" \
  -F "kb_id=your_knowledge_base_id"
```

---

## 目录

0. [认证接口](#0-认证接口)
1. [聊天问答接口](#1-聊天问答接口)
2. [知识库管理接口](#2-知识库管理接口)
3. [文档管理接口](#3-文档管理接口)
4. [会话管理接口](#4-会话管理接口)
5. [分类管理接口](#5-分类管理接口)
6. [标签管理接口](#6-标签管理接口)
7. [评价反馈接口](#7-评价反馈接口)
8. [学习引擎接口](#8-学习引擎接口)
9. [A/B测试接口](#9-ab测试接口)（创建 / 列表 / 详情 / 启停 / 流量分配 / 批量删除）
10. [系统配置接口](#10-系统配置接口)
11. [实时通知接口](#11-实时通知接口)（WebSocket）
12. [错误响应格式](#12-错误响应格式)
13. [坏例管理接口](#13-坏例管理接口)
14. [RAG 评估接口](#14-rag-评估接口)
15. [Wiki 编译层接口](#15-wiki-编译层接口)
16. [请求追踪接口](#16-请求追踪接口)

---

## 0. 认证接口

前缀 `/api/auth`。本模块**不挂全局鉴权依赖**（登录/注册必须匿名可用）。

### 0.1 注册

**POST** `/api/auth/register` → **201 Created**

```json
{ "username": "alice", "password": "至少6位" }
```

| 字段 | 约束 |
|------|------|
| username | 3~64 字符，仅允许 `[a-zA-Z0-9_-]` |
| password | 6~128 字符 |

**响应**：
```json
{ "access_token": "<JWT>", "token_type": "bearer", "user_id": "用户UUID" }
```

> 受 `AUTH_ALLOW_REGISTRATION`（默认 `true`）控制；关闭后返回 403。

### 0.2 登录

**POST** `/api/auth/login`

请求体 `{"username": "...", "password": "..."}`，响应同上。
用户名或密码错误统一返回 401，不区分具体原因。

### 0.3 获取当前用户

**GET** `/api/auth/me` → `{ "user_id": "...", "username": "..." }`

---

## 1. 聊天问答接口

### 1.1 发送消息（非流式）

**POST** `/api/chat/messages`

发送问题并获取回答（非流式）

**请求体**:
```json
{
  "question": "用户问题内容",
  "session_id": "可选，会话ID",
  "kb_ids": ["可选，知识库ID列表"],
  "use_web_search": false,
  "search_mode": "simple",
  "deep_thinking": "off"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 用户问题内容 |
| session_id | string | 否 | 会话UUID，不传则创建新会话 |
| kb_ids | array | 否 | 知识库ID列表，指定查询范围 |
| use_web_search | boolean | 否 | 是否启用联网搜索，默认false |
| search_mode | string | 否 | 搜索模式：`simple` / `function_calling` / `agent`，默认 `simple` |
| deep_thinking | string | 否 | 深度思考开关：`on` / `off`，默认 `off`（有校验器，其他取值返回 422） |

**成功响应** (200) —— 固定 7 个字段：

```json
{
  "session_id": "会话UUID",
  "message_id": "消息UUID",
  "answer": "回答内容",
  "sources": ["来源文本列表"],
  "source_metadata": [
    {
      "index": 1,
      "document_id": "文档UUID",
      "filename": "文件名",
      "chunk_index": 0,
      "total_chunks": 10,
      "content": "预览内容...",
      "score": 0.85,
      "source_type": "kb"
    },
    {
      "index": 2,
      "filename": "网页标题",
      "url": "https://example.com/article",
      "content": "网页摘要...",
      "source_type": "web"
    }
  ],
  "answer_type": "knowledge_base",
  "title": "生成的会话标题"
}
```

**响应字段说明**:

| 字段 | 类型 | 说明 |
|------|------|------|
| session_id | string | 会话唯一标识 |
| message_id | string | 消息唯一标识 |
| answer | string | 回答内容 |
| sources | array | 引用的源文本片段 |
| source_metadata | array | 来源元数据详情 |
| answer_type | string | 实际走的回答路径（如 `knowledge_base` / `llm_direct` / `function_calling`） |
| title | string \| null | 会话标题；仅当本次为会话首条消息且生成了新标题时非空，否则为 `null` |
| source_metadata[].source_type | string | 来源类型：`kb` 知识库 / `web` 网页 |
| source_metadata[].url | string | 网页来源 URL（仅 `web`） |

> 该接口**不返回** `llm_calls` / `vector_searches` / `processing_time` 等统计字段；
> 需要调用与耗时数据请使用 `/api/traces` 或 `/metrics`。
| source_metadata[].filename | string | 文件名或网页标题 |
| answer_type | string | 回答类型：`knowledge_base`、`llm_direct`、`web_search`、`hybrid_search` 等 |
| title | string | 首次发送消息时生成的会话标题，后续消息可能为空 |
| llm_calls | int | LLM调用次数 |
| vector_searches | int | 向量检索次数 |
| processing_time | float | 处理时间（秒） |

### 1.2 流式回答（SSE）

**POST** `/api/chat/stream`

流式获取回答（Server-Sent Events）。采用 POST body 传参而非 GET query：question 走 GET query 会进入 nginx 访问日志与浏览器历史，存在泄露面；且 EventSource 无法携带认证头，POST + fetch 可统一走认证。

**请求体**:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 用户问题 |
| session_id | string | 否 | 会话ID |
| kb_ids | string[] | 否 | 知识库ID列表 |
| use_web_search | boolean | 否 | 是否启用联网搜索，默认false |
| search_mode | string | 否 | 搜索模式：`simple` / `function_calling` / `agent`，默认 `simple` |

**响应格式** (SSE):

> **实现要点**：服务端只发送 `data:` 行，**不使用 SSE 的 `event:` 字段**；
> 事件类型由 `data` JSON 中的 `type` 字段表达（见 `backend/src/api/chat.py` 的流式生成器）。

事件类型共 6 种（`reasoning` / `thinking` / `content` / `end` / `title` / `error`）。
**没有独立的 `sources` 事件**——来源信息随 `end` 事件一并下发：

```
data: {"type": "reasoning", "step": "intent|kb_retrieve|web_search|tool_execute|answer_generate|...",
       "status": "running|done|skipped", "title": "步骤标题", "content": "步骤说明",
       "metadata": {...}}

data: {"type": "thinking", "content": "模型原始思考增量片段"}

data: {"type": "content", "content": "回答片段"}

data: {"type": "end", "message_id": "消息ID", "session_id": "会话ID", "sources": [...],
       "answer_type": "llm_direct|function_calling|agent|...", "reasoning": [...]}

data: {"type": "title", "session_id": "会话ID", "title": "新生成的会话标题"}

data: {"type": "error", "error": "错误信息（已脱敏）", "request_id": "请求追踪ID"}
```

**说明**：
- `reasoning` 为分步过程事件，前端渲染为答案气泡上方的可折叠时间线；`thinking` 为模型原始思考增量，供折叠面板实时展示
- `end` 事件**不含** `title`：标题生成已改为后台任务（避免推迟流完成信号），完成后**在 `end` 之后**补发独立的 `title` 事件。客户端断开时标签生成任务仍会落库，仅 `title` 事件丢失，刷新侧栏可见新标题
- `end` 事件的来源字段名为 `sources`（非 `sources_metadata`），每项含 `source_type` 为 `kb` 或 `web`；网页来源额外含 `url` 字段供前端跳转
- 会话不存在等错误在**流开始前**无法以 4xx 返回时，会以 `200 + error 事件` 形式下发；非流式接口同场景返回 404

### 1.3 获取会话历史

**GET** `/api/sessions/{session_id}`

获取指定会话的消息历史

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 是 | 会话ID |

**成功响应** (200):
```json
{
  "session_id": "会话UUID",
  "title": "会话标题",
  "messages": [
    {
      "id": "消息UUID",
      "role": "user",
      "content": "用户问题",
      "timestamp": "2024-01-01T12:00:00"
    },
    {
      "id": "消息UUID",
      "role": "assistant",
      "content": "助手回答",
      "sources": [...],
      "timestamp": "2024-01-01T12:00:05"
    }
  ],
  "created_at": "2024-01-01T12:00:00",
  "updated_at": "2024-01-01T12:00:05"
}
```

### 1.4 搜索模式说明

聊天接口支持通过 `use_web_search` 和 `search_mode` 控制搜索行为：

| 模式 | `use_web_search` | `search_mode` | 行为 |
|------|-----------------|---------------|------|
| 纯知识库 | false / 不填 | - | 仅检索知识库，无结果时可能降级为 LLM |
| 纯联网搜索 | true | `simple` | 仅使用联网搜索生成回答 |
| 混合搜索 | true + 传入 `kb_ids` | `simple` | 同时使用知识库检索与联网搜索 |
| Function Calling | true | `function_calling` | 有界 Agent 循环调用 `web_search` / `fetch_webpage` / `kb_search` / `wiki_lookup` 等工具（步数与时间预算受限）；可传 `kb_ids` 进入混合模式（Agent 工具收集 + 知识库检索合并生成） |
| ReAct Agent | true | `agent` | 同 Function Calling 的有界 Agent 循环，多轮 DECIDE → ACT → OBSERVE 迭代搜索 |

**降级策略**：
- 当 `search_mode=function_calling|agent` 但对应功能开关未开启时，自动降级为 `simple` 联网搜索
- Agent 循环输出为空或被工具 JSON 污染时，降级为 Phase 2 联网搜索
- 问候/日常对话（如“你好”）会跳过知识库检索，直接由 LLM 回答

---

## 2. 知识库管理接口

### 2.1 创建知识库

**POST** `/api/knowledge_bases`

创建新的知识库

**请求体**:
```json
{
  "name": "知识库名称",
  "description": "知识库描述（可选）",
  "category_id": "分类ID（可选）"
}
```

**成功响应** (201):
```json
{
  "id": "知识库UUID",
  "name": "知识库名称",
  "description": "知识库描述",
  "document_count": 0,
  "created_at": "2024-01-01T12:00:00",
  "updated_at": "2024-01-01T12:00:00"
}
```

### 2.2 获取知识库列表

**GET** `/api/knowledge_bases`

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| page | int | 否 | 页码，默认 1，最小 1 |
| page_size | int | 否 | 每页数量，**默认 10**，范围 1~1000 |

> 该接口**不支持** `keyword` / `category_id` 筛选。
> 结果限定为当前用户拥有的知识库，并按 `(user_id, page, page_size)` 走 Redis 缓存。

**成功响应** (200):
```json
{
  "items": [
    {
      "id": "知识库UUID",
      "name": "知识库名称",
      "description": "描述",
      "document_count": 10,
      "created_at": "2024-01-01T12:00:00"
    }
  ],
  "total": 100,
  "page": 1,
  "page_size": 10,
  "total_pages": 10
}
```

### 2.3 获取知识库详情

**GET** `/api/knowledge_bases/{kb_id}`

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| kb_id | string | 是 | 知识库ID |

**成功响应** (200):
```json
{
  "id": "知识库UUID",
  "name": "知识库名称",
  "description": "知识库描述",
  "document_count": 10,
  "documents": [...],
  "created_at": "2024-01-01T12:00:00",
  "updated_at": "2024-01-01T12:00:00"
}
```

### 2.4 更新知识库

**PUT** `/api/knowledge_bases/{kb_id}`

**请求体**:
```json
{
  "name": "新名称",
  "description": "新描述"
}
```

### 2.5 删除知识库

**DELETE** `/api/knowledge_bases/{kb_id}`

**成功响应** (200):
```json
{"message": "删除成功"}
```

---

## 3. 文档管理接口

### 3.1 上传文档

**POST** `/api/documents/upload`

**请求**: `multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file | File | 是 | 文档文件 |
| kb_id | string | 是 | 目标知识库ID |
| tags | string[] | 否 | 标签列表 |

**成功响应** (201):
```json
{
  "id": "文档UUID",
  "filename": "文件名.pdf",
  "file_type": "pdf",
  "size": 102400,
  "kb_id": "知识库UUID",
  "status": "processing",
  "chunk_count": 0,
  "created_at": "2024-01-01T12:00:00"
}
```

**文档处理状态**:
- `uploading`: 上传中
- `processing`: 处理中
- `completed`: 完成
- `failed`: 失败

### 3.2 获取文档列表

**GET** `/api/documents`

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| kb_id | string | 否 | 知识库筛选（UUID，非法时返回 400）；不传则返回全部知识库的文档 |
| status | string | 否 | 状态筛选。`active` → 映射为库内 `published`；`inactive` → 映射为 `draft` 或 `archived` |

结果始终限定为**当前用户拥有的文档**（`owner_id` 过滤）。

**成功响应** (200)：`DocumentResponse` 的**裸数组**（非分页对象）：

```json
[ { "id": "...", "filename": "...", "...": "..." } ]
```

> ⚠️ **本接口不分页，全量返回**（含 `joinedload` 关联），文档量大时响应体可能很大。
> 同类的 `/api/sessions` 亦不分页。这与 `/api/knowledge_bases`（`page`/`page_size`）、
> `/api/feedback`（`skip`/`limit`）、`/api/traces`（`limit`/`offset`）等接口的分页风格并存，
> 属已知的接口规范不统一问题（见 `docs/archive/code-review-2026-09.md` P1-C2）。

### 3.3 获取文档详情

**GET** `/api/documents/{document_id}`

**成功响应** (200):
```json
{
  "id": "文档UUID",
  "filename": "文件名.pdf",
  "file_type": "pdf",
  "size": 102400,
  "kb_id": "知识库UUID",
  "status": "completed",
  "chunk_count": 5,
  "tags": ["标签1", "标签2"],
  "created_at": "2024-01-01T12:00:00",
  "updated_at": "2024-01-01T12:00:05"
}
```

### 3.4 更新文档

**PUT** `/api/documents/{document_id}`

**请求体**:
```json
{
  "tags": ["新标签1", "新标签2"]
}
```

### 3.5 删除文档

**DELETE** `/api/documents/{document_id}`

**成功响应** (200):
```json
{"message": "删除成功"}
```

### 3.6 批量删除文档

**POST** `/api/documents/batch/delete`

异步处理：立即返回确认消息，后台删除文档，并通过 WebSocket 实时推送删除进度。

**请求体**:
```json
{
  "ids": ["文档ID1", "文档ID2"]
}
```

**成功响应** (200):
```json
{
  "message": "已提交2个删除任务",
  "deleted_count": 2,
  "tasks": [{"doc_id": "文档ID1", "kb_id": "所属知识库ID", "task_id": "delete_xxx"}]
}
```

---

## 4. 会话管理接口

### 4.1 获取会话列表

**GET** `/api/sessions`

**成功响应** (200):
```json
[
  {
    "id": "会话UUID",
    "title": "会话标题",
    "user_id": "用户ID",
    "message_count": 5,
    "last_message": "最后一条消息内容",
    "created_at": "2024-01-01T12:00:00",
    "updated_at": "2024-01-01T12:05:00"
  }
]
```

### 4.2 获取会话详情

**GET** `/api/sessions/{session_id}`

**成功响应** (200)：
```json
{
  "id": "会话UUID",
  "title": "会话标题",
  "messages": [...],
  "kb_ids": ["知识库UUID"]
}
```

> 响应**不包含** `user_id`（避免回显归属信息）；非会话所有者返回 403。

### 4.3 更新会话标题

**PUT** `/api/sessions/{session_id}`

**请求体**:
```json
{
  "title": "新标题"
}
```

### 4.4 删除会话

**DELETE** `/api/sessions/{session_id}`

---

## 5. 分类管理接口

### 5.1 创建分类

**POST** `/api/categories`

**请求体**:
```json
{
  "name": "分类名称",
  "description": "分类描述"
}
```

### 5.2 获取分类列表

**GET** `/api/categories`

### 5.3 更新分类

**PUT** `/api/categories/{category_id}`

### 5.4 删除分类

**DELETE** `/api/categories/{category_id}`

---

## 6. 标签管理接口

### 6.1 创建标签

**POST** `/api/tags`

**请求体**:
```json
{
  "name": "标签名称",
  "color": "#FF5733"
}
```

### 6.2 获取标签列表

**GET** `/api/tags`

### 6.3 删除标签

**DELETE** `/api/tags/{tag_id}`

---

## 7. 评价反馈接口

### 7.1 提交评价

**POST** `/api/chat/messages/{message_id}/feedback`

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| message_id | string | 是 | 消息UUID |

**请求体**:
```json
{
  "rating": "positive",
  "reason": "可选的评价原因",
  "session_id": "可选，会话UUID"
}
```

| rating 值 | 说明 |
|-----------|------|
| positive / like | 正面评价（有用），映射为 5 分 |
| negative / dislike | 负面评价（无用），映射为 1 分 |
| 1-5 整数 | 1-5 星评分，直接记录 |

**成功响应** (201):
```json
{
  "id": "评价UUID",
  "message": "评价提交成功"
}
```

### 7.2 获取评价统计

**GET** `/api/feedback/stats`

**成功响应** (200):
```json
{
  "total_count": 100,
  "positive_count": 80,
  "negative_count": 20,
  "average_rating": 4.2
}
```

---

## 8. 学习引擎接口

前缀 `/api/learning`。全部端点仅需登录，其中修改全局引擎状态的 4 个端点
（`trigger` / `config` / `enable` / `disable`）**与 `/api/config/*` 的 `require_admin` 尺度不一致**，
属已知问题（见 `docs/archive/code-review-2026-09.md` P1-A7）。

### 8.1 获取学习统计

**GET** `/api/learning/stats`

**成功响应** (200)：`learning_engine.get_learning_stats()` 的返回，并按前端期望结构补充 `misclassification_count`。

### 8.2 获取误分类分析

**GET** `/api/learning/misclassification?limit=50`

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| limit | int | 否 | 返回的误分类样本数上限，默认 50 |

### 8.3 触发学习

**POST** `/api/learning/trigger`

**请求体**：无（该端点不接受请求体）

**成功响应** (200):
```json
{ "success": true, "data": { "...": "learning_engine.check_and_trigger_learning() 的返回" } }
```

### 8.4 记录执行 / 记录反馈

- **POST** `/api/learning/record-execution`
- **POST** `/api/learning/record-feedback`

由问答链路内部调用，用于在线学习闭环采样。

### 8.5 学习引擎配置

- **GET** `/api/learning/config` — 获取配置

```json
{
  "success": true,
  "data": {
    "enabled": true,
    "learning_interval_hours": 24,
    "last_learning_time": "2024-01-01T12:00:00"
  }
}
```

- **PUT** `/api/learning/config` — 更新配置

### 8.6 启用 / 禁用学习引擎

- **POST** `/api/learning/enable` → `{"success": true, "message": "学习引擎已启用"}`
- **POST** `/api/learning/disable` → `{"success": true, "message": "学习引擎已禁用"}`

---

## 9. A/B测试接口

### 9.1 创建实验

**POST** `/api/experiments`

**请求体**:
```json
{
  "name": "实验名称",
  "description": "实验描述",
  "variants": [
    {"name": "A组", "weight": 0.5},
    {"name": "B组", "weight": 0.5}
  ],
  "metrics": ["accuracy", "latency"]
}
```

**成功响应** (200):
```json
{
  "success": true,
  "experiment_id": "实验UUID"
}
```

### 9.2 获取实验列表

**GET** `/api/experiments`

**成功响应** (200):
```json
{
  "success": true,
  "data": [
    {
      "id": "实验UUID",
      "name": "实验名称",
      "status": "running",
      "variants": [...],
      "metrics": [...]
    }
  ]
}
```

### 9.3 获取实验详情

**GET** `/api/experiments/{experiment_id}`

**成功响应** (200):
```json
{
  "success": true,
  "data": {
    "id": "实验UUID",
    "name": "实验名称",
    "status": "running",
    "variants": [...]
  }
}
```

### 9.4 启动/停止实验

**POST** `/api/experiments/{experiment_id}/start`

**POST** `/api/experiments/{experiment_id}/stop`

**成功响应** (200):
```json
{
  "success": true,
  "message": "实验已启动"
}
```

### 9.5 分配实验流量

**POST** `/api/experiments/{experiment_id}/allocate?user_id=用户ID&session_id=会话ID`

**成功响应** (200):
```json
{
  "success": true,
  "variant_id": "变体ID"
}
```

### 9.6 批量删除实验

**DELETE** `/api/experiments/batch`

**请求体**:
```json
{
  "experiment_ids": ["实验ID1", "实验ID2"]
}
```

**成功响应** (200):
```json
{
  "success": true,
  "data": {
    "deleted_count": 2
  }
}
```

---

## 10. 系统配置接口

### 10.1 获取系统配置

**GET** `/api/config`

**成功响应** (200):
```json
{
  "processing": {
    "chunk_size": 512,
    "chunk_overlap": 64,
    "top_k": 3
  },
  "model": {
    "embedding_model_name": "bge-m3:latest",
    "embedding_dimension": 1024,
    "ollama_model_name": "deepseek-r1:7b-qwen-distill-q4_K_M",
    "fast_llm_model_name": "qwen2.5:7b"
  },
  "supported_extensions": [".txt", ".pdf", ".docx", ...]
}
```

### 10.2 获取文档处理配置

**GET** `/api/config/processing`

### 10.3 更新文档处理配置

**PUT** `/api/config/processing`

**请求体**:
```json
{
  "chunk_size": 512,
  "chunk_overlap": 64,
  "top_k": 5
}
```

**参数说明**:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| chunk_size | int | 否 | 分块大小，范围 50~5000 |
| chunk_overlap | int | 否 | 分块重叠大小，必须小于 chunk_size |
| top_k | int | 否 | 检索返回数量，范围 1~20 |

### 10.4 获取模型配置

**GET** `/api/config/model`

### 10.5 重置配置

**POST** `/api/config/reset`

**成功响应** (200):
```json
{
  "message": "配置已重置为默认值"
}
```

### 10.6 获取系统信息

**GET** `/api/config/info`

**成功响应** (200):
```json
{
  "version": "1.0.0",
  "description": "RAG Knowledge Base QA System",
  "supported_extensions": [".txt", ".pdf", ".docx", ...],
  "processing_defaults": {
    "chunk_size": 500,
    "chunk_overlap": 50,
    "top_k": 3
  }
}
```

---

## 11. 实时通知接口

系统通过 WebSocket 提供实时通知，主要用于知识库列表变更、文档列表变更、上传任务进度等场景。

**鉴权**：采用**首帧鉴权**，凭据不出现在 URL 上。

浏览器 WebSocket 无法携带自定义请求头，而把密钥放进 query 参数会进入反向代理
访问日志与浏览器历史。因此握手阶段**不做校验**，连接建立后由客户端发送首帧：

```json
{"type": "auth", "api_key": "<API_KEY>"}
// 或 JWT 用户：
{"type": "auth", "token": "<access_token>"}
```

认证通过后服务端回 `{"type": "auth_ok"}`；失败或超过 10 秒未收到认证帧，
以关闭码 **1008** 关闭连接。认证通过前不推送任何业务数据。

未配置 `API_KEY` 且**非 Docker** 的开发环境下，服务端在握手后立即回
`{"type": "auth_ok"}` 并放行（不等待客户端发送认证帧）。

> 实现见 `backend/src/auth.py` 的 `get_current_user_for_ws`。

### 11.1 通用通知通道

**WebSocket** `ws://localhost:8000/api/ws/notifications`

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| channels | string | 否 | 逗号分隔的订阅频道，默认 `kb:*,doc:*` |

**订阅频道示例**:

| 频道 | 说明 |
|------|------|
| `kb:*` | 所有知识库相关通知 |
| `doc:{kb_id}` | 指定知识库的文档变更通知 |
| `task:{task_id}` | 指定上传任务的进度通知 |

**消息类型**:

| 类型 | 说明 |
|------|------|
| connected | 连接成功 |
| kb_list_changed | 知识库列表已变更 |
| doc_list_changed | 文档列表已变更 |
| task_progress | 任务进度更新 |
| task_completed | 任务完成 |
| task_failed | 任务失败 |

**客户端示例**:

```javascript
const ws = new WebSocket('ws://localhost:8000/api/ws/notifications?channels=kb:*,doc:*');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log(msg.type, msg.data);
};

// 心跳
setInterval(() => ws.send(JSON.stringify({type: 'ping'})), 30000);
```

### 11.2 知识库变更通知

**WebSocket** `ws://localhost:8000/api/ws/kb`

订阅所有知识库列表变更通知。

### 11.3 文档变更通知

**WebSocket** `ws://localhost:8000/api/ws/docs/{kb_id}`

订阅指定知识库的文档变更通知。

### 11.4 上传进度通知

**WebSocket** `ws://localhost:8000/api/documents/upload/progress/ws/{upload_id}`

订阅指定上传任务的实时进度通知。客户端可发送 `{"action": "ping"}`，服务端
回复 `{"type": "pong"}` 作为心跳。

---

## 12. 错误响应格式

所有错误由 `backend/src/main.py` 的异常处理器统一输出，字段固定为三个：

```json
{
  "error_code": "HTTP_ERROR",
  "detail": "错误描述信息",
  "request_id": "请求追踪ID"
}
```

- `request_id` 与日志中的追踪 ID 一致，排查问题时提供给运维即可定位
- 500 级错误的 `detail` 一律为固定文案（"Internal Server Error"），**内部异常细节只进日志不回显**；若某个端点返回了具体异常信息，属缺陷（见 `docs/archive/code-review-2026-09.md` P0-10）
- 请求体校验失败（422）时 `detail` 为 Pydantic 的错误数组，而非字符串

### HTTP状态码汇总

| 状态码 | 含义 | 说明 |
|--------|------|------|
| 200 | OK | 请求成功（**删除/批量操作亦返回 200 + `{"message": ...}`，未使用 204**） |
| 201 | Created | 创建成功（仅 `POST /api/auth/register` 使用） |
| 400 | Bad Request | 请求参数错误 / 非法 UUID |
| 401 | Unauthorized | 未认证或凭据无效 |
| 403 | Forbidden | 已认证但无权限（非资源所有者 / 缺少 `X-Admin-Key`） |
| 404 | Not Found | 资源不存在 |
| 413 | Payload Too Large | 上传文件超出 `MAX_UPLOAD_SIZE_MB` |
| 422 | Unprocessable Entity | 请求体校验失败 |
| 500 | Internal Server Error | 服务器内部错误 |

### 错误代码说明

`error_code` 取自异常对象，定义分两处：

| 代码 | 来源 | 触发条件 |
|------|------|------|
| `VALIDATION_ERROR` | `main.py` 处理器 / `ValidationException` | 请求体未通过 Pydantic 校验（422） |
| `HTTP_ERROR` | `main.py` 处理器 | 任意裸 `HTTPException`（400/401/403/404/413 等） |
| `INTERNAL_ERROR` | `main.py` 处理器 / `AppException` 基类 | 未捕获异常（500） |
| `NOT_FOUND` | `ResourceNotFoundException` | 资源不存在（404） |
| `BAD_REQUEST` | `BadRequestException` | 请求参数错误（400） |
| `AUTHENTICATION_ERROR` | `AuthenticationException` | 认证失败（401） |
| `AUTHORIZATION_ERROR` | `AuthorizationException` | 权限不足（403） |

> ⚠️ **已知问题**：`AppException` 体系（`src/exceptions.py`）在 API 层**零使用**——已 grep 确认全部端点直接用裸 `HTTPException`。因此后 5 个错误码与 `AppException` 处理器分支目前是死代码，客户端实际只会收到 `VALIDATION_ERROR` / `HTTP_ERROR` / `INTERNAL_ERROR` 三者。详见 `docs/archive/code-review-2026-09.md` P1-C1。

---

## 13. 坏例管理接口

坏例（Badcase）用于记录问答质量异常样本，系统会自动分类问题类型并触发意图路由的在线学习。前缀：`/api/badcases`。

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/badcases` | 提交坏例反馈 |
| GET | `/api/badcases` | 获取坏例列表 |
| GET | `/api/badcases/stats` | 获取坏例统计（总数/分类分布/平均严重度） |
| GET | `/api/badcases/categories` | 获取坏例分类列表 |

**创建坏例** `POST /api/badcases`：

```json
{
  "question": "必填：用户问题",
  "answer": "可选：系统回答",
  "session_id": "可选：关联会话 ID",
  "message_id": "可选：关联消息 ID",
  "feedback_type": "可选：反馈类型",
  "reason": "可选：问题描述原因",
  "retrieved_sources": "可选：检索到的来源",
  "intent_decision": "可选：意图路由决策记录"
}
```

---

## 14. RAG 评估接口

提供检索质量与生成质量的评估能力，支持单条与批量模式。前缀：`/api/evaluate`。

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/api/evaluate/retrieval` | 单次检索评估（context precision/recall） |
| POST | `/api/evaluate/retrieval/batch` | 批量检索评估 |
| POST | `/api/evaluate/generation` | 单次生成评估（忠实度/相关度） |
| POST | `/api/evaluate/generation/batch` | 批量生成评估 |

**检索评估请求**：

```json
{
  "question": "用户问题",
  "retrieved_docs": ["检索返回的文档/chunk 列表"],
  "expected_doc_ids": ["可选：期望命中的文档 ID"],
  "expected_contents": ["可选：期望命中的内容片段"]
}
```

**生成评估请求**：

```json
{
  "question": "用户问题",
  "answer": "系统生成的答案",
  "contexts": ["生成时使用的上下文片段"]
}
```

---

## 15. Wiki 编译层接口

前缀 `/api/knowledge_bases/{kb_id}/wiki`。Wiki 编译层由 LLM 将知识库文档预编译为结构化页面
（`WIKI_COMPILE_ENABLED` 控制，默认关闭）。全部端点需登录，且校验 `kb_id` 归属。

### 15.1 列出编译页

**GET** `/api/knowledge_bases/{kb_id}/wiki/pages` → `WikiPageListResponse`

### 15.2 获取编译页正文

**GET** `/api/knowledge_bases/{kb_id}/wiki/pages/{page_id}/content` → `WikiPageContentResponse`

### 15.3 一致性检查

**GET** `/api/knowledge_bases/{kb_id}/wiki/lint` → `WikiLintResponse`

检查页面间的矛盾、断链等一致性问题（编译期矛盾抽查由 `WIKI_CONTRADICTION_CHECK` 控制）。

### 15.4 重建

**POST** `/api/knowledge_bases/{kb_id}/wiki/rebuild` → `WikiRebuildResponse`

```json
{ "upload_id": "任务ID", "message": "Wiki 全量重编译任务已提交" }
```

异步任务：提交后立即返回，进度经 WebSocket `task:{upload_id}` 频道推送。

---

## 16. 请求追踪接口

前缀 `/api/traces`。记录每次问答的完整链路（意图路由、检索、搜索、生成各阶段耗时）。
全部端点需登录，结果限定为当前用户。

### 16.1 获取追踪列表

**GET** `/api/traces`

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| session_id | string | 否 | 按会话 ID 过滤 |
| start | datetime | 否 | 起始时间（含） |
| end | datetime | 否 | 结束时间（含） |
| limit | int | 否 | 每页数量，默认 20，范围 1~100 |
| offset | int | 否 | 偏移量，默认 0 |

**成功响应** (200)：`{"total": 100, "items": [...]}`（按创建时间倒序）

> 注意：该接口路径为 `@router.get("")`，且应用以 `redirect_slashes=False` 启动，
> 因此**只能使用 `/api/traces`，带尾斜杠的 `/api/traces/` 会返回 404**。
> 这与其余集合端点使用 `/`（如 `/api/documents/`）的风格不一致，属已知问题
> （见 `docs/archive/code-review-2026-09.md` P1-C3）。

### 16.2 获取追踪详情

**GET** `/api/traces/{trace_id}`

**越权语义**：访问他人的 trace 返回 **404 而非 403**，刻意不泄露资源是否存在。

---

## 附录：API 端点汇总

> 与代码核对至 2026-09-16（`backend/src/api/` 共 16 个路由模块，另有 `main.py` 的健康检查与指标端点，合计 111 条路由）。

| 模块 | 方法 | 端点 | 说明 |
|------|------|------|------|
| 认证 | POST | `/api/auth/register` | 注册（201；受 `AUTH_ALLOW_REGISTRATION` 控制） |
| 认证 | POST | `/api/auth/login` | 登录换取 JWT |
| 认证 | GET | `/api/auth/me` | 获取当前用户信息 |
| 聊天 | POST | `/api/chat/messages` | 发送消息（非流式） |
| 聊天 | POST | `/api/chat/stream` | 流式回答（SSE） |
| 聊天 | POST | `/api/chat/suggestions` | 获取推荐问题 |
| 聊天 | POST | `/api/chat/enhance_context` | 上下文增强 |
| 聊天 | POST | `/api/chat/messages/{message_id}/feedback` | 提交消息评价 |
| 聊天 | POST | `/api/chat/rewrite` | 问题重写 |
| 聊天 | POST | `/api/chat/classify` | 问题分类 |
| 聊天 | POST | `/api/chat/compare` | 知识库答案对比 |
| 知识库 | POST | `/api/knowledge_bases` | 创建知识库 |
| 知识库 | GET | `/api/knowledge_bases` | 获取知识库列表 |
| 知识库 | GET | `/api/knowledge_bases/default` | 获取默认知识库 |
| 知识库 | GET | `/api/knowledge_bases/{kb_id}` | 获取知识库详情 |
| 知识库 | PUT | `/api/knowledge_bases/{kb_id}` | 更新知识库 |
| 知识库 | DELETE | `/api/knowledge_bases/{kb_id}` | 删除知识库 |
| 知识库 | POST | `/api/knowledge_bases/batch-delete` | 批量删除知识库 |
| 知识库 | POST | `/api/knowledge_bases/{kb_id}/set_default` | 设为默认知识库 |
| 知识库 | POST | `/api/knowledge_bases/recommend` | 知识库推荐（候选范围限定为当前用户） |
| 知识库 | POST | `/api/knowledge_bases/knowledge_graph` | 生成知识图谱（范围限定为当前用户） |
| Wiki | GET | `/api/knowledge_bases/{kb_id}/wiki/pages` | 列出编译页 |
| Wiki | GET | `/api/knowledge_bases/{kb_id}/wiki/pages/{page_id}/content` | 获取编译页正文 |
| Wiki | GET | `/api/knowledge_bases/{kb_id}/wiki/lint` | Wiki 一致性检查 |
| Wiki | POST | `/api/knowledge_bases/{kb_id}/wiki/rebuild` | 重建 Wiki |
| 文档 | POST | `/api/documents/upload` | 上传文档（后台异步处理） |
| 文档 | POST | `/api/documents/batch` | 批量上传 |
| 文档 | GET | `/api/documents/upload/progress/{upload_id}` | 轮询上传/解析进度 |
| 文档 | GET | `/api/documents/chunk-config` | 获取分块配置 |
| 文档 | GET | `/api/documents/search` | 文档内容搜索 |
| 文档 | GET | `/api/documents` | 获取文档列表 |
| 文档 | GET | `/api/documents/{doc_id}` | 获取文档详情 |
| 文档 | PUT | `/api/documents/{doc_id}` | 更新文档 |
| 文档 | DELETE | `/api/documents/{doc_id}` | 删除文档 |
| 文档 | PUT | `/api/documents/{doc_id}/status` | 更新文档状态 |
| 文档 | GET | `/api/documents/{doc_id}/preview` | 文档预览 |
| 文档 | GET | `/api/documents/{doc_id}/chunks` | 获取分块列表 |
| 文档 | GET | `/api/documents/{doc_id}/source/{chunk_index}` | 引用溯源到原文片段 |
| 文档 | POST | `/api/documents/{doc_id}/reprocess` | 重新解析 |
| 文档 | POST | `/api/documents/{doc_id}/classify` | 文档分类 |
| 文档 | POST | `/api/documents/{doc_id}/quality` | 文档质量检测 |
| 文档 | POST | `/api/documents/batch/delete` | 批量删除文档 |
| 文档 | POST | `/api/documents/duplicate-detect` | 重复文档检测 |
| 会话 | POST | `/api/sessions` | 创建会话 |
| 会话 | GET | `/api/sessions` | 获取会话列表 |
| 会话 | GET | `/api/sessions/quick_questions` | 获取快捷问题 |
| 会话 | GET | `/api/sessions/{session_id}` | 获取会话详情 |
| 会话 | PUT | `/api/sessions/{session_id}` | 更新会话 |
| 会话 | DELETE | `/api/sessions/{session_id}` | 删除会话 |
| 会话 | POST | `/api/sessions/batch-delete` | 批量删除会话 |
| 分类 | POST | `/api/categories` | 创建分类 |
| 分类 | GET | `/api/categories` | 获取分类列表 |
| 分类 | GET | `/api/categories/{category_id}` | 获取分类详情 |
| 分类 | PUT | `/api/categories/{category_id}` | 更新分类 |
| 分类 | DELETE | `/api/categories/{category_id}` | 删除分类 |
| 标签 | POST | `/api/tags` | 创建标签 |
| 标签 | GET | `/api/tags` | 获取标签列表 |
| 标签 | PUT | `/api/tags/{tag_id}` | 更新标签 |
| 标签 | DELETE | `/api/tags/{tag_id}` | 删除标签 |
| 反馈 | POST | `/api/feedback` | 提交反馈 |
| 反馈 | GET | `/api/feedback` | 获取反馈列表 |
| 反馈 | GET | `/api/feedback/stats` | 获取反馈统计 |
| 反馈 | GET | `/api/feedback/{feedback_id}` | 获取反馈详情 |
| 反馈 | DELETE | `/api/feedback/{feedback_id}` | 删除反馈 |
| 反馈 | POST | `/api/feedback/like` | 点赞 |
| 反馈 | POST | `/api/feedback/dislike` | 点踩 |
| 学习 | GET | `/api/learning/stats` | 获取学习统计信息 |
| 学习 | GET | `/api/learning/misclassification` | 获取误分类分析 |
| 学习 | POST | `/api/learning/trigger` | 触发学习 |
| 学习 | POST | `/api/learning/record-execution` | 记录执行 |
| 学习 | POST | `/api/learning/record-feedback` | 记录反馈 |
| 学习 | PUT | `/api/learning/config` | 更新学习引擎配置 |
| 学习 | GET | `/api/learning/config` | 获取学习引擎配置 |
| 学习 | POST | `/api/learning/enable` | 启用学习引擎 |
| 学习 | POST | `/api/learning/disable` | 禁用学习引擎 |
| 实验 | POST | `/api/experiments` | 创建实验 |
| 实验 | GET | `/api/experiments` | 获取实验列表 |
| 实验 | GET | `/api/experiments/{experiment_id}` | 获取实验详情 |
| 实验 | POST | `/api/experiments/{experiment_id}/start` | 启动实验 |
| 实验 | POST | `/api/experiments/{experiment_id}/stop` | 停止实验 |
| 实验 | POST | `/api/experiments/{experiment_id}/allocate` | 分配实验流量 |
| 实验 | POST | `/api/experiments/{experiment_id}/metrics` | 记录实验指标 |
| 实验 | GET | `/api/experiments/{experiment_id}/metrics` | 获取实验指标 |
| 实验 | POST | `/api/experiments/{experiment_id}/analyze` | 分析实验 |
| 实验 | GET | `/api/experiments/{experiment_id}/result` | 获取实验结果 |
| 实验 | DELETE | `/api/experiments/batch` | 批量删除实验 |
| 配置 | GET | `/api/config` | 获取系统配置 |
| 配置 | GET | `/api/config/processing` | 获取文档处理配置 |
| 配置 | PUT | `/api/config/processing` | 更新文档处理配置 |
| 配置 | GET | `/api/config/model` | 获取模型配置 |
| 配置 | POST | `/api/config/reset` | 重置配置为默认值 |
| 配置 | GET | `/api/config/info` | 获取系统信息 |
| 坏例 | POST | `/api/badcases` | 提交坏例反馈 |
| 坏例 | GET | `/api/badcases` | 获取坏例列表 |
| 坏例 | GET | `/api/badcases/stats` | 获取坏例统计 |
| 坏例 | GET | `/api/badcases/categories` | 获取坏例分类列表 |
| 评估 | POST | `/api/evaluate/retrieval` | 单次检索评估 |
| 评估 | POST | `/api/evaluate/retrieval/batch` | 批量检索评估 |
| 评估 | POST | `/api/evaluate/generation` | 单次生成评估 |
| 评估 | POST | `/api/evaluate/generation/batch` | 批量生成评估 |
| 通知 | WS | `/api/ws/notifications` | 通用实时通知通道 |
| 通知 | WS | `/api/ws/kb` | 知识库变更通知 |
| 通知 | WS | `/api/ws/docs/{kb_id}` | 文档变更通知 |
| 文档 | WS | `/api/documents/upload/progress/ws/{upload_id}` | 上传进度实时通知 |
| 健康检查 | GET | `/` | 根路径信息 |
| 健康检查 | GET | `/health` | 健康检查 |
| 健康检查 | GET | `/health/detail` | 详细健康检查（含数据库、Redis） |
| 监控 | GET | `/metrics` | 获取 Prometheus 监控指标 |
| 监控 | POST | `/metrics/reset` | 重置监控指标（需 `X-Admin-Key`） |
| 追踪 | GET | `/api/traces` | 获取请求追踪列表 |
| 追踪 | GET | `/api/traces/{trace_id}` | 获取单条追踪详情 |