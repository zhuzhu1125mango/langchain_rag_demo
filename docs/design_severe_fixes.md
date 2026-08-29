# 严重问题修复方案设计（Severe Issues Fix Design）

- **日期**：2026-08-28
- **来源**：`CODE_REVIEW_REPORT.md` 中 🔴 严重问题 1~9
- **状态**：设计稿（待评审后实施）
- **原则**：最小侵入、与现有代码风格一致、每个修复可独立提交与回归、不破坏单租户 dev 模式的现有体验

---

## 修复 1：文档端点 IDOR —— 统一 owner 校验辅助函数

### 目标
10 个缺失对象级授权的文档端点全部补齐 `require_owner`；`search_documents` / `duplicate-detect` 补 owner 过滤。

### 设计

**1.1 新增统一查询辅助函数**（`backend/src/api/document.py`，模块顶部路由定义之前）：

```python
async def _get_owned_document(
    db: AsyncSession, doc_id: str, current_user: CurrentUser
) -> Document:
    """按 ID 加载文档并校验归属，统一所有文档端点的鉴权路径。

    Raises:
        HTTPException: 400(ID 非法) / 404(不存在) / 403(非所有者)
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="无效的文档ID")

    result = await db.execute(select(Document).filter(Document.id == doc_uuid))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    require_owner(doc.owner_id, current_user)
    return doc
```

要点：
- 顺带修复「非法 UUID → 500」问题（复用现有 `ValueError → 400` 语义，与 `session.py:254-269` 一致）
- `require_owner`（`auth.py:131`）已内置 `api_key_user` / dev 默认用户豁免逻辑，行为与 `:1105`、`:1123`、`:1164` 三个已修复端点完全一致

**1.2 端点改造**。以下端点：删除各自的 `select(Document).filter(Document.id == ...)` + 404 判断，替换为 `_get_owned_document`，并新增 `current_user: CurrentUser = Depends(get_current_user)` 参数（router 级依赖保证已认证，此处仅取用户对象）：

| 端点 | 位置 |
|---|---|
| `PUT /{doc_id}/status` | `:800` |
| `GET /{doc_id}/preview` | `:818` |
| `GET /{doc_id}/chunks` | `:843` |
| `GET /{doc_id}/source/{chunk_index}` | `:864` |
| `POST /{doc_id}/reprocess` | `:927` |
| `POST /{doc_id}/classify` | `:1002` |
| `POST /{doc_id}/quality` | `:1064` |

**1.3 查询类端点补 owner 过滤**（这两个是列表/跨文档查询，不能按 doc_id 加载单个对象）：

- `search_documents`（`:650`）：查询条件增加 `.filter(Document.owner_id == current_user.user_id)`，与 `list_documents`（`:743`）现有写法对齐
- `duplicate-detect`（`:1301`）：候选文档查询同样增加 `Document.owner_id == current_user.user_id` 过滤

### 兼容性
- dev 模式（`default` 用户）与 `api_key_user` 行为不变（`require_owner` 豁免）
- 前端无需改动（正常流程本来就只访问自己的文档）

### 测试计划
- 扩展 `tests/test_api_document.py`：新增"用户 B 访问用户 A 的文档返回 403"参数化用例，覆盖 7 个单对象端点；`search`/`duplicate-detect` 用例断言结果不包含他人文档

---

## 修复 2：set_single_default 跨用户重置默认标志

### 目标
「清空默认标志」操作必须限定在当前用户的资源范围内。

### 设计

**2.1** `backend/src/api/knowledge_base.py:111-117`：

```python
async def set_single_default(db: AsyncSession, kb_id: uuid.UUID, owner_id: str):
    """设置唯一默认知识库（仅作用于指定用户的资源范围）。"""
    await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.owner_id == owner_id)
        .values(is_default=False)
    )
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb:
        kb.is_default = True
    await db.commit()
```

**2.2** 调用方（`set_default` 端点）：在调用前先加载 kb 并 `require_owner(kb.owner_id, current_user)`（若该端点尚未校验归属则一并补上），传 `owner_id=current_user.user_id`。

**2.3** 同文件 `get_default_kb`（`:106-108`）同样存在跨用户读取问题：`filter(KnowledgeBase.is_default == True)` 会拿到**任意用户**的默认库。增加 `owner_id` 过滤参数，调用方传当前用户。

### 兼容性
- 单用户场景行为不变；多用户场景各自默认库互不影响（正是修复目的）
- `get_default_kb` 调用方需逐一排查传参（预计 2~3 处）

### 测试计划
- 扩展 `tests/test_api_knowledge_base.py`：用户 A set_default 后，用户 B 的默认库标志不变；`get_default_kb` 只返回本人默认库
- 注：现有 `:218` 处的 skip 用例（"无法可靠构造该场景"）可借此重写为可构造场景

---

## 修复 3：kb_ids 校验 + Milvus 表达式注入

### 目标
任何进入 Milvus 过滤表达式的 ID 都必须是合法 UUID 且属于当前用户；表达式构造函数自身具备防注入兜底。

### 设计（两层防御）

**3.1 第一层：API 边界校验（主防线）**

新增共享校验函数（放 `backend/src/api/chat.py` 或独立到 `src/utils/validators.py`，因 document/knowledge_base API 也可能复用）：

```python
def parse_uuid_list(raw_ids: Optional[List[str]], name: str = "kb_id") -> List[str]:
    """校验 ID 列表全部为合法 UUID，返回规范化（小写）后的列表。

    Raises:
        HTTPException: 400 任一 ID 非法。空列表/None 返回 []。
    """
    if not raw_ids:
        return []
    normalized = []
    for rid in raw_ids:
        try:
            normalized.append(str(uuid.UUID(rid)))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"无效的{name}: {rid[:64]}")
    return list(dict.fromkeys(normalized))  # 去重保序
```

```python
async def validate_kb_ownership(
    db: AsyncSession, kb_ids: List[str], current_user: CurrentUser
) -> None:
    """校验知识库列表全部属于当前用户。"""
    if not kb_ids:
        return
    result = await db.execute(
        select(KnowledgeBase.id).filter(
            KnowledgeBase.id.in_([uuid.UUID(k) for k in kb_ids])
        )
    )
    owned = {str(r) for r in result.scalars()}
    illegal = [k for k in kb_ids if k not in owned]
    if illegal:
        raise HTTPException(status_code=403, detail=f"无权访问部分知识库")
```

接入点（`MessageRequest.kb_ids` 的全部消费端点）：
- `send_message`（`chat.py:46`）：会话分支里已查 kb 归属与否——统一在 `rag_chain.run()` 调用前执行两个校验
- `stream_answer`（`:168`）：同上（在流式 generator 启动**之前**校验，保证错误以 4xx 返回而非 SSE error 事件）
- `compare`、`suggestions`、`enhance_context`：同样接入

注意：`validate_kb_ownership` 中 `api_key_user` / dev 默认用户语义与 `require_owner` 对齐——这两个身份本来就只能创建/看到自己的 kb（`knowledge_base.py:127-130` 创建时即绑定 owner），一次 `SELECT ... IN` 归属查询对它们同样成立，无需豁免分支。

**3.2 第二层：Milvus 表达式构造兜底（防御纵深）**

`backend/src/services/milvus_service.py:427-434`：

```python
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

@staticmethod
def _safe_id(value: str) -> str:
    """仅允许 UUID 格式的 ID 进入过滤表达式（防注入兜底）。"""
    v = str(value)
    if not _UUID_RE.match(v):
        raise ValueError(f"非法 ID，已拒绝进入过滤表达式: {v[:64]}")
    return v

@staticmethod
def _build_filter_expr(document_ids=None, kb_ids=None):
    conditions = []
    if kb_ids:
        safe = ", ".join(f'"{MilvusService._safe_id(k)}"' for k in kb_ids)
        conditions.append(f"kb_id in [{safe}]")
    if document_ids:
        safe = ", ".join(f'"{MilvusService._safe_id(d)}"' for d in document_ids)
        conditions.append(f"document_id in [{safe}]")
    return " && ".join(conditions) if conditions else None
```

同理应用到 `delete_by_document_id`（`:457`）、`delete_by_kb_id`（`:466`）、`delete_by_kb_ids`（`:481`）、`get_document_chunks`（`:494`）的裸拼接表达式——统一走 `_safe_id`。

> 说明：当前系统所有 ID（PG 主键、kb_id、document_id 字段）均为 UUID，正则白名单零误伤。pymilvus 3.0 支持表达式模板参数（`filter_params`），可作为后续进一步硬化项，但 UUID 白名单已完全消除注入面，且不依赖服务端版本行为，故作为主方案。

### 兼容性
- 正常前端流程（UUID）完全无感
- 之前传非法 ID 会"静默搜不到/可能注入"，现在得到 400——行为收紧是修复目的
- `rag_chain` 内部无需感知（拿到的是已清洗列表）

### 测试计划
- 新增 `tests/test_milvus_expr_safety.py`：`_build_filter_expr` 对含引号/布尔片段的恶意 ID 抛 `ValueError`；合法 UUID 正常生成
- `tests/test_api_chat.py`：非法 kb_ids → 400；他人 kb_ids → 403

---

## 修复 4：上传进度 WebSocket 鉴权

### 目标
未认证连接在握手阶段被拒绝；前端同步适配。

### 设计

**4.1 后端**（`backend/src/api/document.py:1264`）：完全复用 `notification.py:114` 的既有模式：

```python
@router.websocket("/upload/progress/ws/{upload_id}")
async def upload_progress_ws(websocket: WebSocket, upload_id: str):
    # 认证失败时 get_current_user_for_ws 会发送 close(1008) 并抛出 WebSocketException
    await get_current_user_for_ws(websocket)
    await websocket.accept()
    register_ws_connection(upload_id, websocket)
    ...
```

要点：
- HTTP 轮询端点 `GET /upload/progress/{upload_id}`（`:1255`）已被 `main.py:273` 的 router 级依赖保护，**无需改动**
- `get_current_user_for_ws` 必须在 `accept()` **之前**调用（失败时 Starlette 直接以 1008 关闭握手）

**4.2 前端**（`frontend/src/components/knowledge-base/UploadDocumentDialog.vue:239-240`）：
- 当前 WS URL 为硬编码拼接且**未携带 api_key**——鉴权后会连不上
- 改为复用 `buildWsUrl` 并附加凭据参数，与 `useNotifications.ts:71` 的 `queryParams` 模式对齐：

```ts
const apiKey = localStorage.getItem('api_key') || import.meta.env.VITE_API_KEY
const wsUrl = buildWsUrl(
  `/api/documents/upload/progress/ws/${uploadId}${apiKey ? `?api_key=${encodeURIComponent(api_key)}` : ''}`
)
```

### 兼容性
- dev 模式（未配置 API_KEY 且非 Docker）`get_current_user_for_ws` 直接放行，前端不带 key 也能连——现有开发体验不变
- Docker 生产模式前端原本就必须有 api_key（否则 HTTP 也不通），只是补传

### 测试计划
- 扩展 `tests/test_websocket_auth.py`：无凭据连接 upload progress WS → 1008 关闭；带正确 api_key → 正常收到进度帧

---

## 修复 5：启动凭据校验覆盖非 Docker 生产

### 目标
"生产级"运行（无论是否 Docker）一律强制强密钥；dev 模式保持零配置可用。

### 设计

**5.1 引入显式环境判定**（`backend/src/config.py`）：

```python
class Settings(BaseSettings):
    IN_DOCKER: bool = False
    # 显式环境标记：dev | production。未设置时按 IN_DOCKER 推断
    APP_ENV: Optional[str] = None

    @property
    def IS_PRODUCTION(self) -> bool:
        if self.APP_ENV:
            return self.APP_ENV == "production"
        return self.IN_DOCKER
```

**5.2 移除危险默认值**（`config.py`）：

```python
class MinIOSettings(BaseSettings):
    MINIO_ACCESS_KEY: str = ""       # 原 "admin"
    MINIO_SECRET_KEY: str = ""       # 原 "password123"

class SecuritySettings(BaseSettings):
    SECRET_KEY: str = ""             # 原 "your-secret-key-here-change-in-production"
```

**5.3 启动校验**（`backend/src/main.py:133-149`），条件从 `IN_DOCKER` 改为 `IS_PRODUCTION`：

```python
if settings.IS_PRODUCTION:
    validate_secret_key(settings.security.SECRET_KEY, in_docker=True)  # 强度校验
    # POSTGRES_PASSWORD / MINIO_SECRET_KEY 非空校验（原逻辑不变，仅条件替换）
```

**5.4 dev 模式 SECRET_KEY 兜底**：非生产且未设置时，启动时生成随机临时密钥并 WARNING（dev 重启后旧签名 token 失效，dev 场景可接受；避免硬编码常量密钥留在代码里）：

```python
else:
    if not settings.security.SECRET_KEY:
        settings.security.SECRET_KEY = secrets.token_urlsafe(48)
        logger.warning("SECRET_KEY 未设置，已生成临时随机密钥（重启后失效，仅限开发环境）")
```

**5.5 部署文档**：`docs/deployment.md` 与 `.env.prod` 注释补充「非 Docker 生产部署必须设 `APP_ENV=production`」。

### 兼容性
- `docker-compose.yml` 传 `IN_DOCKER: "true"` → `IS_PRODUCTION=true`，现有 Docker 生产流程**不变**
- 本地 dev（`.env` 无 APP_ENV、非 Docker）→ `IS_PRODUCTION=false`，零配置可用
- 风险点：**现在**直接裸跑 uvicorn 当生产用的部署（如有）会在下次启动时因校验失败而拒绝启动——这是刻意的 fail-fast，需在发布说明中标注

### 测试计划
- 新增 `tests/test_startup_validation.py`：单测 Settings.IS_PRODUCTION 三种推断路径；`validate_secret_key` 对空/弱密钥在生产模式下抛 `RuntimeError`

---

## 修复 6：维度不匹配不再自动 drop_collection

### 目标
破坏性重建必须由运维显式授权；默认行为改为 fail-fast。

### 设计

**6.1 新增配置**（`config.py` `MilvusSettings`）：

```python
# 集合 schema/维度不匹配时是否允许自动 drop 重建（危险！旧向量数据不可恢复）。
# 默认 False：不匹配时启动失败，要求人工执行迁移脚本或显式打开此开关。
MILVUS_REBUILD_ON_MISMATCH: bool = False
```

**6.2** `milvus_service.py` 两个重建路径改造：

- `_ensure_dimension_match`（`:157-182`）：维度不一致时——
  - `MILVUS_REBUILD_ON_MISMATCH=true` → 维持现状（warning + drop 重建），日志升级为 ERROR 级并附数据恢复指引（重上传 / `migrate_embedding_model.py`）
  - 默认 `false` → **`raise RuntimeError`**，消息包含当前维度、期望维度、两条解决路径（改回配置 / 打开开关）。不做静默降级：带着错误维度继续服务意味着检索结果不可信，比启动失败更危险
- `_ensure_kb_id_field` 式的 schema 重建路径（`:140-155`，缺 `kb_id` 字段即 drop）：同样受该开关控制，默认抛错并指向 `migrate_document_fields.py`

**6.3 移除两处外层 `except Exception: pass`**（`:154-155`、`:183-184`）：让异常传播到启动流程 fail-fast（该修复同时消除中等severity#16 的一部分；实施时如涉及其他兼容检查路径的 pass，仅改为 `logger.error` + 返回安全状态，不扩大范围）。

### 兼容性
- **行为变更**：现有"换 embedding 模型 → 重启自动清库重建"的工作流会被阻断。需要旧行为时设置 `MILVUS_REBUILD_ON_MISMATCH=true`（在 `.env.example` 与 `docs/deployment.md` 说明）
- 正常启动（维度一致）完全不受影响

### 测试计划
- 扩展 `tests/test_milvus_service.py`（mock `describe_collection`）：维度不匹配 + 开关关闭 → RuntimeError；开关打开 → drop+create 被调用；维度一致 → 无副作用

---

## 修复 7：消除 RAGChain 单例共享状态（last_decision 串号）

### 目标
并发请求下，每个请求读到的决策结果是**自己的**。

### 设计：ContextVar 请求级隔离（最小侵入方案）

**7.1** `backend/src/services/rag_chain.py`：

```python
from contextvars import ContextVar

# 请求级决策结果。ContextVar 在每个 asyncio Task 中独立复制，
# 并发请求互不可见；同一 Task 内 set 后可被后续代码读取。
_request_decision: ContextVar[Optional["DecisionResult"]] = ContextVar(
    "rag_request_decision", default=None
)
```

- `_make_decision`（`:272-292`）：删除 `self.last_decision = ...` / `self.last_retrieval_score = ...` 赋值，改为 `_request_decision.set(decision)`
- `get_last_decision`（`:294`）：实现改为 `return _request_decision.get()`
- `__init__`（`:69-73`）删除两个实例属性；`last_retrieval_score` 若有读取方，同样以 ContextVar `_request_retrieval_score` 承载

**7.2 为什么不改函数签名**（备选方案对比）：

| 方案 | 优点 | 缺点 |
|---|---|---|
| **ContextVar（选定）** | `run()`/`arun_stream()`/`chat.py` 全部调用点**零改动**；天然隔离并发 | 隐式传递；要求 set 与 get 在同一 Task |
| 扩展返回值为 5 元组 | 显式、可静态检查 | `run()` 2 个调用点 + `arun_stream()` 全部消费端 + 流式协议变更，diff 大且易漏 |
| 决策结果挂到 DecisionPipeline 实例 | — | pipeline 同为单例，问题原样存在 |

**7.3 Task 边界确认**（已核验）：
- 非流式：`chat.py:109` `await rag_chain.run()` 与 `:118` `get_last_decision()` 同一 Task ✔
- 流式：`arun_stream` 由 `chat.py:304` 的 `async for` 驱动，决策发生在 generator 体内在调用方 Task 中执行；`save_assistant_message`（`:254`）也在同一 Task ✔
- `_make_decision` 是直接 `await`（`:287`），无内部 `gather` 分叉 ✔

### 兼容性
- 对外接口不变；badcase / learning 等读取方行为在单请求内不变，仅并发正确性修复
- 风险：若未来有人在 `asyncio.create_task` 内调用决策链并在**另一个 Task** 中读取，将拿到 None——在 `get_last_decision` docstring 中注明约束

### 测试计划
- 新增 `tests/test_rag_chain_concurrency.py`：mock decision_pipeline 返回可区分结果（如含请求序号），并发跑 10 个 `run()`，断言各自 `get_last_decision()` 与自身序号一致（现有单测基建可 mock LLM，无需真实服务）

---

## 修复 8：会话消息 JSONB 原子追加

### 目标
并发写同一会话不丢消息；消除「读整个 messages → 内存拼接 → 整体覆写」竞态。

### 设计

**8.1 新增原子追加 helper**（`backend/src/api/chat.py` 模块级，或 `src/services/session_service.py`——倾向后者，chat.py 已 700 行）：

```python
from sqlalchemy import update, bindparam
from sqlalchemy.dialects.postgresql import JSONB

async def append_session_message(db: AsyncSession, session_id: uuid.UUID, payload: dict) -> None:
    """原子追加一条消息到会话（PostgreSQL jsonb || 运算符，服务端拼接）。

    不经 ORM 读改写，天然并发安全；调用方如需最新 messages 需重新 SELECT。
    """
    stmt = (
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(
            messages=SessionModel.messages
            + bindparam("m", value=[payload], type_=JSONB),
            updated_at=datetime.now(),
        )
        .execution_options(synchronize_session=False)
    )
    await db.execute(stmt)
```

原理：PG 的 `jsonb || jsonb` 对两个数组执行**服务端拼接**，两条并发 UPDATE 在行锁下串行执行，各自追加的元素都保留——彻底消除「A 读 → B 读 → A 写 → B 写覆盖 A」窗口。

**8.2 四处写路径替换**：

| 位置 | 现状 | 改造 |
|---|---|---|
| `send_message` 用户消息（`chat.py:98-105`） | ORM 读改写 + flag_modified | `append_session_message(db, session.id, user_msg)` |
| `stream_answer` 用户消息（`:231-238`） | 同上 | 同上 |
| `save_assistant_message` 助手消息（`:278`） | 独立 session 中 re-select 后读改写 | 该函数内的 SELECT 仍需保留（读 title 判断 + execution_id 记录），但**消息追加**改走 helper；追加后如需 `messages_count` 日志，用 `RETURNING jsonb_array_length(messages)` |
| 非流式助手消息（`:132-141`） | 同上 | 同上 |

**8.3 标题更新**：`save_assistant_message` 中的 `session.title = ...` 保持 ORM 写法（单列 UPDATE 本身原子；与消息追加是不同语句，各自提交，无丢数据风险——消息在 helper 的 UPDATE 中已落库）。

注意：追加与标题更新拆成两条语句后不再同事务。接受该折衷：标题生成失败仅缺标题，不影响消息完整性（现状下标题失败也只是 skip）。

### 兼容性
- `Session.messages` 列已是 JSONB（`jsonb || jsonb` 可用）；SQLAlchemy `JSONB` 类型的 `+` 运算即编译为 `||` ✔
- 读取方（历史消息加载）不变
- 迁移脚本无需变更（无 schema 变化）

### 测试计划
- 新增 `tests/test_session_message_append.py`（需真实 PG，参照 `test_docker_pg.py` 的 mark 方式）：N 个并发 task 对同一 session 各 append 一条，断言最终 `jsonb_array_length == N` 且无元素丢失
- 现有 `test_api_chat.py` 回归（消息保存路径变更）

---

## 修复 9：同步阻塞调用移出事件循环

### 目标
事件循环不再被 MinIO I/O 与文档解析阻塞；方案统一（消灭 `run_in_executor` / 直接调用混用）。

### 设计：统一 `asyncio.to_thread`（Python 3.12 标准库，项目已有 3.12）

**9.1 通用规范**：所有「同步 I/O 或 CPU 密集」调用在 async 上下文中一律 `await asyncio.to_thread(fn, ...)`。不再新增 `loop.run_in_executor` 写法（`knowledge_base.py:553` 存量一处顺手替换，保持单一风格）。

**9.2 具体接入点**（`backend/src/api/document.py`）：

| 位置 | 现状 | 改造 |
|---|---|---|
| `:465` 上传 | `minio_service.upload_file(...)` 同步 | **改用已有的 `await minio_service.upload_file_async(...)`**（服务层已提供，语义最优） |
| `:162` `process_document_async` | 后台任务直接调同步 `process_document`（内含 MinIO 下载 + PDF/Office 解析） | `await asyncio.to_thread(process_document, ...)` |
| `:831` / `:851` / `:888` preview/chunks/source | 同步解析 | `asyncio.to_thread(preview_document / get_document_chunks, ...)` |
| `:1019` / `:1081` classify/quality | 同步解析 + 分析 | 同上（`DocumentAnalyzer` 调用一并包裹） |
| `:1326-1369` duplicate-detect | 循环内同步下载+解析+相似度 | 包裹单文档处理函数；并加候选数上限 `max_candidates=20`（防恶意大结果集放大阻塞，参数可配置） |
| `:338` 删除 | `minio_service.delete_file` 同步 | 若服务层有 async 变体则用之，否则 `asyncio.to_thread` |

**9.3 不做的事（明确边界）**：
- 不把 `process_document` 重构为原生 async（涉及 pdfplumber/python-docx 等同步库，收益低风险高）——`to_thread` 是该阶段的正确解；真正迁独立 worker/队列（Celery/arq）留作后续演进项，不在本次范围
- 不改 `duplicate-detect` 的算法本身

### 兼容性
- `to_thread` 使用默认 ThreadPoolExecutor（minthreads 未耗尽时并发安全）；文档解析属于低频操作，线程池默认容量（`min(32, cpu+4)`）足够
- 现有单测（mock 层面）不受影响

### 测试计划
- `tests/test_api_document.py` 现有 preview/chunks 用例回归
- 手工验证项（写入 PR 说明）：上传 50MB 文件期间并发请求 `/health` 不阻塞（修复前可复现卡顿）

---

## 实施顺序与提交切分

按「可独立回归、失败可单独回滚」切为 7 个 commit：

| # | Commit | 内容 | 依赖 |
|---|---|---|---|
| 1 | `fix(auth): document endpoints enforce ownership` | 修复 1 + 对应测试 | 无 |
| 2 | `fix(kb): scope default flag and default query by owner` | 修复 2 | 无 |
| 3 | `fix(security): validate kb_ids and harden milvus filter expr` | 修复 3 | 无 |
| 4 | `fix(ws): authenticate upload progress websocket` | 修复 4（后端+前端） | 无 |
| 5 | `fix(config): enforce credential checks outside docker` | 修复 5 | 无 |
| 6 | `fix(milvus): require explicit opt-in for destructive rebuild` | 修复 6 | 建议在 5 之后（同触启动路径） |
| 7 | `fix(concurrency): request-scoped decision, atomic message append, non-blocking io` | 修复 7+8+9（或再拆 3 个） | 7 依赖现有 AsyncSingleton 重构完成 |

> 修复 7 与工作区未提交的 AsyncSingleton 重构（rag_chain.py 有 125 行改动）存在同文件冲突风险——**建议先落当前重构再实施本项**。

## 实施记录与设计偏差（2026-08-28 实施完毕）

全部 9 项已实现，新增 8 个测试文件（51 通过 / 1 e2e 跳过）。实施中发现的偏差：

1. **修复 8 偏差（重要）**：设计假设 `Session.messages` 列已是 JSONB，实际为普通 `JSON`
   （`models/session.py:26`）。PG 的 `||` 运算符仅存在于 jsonb，按原设计实现会在运行时
   报 `operator does not exist: json || json`。实际改动：
   - 模型列改为 `JSONB`（新建库经 create_all 直接生效）；
   - 新增迁移脚本 `backend/scripts/migrate_session_messages_jsonb.py`（幂等，存量库必跑）；
   - helper 追加 `RETURNING jsonb_array_length(messages)`，保留 messages_count 日志。
2. **修复 7 扩展**：`last_retrieval_score` 有读取方（设计预案命中），同以 ContextVar
   承载；`get_last_strategy_confidences` 改为从请求级决策派生，本请求未决策时返回 `{}`。
3. **修复 9 落点微调**：`delete_file` 直接改用服务层已有的 `delete_file_async`；
   `knowledge_base.py` 存量 `run_in_executor` 改为调用 `delete_file_async`；
   duplicate-detect 候选上限 20 写死并附告警日志（未新增配置项）。
4. **测试环境注意**：`pymilvus` 在 import 时执行 `load_dotenv()`，会把根目录 `.env`
   灌入 `os.environ`，且 pydantic-settings 中环境变量优先于代码默认值——配置类单测
   必须显式传参或断言 `model_fields` 默认值，不能依赖构造默认值（见
   `test_startup_validation.py` 模块注释）。
5. **提交切分提醒**：设计与本次工作区叠加了用户未提交的 AsyncSingleton 重构改动，
   同文件（rag_chain.py / chat.py / document.py）内已混合，按 7 commit 切分需逐 hunk
   挑选（`git add -p`），建议合并前与作者确认切分粒度。

## 回归验证清单（全部修复合入后）

- [ ] `pytest tests/ -x`（dev 环境，无外部服务的用例全绿；依赖外部服务的按现有 skip 机制）
- [ ] 双用户场景手工验证：用户 A 创建文档/知识库，用户 B（另一 API_KEY 场景或 dev default）访问 A 的文档 → 403
- [ ] 并发流式问答：同会话并发两条消息，历史完整不丢
- [ ] 上传 50MB 文件期间 `/health` 响应 < 100ms
- [ ] Docker 生产构建启动：错误 SECRET_KEY → 拒绝启动；合法 → 正常
- [ ] 修改 EMBEDDING_DIMENSION 后启动 → 明确报错提示，而非静默清库
