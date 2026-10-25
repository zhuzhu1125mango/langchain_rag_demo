# 前端视觉收口与 UI 打磨（VIS-3，2026-10）

> 状态：已实施（2026-10-24）
> 关联：[security-multitenancy-hardening-2026-10.md](security-multitenancy-hardening-2026-10.md)（同期安全改造）、[evolution-roadmap-2026-10.md](evolution-roadmap-2026-10.md)（VIS-3 来源）、[project-status.md](../project-status.md)（台账）
> 范围：仅视觉/布局/交互细节，不改业务逻辑、接口与测试选择器。

## 1. 调研结论

对标 2026 主流 AI 工作台（ChatGPT / Claude / Perplexity）与 SaaS 后台（Linear / Stripe / Vercel / Attio）的设计取向：

| 主题 | 结论 | 落到本项目 |
|---|---|---|
| 聊天排版 | 主对话内容列约 720–768px，侧栏弱化、输入区固定底部 | 消息与输入区统一 `max-w-3xl` 居中；侧栏保持 64/256px |
| 网格/间距 | 8px 网格，卡片内边距与元素间距成组变化 | 页头 12px、卡片 16/24px、消息间距 20px |
| 层级 | 标题与正文要有明显字号/字重差；正文单行 65–75 字符 | 页头 `text-base/lg` + `tracking-tight`；聊天列宽约 720px |
| 配色 | 近白底 + 轻边框 + 低阴影 + 单一主色；语义色只表达状态 | 沿用 primary blue；surface/line/content token；状态色只用于成功/警告/错误/评分 |
| 圆角/阴影 | 控件 8–10px、卡片 12–14px、弹窗 16px；阴影克制 | `radius-control/card/panel` + `shadow-card/popover` |
| 弹窗 | 语义化、标题清晰、Esc/遮罩可关闭、焦点管理、移动端不溢出 | `AppDialog` 居中 + 统一 header/body/footer + 最大宽度约束 |
| 下拉/选择 | 选中态明确、键盘可达、popper 与表单同源 | Element Plus select/popper 统一 token、圆角、选中色 |

参考：
- [Designing AI chat interfaces](https://www.setproduct.com/blog/ai-chat-interface-ui-design)（对话列宽 720–768px、消息层级）
- [Admin Dashboard Design 2026](https://adminlte.io/blog/admin-dashboard-design/)（SaaS 后台布局/密度）
- [Accessible Modal Dialog](https://www.adacompliancepros.com/blog/accessible-modal-dialog)（弹窗焦点/关闭/语义）
- [frontend design laws](https://tool.lu/en_US/skill/s/oAF)（8px 网格、65–75ch、OKLCH 思路）
- [GPT-6 Intelligent UI](https://www.liuqi.dev/blog/2026-10-07-openai-gpt-6-intelligent-ui-for-everyone)（文本优先与可交互产物的取舍）

## 2. 设计方向

**Calm enterprise AI workspace（克制型智能工作台）**

- 近白 `bg-page` + 白色 `surface`；暗色使用同一套语义 token；
- 单一 primary blue 承担主操作，状态色只用于状态/评分；
- 轻边框（1px line）+ 低阴影；hover 以背景/边框变化为主，不做布局位移；
- 8px 网格；控件高度 36–40px；聊天主列 768px；
- 标题紧凑字距，正文 14–15px、1.55–1.7 行高；
- 弹窗/下拉/表单统一圆角、边框、选中态与焦点环。

## 3. 已实施

### 3.1 全局与 Element Plus

- `style.css`：EP 语义变量（圆角/字号/阴影）、字体特性、`::selection`；
- 弹窗：16px 圆角、24px 水平内边距、header/body/footer 分层、footer 上边框、遮罩模糊、移动端 `max-width` 与 body 滚动；
- 下拉/选择：12px popper、8px item、选中 primary soft、键盘 hover 态；
- 输入/表格：统一圆角、focus ring、表头字号与行 hover；
- 修复全局 `:focus-visible` 与输入控件自身 focus ring 叠加导致的“双蓝框”：文本类 input/textarea/select 不再叠加 outline，复选框/单选框保留全局焦点环；
- 输入区图标刷新：深度思考 `BrainCircuit`、联网搜索 `Globe2`、发送 `ArrowUp`、猜你想问 `MessageCircleQuestion`；
- `AppDialog` 增加 `align-center`，统一居中弹窗。

### 3.2 应用壳与侧栏

- 品牌标识圆角/阴影、标题字重与字距收敛；
- 主导航 14px、active primary soft、`aria-current`、折叠态 tooltip；
- 对话历史分组更轻；用户区头像与主题/折叠按钮统一 token；
- 设置页侧栏宽度 240px、菜单字号/active 与主导航一致。

### 3.3 聊天工作台

- 消息列与输入区统一 768px 居中，消除大屏“左侧堆叠”；
- 欢迎态：更清晰的标题层级、示例问题卡片、能力标签；
- 用户/助手气泡圆角与间距收敛；助手卡片/来源卡片轻边框；
- 消息操作在移动端常显、桌面端 hover 显示；
- 输入区 focus ring、占位符 token 化。

### 3.4 知识库/历史/追踪

- 页头统一 `bg-surface/95 + backdrop-blur`、字号与字距；
- 知识库工具栏移动端换行，表格 `min-w` 横向滚动、表头 sticky、表头不竖排；
- 设置页内容宽度统一 `max-w-4xl`，纵向节奏统一。

## 4. 验证

- `pnpm lint`：0 error / 0 warning
- `pnpm typecheck`：通过
- `pnpm test:unit`：126 passed
- `pnpm build`：通过
- `pnpm test:e2e`：5/5 passed
- Playwright mock 截图：聊天（空/有消息）、知识库、设置、历史、弹窗的浅/深色；移动端 390×844 聊天/知识库/弹窗，无横向溢出（知识库表格在容器内横向滚动）

## 5. 非目标 / 后续

- 不引入新的 UI 组件库或重写 Element Plus；
- 不做长会话虚拟滚动（UX-6）、历史页搜索/导出（UX-5）、Artifacts（AGT-4）；
- 页面密度与视觉进一步收口可随 VIS-3 后续迭代继续调整。

## 6. VIS-4 视觉刷新（2026-10-25）

**目标**：从“白卡片堆叠 + 蓝色高亮”调整为更接近 ChatGPT / Claude / Linear 的冷静 AI 工作台，减少装饰和蓝块，强调内容层级。

### 6.1 设计 token v2

- 色板从 Tailwind gray/blue 混用改为中性 zinc 体系：页面 `#f6f6f7`、侧栏 `#fafafa`、主画布 `#ffffff`；深色页面 `#0e0e10`、侧栏 `#111113`、主画布 `#151517`，去掉原来的紫蓝底。
- 新增 `sidebar` / `sidebar-hover` / `chat-user` 语义色，侧栏和用户消息不再复用品牌色。
- 圆角收敛为控制 10px / 卡片 14px / 面板 18px；阴影改为低对比扩散阴影，减少“浮夸卡片感”。
- 字体开启 Inter 字体特性、优化字距与渲染；选区颜色统一品牌色。

### 6.2 应用壳与导航

- 侧栏改为浅灰工作区底色，品牌标识由渐变改为纯主色 + 细描边。
- 主导航/历史会话/设置子导航 active 态统一为 **白色浮起卡片 + 主色图标 + 细边框**，替代整块蓝色 pill。
- 历史分组文案改为「最近对话」，新建对话入口降为轻量按钮。

### 6.3 聊天工作台

- 主画布改为白色，头部压缩到 64px，标题显示当前会话标题，副标题「知识库问答 · 本地模型」。
- 欢迎态改为「今天想了解什么？」+ 2×2 示例卡（两行文本 + hover 箭头），能力标签改为带图标的内联说明。
- **助手回答去掉卡片边框/阴影**，改为头像 + 正文流；用户消息改为中性灰气泡，减少整页蓝色块。
- 输入区改为浮起式 composer：更大圆角、低对比阴影、primary 圆形发送按钮，focus 时阴影增强。
- 引用来源、推理面板、停止/失败态保持原有语义，仅收敛间距与圆角。

### 6.4 知识库 / 历史 / 设置

- 知识库页头统一 64px、标题字号收敛；KB 列表侧栏改浅灰底，选中项为白色卡片；文档/图谱 tab 改为分段控件。
- 历史页头部统一，危险操作由红色实心按钮降为 danger-ghost；卡片 hover 使用轻阴影。
- 设置子导航 active 态与全局导航一致，侧栏底色改为 `bg-sidebar`。

### 6.5 验证

- `npm run lint`：0 error
- `npm run typecheck`：通过
- `npx vitest run`：131 passed
- `npm run build`：通过
- `npm run test:e2e`：5/5 passed（同步更新输入框 placeholder 与「最近对话」文案）
- Playwright 截图：1440×900 聊天空态/会话态（浅/深）、知识库、历史、设置；390×844 移动端聊天，均无横向溢出。

## 7. 知澜品牌与克制版液态玻璃（VIS-5，2026-10-25）

### 7.1 品牌升级

- 产品名由「RAG 知识库」改为 **知澜**，副标「企业知识问答」；RAG 仅保留为技术描述。
- 新增 `BrandMark.vue`：涟漪源点（中心实心点 + 两圈扩散环），统一用于侧栏、登录页、移动端顶栏。
- Favicon 体系：`favicon.svg`（矢量优先）、`favicon.ico`（16/32/48 兼容）、`favicon-32.png`、`apple-touch-icon.png`（180×180）；`index.html` 增加 SVG/ICO/Apple Touch 与 theme-color。
- 浏览器标题统一为 `页面名 - 知澜`；README 标题同步更新。

### 7.2 液态玻璃材质

采用“导航壳/浮层玻璃 + 内容面实色”的分层策略，避免全站强玻璃影响可读性与 GPU 性能：

- 新增 `glass-chrome` / `glass-panel` / `glass-subtle` 材质类与 `app-ambient` 环境光层；
- 应用范围：全局侧栏、移动端顶栏、聊天/KB/历史页头、composer、Element Plus Dialog/Drawer/Dropdown/MessageBox、Toast；
- 聊天正文、文档表格、表单、设置卡片保持实色；
- 降级：`@supports not (backdrop-filter)` 回退实色；`prefers-reduced-transparency` 关闭模糊；移动端 blur 降至 12px。

### 7.3 验证

- `npm run lint`：0 error
- `npm run typecheck`：通过
- `npx vitest run`：131 passed
- `npm run build`：通过
- `npm run test:e2e`：5/5 passed
- Playwright 截图：1440×900 聊天浅/深色、登录页、知识库、上传弹窗、设置页；390×844 移动端聊天；确认品牌标识、玻璃侧栏/composer/弹窗与降级策略生效。
