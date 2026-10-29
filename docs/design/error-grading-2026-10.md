# 异常分级治理（CODE-2）— 分级模型与推广顺序

> 状态：**样板已落地（2026-10-28）**，待按模块分批推广。
> 实现：`backend/src/utils/error_handling.py`；单测：`backend/tests/test_error_handling.py`（26 例）。
> 背景：审计口径 **405 处 `except Exception`**（2026-10-28 实测），此前各处自定义文案与级别，导致
> 「上游抖动」与「代码缺陷」混在同一 error 流里，既淹没信号、也无法据此决定重试。

## 1. 分级模型

| 级别 | 含义 | 日志级别 | 是否重试 | 典型异常 |
|---|---|---|---|---|
| `RECOVERABLE` | 可恢复：上游抖动、超时、限流、连接失败 | `warning`（不输出堆栈） | 可重试 / 可降级 | `TimeoutError`、`ConnectionResetError`、`httpx.*`、`pymilvus.exceptions.*`、`redis.*` |
| `REPORTABLE` | 需上报：未知业务异常、数据不一致、返回不可解析 | `error` + 堆栈 | 不可重试 | `RuntimeError`、未知 `Exception`、业务 `AppException` |
| `PROGRAMMING` | 编程错误：本不该发生，说明代码有缺陷 | `error` + 堆栈 | 禁止重试 | `TypeError`、`AttributeError`、`KeyError`、`IndexError`、`AssertionError`、`*InvalidArgument*` |

判定顺序（`classify_exception`）：**编程错误类型/类名 → 可恢复（内建类型 / 模块前缀 / 类名特征 / 消息强信号）→ 需上报**。

设计取舍：
- **类型优先于文案**：`KeyError("connection refused")` 仍判 `PROGRAMMING`（有单测守护）；
- **文案只认强信号**（`rate limit`、`connection refused`、`timed out` 等），避免业务文案里的 "connection" 把 `ValueError` 误判为可恢复；
- **不为分类引入重依赖**：第三方客户端按 `__module__` 前缀与类名识别，不 import `httpx`/`pymilvus`；
- **可恢复不刷堆栈**：网络抖动的高频日志不应淹没真正的 error。

## 2. 使用方式

```python
from src.utils.error_handling import ErrorSeverity, log_exception, is_recoverable

# 路由层：记录 + 对外仍是通用文案
except Exception as exc:
    log_exception(logger, exc, action="document.classify", message="文档分类失败", doc_id=doc_id)
    raise HTTPException(status_code=500, detail="文档分类失败，请稍后重试")

# 服务层：据分级决定是否重试/降级
except Exception as exc:
    severity = log_exception(logger, exc, action="milvus.bm25_vocab.load", severity=ErrorSeverity.RECOVERABLE)
    return False  # 降级
```

约定：
- `action` 用**稳定点分标识**（`模块.动作[.子步骤]`），便于日志检索与聚合，不写自然语言；
- 对外响应**只给通用文案**，异常细节只进日志（与 SSE 脱敏口径一致）；
- `severity=` 显式覆盖仅用于「确知该 SDK 错误可降级」的场景，默认交给自动判定。

## 3. 样板覆盖（2026-10-28）

| 文件 | 处理点数 | 展示的能力 |
|---|---|---|
| `api/document/manage.py` | 6 | 路由层：best-effort 降级、500 兜底统一上下文、查重候选逐个跳过时不再静默 |
| `services/milvus_writer.py` | 2 | 服务层：显式 `RECOVERABLE` 的降级路径（BM25 词表加载/持久化） |

合计 **9 处调用点**（`grep -c log_exception`）。`except Exception` 总数仍是 405 —— 分级不减少捕获点数量，改变的是**记录质量与可判定性**；后续推广按「是否真的能处理」删减，而不是机械替换。

## 3.1 推广批次记录

| 批次 | 范围 | 内容要点 |
|---|---|---|
| 一（10-29） | `chat/stream.py` 9、`admin_models.py` 15、`search_agent*` 9、`pipeline/*` 5、`agent_orchestrator*` 3 | 引入 `_fail(exc, action)` 助手统一路由层 500；修掉「后台任务异常被静默吞掉」「把服务端故障伪装成 400」两处失真 |
| 二（10-29） | `semantic_cache_service.py` 11、`milvus_*` 12 | 全 fail-open 路径显式 `RECOVERABLE`；Milvus 启动自检失败显式 `REPORTABLE`（检索降级必须被看到） |
| 三（10-29） | `web_search_*` 14、`experiment.py` 11、`learning.py` 7、`learning_engine.py` 1 | 外部依赖边界统一降级语义；`experiment`/`learning` 共 17 个端点改用 `_fail` 并各自带 `action=`；`search_multi` 改为逐路记录单 query 失败（原先只拼一句 warning，丢失堆栈与定位） |

累计 **78 处调用点 / 覆盖约 105 个 `except` 分支**（助手合并的端点按一次调用计）。第三批评为「预期控制流」而有意保留静默的只有 4 处（`get_extra_info` 非 socket 传输、`urlparse` 畸形输入、`model_manager` 属性探测、各引擎 `raise WebSearchError(...)` 包装上抛），全部就地写明理由；连同前两批的 `json.loads` 兜底，静默点共 5 处。

## 4. 推广顺序（建议）

1. ~~**已拆小的高频路由**：`api/document/{upload,query}.py`、`api/admin_models.py`~~（admin_models 已完成；document 两文件待办）；
2. ~~**外部依赖边界**：`services/milvus_*.py`、`services/web_search_*.py`~~（已完成）；
3. **下一批**：`services/cache_service.py`（10）、`services/embedding_migration.py`（9）、`services/document_tasks.py`（9）、`services/wiki_cascade.py`（7）、`services/react_agent.py`（7，已部分接入）、`services/account_service.py`（7）；
4. **长尾**：其余 ~290 处随日常改动顺带迁移，避免二次触碰同一文件；
5. **收敛规则**：迁移时逐点判断「能否处理」——能处理（重试/降级/跳过）→ 保留 `except` + 分级记录；不能处理（应继续抛）→ 删掉本地 `except`，交给上层。

## 5. 验收口径（单模块）

- 该模块所有 `except Exception` 都走 `log_exception`，或已被删除；
- 日志中 `action=` 可唯一定位到代码位置；
- 可恢复路径无堆栈、需上报路径有堆栈（可用 `caplog` 断言）；
- 对外响应文案不变（回归用既有接口测试）。
