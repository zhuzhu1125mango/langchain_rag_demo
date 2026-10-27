"""Provider 级健康检查与熔断（MODEL-P2.1）。

- 连续失败达到阈值后打开熔断，冷却期内跳过该 Provider，直接走 fallback；
- 冷却结束进入半开，只放行一次探测；成功关闭，失败重新打开；
- 状态仅保存在进程内存，registry 热更新/进程重启后重置。

设计见 docs/design/model-provider-registry-2026-10.md §7/§9。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("rag_system")

FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 60.0
HALF_OPEN_MAX_PROBES = 1


class ProviderCircuitError(RuntimeError):
    """Provider 处于熔断/半开探测中，本次调用被跳过。"""


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    opened_at: Optional[float] = None
    half_open_in_flight: int = 0
    total_calls: int = 0
    total_failures: int = 0
    total_skipped: int = 0
    last_error: str = ""
    last_failure_at: Optional[float] = None


class ProviderHealthTracker:
    """线程安全的 Provider 熔断器。"""

    def __init__(
        self,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._states: Dict[str, _ProviderState] = {}

    def _state_for(self, provider_id: str) -> _ProviderState:
        return self._states.setdefault(provider_id, _ProviderState())

    def state(self, provider_id: str) -> str:
        """返回 closed / open / half_open。"""
        with self._lock:
            st = self._state_for(provider_id)
            if st.opened_at is None:
                return "closed"
            if self._clock() - st.opened_at >= self.cooldown_seconds:
                return "half_open"
            return "open"

    def is_available(self, provider_id: str) -> bool:
        """是否允许调用；半开状态只放行有限次探测。"""
        with self._lock:
            st = self._state_for(provider_id)
            if st.opened_at is None:
                return True
            if self._clock() - st.opened_at < self.cooldown_seconds:
                st.total_skipped += 1
                return False
            if st.half_open_in_flight >= HALF_OPEN_MAX_PROBES:
                st.total_skipped += 1
                return False
            st.half_open_in_flight += 1
            return True

    def record_success(self, provider_id: str) -> None:
        with self._lock:
            st = self._state_for(provider_id)
            was_open = st.opened_at is not None
            st.total_calls += 1
            st.consecutive_failures = 0
            st.opened_at = None
            st.half_open_in_flight = 0
            st.last_error = ""
        if was_open:
            logger.info("Provider 熔断已关闭: provider=%s", provider_id)
            self._update_gauge(provider_id, "closed")

    def record_failure(self, provider_id: str, error: BaseException) -> None:
        message = f"{type(error).__name__}: {error}"[:500]
        with self._lock:
            st = self._state_for(provider_id)
            was_open = st.opened_at is not None
            st.total_calls += 1
            st.total_failures += 1
            st.consecutive_failures += 1
            st.last_error = message
            st.last_failure_at = self._clock()
            st.half_open_in_flight = 0
            should_open = st.consecutive_failures >= self.failure_threshold
            if should_open:
                st.opened_at = self._clock()
        if should_open:
            if not was_open:
                logger.warning(
                    "Provider 熔断打开: provider=%s, consecutive_failures=%d, cooldown=%.0fs, error=%s",
                    provider_id,
                    st.consecutive_failures,
                    self.cooldown_seconds,
                    message,
                )
            self._update_gauge(provider_id, "open")

    def reset(self, provider_id: Optional[str] = None) -> None:
        with self._lock:
            if provider_id is None:
                provider_ids = list(self._states)
                self._states.clear()
            else:
                provider_ids = [provider_id] if provider_id in self._states else []
                self._states.pop(provider_id, None)
        for pid in provider_ids:
            self._update_gauge(pid, "closed")

    def snapshot(self) -> List[dict]:
        now = self._clock()
        with self._lock:
            rows = []
            for provider_id, st in sorted(self._states.items()):
                if st.opened_at is None:
                    state = "closed"
                elif now - st.opened_at >= self.cooldown_seconds:
                    state = "half_open"
                else:
                    state = "open"
                rows.append(
                    {
                        "provider_id": provider_id,
                        "state": state,
                        "consecutive_failures": st.consecutive_failures,
                        "total_calls": st.total_calls,
                        "total_failures": st.total_failures,
                        "total_skipped": st.total_skipped,
                        "opened_at": st.opened_at,
                        "last_failure_at": st.last_failure_at,
                        "last_error": st.last_error,
                    }
                )
            return rows

    @staticmethod
    def _update_gauge(provider_id: str, state: str) -> None:
        try:
            from src.middleware.prometheus import set_model_circuit_state

            set_model_circuit_state(provider_id, state)
        except Exception:  # 指标不可用不能影响主链路
            pass


provider_health = ProviderHealthTracker()
