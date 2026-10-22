# Agent 护栏与 HITL 审批点（AM-2）

> 状态：已实施（2026-10-22）
> 关联：[agent-maturity-gap-analysis.md](agent-maturity-gap-analysis.md)（AM-2 来源）、[agent-evolution.md](agent-evolution.md)（有界 Agent 循环）、[agent-eval-trace.md](agent-eval-trace.md)（轨迹可观测）
> 定位：安全底线（P1）。**不引入 LangGraph checkpoint**——持久化/断线恢复留给 AM-4。

## 1. 背景与目标

对标结论（AM-2）：本项目此前只有入口敏感词与 `fetch_webpage` 的 URL 校验，缺少

- 提示注入 / 越权指令防护（输入）
- 统一的工具参数安全校验（工具层）
- 凭证 / 系统提示泄漏防护（输出）
- 人在环审批点（高危工具执行前）

目标：以**确定性规则**（零额外 LLM 调用、零额外延迟、可单测）补齐三层护栏，并提供
「高危工具执行前人工批准」的 HITL 通道。当前工具集全为只读/外部读取，**审批名单默认为空**，
框架先就绪；一旦上线写操作工具，只需配置 `AGENT_APPROVAL_TOOLS` 即可生效。

非目标：

- 不做 checkpoint / 断线恢复（依赖 AM-4 持久化执行）
- 不做服务端 MCP 暴露（AM-3）
- 不替换既有敏感词过滤器（入口敏感词仍在 API 层先执行）

## 2. 分层架构

```
用户问题
  │
  ├─ [API 层] 敏感词过滤（既有）
  │
  ├─ [管线入口] InputGuard ── tripwire ──▶ 拒答（guardrail_blocked）
  │
  ├─ Agent DECIDE
  │
  ├─ [执行前] ApprovalGate ── approval_required(SSE) ──▶ 等待批准/拒绝
  │
  ├─ [工具入口] ToolGuard ── 参数/URL/风险校验 ──▶ ToolResult(success=False)
  │
  ├─ ACT / OBSERVE
  │
  └─ [chunk 出口] OutputGuard ── 敏感信息脱敏 + trace 埋点 ──▶ SSE
```

收口点选择：

| 护栏 | 收口位置 | 理由 |
|---|---|---|
| InputGuard | `pipeline/stages.py::_pipeline_process` 首步 | 覆盖流式/非流式、所有问答模式；早于改写/检索 |
| ToolGuard | `ToolManager.execute` / `execute_parallel` | 全部工具调用的唯一入口，含旧 FunctionCalling 路径 |
| ApprovalGate | `AgentOrchestrator.run_stream` ACT 之前 | 需要向 SSE 发事件，只有编排器持有流 |
| OutputGuard | `pipeline/stages.py::_pipeline` 事件转发层 | 所有 chunk 的统一出口，覆盖各生成阶段 |

## 3. 规则定义

### 3.1 InputGuard（输入）

| 规则 | 判定 | 动作 |
|---|---|---|
| `input_empty` | 去空白后为空 | 放行（上游已有校验，避免误伤） |
| `input_too_long` | 长度 > `GUARDRAIL_INPUT_MAX_CHARS`（默认 4000） | 拒答 |
| `control_chars` | 含 NUL 或控制字符 ≥ 20 个 | 拒答 |
| `prompt_injection` | 中英文注入特征（忽略/无视既有指令、套取系统提示、越狱模式、身份覆盖、`ignore previous instructions` 等） | 拒答 |

- 文本先做 NFKC 归一化，规避全角规避；
- 只做「明确指令型」匹配（要求关键词共现），避免把正常提问/引用文档误判；
- 拒答走正常 chunk 通道（`answer_type=guardrail_blocked`），**不写语义缓存**。

### 3.2 ToolGuard（工具）

| 规则 | 判定 | 动作 |
|---|---|---|
| `arg_too_large` | 参数 JSON 序列化长度 > `GUARDRAIL_TOOL_MAX_ARG_CHARS`（默认 4000） | 拒绝执行 |
| `unsafe_url_scheme` | URL 参数 scheme 非 http/https（`file:`/`gopher:`/`javascript:` 等） | 拒绝执行 |
| `private_network` | URL host 为 localhost / 私网 / 链路本地 / 云元数据地址（`169.254.169.254` 等） | 拒绝执行 |
| `url_userinfo` | URL 含 `user:pass@` | 拒绝执行 |
| `path_traversal` | 任意字符串参数含 `../` 或 `..\\` | 拒绝执行 |

- 工具通过 `BaseTool.risk_level` 声明风险等级：`read`（默认）/ `external`（外部网络）/ `write`（有副作用）；
- 被拦截时返回 `ToolResult(success=False, error="安全护栏拦截: ...")`，Agent 观察中可见，模型可换策略；
- `ToolGuard` 不依赖 Redis/网络，纯函数式校验。

### 3.3 OutputGuard（输出）

| 规则 | 判定 | 动作 |
|---|---|---|
| `private_key` | PEM 私钥头 | 脱敏 |
| `openai_key` | `sk-` 开头长随机串 | 脱敏 |
| `aws_key` | `AKIA` + 16 位大写字母数字 | 脱敏 |
| `db_credentials` | `postgres://user:pass@` | 脱敏 |
| `bearer_token` | `Bearer` + 20 位以上 token | 脱敏 |
| `system_prompt_leak` | 决策/回答提示词特征句逐字外泄 | 脱敏 |

- 命中即把敏感片段替换为 `[已屏蔽敏感内容]`，同时修正 `state.final_answer`，
  确保 SSE、落库、trace 三处一致；
- 每命中一次写 `trace.stages` 的 `guardrail_output`（status=redacted，detail 记规则名）；
- 已知限制：密钥跨 chunk 切分时可能漏检（单个 chunk 内检测）；已在文档登记，后续可加滚动窗口。

## 4. HITL 审批协议

### 4.1 模式

`AGENT_APPROVAL_MODE`：

| 值 | 行为 |
|---|---|
| `off`（默认） | 不检查，保持现状 |
| `reject` | 命中审批名单的工具有**直接拒绝**，返回「需人工审批，当前未启用审批通道」观察 |
| `wait` | SSE 发 `approval_required`，等待前端批准/拒绝，超时（默认 60s）自动拒绝 |

审批触发条件（任一命中）：

1. 工具名出现在 `AGENT_APPROVAL_TOOLS`（逗号分隔）；
2. 工具 `risk_level == "write"`；
3. 工具 `requires_approval == True`。

### 4.2 SSE 事件

```json
{
  "type": "approval_required",
  "approval_id": "<uuid>",
  "tool": "write_note",
  "arguments": {"...": "..."},
  "timeout_s": 60,
  "expires_at": "2026-10-22T12:00:00+00:00"
}
```

`wait` 模式最多阻塞 `AGENT_APPROVAL_TIMEOUT_S` 秒；批准后继续执行原工具，拒绝则写入
观察「用户拒绝执行工具 X」并把该调用记为失败（模型可改写策略或直接作答）。

### 4.3 审批回执 API

`POST /api/chat/approvals/{approval_id}`

```json
{ "approved": true, "reason": "可选" }
```

- 需登录；只允许**发起该审批的用户**回执（Redis 记录 `user_id`，不匹配返回 403）；
- 审批记录存 Redis：`guardrail:approval:{id}`，JSON，TTL = 超时 + 30s；
- Redis 不可用或记录不存在 → 404 / 视为拒绝（fail-closed，安全优先）；
- 同一审批只能回执一次（已处理返回 409）。

## 5. 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `GUARDRAIL_ENABLED` | `true` | 总开关 |
| `GUARDRAIL_INPUT_ENABLED` | `true` | 输入护栏 |
| `GUARDRAIL_OUTPUT_ENABLED` | `true` | 输出护栏 |
| `GUARDRAIL_TOOL_ENABLED` | `true` | 工具护栏 |
| `GUARDRAIL_INPUT_MAX_CHARS` | `4000` | 输入长度上限 |
| `GUARDRAIL_TOOL_MAX_ARG_CHARS` | `4000` | 工具参数长度上限 |
| `AGENT_APPROVAL_MODE` | `off` | `off` / `reject` / `wait` |
| `AGENT_APPROVAL_TOOLS` | 空 | 审批工具名单（逗号分隔） |
| `AGENT_APPROVAL_TIMEOUT_S` | `60` | 等待审批超时 |

全部新增键同步写入 `.env.example`（满足 env-consistency 校验 C）。

## 6. 可观测

- `trace.stages` 新增：`guardrail_input` / `guardrail_output` / `approval`，
  `detail` 含 `kind`/`rule`/`tool`/`approved` 等；
- 工具护栏在 `ToolManager` 内无 trace 上下文，命中仅写 `logger.warning`
  （`tool=... rule=...`），并作为失败 `ToolResult` 进入 Agent 观察；
- 不新增数据库列（复用 `stages` JSON），避免迁移。

## 7. 测试计划

| 层 | 用例 |
|---|---|
| InputGuard | 正常问题放行；超长；控制字符；中/英注入；全角归一化 |
| ToolGuard | 正常参数放行；超长参数；file:// / 私网 / 元数据地址 / userinfo / 路径穿越 |
| OutputGuard | 私钥 / sk- / AKIA / DB 凭证 / Bearer / 系统提示外泄 → 脱敏；正常文本不动 |
| ApprovalGate | 未命中不触发；reject 直接拒绝；wait 批准/拒绝/超时；非发起人回执 403；Redis 不可用 fail-closed |
| 编排器 | 审批拒绝后观察写入、循环继续；批准后工具执行 |
| 回归 | 全量后端单测 + 既有 Agent/管线测试不回归 |

## 8. 风险与后续

- **误杀风险**：注入规则采用关键词共现，若线上出现误杀，可把 `GUARDRAIL_INPUT_ENABLED` 关闭或收窄规则；
- **审批断线**：`wait` 模式依赖流保持打开，断线即超时拒绝；真正的可恢复审批依赖 AM-4；
- **跨 chunk 密钥**：见 §3.3；
- **后续**：AM-4 落地后，把审批状态迁移到持久化 run 表并支持 `run_id` 恢复；AM-6 工具治理时同步维护 `risk_level`。
