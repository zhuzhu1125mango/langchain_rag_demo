"""Agent 工具动作：白名单/schema、并行执行与去重（CODE-1 拆分）。"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.config import settings

logger = logging.getLogger("rag_system")

from src.services.agent_state import AgentLoopState

class AgentActionsMixin:
    """工具选择与执行，由 AgentOrchestrator 组合。"""

    def _enabled_tools(self):
        """按 AGENT_TOOLS_ENABLED 白名单解析工具实例（缺省跳过未注册项）。"""
        whitelist = [
            n.strip()
            for n in settings.search.AGENT_TOOLS_ENABLED.split(",")
            if n.strip()
        ]
        tools = []
        for name in whitelist:
            # SEC-5：深度抓取默认关闭；未显式开启时不向模型暴露该工具。
            if name == "fetch_webpage" and not settings.search.AGENT_WEB_FETCH_ENABLED:
                continue
            tool = self.tool_manager.registry.get(name)
            if tool is None:
                logger.debug(f"Agent 工具白名单项未注册，跳过: {name}")
                continue
            tools.append(tool)
        return tools

    def _tools_schema(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self._enabled_tools()]

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------

    @staticmethod
    def _call_key(name: str, args: Dict[str, Any]) -> str:
        try:
            return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        except (TypeError, ValueError):
            return f"{name}:{str(args)}"

    async def _execute_actions(
        self, actions: List[Dict[str, Any]], loop: AgentLoopState,
        kb_ids: Optional[List[str]] = None,
        owner_id: Optional[str] = None,
        trace: Any = None,
    ):
        """执行本步全部工具调用（去重 → 审批 → 并行执行）。

        AM-2：命中审批闸门的工具在 ACT 之前 yield ("approval_required", payload)，
        wait 模式阻塞等待人工回执；最终 yield ("action_results", results, observations)。

        kb_search / wiki_lookup 的 KB 范围一律以编排层注入为准：
        - 剥离模型显式传入的 kb_ids（不可信，可能指向他人知识库）；
        - 会话 kb_ids（已经 API 层 validate_kb_ownership 校验）注入为检索白名单；
        - 注入 _owner_id 供支持 supports_owner_scope 的工具做二道归属校验（纵深防御）。
        """
        from src.services.guardrails import get_approval_gate
        from src.services.tools.tool_manager import ToolResult

        gate = get_approval_gate()
        pending = []
        observations: List[Dict[str, str]] = []
        for action in actions:
            name = action["name"]
            args = action.get("args") or {}
            if name in ("kb_search", "wiki_lookup"):
                # 安全（W1-4）：模型不可信，剥离其显式 kb_ids；仅当会话已绑定 KB 时注入白名单
                args = {k: v for k, v in args.items() if k != "kb_ids"}
                if kb_ids:
                    args = {**args, "kb_ids": list(kb_ids)}
                tool = self.tool_manager.registry.get(name)
                if owner_id and getattr(tool, "supports_owner_scope", False):
                    args = {**args, "_owner_id": owner_id}
            key = self._call_key(name, args)
            if key in loop.seen_calls:
                observations.append({
                    "tool": name,
                    "args": args,
                    "observation": "重复调用（相同参数已执行过）。请更换查询词或直接总结已有结果作答。",
                })
                continue
            loop.seen_calls.add(key)

            # AM-2 HITL：高危工具执行前审批（模式 off / 未命中名单时直接放行）
            tool = self.tool_manager.registry.get(name)
            if gate.needs_approval(name, tool):
                if gate.mode == "reject":
                    logger.warning(f"审批拒绝（reject 模式）: tool={name}")
                    observations.append({
                        "tool": name,
                        "args": args,
                        "observation": f"执行失败: 工具 {name} 需要人工审批，当前审批模式已直接拒绝。",
                    })
                    if trace is not None:
                        trace.add_stage(
                            "approval", "rejected", latency_ms=0,
                            detail={"tool": name, "mode": "reject"},
                        )
                    continue

                record = await gate.request(name, args, user_id=owner_id)
                if record is None:
                    observations.append({
                        "tool": name,
                        "args": args,
                        "observation": f"执行失败: 工具 {name} 的审批通道不可用，已拒绝执行。",
                    })
                    if trace is not None:
                        trace.add_stage(
                            "approval", "unavailable", latency_ms=0,
                            detail={"tool": name},
                        )
                    continue

                yield ("approval_required", json.dumps({
                    "type": "approval_required",
                    "approval_id": record["approval_id"],
                    "tool": name,
                    "arguments": record.get("arguments") or {},
                    "timeout_s": gate.timeout_s,
                    "expires_at": record["expires_at"],
                }, ensure_ascii=False))
                approval_start = time.time()
                approved, reason = await gate.wait(record["approval_id"])
                approval_ms = int((time.time() - approval_start) * 1000)
                if trace is not None:
                    trace.add_stage(
                        "approval", "approved" if approved else "rejected",
                        latency_ms=approval_ms,
                        detail={"tool": name, "mode": "wait", "reason": reason[:120]},
                    )
                if not approved:
                    logger.info(f"人工审批未通过: tool={name} reason={reason}")
                    observations.append({
                        "tool": name,
                        "args": args,
                        "observation": f"执行失败: 工具 {name} 未获人工批准（{reason}）。请勿重复调用，改用其他方式或直接作答。",
                    })
                    yield (
                        "reasoning",
                        self._step_payload(
                            "done", "人工审批",
                            f"工具 {name} 未获批准，跳过执行",
                            duration_ms=approval_ms,
                            metadata={"tool": name, "approved": False},
                        ),
                    )
                    continue
                yield (
                    "reasoning",
                    self._step_payload(
                        "done", "人工审批",
                        f"工具 {name} 已获批准，继续执行",
                        duration_ms=approval_ms,
                        metadata={"tool": name, "approved": True},
                    ),
                )

            pending.append({"tool_name": name, "arguments": args, "key": key})

        results: List[ToolResult] = []
        if pending:
            calls = [{"tool_name": p["tool_name"], "arguments": p["arguments"]} for p in pending]
            results = await self.tool_manager.execute_parallel(calls)

        max_chars = settings.search.AGENT_OBS_MAX_CHARS
        for p, result in zip(pending, results):
            output = result.output if result.success else f"执行失败: {result.error}"
            if len(output) > max_chars:
                output = output[:max_chars] + "…(已截断)"
            observations.append({
                "tool": p["tool_name"],
                "args": p["arguments"],
                "observation": output,
            })
            # ToolResult 携带 key 便于综合阶段排除去重占位
            result.input_arguments = p["arguments"]
        yield ("action_results", results, observations)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
