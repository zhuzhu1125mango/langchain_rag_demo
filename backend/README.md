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

## 模型配置（P1/P2）

- 优先级：DB（设置 → 模型服务，管理员保存并「校验并应用」）> `MODEL_REGISTRY_FILE` / `data/model_registry.json` > `.env` 的 `OLLAMA_*` legacy。
- P1 文件方式：复制 `configs/model_registry.example.json` 到 `data/model_registry.json`，配置 Ollama / OpenAI-Compatible Provider 与任务路由；外部 API Key 建议通过 `api_key_env` 指向环境变量，不要写入 JSON；修改后按 mtime 热加载。
- P2 管理方式：前端 `/settings/models` 或 `/api/admin/models/*`（生产需 `X-Admin-Key`）；Provider Key 在 DB 中 Fernet 加密（`MODEL_SECRET_ENC_KEY`，缺省从 `SECRET_KEY` 派生）。
- `answer` / `fast` / `embedding` 为必需路由；`agent_decision` 必须声明 `tools`；本地→外部 fallback 需显式 `allow_external_fallback`。
- Provider 连续失败 3 次会熔断 60s（半开自动探测）；模型可配置每 1K tokens 输入/输出价格与币种，调用时估算成本；用量增量默认每 60s 落库到 `model_usage_daily`（`MODEL_USAGE_FLUSH_INTERVAL_S`），应用关闭时最后 flush。`GET /api/admin/models/usage?days=7` 返回持久化聚合 + 进程内未落库增量；未定价调用成本记 0 并计入 `unpriced_calls`。
- Embedding 维度以 registry 为权威；切换后在设置页「向量索引 / Embedding」重建（影子集合重向量化 + 原子切换），失败保留旧索引、可回滚；重建会全量失效语义缓存。可选 `rerank` 路由支持 Ollama 托管模型热切换。
- 相关单测：`uv run pytest tests/test_model_registry.py tests/test_model_config_service.py tests/test_model_health.py tests/test_model_usage_pricing.py tests/test_embedding_migration.py tests/test_milvus_embedding_migration.py -q`。

## 文档

- 架构总览：[docs/architecture.md](../docs/architecture.md)
- 开发与测试指南（含踩坑记录）：[docs/guide/development.md](../docs/guide/development.md)
- API 接口：[docs/api.md](../docs/api.md)
- 部署：[docs/guide/deployment.md](../docs/guide/deployment.md)
