# Backend

FastAPI 后端服务（全链路 async），依赖管理使用 uv。

```bash
# 安装依赖（按 uv.lock 精确安装）
uv sync --frozen

# 启动（需先通过 scripts/start-dev 启动基础设施）
uv run python start.py

# 运行测试
uv run pytest -q
```

## 目录速览

- `src/api/` 路由层（不写业务逻辑）
- `src/services/` 业务核心（决策管线/检索/生成/搜索/工具/评估）
- `src/prompts/` 提示词模板
- `scripts/` 手动迁移与排障脚本（见 `scripts/README.md`）
- `tests/` 单元与集成测试

## 文档

- 架构总览：[docs/architecture.md](../docs/architecture.md)
- 开发与测试指南（含踩坑记录）：[docs/guide/development.md](../docs/guide/development.md)
- API 接口：[docs/api.md](../docs/api.md)
- 部署：[docs/guide/deployment.md](../docs/guide/deployment.md)
