# 开发与测试指南

> 定位：本地开发、测试执行、CI 说明与已知踩坑。部署见 [deployment.md](deployment.md)。
> 更新时间：2026-09-01

## 1. 环境准备

```bash
# 后端（uv + uv.lock，精确复现）
cd backend
uv sync --frozen

# 前端（pnpm）
cd frontend
pnpm install

# Ollama 模型清单见 deployment.md 3.3 节

开发环境一键启动：`scripts/start-dev.ps1`（或 .bat / .sh），基础设施单独启动见 deployment.md。

## 2. 运行测试

### 2.1 单元测试（无外部依赖）

```bash
cd backend
uv run pytest -q            # 全量
uv run pytest tests/test_rag_chain.py -q   # 单文件
```

基线：552 passed / 97 skipped（2026-08-31）。

### 2.2 集成测试（需容器）

本地集成环境使用独立端口与密码，**避免与开发环境（5433/dev_*）冲突**：

| 服务 | 地址 | 凭据 |
|---|---|---|
| PostgreSQL | localhost:15432 | 见 .env 覆盖变量 |
| Redis | localhost:16379 | 密码 rag_ci_password |
| MinIO | localhost:9001（API 9000） | rag_ci_minio / rag_ci_minio_password |

需用环境变量覆盖根目录 `.env` 的开发配置后再跑集成测试；测试入口统一使用 `tests/conftest.py` 的 session 级 `integration_client` fixture（TestClient 共享，避免跨事件循环单例崩溃）。

### 2.3 检索质量评估（全离线）

```bash
cd backend
uv run python scripts/run_eval.py --report eval_report.json
```

基于 `tests/evaluation/kb_eval_dataset.jsonl`（问题 + golden 文档）与 `eval_corpus.jsonl`（评估语料），使用与生产一致的 BM25 稀疏检索栈计算 hit_rate@5 / mrr@5 / recall@5，低于阈值（`EVAL_MIN_HIT_RATE` / `EVAL_MIN_MRR` / `EVAL_MIN_RECALL`，默认 0.8 / 0.5 / 0.5）时非零退出。同一逻辑以 `tests/evaluation/test_retrieval_quality.py` 纳入单测，CI 中另有独立 `rag-eval` job 输出指标报告并卡点。

### 2.4 分块行为说明（P0-2a 结构化分块）

- Markdown（`.md`，TextLoader 加载保留原始语法）：按标题栈切分，chunk 内容前置 `heading_path`（如 `员工手册 > 请假制度`），超长正文二次切分并传播路径；fenced 代码块整块保留；表格按行拆分（内容 = 表头 + 该行）
- HTML：header 切分 + 表格行级还原 + 正文二次切分
- Word（elements 模式加载）：Title 元素构建章节路径（启发式标题栈），Table 整块保留
- Milvus `heading_path` 字段对旧集合在线补加（`add_collection_field`），失败自动降级；检索结果 metadata 含 `heading_path`
- 修改分块逻辑后运行 `uv run pytest tests/test_structured_chunking.py tests/evaluation/test_structured_vs_recursive.py` 验证

### 2.5 OCR 深度解析（P0-2b 扫描版 PDF）

扫描版 PDF 自动检测（pypdf 字符密度）并分流 OCR 管线，输出 Markdown 后复用 Markdown 结构化分块；文本型 PDF 走内置 PyPDF 解析不变。OCR 后端为**可选依赖组**，默认不安装、CI 不装：

```bash
cd backend
uv sync --group ocr-mineru        # MinerU pipeline 后端（torch，约 2GB）
uv sync --group ocr-paddle        # PaddleOCR PP-StructureV3（paddlepaddle，约 1GB）
uv run python scripts/download_ocr_models.py --backend all   # 模型预热 + 冒烟验证（首次联网下载）
```

- 未安装后端 / 解析失败时自动回退内置解析器（warning 日志），不阻断上传
- 注意：不带 `--group` 的 `uv sync --frozen` 会把 OCR 后端从本地环境清除，需重新带组安装；`uv run` 为非精确同步不会清除
- 关键配置（`.env`，完整见 `.env.example`）：`DEEP_PARSING_ENABLED`（总开关，关闭即整体回退旧管线）、`OCR_BACKEND=auto|mineru|paddle`、`OCR_DEVICE=auto|cpu|cuda`
- e2e 对比测试：`uv run pytest tests/evaluation/test_ocr_backend_comparison.py -m e2e`（需双后端，否则跳过）
- 已知依赖取舍：`huggingface_hub` 放宽至 `>=0.34,<2`（mineru 需 transformers 4.x → hub<1.0）、`PyYAML>=6.0.2`（paddlex 锁定 6.0.2）；两包均在 ST 5.5.1 声明兼容范围内
- paddle 后端注意：`paddleocr` 3.x 不自带 paddle 框架（组内显式声明 `paddlepaddle`），PP-StructureV3 需 `paddlex[ocr]` 附加依赖（组内已含）；paddle 3.x Windows CPU 推理必须禁用 MKLDNN（`enable_mkldnn=False`，否则 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support`，代码已处理）
- 模型缓存默认写用户主目录（`~/.modelscope`、`~/.paddlex`），可用环境变量重定向：`MODELSCOPE_HOME` / `MODELSCOPE_CACHE` / `PADDLE_PDX_CACHE_HOME`
- 样例实测（1 页中文扫描件，CPU）：mineru 90.1s（相似度 0.8235，表格还原 2 行）、paddle 110.6s（相似度 0.8375，无表格还原），耗时主要为模型初始化；正式选型结论需用真实扫描件复测

### 2.6 Milvus 注意事项

- Milvus 容器依赖 etcd + MinIO，首次启动需 1-2 分钟
- 若遇 Docker DNS 问题导致 Milvus 无法组网，可用 IP 直连重建：
  `ETCD_ENDPOINTS=<etcd容器IP>:2379, MINIO_ADDRESS=<minio容器IP>:9000`

## 3. CI 结构（.github/workflows/ci.yml）

| Job | 内容 | 关键点 |
|---|---|---|
| backend-unit | `uv sync --frozen` + pytest 单测 | 干净安装，暴露隐性依赖缺失 |
| backend-integration | docker run 启动 MinIO/etcd/Milvus/Redis + pytest 集成测试 | 不用 GHA services（镜像无默认 CMD）；Redis 必须带密码 |
| rag-eval | 离线检索质量评估（依赖 backend-unit） | `scripts/run_eval.py` 阈值卡点 + 上传 eval_report.json |
| frontend | pnpm build | 依赖变更时需 `--build` 重建镜像 |

## 4. 已知踩坑记录（改代码前先看）

1. **隐性依赖**：pymilvus-model 的 BM25 tokenizers 模块级依赖 nltk 但未声明，必须显式保留 `nltk==3.10.3`；本地残留环境会掩盖缺失，验证依赖以 `uv sync --frozen` 干净安装为准
2. **pymilvus-model 0.3.2 导入路径**：`from pymilvus.model.sparse import BM25EmbeddingFunction, build_default_analyzer`（类名 Tokenizer 是单数），不是 `pymilvus_model` 顶层模块
3. **Windows 编码**：测试临时文件必须显式 `encoding='utf-8'`（默认 GBK 会导致文档解析失败）
4. **content-type 断言**：Starlette 会给 text/* 追加 `'; charset=utf-8'`，断言用 `startswith`
5. **异步处理**：文档上传接口为后台异步处理，返回「文件上传成功，正在后台处理」，测试断言用前缀匹配
6. **事件循环作用域**：进程级单例（CacheService/DB engine）绑定首次创建的事件循环，测试中禁止每个用例新建 client，统一用 `integration_client`
7. **沙箱删除限制**：PowerShell `Remove-Item` 对部分 site-packages 文件可能 Access denied，改用 .NET API `[System.IO.File]::Delete` / `[System.IO.Directory]::Delete`

## 5. 代码约定

- 路由层（`api/`）不写业务逻辑，业务在 `services/`
- IO 必须异步：`aiofiles`、`ainvoke`、异步 SDK；CPU 密集用 `run_in_executor`
- 服务单例继承 `AsyncSingleton`，实现异步初始化/清理
- 提示词放 `prompts/`，用 prompt_loader 异步加载
- 新增依赖必须开源可离线；改动依赖后运行 `uv lock` 并提交 uv.lock
- 重大设计改动先写 `docs/design_<主题>.md` 评审，再实施
