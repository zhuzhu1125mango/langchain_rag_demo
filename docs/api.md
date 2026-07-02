# 📡 RAG 知识库问答系统 API 文档

## 基础信息

- **API 地址**: `http://localhost:8000/api`
- **文档地址**: `http://localhost:8000/docs` (Swagger UI)
- **健康检查**: `GET /health`
- **版本**: v1.0
- **依赖管理**: Poetry

### API 使用示例

```bash
# 发送消息（非流式）
curl -X POST http://localhost:8000/api/chat/messages \
  -H "Content-Type: application/json" \
  -d '{"question": "什么是 RAG？", "stream": false}'

# 发送消息并启用联网搜索
curl -X POST http://localhost:8000/api/chat/messages \
  -H "Content-Type: application/json" \
  -d '{"question": "2025 年最新的大模型进展", "use_web_search": true, "search_mode": "simple"}'

# 流式回答
curl -N "http://localhost:8000/api/chat/stream?question=什么是RAG"

# 流式回答并启用联网搜索
curl -N "http://localhost:8000/api/chat/stream?question=2025年AI趋势&use_web_search=true&search_mode=simple"

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
  "search_mode": "simple"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 用户问题内容 |
| session_id | string | 否 | 会话UUID，不传则创建新会话 |
| kb_ids | array | 否 | 知识库ID列表，指定查询范围 |
| use_web_search | boolean | 否 | 是否启用联网搜索，默认false |
| search_mode | string | 否 | 搜索模式：`simple` / `function_calling` / `agent`，默认 `simple` |

**成功响应** (200):
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
  "title": "生成的会话标题",
  "llm_calls": 1,
  "vector_searches": 1,
  "processing_time": 2.5
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
| source_metadata[].source_type | string | 来源类型：`kb` 知识库 / `web` 网页 |
| source_metadata[].url | string | 网页来源 URL（仅 `web`） |
| source_metadata[].filename | string | 文件名或网页标题 |
| answer_type | string | 回答类型：`knowledge_base`、`llm_direct`、`web_search`、`hybrid_search` 等 |
| title | string | 首次发送消息时生成的会话标题，后续消息可能为空 |
| llm_calls | int | LLM调用次数 |
| vector_searches | int | 向量检索次数 |
| processing_time | float | 处理时间（秒） |

### 1.2 流式回答（SSE）

**GET** `/api/chat/stream`

流式获取回答（Server-Sent Events）

**查询参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| question | string | 是 | 用户问题 |
| session_id | string | 否 | 会话ID |
| kb_ids | string[] | 否 | 知识库ID列表（逗号分隔） |
| use_web_search | boolean | 否 | 是否启用联网搜索，默认false |
| search_mode | string | 否 | 搜索模式：`simple` / `function_calling` / `agent`，默认 `simple` |

**响应格式** (SSE):
```
event: content
data: {"type": "content", "content": "回答片段"}

event: content
data: {"type": "content", "content": "继续的回答"}

event: end
data: {"type": "end", "message_id": "消息ID", "session_id": "会话ID", "title": "生成的会话标题", "sources": [...], "sources_metadata": [{"filename": "网页标题", "url": "https://example.com", "source_type": "web"}, ...]}

event: error
data: {"type": "error", "error": "错误信息"}
```

**说明**：
- `end` 事件返回的 `sources_metadata` 同时包含 `kb` 与 `web` 类型来源
- 网页来源包含 `url` 字段，前端可直接点击跳转
- `title` 字段在首次发送消息并生成会话标题时返回，前端可用于即时更新会话列表

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
| Function Calling | true | `function_calling` | LLM 决定调用 `web_search` / `fetch_webpage` 工具 |
| ReAct Agent | true | `agent` | 多轮 Thought → Action → Observation 迭代搜索 |

**降级策略**：
- 当 `search_mode=function_calling|agent` 但对应功能开关未开启时，自动降级为 `simple` 联网搜索
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
| page | int | 否 | 页码，默认1 |
| page_size | int | 否 | 每页数量，默认20 |
| keyword | string | 否 | 关键词搜索 |
| category_id | string | 否 | 分类筛选 |

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
  "page_size": 20
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
| page | int | 否 | 页码，默认1 |
| page_size | int | 否 | 每页数量，默认20 |
| kb_id | string | 否 | 知识库筛选 |
| status | string | 否 | 状态筛选 |
| keyword | string | 否 | 文件名搜索 |

**成功响应** (200):
```json
{
  "items": [...],
  "total": 100,
  "page": 1,
  "page_size": 20
}
```

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

**DELETE** `/api/documents/batch`

**请求体**:
```json
{
  "ids": ["文档ID1", "文档ID2"]
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

**成功响应** (200):
```json
{
  "id": "会话UUID",
  "title": "会话标题",
  "user_id": "用户ID",
  "messages": [...],
  "kb_ids": ["知识库UUID"],
  "created_at": "2024-01-01T12:00:00",
  "updated_at": "2024-01-01T12:05:00"
}
```

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

### 8.1 触发学习

**POST** `/api/learning/trigger`

**请求体**:
```json
{
  "sample_count": 100,
  "retrain": true
}
```

**成功响应** (200):
```json
{
  "status": "completed",
  "samples_learned": 100,
  "rules_added": 5,
  "rules_updated": 3
}
```

### 8.2 获取学习状态

**GET** `/api/learning/status`

**成功响应** (200):
```json
{
  "is_running": false,
  "last_run": "2024-01-01T12:00:00",
  "total_rules": 50,
  "total_samples": 1000
}
```

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

### 11.1 通用通知通道

**WebSocket** `ws://localhost:8000/ws/notifications`

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
const ws = new WebSocket('ws://localhost:8000/ws/notifications?channels=kb:*,doc:*');

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log(msg.type, msg.data);
};

// 心跳
setInterval(() => ws.send(JSON.stringify({type: 'ping'})), 30000);
```

### 11.2 知识库变更通知

**WebSocket** `ws://localhost:8000/ws/kb`

订阅所有知识库列表变更通知。

### 11.3 文档变更通知

**WebSocket** `ws://localhost:8000/ws/docs/{kb_id}`

订阅指定知识库的文档变更通知。

### 11.4 上传进度通知

**WebSocket** `ws://localhost:8000/ws/upload/{upload_id}`

订阅指定上传任务的实时进度通知。

---

## 12. 错误响应格式

```json
{
  "detail": "错误描述信息",
  "code": "错误代码",
  "timestamp": "2024-01-01T12:00:00"
}
```

### HTTP状态码汇总

| 状态码 | 含义 | 说明 |
|--------|------|------|
| 200 | OK | 请求成功 |
| 201 | Created | 创建成功 |
| 400 | Bad Request | 请求参数错误 |
| 401 | Unauthorized | 未授权 |
| 403 | Forbidden | 禁止访问 |
| 404 | Not Found | 资源不存在 |
| 422 | Unprocessable Entity | 请求格式错误 |
| 500 | Internal Server Error | 服务器内部错误 |

### 错误代码说明

| 代码 | 说明 |
|------|------|
| VALIDATION_ERROR | 参数验证失败 |
| NOT_FOUND | 资源不存在 |
| UNAUTHORIZED | 未授权访问 |
| DATABASE_ERROR | 数据库操作失败 |
| SERVICE_ERROR | 服务内部错误 |
| TIMEOUT | 请求超时 |
| SEARCH_ENGINE_ERROR | 搜索引擎调用失败 |
| SEARCH_FETCH_ERROR | 网页内容抓取失败 |

---

## 附录：API 端点汇总

| 模块 | 方法 | 端点 | 说明 |
|------|------|------|------|
| 聊天 | POST | `/api/chat/messages` | 发送消息 |
| 聊天 | GET | `/api/chat/stream` | 流式回答 |
| 聊天 | POST | `/api/chat/suggestions` | 获取推荐问题 |
| 聊天 | POST | `/api/chat/enhance_context` | 上下文增强 |
| 聊天 | POST | `/api/chat/rewrite` | 问题重写 |
| 聊天 | POST | `/api/chat/classify` | 问题分类 |
| 聊天 | POST | `/api/chat/compare` | 知识库答案对比 |
| 聊天 | POST | `/api/chat/messages/{message_id}/feedback` | 提交消息评价 |
| 知识库 | POST | `/api/knowledge_bases` | 创建知识库 |
| 知识库 | GET | `/api/knowledge_bases` | 获取知识库列表 |
| 知识库 | GET | `/api/knowledge_bases/{kb_id}` | 获取知识库详情 |
| 知识库 | PUT | `/api/knowledge_bases/{kb_id}` | 更新知识库 |
| 知识库 | DELETE | `/api/knowledge_bases/{kb_id}` | 删除知识库 |
| 文档 | POST | `/api/documents/upload` | 上传文档 |
| 文档 | GET | `/api/documents` | 获取文档列表 |
| 文档 | GET | `/api/documents/{document_id}` | 获取文档详情 |
| 文档 | PUT | `/api/documents/{document_id}` | 更新文档 |
| 文档 | DELETE | `/api/documents/{document_id}` | 删除文档 |
| 文档 | DELETE | `/api/documents/batch` | 批量删除文档 |
| 会话 | POST | `/api/sessions` | 创建会话 |
| 会话 | GET | `/api/sessions` | 获取会话列表 |
| 会话 | GET | `/api/sessions/quick_questions` | 获取快捷问题 |
| 会话 | GET | `/api/sessions/{session_id}` | 获取会话详情 |
| 会话 | PUT | `/api/sessions/{session_id}` | 更新会话 |
| 会话 | DELETE | `/api/sessions/{session_id}` | 删除会话 |
| 会话 | POST | `/api/sessions/batch-delete` | 批量删除会话 |
| 分类 | POST | `/api/categories` | 创建分类 |
| 分类 | GET | `/api/categories` | 获取分类列表 |
| 分类 | PUT | `/api/categories/{category_id}` | 更新分类 |
| 分类 | DELETE | `/api/categories/{category_id}` | 删除分类 |
| 标签 | POST | `/api/tags` | 创建标签 |
| 标签 | GET | `/api/tags` | 获取标签列表 |
| 标签 | DELETE | `/api/tags/{tag_id}` | 删除标签 |
| 反馈 | GET | `/api/feedback/stats` | 获取评价统计 |
| 学习 | POST | `/api/learning/trigger` | 触发学习 |
| 学习 | GET | `/api/learning/status` | 获取学习状态 |
| 实验 | POST | `/api/experiments` | 创建实验 |
| 实验 | GET | `/api/experiments` | 获取实验列表 |
| 实验 | GET | `/api/experiments/{experiment_id}` | 获取实验详情 |
| 实验 | POST | `/api/experiments/{experiment_id}/start` | 启动实验 |
| 实验 | POST | `/api/experiments/{experiment_id}/stop` | 停止实验 |
| 实验 | POST | `/api/experiments/{experiment_id}/allocate` | 分配实验流量 |
| 实验 | DELETE | `/api/experiments/batch` | 批量删除实验 |
| 配置 | GET | `/api/config` | 获取系统配置 |
| 配置 | GET | `/api/config/processing` | 获取文档处理配置 |
| 配置 | PUT | `/api/config/processing` | 更新文档处理配置 |
| 配置 | GET | `/api/config/model` | 获取模型配置 |
| 配置 | POST | `/api/config/reset` | 重置配置为默认值 |
| 配置 | GET | `/api/config/info` | 获取系统信息 |
| 通知 | WS | `/ws/notifications` | 通用实时通知通道 |
| 通知 | WS | `/ws/kb` | 知识库变更通知 |
| 通知 | WS | `/ws/docs/{kb_id}` | 文档变更通知 |
| 通知 | WS | `/ws/upload/{upload_id}` | 上传进度通知 |
| 健康检查 | GET | `/health` | 健康检查 |
| 健康检查 | GET | `/health/detail` | 详细健康检查（含数据库、Redis） |
| 监控 | GET | `/metrics` | 获取 Prometheus 监控指标 |
| 监控 | POST | `/metrics/reset` | 重置监控指标 |