"""一致性巡检周期调度（P1-3 的容器内实现）。

背景：巡检原本靠 Windows 计划任务跑 `scripts/nightly_consistency.ps1`，但 `schtasks`
在受限机器上会被安全策略直接拦死（不可绕过）。结果是「每天巡检一次」这件事挂在了
某台机器的任务计划里——换台机器、换个人就静默消失，而台账上还写着「脚本已验证」。

把循环放进进程内，巡检就跟着服务走：容器在跑，巡检就在跑。

三条刻意的设计：

1. **不无条件「启动即跑」**：先看 Redis 里的上次执行时间（`rag:audit:last_run`）。
   间距未到就只等剩余时间；没有记录才在启动后 60s 补跑一次。
   否则「容器每天重启一次」会让每轮都从头计时，永远等不到执行窗口——
   这是计划任务版不存在、而进程内版必然踩到的坑。
2. **单轮超时**：`TIMEOUT_SECONDS` 兜底。扫描卡住时不能把后台循环永久占住，
   否则从外面看只是「最近没日志」，看不出是卡死还是没到点。
3. **结果落盘 + 分级日志**：每轮写一份 JSON（默认 `data/consistency/`，生产该目录
   已挂载到宿主机），并按有无差异分别 WARNING / INFO。有差异必须能被日志告警抓到，
   不能只体现在脚本退出码里——后台循环没有退出码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.config import BASE_DIR, settings
from src.utils.error_handling import log_exception

logger = logging.getLogger("rag_system")

# 与巡检脚本共用同一个「上次执行时间」键，手工跑一次也算数
LAST_RUN_KEY = "rag:audit:last_run"
# 没有历史记录时，启动后多久补跑（给迁移/预热留出余量）
_FIRST_RUN_DELAY_SECONDS = 60

_task: Optional[asyncio.Task] = None


def compute_delay(
    interval_hours: int,
    last_run_ts: Optional[float],
    now: Optional[float] = None,
) -> float:
    """返回距离下一次执行还要等多少秒（纯函数，便于测试）。

    - `last_run_ts is None`（没有历史记录）→ 启动后补跑一次的短延迟
    - 已超期 → 同样短延迟（尽快跑，不等到下一个整点）
    - 未超期 → 剩余时间
    """
    if interval_hours <= 0:
        return float("inf")
    now = time.time() if now is None else now
    interval_seconds = interval_hours * 3600
    if last_run_ts is None:
        return float(_FIRST_RUN_DELAY_SECONDS)
    elapsed = now - last_run_ts
    if elapsed >= interval_seconds or elapsed < 0:
        # elapsed < 0 说明时钟回拨或记录来自未来：按「已超期」处理，避免长时间不跑
        return float(_FIRST_RUN_DELAY_SECONDS)
    return max(float(_FIRST_RUN_DELAY_SECONDS), interval_seconds - elapsed)


def resolve_output_dir() -> str:
    """报告目录：显式配置优先，否则 `<backend根>/data/consistency`。

    注意不能直接用 `DATA_DIR`：它是 `BASE_DIR/data`，而 `BASE_DIR` 在容器里是 `/app/src`，
    于是报告会写进镜像层（`/app/src/data`），容器重建即丢。生产挂载的是 `/app/data`，
    也就是后端根下的 `data`——与 `LOG_DIR` 用 `BASE_DIR/../logs` 是同一个口径。
    """
    configured = (settings.consistency_audit.CONSISTENCY_AUDIT_OUTPUT_DIR or "").strip()
    if configured:
        return configured
    backend_root = os.path.dirname(os.path.abspath(BASE_DIR))
    return os.path.join(backend_root, "data", "consistency")


def prune_reports(directory: str, keep: int) -> List[str]:
    """只保留最近 `keep` 份报告（按文件名升序删旧）。返回被删的文件名。"""
    if keep <= 0:
        return []
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return []
    removed = names[:-keep] if len(names) > keep else []
    for name in removed:
        try:
            os.remove(os.path.join(directory, name))
        except OSError as exc:
            logger.warning(f"巡检报告清理失败 file={name}: {exc}")
    return removed


def write_report(report: Dict[str, Any], directory: Optional[str] = None) -> Optional[str]:
    """把报告写到 `directory`，返回路径；写失败只告警（不影响巡检结论）。"""
    target_dir = directory or resolve_output_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(target_dir, f"consistency_{stamp}.json")
    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning(f"巡检报告写入失败 dir={target_dir}: {exc}")
        return None
    keep = settings.consistency_audit.CONSISTENCY_AUDIT_KEEP_FILES
    removed = prune_reports(target_dir, keep)
    if removed:
        logger.info(f"巡检报告已轮转，删除 {len(removed)} 份旧报告（保留 {keep} 份）")
    return path


async def _read_last_run() -> Optional[float]:
    """读上次执行时间；缓存不可用返回 None（按「没有记录」处理，宁可多跑一轮）。"""
    try:
        from src.services.cache_service import CacheService

        cache = await CacheService.get_instance()
        raw = await cache.client.get(LAST_RUN_KEY)
        return float(raw) if raw else None
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不能阻断巡检调度
        logger.warning(f"读取巡检上次执行时间失败，按无记录处理: {exc}")
        return None


async def _mark_last_run() -> None:
    try:
        from src.services.cache_service import CacheService

        cache = await CacheService.get_instance()
        # 记录本身不需要长期保留：超过间隔即过期，少占内存也不会误判
        await cache.client.set(LAST_RUN_KEY, str(time.time()), ex=60 * 60 * 24 * 30)
    except Exception as exc:  # noqa: BLE001 - 记录失败只影响下一轮时机
        logger.warning(f"记录巡检执行时间失败: {exc}")


async def run_once() -> Dict[str, Any]:
    """执行一轮巡检并落盘；异常不外抛（后台循环必须活着）。"""
    from src.services.consistency_audit import run_audit

    timeout = max(30, settings.consistency_audit.CONSISTENCY_AUDIT_TIMEOUT_SECONDS)
    started = time.time()
    try:
        report = await asyncio.wait_for(run_audit(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.error(f"一致性巡检超时（>{timeout}s），本轮放弃，等待下一轮")
        return {"exit_code": 2, "issue_count": 0, "fatal": "timeout"}
    except Exception as exc:  # noqa: BLE001 - 单轮失败不能终止循环
        log_exception(
            logger,
            exc,
            action="consistency.audit.run",
            message="一致性巡检执行失败，等待下一轮",
            timeout_seconds=timeout,
        )
        return {"exit_code": 2, "issue_count": 0, "fatal": str(exc)}

    path = write_report(report)
    issues = int(report.get("issue_count") or 0)
    duration = round(time.time() - started, 2)
    if report.get("fatal") or issues:
        # 有差异走 WARNING：后台循环没有退出码，日志是唯一能被告警抓到的出口
        sections = ",".join(
            f"{name}:{data.get('issues', 0)}"
            for name, data in (report.get("sections") or {}).items()
        )
        logger.warning(
            f"一致性巡检发现差异 issues={issues} exit_code={report.get('exit_code')} "
            f"duration={duration}s report={path or '<未落盘>'} sections={sections or '<无'}"
            f" fatal={report.get('fatal') or ''}"
        )
    else:
        logger.info(
            f"一致性巡检通过 issues=0 duration={duration}s report={path or '<未落盘>'} "
            f"documents={(report.get('db') or {}).get('documents', 0)}"
        )
    return report


async def _loop() -> None:
    interval = max(0, settings.consistency_audit.CONSISTENCY_AUDIT_INTERVAL_HOURS)
    while True:
        delay = compute_delay(interval, await _read_last_run())
        if delay == float("inf"):
            return
        logger.info(f"一致性巡检下次执行将在 {delay / 60:.0f} 分钟后（间隔 {interval}h）")
        await asyncio.sleep(delay)
        await run_once()
        await _mark_last_run()


def start_scheduler() -> Optional[asyncio.Task]:
    """启动巡检循环（幂等；间隔 ≤0 时不启动）。返回任务或 None。"""
    global _task
    if _task is not None and not _task.done():
        return _task
    interval = max(0, settings.consistency_audit.CONSISTENCY_AUDIT_INTERVAL_HOURS)
    if interval <= 0:
        return None
    _task = asyncio.create_task(_loop())
    logger.info(f"一致性巡检周期调度已启动（每 {interval} 小时）")
    return _task


async def stop_scheduler() -> None:
    """取消后台循环（lifespan shutdown 调用；幂等）。"""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
