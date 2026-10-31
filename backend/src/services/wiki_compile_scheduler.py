"""Wiki 编译去抖调度器（P5）。

批量上传 N 个文档时合并为一次编译：per-KB pending 集合 + 计时器
（WIKI_COMPILE_DEBOUNCE_SECONDS，默认 20s）。计时器触发时快照并清空
pending，从 Milvus 拉取各文档 raw chunks（load_raw_chunks，无需跨任务
传递内存 chunks），合并调用一次 WikiCompiler.compile_document（doc_ids 多值）。

进程内实现（单 worker 语义）：多副本部署时各 worker 本地去抖，仍正确，
仅合并度下降（§14.5 明确不做跨 worker 共享 pending）。
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Set

from src.config import settings
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("wiki_compile_scheduler")

# per-KB 待编译文档集合（kb_id -> set(doc_id)）
_pending: Dict[str, Set[str]] = {}
# per-KB 去抖计时器（kb_id -> asyncio.TimerHandle）
_timers: Dict[str, asyncio.TimerHandle] = {}
# 持有在途编译任务引用防 GC（done_callback 自动清理）
_running_tasks: Set[asyncio.Task] = set()


def schedule_compile(kb_id: str, doc_id: str) -> None:
    """登记待编译文档并重置该 KB 的去抖计时器（同步非阻塞，上传管线调用）。

    必须在运行中的事件循环内调用（上传处理协程满足）。
    """
    loop = asyncio.get_running_loop()
    key = str(kb_id)
    pending = _pending.setdefault(key, set())
    pending.add(str(doc_id))

    old = _timers.get(key)
    if old is not None:
        old.cancel()

    delay = max(0, settings.wiki_compile.WIKI_COMPILE_DEBOUNCE_SECONDS)
    _timers[key] = loop.call_later(delay, _fire, key)
    logger.info(
        f"Wiki 编译已入队（去抖 {delay}s）: kb={kb_id}, doc={doc_id}, "
        f"当前队列 {len(pending)} 篇"
    )


def pending_docs(kb_id: str) -> Set[str]:
    """查看某 KB 当前待编译文档集合（测试 / 运维观测用）。"""
    return set(_pending.get(str(kb_id), ()))


def _fire(key: str) -> None:
    """计时器回调：快照并清空 pending，创建后台编译任务（fire-and-forget）。"""
    _timers.pop(key, None)
    doc_ids = sorted(_pending.pop(key, ()))
    if not doc_ids:
        return
    task = asyncio.create_task(_compile_batch(key, doc_ids))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


async def wait_running_tasks(timeout: Optional[float] = None) -> None:
    """等待全部在途去抖编译任务完成（测试 / 优雅收尾用）。"""
    if not _running_tasks:
        return
    await asyncio.wait(
        set(_running_tasks), timeout=timeout, return_when=asyncio.ALL_COMPLETED
    )


async def drain(timeout: Optional[float] = 30.0) -> Dict[str, int]:
    """关机前排空去抖队列与在途编译，避免重启丢掉已上传文档的 Wiki 编译。

    上传管线在 debounce>0 时只做两件事：把 doc_id 放进 pending、起一个
    ``loop.call_later`` 计时器（默认 20s）。这两样都只在**进程内存**里，
    进程一旦退出就一起消失 —— 上一次上传回报的是「已加入批量编译队列」，
    之后既没有任务记录也没有日志，这些文档的 Wiki 页从此不会生成，
    wiki_lookup 也永远检索不到它们。

    因此在 lifespan shutdown 里必须先「立即触发待编译 + 等完在途任务」，
    再退出进程。返回 ``{"fired", "running", "cancelled"}``：
    ``cancelled > 0`` 说明超时窗口内没跑完，这部分文档的 Wiki 仍会缺失。

    Args:
        timeout: 等待在途编译完成的上限（秒），None 表示不设上限。
    """
    # 1. 取消全部去抖计时器，改为立即触发（不再等 20s 窗口）
    fired = 0
    for handle in list(_timers.values()):
        handle.cancel()
    _timers.clear()
    for key in list(_pending.keys()):
        if _pending.get(key):
            _fire(key)
            fired += 1

    running = len(_running_tasks)
    cancelled = 0
    if _running_tasks:
        pending_tasks = set(_running_tasks)
        _, still_running = await asyncio.wait(
            pending_tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED
        )
        if still_running:
            cancelled = len(still_running)
            for task in still_running:
                task.cancel()
            # 给取消留一点收敛时间，避免 "Task was destroyed but pending"
            await asyncio.wait(still_running, timeout=5)

    if cancelled:
        # 编译是「文档 → Wiki 页」的唯一写入方，没跑完就是永久缺页；
        # 此时进程即将退出，只能靠这条日志让运维知道重启代价
        log_exception(
            logger,
            RuntimeError("Wiki 编译未在关机超时内完成"),
            action="wiki.scheduler.drain_timeout",
            severity=ErrorSeverity.REPORTABLE,
            message="关机时在途 Wiki 编译被取消，相关文档的 Wiki 页将缺失",
            cancelled=cancelled,
            timeout_s=timeout,
        )

    return {"fired": fired, "running": running, "cancelled": cancelled}


async def _compile_batch(kb_id: str, doc_ids: List[str]) -> None:
    """执行一次合并编译：逐文档拉 raw chunks → 合并 → compile_document。

    去抖窗口内被删除的文档 load_raw_chunks 返回空列表自然跳过；
    编译超时/失败仅记日志与指标（与上传管线阶段 7 同语义，不产生用户可见错误）。
    """
    from src.database import async_session_maker
    from src.middleware.prometheus import record_wiki_compile
    from src.services.wiki_compiler import WikiCompiler
    from src.services.wiki_rebuild import load_raw_chunks

    started = time.monotonic()
    try:
        async with async_session_maker() as db:
            chunks = []
            valid_doc_ids: List[str] = []
            for doc_id in doc_ids:
                try:
                    doc_chunks = await load_raw_chunks(kb_id, doc_id)
                except Exception as e:
                    # 单篇拉取失败只跳过该文档，但结果是「这篇的内容没进 wiki」，
                    # 必须能按 doc_id 追到，否则表现为 wiki 内容莫名缺失
                    log_exception(
                        logger,
                        e,
                        action="wiki.scheduler.load_chunks",
                        message="拉取 raw chunks 失败，跳过该文档",
                        kb_id=kb_id,
                        doc_id=doc_id,
                    )
                    continue
                if doc_chunks:
                    chunks.extend(doc_chunks)
                    valid_doc_ids.append(doc_id)

            if not chunks:
                logger.info(f"Wiki 去抖编译跳过（无有效 raw chunks）: kb={kb_id}, docs={doc_ids}")
                return

            try:
                result = await asyncio.wait_for(
                    WikiCompiler().compile_document(db, kb_id, valid_doc_ids, chunks),
                    timeout=settings.wiki_compile.WIKI_COMPILE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                record_wiki_compile(
                    result="timeout", duration=time.monotonic() - started
                )
                logger.warning(
                    f"Wiki 去抖编译超时: kb={kb_id}, docs={valid_doc_ids}, "
                    f"超时阈值 {settings.wiki_compile.WIKI_COMPILE_TIMEOUT_SECONDS}s"
                )
                return

            record_wiki_compile(
                result="ok",
                pages_created=result.pages_created,
                pages_updated=result.pages_updated,
                duration=time.monotonic() - started,
                fact_retention=result.fact_retention_rate,
            )
            logger.info(
                f"Wiki 去抖编译完成: kb={kb_id}, docs={len(valid_doc_ids)} 篇, "
                f"新建 {result.pages_created} 页, 更新 {result.pages_updated} 页, "
                f"耗时 {time.monotonic() - started:.1f}s"
            )
    except Exception as e:
        record_wiki_compile(result="failed")
        # 编译失败对用户不可见（去抖后台任务），指标只给计数，
        # 这里要留下 kb/doc 与堆栈，才能判断是数据问题还是编译器缺陷
        log_exception(
            logger,
            e,
            action="wiki.scheduler.compile",
            message="Wiki 去抖编译失败",
            kb_id=kb_id,
            docs=len(doc_ids),
        )
