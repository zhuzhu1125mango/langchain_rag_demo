"""模型用量增量落库与持久化聚合（MODEL-P2.1）。

- ``flush_usage_deltas``：把进程内未落库增量合并进 ``model_usage_daily``；
  写库失败会把增量回填内存，主链路不受影响。
- ``query_usage_totals``：读取最近 N 天的聚合，供管理 API 与未落库增量叠加。
- ``merge_usage_rows``：把持久化聚合与进程内增量按
  ``provider + model + task + 币种`` 合并。
- ``periodic_flush``：后台周期任务；应用关闭时取消并做最后一次 flush。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_maker
from src.models.model_registry import ModelUsageDaily
from src.services.model_usage import model_usage
from src.utils.error_handling import log_exception

logger = logging.getLogger("rag_system")

_SUM_FIELDS = (
    "calls",
    "errors",
    "input_tokens",
    "output_tokens",
    "total_latency_ms",
    "priced_calls",
    "unpriced_calls",
    "estimated_cost",
)


async def _upsert_row(session: AsyncSession, row: dict) -> None:
    key = (
        row["usage_date"],
        row["provider_id"],
        row["model_id"],
        row["task"],
        row["cost_currency"],
    )
    existing = await session.get(ModelUsageDaily, key)
    if existing is None:
        session.add(ModelUsageDaily(**row))
        return
    existing.model_name = row.get("model_name") or existing.model_name
    for name in _SUM_FIELDS:
        setattr(existing, name, (getattr(existing, name) or 0) + (row.get(name) or 0))
    if row.get("last_error"):
        existing.last_error = row["last_error"]


async def flush_usage_deltas() -> int:
    """把未落库增量写入数据库；返回成功落库的行数。"""
    rows = model_usage.drain()
    if not rows:
        return 0
    try:
        async with async_session_maker() as session:
            for row in rows:
                await _upsert_row(session, row)
            await session.commit()
    except Exception as exc:
        model_usage.restore(rows)
        # 增量已回填内存，下个周期会重试；连接类记 warning，结构/类型类错误升级，
        # 否则「用量一直落不了库」只会在 warning 里被忽略（影响计费口径）
        log_exception(
            logger,
            exc,
            action="model.usage.flush",
            message="模型用量落库失败，增量已回填内存待下轮重试",
            rows=len(rows),
        )
        return 0
    return len(rows)


async def query_usage_totals(db: AsyncSession, days: int = 7) -> List[dict]:
    """读取最近 N 天（含今天）按 Provider/模型/任务/币种聚合的用量。"""
    days = max(1, int(days))
    start = date.today() - timedelta(days=days - 1)
    stmt = (
        select(
            ModelUsageDaily.provider_id,
            ModelUsageDaily.model_id,
            ModelUsageDaily.task,
            ModelUsageDaily.cost_currency,
            func.max(ModelUsageDaily.model_name).label("model_name"),
            func.coalesce(func.sum(ModelUsageDaily.calls), 0).label("calls"),
            func.coalesce(func.sum(ModelUsageDaily.errors), 0).label("errors"),
            func.coalesce(func.sum(ModelUsageDaily.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(ModelUsageDaily.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(ModelUsageDaily.total_latency_ms), 0.0).label("total_latency_ms"),
            func.coalesce(func.sum(ModelUsageDaily.priced_calls), 0).label("priced_calls"),
            func.coalesce(func.sum(ModelUsageDaily.unpriced_calls), 0).label("unpriced_calls"),
            func.coalesce(func.sum(ModelUsageDaily.estimated_cost), 0.0).label("estimated_cost"),
        )
        .where(ModelUsageDaily.usage_date >= start)
        .group_by(
            ModelUsageDaily.provider_id,
            ModelUsageDaily.model_id,
            ModelUsageDaily.task,
            ModelUsageDaily.cost_currency,
        )
    )
    result = await db.execute(stmt)
    rows: List[dict] = []
    for item in result.mappings().all():
        data = dict(item)
        rows.append(
            {
                "provider_id": data["provider_id"],
                "model_id": data["model_id"],
                "model_name": data.get("model_name") or data["model_id"],
                "task": data["task"],
                "calls": int(data.get("calls") or 0),
                "errors": int(data.get("errors") or 0),
                "input_tokens": int(data.get("input_tokens") or 0),
                "output_tokens": int(data.get("output_tokens") or 0),
                "total_latency_ms": float(data.get("total_latency_ms") or 0.0),
                "priced_calls": int(data.get("priced_calls") or 0),
                "unpriced_calls": int(data.get("unpriced_calls") or 0),
                "estimated_cost": float(data.get("estimated_cost") or 0.0),
                "currency": data.get("cost_currency") or "USD",
                "last_error": "",
            }
        )
    return rows


def merge_usage_rows(*groups: List[dict]) -> List[dict]:
    """合并多组用量（均需含 total_latency_ms 等原始累计字段），返回 API 结构。"""
    merged: Dict[tuple, dict] = {}
    for rows in groups:
        for row in rows:
            key = (
                row.get("provider_id") or "unknown",
                row.get("model_id") or "unknown",
                row.get("task") or "unknown",
                row.get("currency") or "USD",
            )
            agg = merged.setdefault(
                key,
                {
                    "provider_id": key[0],
                    "model_id": key[1],
                    "model_name": row.get("model_name") or key[1],
                    "task": key[2],
                    "currency": key[3],
                    "calls": 0,
                    "errors": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_latency_ms": 0.0,
                    "priced_calls": 0,
                    "unpriced_calls": 0,
                    "estimated_cost": 0.0,
                    "last_error": "",
                },
            )
            for name in _SUM_FIELDS:
                agg[name] += row.get(name) or 0
            if row.get("model_name"):
                agg["model_name"] = row["model_name"]
            if row.get("last_error"):
                agg["last_error"] = row["last_error"]
    result: List[dict] = []
    for agg in merged.values():
        latency_total = agg.pop("total_latency_ms")
        agg["avg_latency_ms"] = round(latency_total / (agg["calls"] or 1), 1)
        agg["estimated_cost"] = round(agg["estimated_cost"], 6)
        for name in ("input_tokens", "output_tokens", "priced_calls", "unpriced_calls", "calls", "errors"):
            agg[name] = int(agg[name])
        result.append(agg)
    result.sort(key=lambda item: (item["provider_id"], item["model_id"], item["task"], item["currency"]))
    return result


async def periodic_flush(interval_s: float) -> None:
    """后台周期落库；异常已在 flush 内部处理，不会中断循环。"""
    interval = max(5.0, float(interval_s or 0))
    while True:
        await asyncio.sleep(interval)
        await flush_usage_deltas()
