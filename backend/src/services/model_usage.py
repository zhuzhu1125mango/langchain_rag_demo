"""模型调用用量统计与成本估算（MODEL-P2.1）。

进程内累计各 Provider/模型/任务的调用次数、失败数、Token、延迟与估算成本，
并由 ``model_usage_store`` 周期把「尚未落库的增量」写入 ``model_usage_daily``。
已落库数据在重启后保留；未落库增量最多丢失一个 flush 周期。

成本 = input_tokens / 1000 × input_cost_per_1k
     + output_tokens / 1000 × output_cost_per_1k。
模型未配置价格时成本记 0，并累计 ``unpriced_calls`` 供界面标注。

设计见 docs/design/model-provider-registry-2026-10.md §9（P2.1）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple


@dataclass
class _UsageStat:
    model_name: str = ""
    calls: int = 0
    errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_latency_ms: float = 0.0
    priced_calls: int = 0
    unpriced_calls: int = 0
    estimated_cost: float = 0.0
    currency: str = "USD"
    last_error: str = ""


@dataclass
class _UsageDelta(_UsageStat):
    usage_date: date = field(default_factory=date.today)


def _clean_price(value: Optional[float]) -> Optional[float]:
    """未配置或非法/负价格视为未定价。"""
    if value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price >= 0 else None


def _row(provider_id: str, model_id: str, task: str, st: _UsageStat) -> dict:
    calls = st.calls or 1
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "model_name": st.model_name or model_id,
        "task": task,
        "calls": st.calls,
        "errors": st.errors,
        "input_tokens": st.input_tokens,
        "output_tokens": st.output_tokens,
        "total_latency_ms": st.total_latency_ms,
        "avg_latency_ms": round(st.total_latency_ms / calls, 1),
        "priced_calls": st.priced_calls,
        "unpriced_calls": st.unpriced_calls,
        "estimated_cost": round(st.estimated_cost, 6),
        "currency": st.currency,
        "last_error": st.last_error,
    }


class ModelUsageTracker:
    """线程安全的用量聚合器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stats: Dict[Tuple[str, str, str, str], _UsageStat] = {}
        # 未落库增量：key 含日期与币种，便于按天持久化且不混币种
        self._pending: Dict[Tuple[date, str, str, str, str], _UsageDelta] = {}

    def record(
        self,
        provider_id: str,
        model_id: str,
        model_name: str,
        task: str,
        latency_ms: float,
        success: bool,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error: str = "",
        input_cost_per_1k: Optional[float] = None,
        output_cost_per_1k: Optional[float] = None,
        currency: str = "USD",
    ) -> None:
        provider_id = provider_id or "unknown"
        model_id = model_id or "unknown"
        task = task or "unknown"
        currency = (currency or "USD").upper()[:8] or "USD"
        in_tokens = max(0, int(input_tokens or 0))
        out_tokens = max(0, int(output_tokens or 0))
        latency = max(0.0, float(latency_ms or 0))
        in_price = _clean_price(input_cost_per_1k)
        out_price = _clean_price(output_cost_per_1k)

        priced = success and (in_price is not None or out_price is not None)
        cost = 0.0
        if priced:
            cost = (in_tokens * (in_price or 0.0) + out_tokens * (out_price or 0.0)) / 1000.0

        with self._lock:
            st = self._stats.setdefault((provider_id, model_id, task, currency), _UsageStat())
            self._apply(
                st,
                model_name=model_name or model_id,
                latency=latency,
                success=success,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                priced=priced,
                cost=cost,
                error=error,
                currency=currency,
            )
            delta = self._pending.setdefault(
                (date.today(), provider_id, model_id, task, currency),
                _UsageDelta(usage_date=date.today()),
            )
            self._apply(
                delta,
                model_name=model_name or model_id,
                latency=latency,
                success=success,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                priced=priced,
                cost=cost,
                error=error,
                currency=currency,
            )
        self._emit_metrics(provider_id, model_name or model_id, task, success, in_tokens, out_tokens)

    @staticmethod
    def _apply(
        st: _UsageStat,
        *,
        model_name: str,
        latency: float,
        success: bool,
        input_tokens: int,
        output_tokens: int,
        priced: bool,
        cost: float,
        error: str,
        currency: str,
    ) -> None:
        st.model_name = model_name or st.model_name
        st.currency = currency or st.currency
        st.calls += 1
        st.total_latency_ms += latency
        if success:
            st.input_tokens += input_tokens
            st.output_tokens += output_tokens
            if priced:
                st.priced_calls += 1
                st.estimated_cost += cost
            else:
                st.unpriced_calls += 1
        else:
            st.errors += 1
            st.last_error = (error or "")[:500]

    def snapshot(self) -> List[dict]:
        """进程内累计（含已落库和未落库）。"""
        with self._lock:
            return [
                _row(provider_id, model_id, task, st)
                for (provider_id, model_id, task, _currency), st in sorted(self._stats.items())
            ]

    def pending_snapshot(self) -> List[dict]:
        """尚未落库的增量（供管理 API 与持久化聚合叠加）。"""
        with self._lock:
            return [
                _row(provider_id, model_id, task, delta)
                for (_day, provider_id, model_id, task, _currency), delta in sorted(self._pending.items())
            ]

    def drain(self) -> List[dict]:
        """取出并清空未落库增量（写库失败时用 restore 回填）。"""
        with self._lock:
            rows = [
                {
                    "usage_date": delta.usage_date,
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "task": task,
                    "model_name": delta.model_name or model_id,
                    "calls": delta.calls,
                    "errors": delta.errors,
                    "input_tokens": delta.input_tokens,
                    "output_tokens": delta.output_tokens,
                    "total_latency_ms": delta.total_latency_ms,
                    "priced_calls": delta.priced_calls,
                    "unpriced_calls": delta.unpriced_calls,
                    "estimated_cost": delta.estimated_cost,
                    "cost_currency": delta.currency,
                    "last_error": delta.last_error,
                }
                for (_day, provider_id, model_id, task, _currency), delta in self._pending.items()
            ]
            self._pending.clear()
            return rows

    def restore(self, rows: List[dict]) -> None:
        """落库失败时把增量合并回内存，避免丢失。"""
        if not rows:
            return
        with self._lock:
            for row in rows:
                currency = (row.get("cost_currency") or "USD").upper()[:8] or "USD"
                key = (
                    row["usage_date"],
                    row["provider_id"],
                    row["model_id"],
                    row["task"],
                    currency,
                )
                delta = self._pending.setdefault(key, _UsageDelta(usage_date=row["usage_date"]))
                delta.model_name = row.get("model_name") or delta.model_name
                delta.currency = currency
                delta.calls += int(row.get("calls") or 0)
                delta.errors += int(row.get("errors") or 0)
                delta.input_tokens += int(row.get("input_tokens") or 0)
                delta.output_tokens += int(row.get("output_tokens") or 0)
                delta.total_latency_ms += float(row.get("total_latency_ms") or 0)
                delta.priced_calls += int(row.get("priced_calls") or 0)
                delta.unpriced_calls += int(row.get("unpriced_calls") or 0)
                delta.estimated_cost += float(row.get("estimated_cost") or 0)
                if row.get("last_error"):
                    delta.last_error = row["last_error"][:500]

    def reset(self) -> None:
        with self._lock:
            self._stats.clear()
            self._pending.clear()

    @staticmethod
    def _emit_metrics(
        provider_id: str,
        model_name: str,
        task: str,
        success: bool,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        try:
            from src.middleware.prometheus import record_model_usage

            record_model_usage(
                provider_id=provider_id or "unknown",
                model_name=model_name or "unknown",
                task=task or "unknown",
                success=success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except Exception:  # 指标不可用不能影响主链路
            pass


model_usage = ModelUsageTracker()
