# 前端测试与 E2E 设计（P2-6）

> 状态：已实施（2026-09-06，单测 8 文件 40 用例 + E2E 3 条核心链路）
> 关联：roadmap P2-6（前端 E2E 核心链路）

## 1. 背景与现状

测试基建为 create-vue 脚手架默认模板，`package.json` 已有 `test:unit` / `test:e2e` 脚本及 vitest、`@playwright/test` devDeps，`vitest.config.ts` / `playwright.config.ts` 已存在。但存在以下缺口：

- **依赖缺失**：`@vue/test-utils` 与 `jsdom` 未安装，而 `vitest.config.ts` 已配置 `environment: 'jsdom'` → `pnpm test:unit` 当前直接失败
- **占位测试**：`e2e/vue.spec.ts` 断言根路径 `h1` 为 `'You did it!'`，与真实应用不符 → `pnpm test:e2e` 必失败
- **零覆盖**：`src/` 下无任何单测；CI 的 frontend job 仅做构建，无前端测试卡点

影响方案的关键事实：

- 聊天流式为 `POST /api/chat` + fetch ReadableStream（SSE 事件协议：`content` / `reasoning` / `search_status` / `end` / `error`，见 ChatView.vue `sendMessage`）
- 上传解析进度走 WebSocket：`/api/documents/upload/progress/ws/{uploadId}`（UploadDocumentDialog.vue，先建 WS 再发上传请求）
- axios `baseURL: '/api'`；路由守卫要求 localStorage 持有 `token` 或 `api_key`
- `mergeReasoningSteps`（合并键 `step`，2026-09-06 修复点）内联在 ChatView.vue，为纯函数但无回归测试

## 2. 目标与验收标准

前端获得可进 CI 的测试卡点，覆盖核心工具函数 / 状态 / 组件 + 三条 E2E 核心链路。

验收标准：

1. `pnpm test:unit` 全绿，且进 CI 卡点
2. `pnpm test:e2e` 三条链路全绿（Playwright route mock，不依赖真实后端与 Ollama），chromium 进 CI
3. CI frontend job 红绿真实反映前端质量（人为破坏断言可红）
4. 占位 `vue.spec.ts` 移除，无遗留失败

## 3. 分层方案

### 3.1 单元测试（Vitest + @vue/test-utils + jsdom）

新增 devDeps：`@vue/test-utils`、`jsdom`。

| 范围 | 文件 | 覆盖点 |
|---|---|---|
| utils 纯函数 | `src/utils/__tests__/format.spec.ts` | formatDate / formatChatDate 边界（null、无效字符串） |
| | `src/utils/__tests__/url.spec.ts` | isSafeExternalUrl 安全用例（`javascript:` / `data:` 拒绝、http/https 放行）、openExternalUrl（mock window.open） |
| | `src/utils/__tests__/ws.spec.ts` | buildWsUrl 的 DEV / 生产 http / 生产 https 三场景（vi.stubEnv） |
| | `src/utils/__tests__/id.spec.ts` | generateId 唯一性 |
| store | `src/stores/__tests__/chat.spec.ts` | 消息增删改、questionHistory 上限 20 与去重、setCurrentSession / updateCurrentSessionTitle |
| 核心逻辑 | `src/utils/__tests__/reasoning.spec.ts` | mergeReasoningSteps：按 step 合并去重（id→step 修复的回归测试）、同 step 状态覆盖、不同 step 保留 |
| 组件冒烟 | `src/components/__tests__/MarkdownRenderer.spec.ts` | Markdown 渲染、XSS 净化（script 标签清除）、代码块渲染 |
| | `src/components/__tests__/ReasoningPanel.spec.ts` | 步骤渲染、状态文案、空态 |

**唯一重构**：`mergeReasoningSteps` 从 ChatView.vue 抽至 `src/utils/reasoning.ts` 导出，ChatView 改为 import。理由：该函数是纯函数且为最近修复点，抽离后可直接单测；挂载 771 行的 ChatView 成本过高。

### 3.2 E2E（Playwright + route mock）

**关键决策**：CI（GHA）无法提供 Ollama 推理，真实全栈 E2E 在 CI 不可行。采用 `page.route` 拦截 `/api/**` + `page.routeWebSocket` 拦截上传进度 WS，前端独立回归；真实链路联调仍由后端 `verify_e2e.py` 与手动验收承担。Playwright 1.60 已支持 `routeWebSocket`。

**认证**：`addInitScript` 预置 localStorage `token` / `api_key` 绕过路由守卫（登录流程不在本次范围）。

三条链路（与 roadmap P2-6 原文对齐）：

| spec | 链路 | mock 要点 | 断言 |
|---|---|---|---|
| `e2e/chat.spec.ts` | 上传→解析完成→提问命中→引用展示 | POST `/api/documents/upload`、WS 进度事件推送、POST `/api/chat` SSE 流（reasoning→content→end+sources）、GET 知识库列表 | 解析进度走到完成、流式内容渲染、引用徽章与来源列表出现 |
| `e2e/sessions.spec.ts` | 会话切换与历史查看 | GET `/api/sessions/` 返回两个会话、各自消息列表 | 侧栏切换后消息区内容随会话变化；历史页列表渲染 |
| `e2e/kb.spec.ts` | 知识库增删 | GET / POST / DELETE 知识库接口 | 创建对话框提交后列表新增、删除确认后列表移除 |

**mock 组织**：`e2e/fixtures.ts` 提供 `installMocks(page)` 统一注册路由与 mock 数据；数据字段与 `queries/*.ts` 的 TS 接口对齐（snake_case）；SSE 流用 ReadableStream 分段返回。

### 3.3 Playwright 配置调整

- `webServer.command`：`npm run dev/preview` → `pnpm run dev/preview`（包管理器约束）
- `projects`：仅保留 chromium（CI 时长与稳定性考虑；三浏览器矩阵对当前规模收益低）
- CI 下走 preview server（构建产物，端口 4173）+ headless，现有配置已支持

## 4. CI 集成

frontend job 顺序执行（单 job，不拆分）：

1. `pnpm install --frozen-lockfile`（现有）
2. `pnpm build`（现有，含 vue-tsc 类型检查）
3. `pnpm test:unit`（新增）
4. `pnpm exec playwright install --with-deps chromium`（新增）
5. `pnpm test:e2e`（新增；CI 环境自动 preview + headless + workers=1）
6. `always()` 上传 playwright-report 工件

## 5. 改动清单

| 类型 | 文件 | 说明 |
|---|---|---|
| 依赖 | frontend/package.json、pnpm-lock.yaml | + `@vue/test-utils`、`jsdom` |
| 重构 | frontend/src/utils/reasoning.ts（新增） | mergeReasoningSteps 抽离导出 |
| | frontend/src/views/ChatView.vue | 移除内联实现，改 import |
| 单测 | frontend/src/utils/__tests__/{format,url,ws,id,reasoning}.spec.ts | 新增 |
| | frontend/src/stores/__tests__/chat.spec.ts | 新增 |
| | frontend/src/components/__tests__/{MarkdownRenderer,ReasoningPanel}.spec.ts | 新增 |
| E2E | frontend/e2e/vue.spec.ts（删除） | 占位替换 |
| | frontend/e2e/fixtures.ts、{chat,sessions,kb}.spec.ts（新增） | 三条链路 + mock |
| 配置 | frontend/playwright.config.ts | webServer 用 pnpm、projects 仅 chromium |
| CI | .github/workflows/ci.yml | frontend job 增加单测 + E2E 步骤与报告工件 |

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| routeWebSocket mock 时序（进度事件早于连接建立而丢失） | fixture 等 WS 连接建立后再推送；UploadDocumentDialog 本身先建 WS 再发请求，天然规避 |
| SSE mock 流式时序导致 flake | ReadableStream 分段 enqueue；断言以最终态（end 事件后）为准，不断言中间态 |
| element-plus 组件 jsdom 挂载兼容性 | 仅做冒烟断言核心渲染；遇问题组件降级为纯逻辑测试 |
| E2E mock 与真实后端契约漂移 | mock 数据以 queries/*.ts 的 TS 接口为单一事实源，接口变更时类型检查先红 |

## 7. 明确不做（本次范围外）

- 真实全栈 E2E（依赖 Ollama，仅本地手动；后端已有 verify_e2e.py）
- 登录/注册流程 E2E、视觉回归测试、覆盖率阈值卡点
- firefox / webkit 浏览器矩阵
