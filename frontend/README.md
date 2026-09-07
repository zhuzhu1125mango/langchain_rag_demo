# Frontend

Vue 3 + Pinia + Vue Query + Element Plus + Vite，包管理使用 pnpm。

```bash
# 安装依赖
pnpm install

# 开发模式（Docker 外本地调试时由 scripts/start-dev 统一拉起，命令为 pnpm run dev -- --host 0.0.0.0）
pnpm run dev

# 构建
pnpm run build
```

## 目录速览

- `src/views/` 页面（Chat / KnowledgeBase / History / Settings）
- `src/components/` 组件（chat 对话区、knowledge-base 知识库管理等）
- `src/stores/` Pinia 状态（app / chat / kb）
- `src/queries/` Vue Query 数据获取
- `e2e/` Playwright 端到端测试

## 文档

- API 接口：[docs/api.md](../docs/api.md)
- 架构总览：[docs/architecture.md](../docs/architecture.md)
- 开发与测试指南：[docs/guide/development.md](../docs/guide/development.md)
