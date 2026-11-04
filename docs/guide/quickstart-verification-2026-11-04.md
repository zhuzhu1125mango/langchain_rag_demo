# 干净目录复现验证：从 clone 到能提问（2026-11-04）

> 「工程化参考实现」的验收标准只有一条：**别人能不能 10 分钟跑起来**。
> README 一直这么写，但此前从没人在一个真的什么都没有的目录里跑过。
> 这次跑了 7 遍，找出 **7 处与声称不符**，其中 **4 处会直接挡住启动**。
> 全部已修，每一处的判定都做了对照实验（只改一个变量）。

## 1. 怎么测的

```bash
git clone <repo> /tmp/repro-check
cd /tmp/repro-check
bash scripts/start-dev.sh          # 完全按 README「上手」步骤
```

对照组是作者本机：已有 8 天前的镜像、已有 `.env.dev`、shell 里还带着一些变量。
**本机跑得通不代表新用户跑得通**——这正是这次验证的意义。

## 2. 七处问题（按是否挡住启动排序）

### 2.1 `ALLOWED_ORIGINS` 的逗号写法根本解析不了（挡住启动）

`.env.example` 里写的是：

```
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:8080,http://127.0.0.1:5173
```

而 `config.py` 对 `List[str]` 字段在**取值阶段**就先做 JSON 解码，非 JSON 直接抛
`SettingsError`，那个 `_split_csv` 的 `mode="before"` validator **永远轮不到**。
结果：`backend` 启动即退出，报 `error parsing value for field "ALLOWED_ORIGINS"`。

判定（不依赖任何容器）：

```python
os.environ['ALLOWED_ORIGINS'] = 'http://a,http://b'      # → SettingsError
os.environ['ALLOWED_ORIGINS'] = '["http://a","http://b"]' # → ['http://a','http://b']
```

修法：给三个列表字段加 `Annotated[List[str], NoDecode]`，让原始字符串进到 validator，
validator 同时接受 JSON 数组与逗号分隔。**本地开发机看不到这个故障**——它们的 `.env.dev`
里压根没写这个键（走的是代码默认值），只有新用户才会踩。

### 2.2 `KEY=   # 说明` 这种行会把注释当值注入（挡住启动）

`KB_RERANK_MAX_LENGTH=                      # CrossEncoder 打分输入最大 token 数…`
—— 值是空的，注释跟在后面。compose 的 `env_file` 解析对这种行**不去注释**，
于是 `KB_RERANK_MAX_LENGTH` 的值变成 `# CrossEncoder 打分输入…`，pydantic 报
`int_parsing`，backend 再次启动即退出。

同样的形态在模板里有 5 处，另外 3 处是字符串字段（`AB_CHAT_EXPERIMENT_ID`、`WIKI_COMPILE_MODEL`、
`GUARDRAIL_TOOL_DOMAIN_ALLOWLIST`/`DENYLIST`）——它们不会报错，而是**静默拿到一句注释当配置**
（比如「编译模型名」变成「# 编译模型名…」）。

修法：注释一律挪到上一行，本行只留 `KEY=`。

### 2.3 `MINIO_ACCESS_KEY` 与 `MINIO_ROOT_USER` 不是同一个账号（挡住启动）

模板里 `MINIO_ROOT_USER=dev_minio`、`MINIO_ACCESS_KEY=dev_access_key`，而 MinIO 只认
**已存在**的账号——`dev_access_key` 从来没有被创建过（`mc admin user add` 这一步项目里不存在）。

对照实验（只改这一个变量，其余全同）：把 `MINIO_ACCESS_KEY` 改回 `dev_access_key` →

```
backend 日志：InvalidAccessKeyId ×12
GET /health/ready → 503
```

生产环境里这两个值其实是**相等**的（实测 `docker exec backend-prod` 比对通过），
所以模板与生产口径不一致。

修法：模板里 `MINIO_ACCESS_KEY=dev_minio`，并在注释里写明「默认复用管理员账号；
独立 key 需先建服务账号，否则 /health/ready 会 503」。

### 2.4 首次运行没有 `.env.dev`，脚本只让你「自己看着办」（挡住启动）

`.gitignore` 排除了 `.env*`，仓库只发布 `.env.example`（184 个键）。原脚本的逻辑是：

```
[ERROR] 配置文件 .../.env.dev 不存在
[INFO] 请创建 .env.dev 文件，可参考以下必要变量:   # 只列了 7 个，实际需要 184 个
```

修法：首次运行自动 `cp .env.example .env.dev`，并**随机生成 `SECRET_KEY`**。
必须生成的原因：模板里 `SECRET_KEY` 是空值，而 `auth.py` 签发 JWT 时会
`raise ValueError("SECRET_KEY 未配置")` —— 容器起着、界面开着，但注册/登录直接失败，
这是最难自查的一类故障。**只对 dev 这么做**，生产必须由人显式配置。

### 2.5 完全不检查 Ollama 模型（不挡启动，但报错不可读）

Ollama 不在 compose 内。后端启动时校验三个模型是否存在，缺失则拒绝启动，
但脚本不做任何预检，用户看到的是「7 个容器里 backend 一直 unhealthy」。

修法：启动前读 `.env.dev` 的三个模型名，比对 `ollama list`，缺哪个就把
`ollama pull <name>` 打出来。tag 差异按前缀匹配（`bge-m3` 与 `bge-m3:latest` 等价）。
`SKIP_OLLAMA_CHECK=1` 可跳过。

> 这一步自己踩了个坑：`.env.example` 的值后面带行内注释，第一版解析把注释也当成模型名，
> 于是「模型明明装了却报缺失」。是**真跑一遍**才发现的——`CHECK_ONLY` 模式跳过了模型检查，
> 反而看不到这个 bug。

### 2.6 健康等待 120s 不够首次冷启动（不挡启动，但会误报失败）

实测从 `up` 到 backend `healthy` 约 **3–4 分钟**（镜像构建 + 模型预热）。
原默认 120s 会在「其实马上就绪」时报红：

```
! 开发环境已启动，但部分服务未完全就绪
```

而此刻 `curl /health/ready` 已经是 **200**。修法：默认改为 240s（`MAX_WAIT_SECONDS` 仍可覆盖）。

### 2.7 README 的用例数与实际不符

| 项 | README 原值 | 实测 |
|---|---|---|
| 后端全量 | 1553 | **1641**（当日补 4 例 CORS 解析、修正 `IN_DOCKER` 隔离后 8 例不再误报、新增 19 例巡检调度） |
| `test_chat_run.py` | 29 | **35** |

（`test_error_handling` 93、`test_consistency_delete_chain` 13、`test_consistency_audit` 29 三处核对无误。）

## 3. 为什么这些问题能长期存在

**CI 校验了「键是否齐全」，没校验「值是否可用」。** `ci.yml` 生成占位文件的做法是：

```bash
grep -E '^[A-Z][A-Z0-9_]*=' .env.example | cut -d= -f1 | while read -r k; do echo "$k=ci_placeholder"; done
```

每个键都给 `ci_placeholder` —— 于是 `ALLOWED_ORIGINS` 的逗号问题、
`KB_RERANK_MAX_LENGTH` 的注释问题、MinIO 账号不一致，在 CI 里**永远不会出现**。

补了两条静态校验（`scripts/check_env_consistency.py`）：

- **校验 D**：禁止 `KEY=   # 说明` 形态（空值 + 行内注释）
- **校验 E**：被 compose `${VAR}` 插值引用的键，不许带行内注释
  （compose 插值不剥注释、`env_file` 注入会剥，同一个变量两端取值不同）

## 4. 复现结果

```
[1/5] 检测运行环境...
[OK] Docker 已安装 / Compose 已安装
[OK] 已从 .env.example 生成 .../.env.dev，并随机生成 SECRET_KEY
[OK] Docker 守护进程运行中
[2/5] 加载环境变量...
[OK] Ollama 模型齐全：bge-m3:latest qwen2.5:3b-instruct qwen3:4b
[3/5] 启动 Docker Compose 服务...
[OK] compose 解析通过，服务数 7: etcd minio milvus-standalone postgres redis backend frontend
...（构建 + 起栈）
GET /health/ready → 200
```

七轮里前六轮分别卡在 §2.1 / §2.2 / §2.3，第七轮通过；每轮都只改一处。

## 5. 时间账

| 步骤 | 实测 |
|---|---|
| clone（本地克隆，不含下载） | ~5s |
| 生成 `.env.dev` + 预检 | ~2s |
| 首次镜像构建（uv sync 命中缓存的层之外） | ~35s 依赖安装 + 镜像导出 |
| 起栈到 backend healthy | **3–4 分钟**（首次；模型已在本机） |
| 首次 `ollama pull` 三个模型 | 未计入，约 5.6GB |

**「10 分钟」只在「镜像与模型都在本机」时成立**——README 已注明「已经拉过一次之后」，
但容易被读成首次也只要 10 分钟。诚实说法：**首次 15–30 分钟（含模型下载），之后 2–4 分钟**。

## 6. 这次没验到的

- **没换机器**：仍是同一台、同一套 Docker 与本地 Ollama。GPU 驱动、Docker Desktop 的
  `host.docker.internal`、路径分隔符这些「换机才暴露」的差异没覆盖。
- **没实跑 `start-dev.ps1`**：PowerShell 版的改动是逐行对齐 `.sh` 写的，只做了语法层核对。
- **没验生产路径**：`start-prod.sh` 不自动生成 `.env.prod`（刻意如此），本次未检查它缺文件时的提示质量。
- **没验「提问能不能答对」**：这一轮只到 `/health/ready` 200。建库、上传、问答仍然只有
  `docs/e2e-verification-2026-11-03.md` 那 10 题的人眼结论。

## 7. 一条方法论教训

第一次「凭据不一致」的判断是**错的**：当时 shell 里被注入了 `MINIO_ROOT_USER=admin`，
compose 的变量优先级是「进程环境 > `--env-file`」，于是 minio 拿到 `admin`、
backend 从 `env_file` 拿到 `dev_minio`——看起来像项目 bug，其实是我自己的环境污染。

后来用 `env -u MINIO_ROOT_USER -u MINIO_ROOT_PASSWORD` 重跑，才把真因（账号不存在）
和假象（两端取到不同来源）分开。**验证环境本身也是被测系统的一部分**，
记录时把「怎么排除环境污染」写进报告，比只写结论有用。
