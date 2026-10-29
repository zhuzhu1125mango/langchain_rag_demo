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
| 四（10-29） | `cache_service.py` 6、`embedding_migration.py` 9、`document_tasks.py` 9、`wiki_cascade.py` 7、`account_service.py` 7、`react_agent.py` 1 | Redis 降级路径全部 `RECOVERABLE`；迁移/重建失败改为保留堆栈并按「需人工介入」上报；**发现并修复真实缺陷**：注销账号时 `invalidate_kb_list_cache` 从错误模块导入，ImportError 被 `except: pass` 吞掉，导致缓存从未失效（已加回归测试） |
| 五（10-29） | `pipeline/agent.py` 7、`wiki_pages.py` 6、`wiki_navigator.py` 6、`rag_chain.py` 5、`exchange_rate_tool.py` 2、`gold_price_tool.py` 2 | 管线记忆/联网搜索降级路径分级；RAGChain 装配期的「持久性降级」（缓存/embeddings/后处理器）标可恢复，编排器初始化失败标需上报；行情类工具多源失败按可恢复、数值/时间解析保留静默并注明 |
| 六（10-29） | `auth.py` 5、`utils/quota.py` 5、`model_manager.py` 4、`hybrid_search.py` 4、`document/upload.py` 5、`document/query.py` 5、`knowledge_base/manage.py` 5、`chat/tools.py` 5 | 认证链（吊销/Key 查询）标注 fail-open/fail-closed 语义；配额后端故障按环境分流（**生产 REPORTABLE，开发 RECOVERABLE**）；**补上两个完全静默的 500**（文档预览、取文本块原先连日志都没有） |
| 七（10-29） | `model_adapters.py` 4、`tool_manager.py` 4、`kb_retrieval_service.py` 4、`wiki_persist.py` 4、`wiki_lock.py` 4、`knowledge_base/query.py` 4、`chat/message.py` 4 | Provider fallback 链按「换模型属正常降级」记 `RECOVERABLE`，**流式已下发分片后中断改记 `REPORTABLE`**（回答被截断必须可见）；检索「混合失败→dense 兜底」分级，两级皆失败先留痕再上抛；Wiki 归属查询失败不再静默返回空；工具执行失败按需上报（以 `success=False` 回给 Agent 属可见降级） |
| 八（10-29） | `notification.py` 5、`wiki_rebuild.py` 3、`wiki_compiler.py` 3、`question_processor.py` 3、`kb_comparator.py` 3、`intent_router/{__init__,embedding_classifier}` 3+3、`wiki_lookup_tool.py` 3、`ocr_parser.py` 3、`search_postprocessor.py` 3 | **`notification.py` 此前没有 logger**：三处 WS 主循环兜底、单播失败、归属校验全部静默；归属校验改为 `HTTPException`（正常拒绝）与其它异常（需上报）分流，DB 故障不再伪装成「无权限」。Wiki 空白基线失败**显式抬到 `REPORTABLE`**（底层是连接异常，但后果是 revision 归零、需人工核对产物）。`search_postprocessor` 三处 URL 解析容错判定为预期控制流，保留静默并注明 |
| 九（10-29） | `context_enhancer.py` 3、`badcase/feedback_handler.py` 3、`api/badcase.py` 1 | 上下文三处降级（压缩→截断、消解→保留原问题、摘要→沿用旧摘要）全部标可恢复并带上 `context_len`/`session_id` 等定位键；badcase 三处按后果分层：**落库失败显式 `REPORTABLE`**（接口仍返回 200，但反馈实际丢了，属数据损失，且 commit 失败后补 `rollback()` 避免同一会话后续查询直接报错）、在线学习初始化/执行失败按异常性质自动判定（依赖缺失→需上报，模型服务抖动→可恢复）、`api/badcase.py` 里吞掉的 `except ValueError: pass`（自身主键解析失败）改为编程错误留痕 |
| 十（10-29） | `audit_service.py` 1、`notification_service.py` 1、`retention_service.py` 1、`model_usage_store.py` 1、`kb_recommender.py` 1、`guardrails/approval.py` 1 | 进入 1~2 处/文件的**长尾**，只处理「失败后仍有可见后果」的点：**审计写库失败显式 `REPORTABLE`**（不阻断业务是对的，但审计链出现缺口属合规问题，连接类异常也要升级）、`notification_service.broadcast` 推送失败此前完全静默（分不清「用户关页面」与「序列化/权限问题导致全员收不到」）、保留期清扫与用量落库按异常性质分级（连接类 warning，结构类升级，避免「过期审计日志一直没删」被 warning 淹没）、知识库推荐失败补 `kb_scope`/`top_k` 便于聚合、审批缓存不可用按 fail-closed 语义留痕（否则「审批一律被拒」会被误读成用户没点同意） |
| 十一（10-29） | `wiki_compile_scheduler.py` 2、`wiki_lint_scheduler.py` 2、`trace_collector.py` 2、`utils/rate_limit.py` 1 | 后台任务与埋点的缺口可追：Wiki 去抖编译的单篇 raw chunks 失败补 `kb_id`/`doc_id`（否则表现为「wiki 内容莫名缺失」）、整批编译失败保留堆栈与文档数；lint 调度区分「单个 KB 失败」与「循环整体异常」，都能定位到 KB；trace 落库失败带 `trace_id`；**限流 Redis 探测失败按需上报但只记首次**——该函数每个请求都走，且失败后果是限流/配额静默退化为单进程（多副本下限流被放大 N 倍） |
| 十二（10-29） | `title_generator.py` 2、`query_rewriter.py` 2、`strategy_manager.py` 2、`progress_manager.py` 1、`pipeline/generation.py` 1 | 对话体验类降级统一留痕：标题生成（初始化/生成）与 Query 改写（默认 LLM/多查询）失败都退回降级方案，补 `question_len`/`query_len` 便于判断「是不是都在超时」；策略配置回载失败带 `strategy` 名（否则「重启后某策略没生效」无从排查）；答案校验失败只丢「可能有误」提示，按异常性质分级；**上传进度推送失败此前完全静默**（用户只看到进度条卡住），现摘连接并带 `upload_id`；`_cleanup_terminal_progress` 里关闭已断开连接的失败判定为预期控制流，保留静默并注明 |
| 十三（10-29） | `rule_learner.py` 2、`wiki_synthesis_service.py` 2 | **学习成果静默丢失**：决策阈值写库失败时接口仍返回 `success`，现显式 `REPORTABLE` 并在返回值补 `threshold_persisted`（重启后阈值回到旧值这事对调用方可见）；逐样本跳过补 `skipped` 计数便于判断模型侧还是脏数据。synthesis 去重检索失败维持 fail-open（按不存在处理），但补 `kb_scope`——否则检索层长期故障会静默产出大量重复综合页；回流写入失败属本批条目全丢，显式 `REPORTABLE` 并带 `kb_id`/`items` |
| 十四（10-30） | `document_processor.py` 2、`document_analyzer.py` 2、`document_loader.py` 1、`answer_generator.py` 2、`citation_backfiller.py` 2、`evaluation/retrieval_evaluator.py` 3、`evaluation/generation_evaluator.py` 2、`model_config_service.py` 1 | **预览接口的类型掩盖**：`preview_document` 原先把所有异常包成 `ValueError`，路由据此返回 **400**，于是 MinIO/OCR/解析器故障被说成「你的请求有问题」——既不告警也看不到原始异常类型；现只让「文件不存在/类型不支持/超限」保留 ValueError，其余原样上抛走 500 并带 `doc_id`/`filename`/`page`。**重复检测失败返回 `[]` = 「没有重复」**，上传流程据此放行，属结论错误 → 显式 `REPORTABLE`（带 `kb_id`/`threshold`）。引用补全的两级降级：来源向量失败＝整体退化为 `[?]`（记 `sources`），逐句 embedding 失败按句发生，前 3 次记录、之后每 50 次记录，避免一条长答案刷几十行。评估器把「自身故障」与「真的不相关」都算成 0 分，相似度环节显式 `REPORTABLE`，否则只能看到指标掉、看不到是评估器坏了。`model_config_service.test_provider` 的失败返回属预期控制流（管理员点连接测试），保留静默并注明 |

累计 **282 处调用点 / 覆盖约 308 个 `except` 分支**（助手合并的端点按一次调用计）。保留静默的点全部就地写明理由（`get_extra_info` 非 socket 传输、`urlparse` 畸形输入、`model_manager` 属性探测、`aclose` 关闭已失效连接、`_maybe_recover` 冷却期探测、ReAct 行动 JSON 解析、非法金额/时间戳/数值解析、`json.loads` 兜底、SSE 中断指标计数、provider 健康状态重置、registry 版本探测、检索失败埋点、Redis 锁操作的 `ConnectionError` 包装上抛、`search_postprocessor` 三处 URL 解析容错、各引擎 `raise WebSearchError(...)` 包装上抛）。

## 4. 推广顺序（建议）

1. ~~**已拆小的高频路由**：`api/document/{upload,query}.py`、`api/admin_models.py`~~（admin_models 已完成；document 两文件待办）；
2. ~~**外部依赖边界**：`services/milvus_*.py`、`services/web_search_*.py`~~（已完成）；
3. ~~**各 3 处的小文件**：`services/context_enhancer.py`、`services/badcase/feedback_handler.py`、`services/evaluation/retrieval_evaluator.py`~~（已完成，评估器并入第十四批）；当前转 1~2 处/文件的**长尾**：`document_*`、`answer_generator`、`citation_backfiller`、`evaluation/generation_evaluator` 已完成，剩下 `strategies/*`、`tools/plugins/*`、`guardrails` 其余点位；`main.py`（9）判定保持现状（应用装配，异常即启动失败）；
4. **长尾**：其余 ~235 处随日常改动顺带迁移，避免二次触碰同一文件；
5. **收敛规则**：迁移时逐点判断「能否处理」——能处理（重试/降级/跳过）→ 保留 `except` + 分级记录；不能处理（应继续抛）→ 删掉本地 `except`，交给上层。

## 5. 验收口径（单模块）

- 该模块所有 `except Exception` 都走 `log_exception`，或已被删除；
- 日志中 `action=` 可唯一定位到代码位置；
- 可恢复路径无堆栈、需上报路径有堆栈（可用 `caplog` 断言）；
- 对外响应文案不变（回归用既有接口测试）。
