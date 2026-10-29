"""HITL 审批点（AM-2）：高危工具执行前的人工批准。

存储：Redis（复用 CacheService），键 ``guardrail:approval:{id}``，TTL = 超时 + 30s。
语义：wait 模式在 SSE 流内轮询等待；Redis 不可用或记录缺失一律按拒绝处理（fail-closed）。
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from src.config import settings
from src.utils.error_handling import log_exception

logger = logging.getLogger("rag_system")

_KEY_PREFIX = "guardrail:approval:"
_POLL_INTERVAL_S = 0.2
_ARGS_PREVIEW_CHARS = 1000


class ApprovalGate:
    """审批闸门（无状态，可注入假缓存用于测试）。"""

    def __init__(self, cache: Any = None, timeout_s: Optional[int] = None):
        """
        Args:
            cache: 缓存客户端（需提供 get/set/delete 异步方法）；None 时懒加载 CacheService。
            timeout_s: 覆盖配置里的等待超时（测试用）。
        """
        self._cache_override = cache
        self._timeout_override = timeout_s

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    @property
    def mode(self) -> str:
        """审批模式：off / reject / wait（未知值按 off 处理并告警）。"""
        mode = (settings.guardrail.AGENT_APPROVAL_MODE or "off").strip().lower()
        if mode not in ("off", "reject", "wait"):
            logger.warning(f"AGENT_APPROVAL_MODE 非法值 {mode!r}，按 off 处理")
            return "off"
        return mode

    @property
    def timeout_s(self) -> int:
        if self._timeout_override is not None:
            return self._timeout_override
        return max(1, int(settings.guardrail.AGENT_APPROVAL_TIMEOUT_S))

    def approval_tools(self) -> set:
        raw = settings.guardrail.AGENT_APPROVAL_TOOLS or ""
        return {n.strip() for n in raw.split(",") if n.strip()}

    def needs_approval(self, tool_name: str, tool: Any = None) -> bool:
        """判断某工具是否需要人工审批（模式为 off 时恒 False）。"""
        if self.mode == "off":
            return False
        if tool_name in self.approval_tools():
            return True
        if tool is None:
            return False
        if bool(getattr(tool, "requires_approval", False)):
            return True
        return getattr(tool, "risk_level", "read") == "write"

    # ------------------------------------------------------------------
    # 缓存依赖
    # ------------------------------------------------------------------
    async def _cache(self):
        if self._cache_override is not None:
            return self._cache_override
        try:
            from src.services.cache_service import CacheService

            return await CacheService.get_instance()
        except Exception as exc:
            # Redis 不可用时按 fail-closed 处理（后续 get 取不到记录 → 拒绝执行），
            # 属预期降级，但要留痕：否则「审批一律被拒」会被误读成用户没点同意
            log_exception(
                logger,
                exc,
                action="guardrails.approval.cache",
                message="审批通道缓存不可用，后续审批按拒绝处理",
            )
            return None

    @staticmethod
    def _key(approval_id: str) -> str:
        return f"{_KEY_PREFIX}{approval_id}"

    # ------------------------------------------------------------------
    # 审批生命周期
    # ------------------------------------------------------------------
    async def request(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """创建一条待审批记录；缓存不可用返回 None（调用方按拒绝处理）。"""
        cache = await self._cache()
        if cache is None:
            return None

        approval_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=self.timeout_s)
        record = {
            "approval_id": approval_id,
            "status": "pending",
            "tool": tool_name,
            "arguments": _preview(arguments),
            "user_id": user_id or "",
            "session_id": session_id or "",
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "reason": "",
        }
        ok = await cache.set(
            self._key(approval_id),
            record,
            expire=timedelta(seconds=self.timeout_s + 30),
        )
        if not ok:
            logger.warning("审批记录写入失败，按拒绝处理")
            return None
        return record

    async def wait(self, approval_id: str) -> Tuple[bool, str]:
        """等待审批结果；返回 (是否批准, 说明)。超时/不可用均返回 False。"""
        cache = await self._cache()
        if cache is None:
            return False, "审批通道不可用"

        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            record = await cache.get(self._key(approval_id))
            if not record:
                return False, "审批记录不存在或已过期"
            status = record.get("status")
            if status == "approved":
                return True, record.get("reason") or ""
            if status == "rejected":
                return False, record.get("reason") or "用户拒绝"
            await asyncio.sleep(_POLL_INTERVAL_S)

        await cache.delete(self._key(approval_id))
        return False, f"审批超时（{self.timeout_s}s）"

    async def resolve(
        self,
        approval_id: str,
        approved: bool,
        user_id: Optional[str] = None,
        reason: str = "",
    ) -> Tuple[bool, str]:
        """回执审批；返回 (是否成功, 错误码 ok/not_found/forbidden/conflict)。"""
        cache = await self._cache()
        if cache is None:
            return False, "not_found"

        key = self._key(approval_id)
        record = await cache.get(key)
        if not record:
            return False, "not_found"

        owner = record.get("user_id") or ""
        if owner and owner != (user_id or ""):
            return False, "forbidden"
        if record.get("status") != "pending":
            return False, "conflict"

        record["status"] = "approved" if approved else "rejected"
        record["reason"] = (reason or "")[:200]
        ttl = timedelta(seconds=self.timeout_s + 30)
        if not await cache.set(key, record, expire=ttl):
            return False, "not_found"
        logger.info(
            f"审批回执: id={approval_id} tool={record.get('tool')} "
            f"approved={approved} user={user_id}"
        )
        return True, "ok"


def _preview(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """审批记录里的参数预览（截断，避免把大对象写进 Redis）。"""
    preview = {}
    for k, v in (arguments or {}).items():
        text = str(v)
        preview[k] = text[:_ARGS_PREVIEW_CHARS]
    return preview


_default_gate = ApprovalGate()


def get_approval_gate() -> ApprovalGate:
    """获取进程级审批闸门实例（配置每次动态读取，便于测试/热更）。"""
    return _default_gate
