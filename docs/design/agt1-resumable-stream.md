# AGT-1：可恢复、可取消的流式问答

> 落地日期：2026-11-01 ｜ 关联：AM-4（持久执行）、UX-1（停止生成）、AM-2（HITL 审批）
> 代码：`backend/src/services/chat_run.py`、`backend/src/api/chat/runs.py`、`backend/src/api/chat/stream.py`、`frontend/src/composables/useChatStream.ts`、`useSessionLoader.ts`

## 1. 改造前的问题

一次问答的生命周期**绑死在 HTTP 连接上**：`generate()` 本身就是 ASGI 响应生成器，
客户端一断连，`CancelledError` 从 `yield` 点抛进来，生成立即终止。

| 触发 | 旧行为 | 用户感受 |
|---|---|---|
| 切 Wi-Fi / 地铁断网 | 生成被取消，只落库半截答案 | 答案没了，只能重问（再烧一次 LLM） |
| 刷新页面 / 切标签 | 同上；且助手消息要等生成结束才落库 | 会话里只剩一个孤立的问题 |
| 点「停止」 | 前端 `abort()`，服务端察觉断连前仍在生成 | token 白烧，回来发现回答还在变长 |
| 长检索 30s 无事件 | 代理/浏览器按空闲超时掐连接 | 偶发的「连接断开」，无法复现 |

## 2. 模型：运行与连接解耦

```
POST /chat/stream ──创建──> ChatRun（run_id, 事件缓冲, 序号, 状态）
                              ▲          │
                    publish ──┘          └──> 订阅者（0..n 条连接）
客户端 A（首连）  ──subscribe(after=0)──┐
客户端 A（重连）  ──subscribe(after=N)──┤─> 同一缓冲，各自游标
客户端 A（另一设备）──subscribe(after=0)─┘
```

- **生成跑在独立 task**（`_pump`）：连接只是订阅者，断开不影响生成，答完照常落库。
- **事件带序号**：每帧 `id: <seq>`，既是 SSE 标准续传游标，也是客户端去重依据。
- **多订阅者**：同一 run 可被多条连接同时跟随（刷新后新旧连接短暂并存也不冲突）。
- **终态**：`completed / stopped / failed`。终态后保留 300s（TTL）供「刚答完就刷新」重连，之后回收。

## 3. 端点契约

| 方法 | 路径 | 语义 | 失败 |
|---|---|---|---|
| POST | `/api/chat/stream` | 创建运行并订阅；首帧 `{"type":"run","run_id":...}` | 429 = 在途运行超用户上限 |
| GET | `/api/chat/runs/{id}/stream?after=N` | 从游标续传（也认 `Last-Event-ID`） | 404 已过期 / 403 非本人 / 409 事件断档 |
| POST | `/api/chat/runs/{id}/stop` | 取消并**等部分答案落库**后返回 | 404 / 403 |
| GET | `/api/chat/runs/active?session_id=` | 找回该会话在途运行（刷新/换设备） | — |

几个刻意的决定：

- **越权即 403**：`run_id` 是随机 UUID，但仍按 `user_id` 校验——答案内容是用户数据，
  不能靠「猜不到」当权限。
- **停止要等落库**：`stop` 返回前 `await run.settle()`，否则用户点停止后立刻刷新，
  看到的仍是「只有一个问题」。落库任务在取消路径上，靠 ContextVar 登记到 run。
- **404/409 是正常语义**：前者是运行已过期，后者是中间事件被缓冲上限回收；
  前端据此回落到「重新加载历史」，不重试、不空等。
- **心跳**：空闲 15s 发 SSE 注释帧（`: ping`），长检索与审批等待不再被代理掐断。

## 4. 前端的两个行为变化

1. **停止 = 先停服务端再断连**。只 `abort()` 已经停不掉生成了，
   `stopGeneration()` 先 `POST /runs/{id}/stop`，失败也照常本地 abort（服务端有空闲回收兜底）。
2. **断线自动续传**。流在没收到 `end` 时结束 = 静默中断，用 `run_id + lastEventSeq`
   重连一次；404/409 直接放弃并提示「连接断开」，其余（网络仍不通）退避重试一次。
   刷新场景走另一条路：`useSessionLoader` 发现会话有在途运行 → 放占位消息 →
   等运行结束 → 重拉历史（不逐帧重放，避免与占位消息生命周期纠缠）。

## 5. 边界（明确不做）

- **进程内缓冲**。进程重启即丢，本特性只覆盖「连接断、进程活」。
  跨重启恢复需要 checkpoint，属 AM-4。
- **不做步骤级恢复**。续传是「重放已产生事件 + 继续接收」，不是从半截 Agent 步骤续跑。
- **缓冲上限**：单 run 4000 帧，超出丢最旧并记 `dropped`；游标早于最旧帧判 409。
- **单 worker**：当前 uvicorn 无 `--workers`，注册表与连接同进程；将来多 worker 需要
  把运行路由到固定 worker（或改 Redis pub/sub），届时 404 会变成常态而非异常。

## 6. 失效模式与处置

| 失效模式 | 处置 |
|---|---|
| 客户端断连 | 订阅者退出，生成继续；答完落库 |
| 用户点停止 | `task.cancel()` → 生成器 `except CancelledError` 落部分答案 → 终态 `stopped` |
| 生成卡死（无事件） | `sweep()` 超 `CHAT_RUN_IDLE_TIMEOUT_SECONDS` 取消并 **REPORTABLE** 上报 |
| 关机 | `registry.drain()` 取消在途并等落库（与 wiki 编译、语义缓存失效排空对称） |
| 并发打满 | 预检在**用户消息落库之前**拒绝（429），避免历史里留下「有问无答」 |
| 客户端不重连 | 无关紧要：答案已落库，下次打开会话可见 |
| 需要整体回滚 | `.env.prod` 设 `CHAT_RUN_ENABLED=false` → 退回「连接即生命周期」的旧行为，**不必回滚镜像** |

## 7. 验证

- `backend/tests/test_chat_run.py`（29 例）：缓冲与游标、溢出判 gap、多订阅者独立游标、
  **订阅者退出不结束运行**、停止落库、限流预检、越权 403、过期 404、断档 409、
  `id:` 游标注入、心跳帧、终态后拒收事件、关机排空。
- `frontend/src/composables/__tests__/useChatStream.spec.ts`：停止调服务端接口、
  断线按 `after=N` 续传且不重复渲染、续传 404 回落提示。
- 全量：后端 **1508 passed / 105 skipped**，前端 **140 passed**。
