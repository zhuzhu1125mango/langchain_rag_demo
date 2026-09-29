# 开发与测试指南

> 定位：本地开发、测试执行、CI 说明与已知踩坑。部署见 [deployment.md](deployment.md)。
> 更新时间：2026-09-24

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

基线：929 passed / 103 skipped（2026-09-24）。

### 2.2 集成测试（需容器）

本地集成环境使用独立端口与密码，**避免与开发环境（5433/dev_*）冲突**：

| 服务 | 地址 | 凭据 |
|---|---|---|
| PostgreSQL | localhost:15432 | 见 .env 覆盖变量 |
| Redis | localhost:16379 | 密码 rag_ci_password |
| MinIO | localhost:9001（API 9000） | rag_ci_minio / rag_ci_minio_password |

需用环境变量覆盖 `.env.dev`（后端默认读取，见 [deployment.md](deployment.md) §6.2）中的开发配置后再跑集成测试；测试入口统一使用 `tests/conftest.py` 的 session 级 `integration_client` fixture（TestClient 共享，避免跨事件循环单例崩溃）。

### 2.3 检索质量评估（全离线）

```bash
cd backend
uv run python scripts/run_eval.py --report eval_report.json
```

基于 `tests/evaluation/kb_eval_dataset.jsonl`（问题 + golden 文档）与 `eval_corpus.jsonl`（评估语料），使用与生产一致的 BM25 稀疏检索栈计算 hit_rate@5 / mrr@5 / recall@5，低于阈值（`EVAL_MIN_HIT_RATE` / `EVAL_MIN_MRR` / `EVAL_MIN_RECALL`，默认 0.8 / 0.5 / 0.5）时非零退出。同一逻辑以 `tests/evaluation/test_retrieval_quality.py` 纳入单测，CI 中另有独立 `rag-eval` job 输出指标报告并卡点。

另有多查询检索离线 A/B（`scripts/run_multiquery_ab.py` + `tests/evaluation/mq_eval_dataset.jsonl`，16 题含 query_variants），实测无增益（RRF 口径），**多查询保持默认关闭**（`KB_MULTI_QUERY_ENABLED=false`）；CI `rag-eval` 中以信息性步骤运行（continue-on-error，产出 mq-ab-report），非卡点。

### 2.4 分块行为说明（P0-2a 结构化分块）

- 文档加载：txt/md/csv/json/pdf 走本地直读加载器 `src/services/document_loaders.py`（PDF 基于 pypdf）；Office/HTML/EPUB 暂走 langchain-community unstructured（待迁）
- Markdown（`.md`，TextFileLoader 直读保留原始语法）：按标题栈切分，chunk 内容前置 `heading_path`（如 `员工手册 > 请假制度`），超长正文二次切分并传播路径；fenced 代码块整块保留；表格按行拆分（内容 = 表头 + 该行）
- HTML：header 切分 + 表格行级还原 + 正文二次切分
- Word（elements 模式加载）：Title 元素构建章节路径（启发式标题栈），Table 整块保留
- Milvus `heading_path` 字段对旧集合在线补加（`add_collection_field`），失败自动降级；检索结果 metadata 含 `heading_path`
- 修改分块逻辑后运行 `uv run pytest tests/test_structured_chunking.py tests/evaluation/test_structured_vs_recursive.py` 验证

### 2.5 OCR 深度解析（P0-2b 扫描版 PDF）

扫描版 PDF 自动检测（pypdf 字符密度）并分流 OCR 管线，输出 Markdown 后复用 Markdown 结构化分块；文本型 PDF 走内置 PdfFileLoader（pypdf）解析不变。OCR 后端为**可选依赖组**，默认不安装、CI 不装：

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
| env-consistency | 用 `.env.example` 生成占位 env 校验键集合一致 | `.env.dev/.env.prod` 被 gitignore 不入库；脚本对缺失文件容错跳过 |
| lint | backend ruff（`uvx ruff --select E9,F63,F7,F82`）+ 前端 eslint / typecheck | 真错误级规则集，零风格噪音；runner 无预装 uvx，须先 setup-uv |
| secrets-scan | gitleaks `detect --no-git --redact` | 只扫工作树不扫历史；误报按 fingerprint 登记到 `.gitleaksignore` 并注明原因 |
| backend-unit | `uv sync --frozen` + pytest 单测 + 覆盖率卡点 | `--cov-fail-under=58`（基线 61%，随覆盖提升收紧）；干净安装暴露隐性依赖缺失 |
| backend-integration | docker run 启动 MinIO(pgsty fork)/etcd/Milvus/Redis + pytest 集成测试 | 不用 GHA services（镜像无默认 CMD）；Redis 必须带密码；pytest-timeout 120s 防挂死 + step timeout-minutes 40 |
| rag-eval | 离线检索质量评估（依赖 backend-unit） | `scripts/run_eval.py` 阈值卡点 + 上传 eval_report.json；另有信息性多查询 A/B 步骤（continue-on-error，非卡点） |
| frontend | pnpm 单测（覆盖率阈值）+ build（含 vue-tsc -b）+ Playwright E2E | 依赖变更时需 `--build` 重建镜像 |

全部 job 均设 `timeout-minutes`（5~45），防挂死耗尽上限。

## 4. 已知踩坑记录（改代码前先看）

1. **隐性依赖**：pymilvus-model 的 BM25 tokenizers 模块级依赖 nltk 但未声明，必须显式保留 `nltk==3.10.3`；本地残留环境会掩盖缺失，验证依赖以 `uv sync --frozen` 干净安装为准
2. **pymilvus-model 0.3.2 导入路径**：`from pymilvus.model.sparse import BM25EmbeddingFunction, build_default_analyzer`（类名 Tokenizer 是单数），不是 `pymilvus_model` 顶层模块
3. **Windows 编码**：测试临时文件必须显式 `encoding='utf-8'`（默认 GBK 会导致文档解析失败）
4. **content-type 断言**：Starlette 会给 text/* 追加 `'; charset=utf-8'`，断言用 `startswith`
5. **异步处理**：文档上传接口为后台异步处理，返回「文件上传成功，正在后台处理」，测试断言用前缀匹配
6. **事件循环作用域**：进程级单例（CacheService/DB engine）绑定首次创建的事件循环，测试中禁止每个用例新建 client，统一用 `integration_client`
7. **沙箱删除限制**：PowerShell `Remove-Item` 对部分 site-packages 文件可能 Access denied，改用 .NET API `[System.IO.File]::Delete` / `[System.IO.Directory]::Delete`
8. **scripts 目录脚本运行方式**：`scripts/` 下脚本（如 `run_agent_ab.py`）需用 `python -m scripts.run_agent_ab`（把 backend 加入 sys.path）运行；直接 `python scripts/xxx.py` 会 `ModuleNotFoundError: src`
9. **pydantic-settings List[str] 解析**：.env 中 List 类型按 JSON 解析；对 CORS 等列表配置已加 `field_validator(mode="before")` 支持逗号分隔，新增 List 配置键时注意同样处理
10. **vue-tsc --noEmit 是空检查假绿**：根 tsconfig（references + 空 files）上跑 `--noEmit` 不检查任何文件；类型检查必须用 `pnpm typecheck`（`vue-tsc -b`）——曾因假绿漏过模板解构缺失导致 CI Frontend Build 失败
11. **vitest coverage 版本耦合**：`@vitest/coverage-v8` 大版本必须与 vitest 一致（5.x 配 vitest 4 启动即报 `coverageFilesDirectory is required`）；v8 provider 对 .vue SFC sourcemap 映射失真会稀释覆盖率，配置 include 收窄到逻辑层目录
12. **本机代理 Fake-IP 陷阱**：Clash 类代理的 Fake-IP 模式把外网域名解析到 198.18.0.0/15（is_private=True）→ `validate_url_safe` 判受限，联网搜索在本机开代理时不可用（环境限制非缺陷）；URL 安全测试需 mock `getaddrinfo` 消除对真实 DNS 的依赖
13. **宿主端口占用静默失效**：本地已有 redis（如 WSL 内实例）占 6379 时，docker compose 的端口发布可能静默不生效（容器正常跑但 `docker port` 为空、宿主连到的是另一个无密码实例）；排查用 `docker port <容器>` 与 `netstat` 对照，勿假设 compose ports 必然生效
14. **GHA runner 无预装 uv/uvx**：CI 里用 `uvx ruff` 等必须先 `astral-sh/setup-uv`；PowerShell 不支持 bash heredoc，Windows 本地写多行 commit message 用 `git commit -F <文件>`

## 5. 代码约定

- 路由层（`api/`）不写业务逻辑，业务在 `services/`
- IO 必须异步：`aiofiles`、`ainvoke`、异步 SDK；CPU 密集用 `run_in_executor`
- 服务单例继承 `AsyncSingleton`，实现异步初始化/清理
- 提示词放 `prompts/`，用 prompt_loader 异步加载
- 新增依赖必须开源可离线；改动依赖后运行 `uv lock` 并提交 uv.lock
- 重大设计改动先写 `docs/design/<主题>.md`（小写 kebab-case）评审，再实施
