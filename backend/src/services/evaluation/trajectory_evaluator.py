"""Agent 轨迹级评测（AM-1）。

在答案级判分（GenerationEvaluator）之外补一层「轨迹判分」：以**确定性规则**对
Agent 的**工具调用轨迹**打分，覆盖四个维度——工具选择 / 步数效率 / 信息覆盖 / 合规，
输出 0-1 分与 pass/fail。

演化说明（2026-10-20 三轮抽检）：四维原由 LLM-as-judge 打分，但项目受 4GB 显存
红線约束、辅助模型只能是 3B；实证 3B judge 对**每一维**都不可靠（详见各 `_rule_*`
说明），故四维全部改为确定性规则——判分可离线、可复现、进 CI，且不再依赖 LLM。
设计见 docs/design/agent-eval-trace.md。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.config import settings

# 单条 observation / 参数 摘要截断（控判分输入长度，按 token 效率优化）
DIGEST_CHARS = 300

# 「无实质/无依据作答」确定性模式：命中即判部分不合规（合规维不走 LLM）
_NON_ANSWER_RE = re.compile(
    r"(无法找到|找不到|未找到|没有找到|无法回答|无法提供|无法直接|无法确定|"
    r"请先选择|未选择|暂无法|暂无相关)"
)


def _digest(value: Any, limit: int = DIGEST_CHARS) -> str:
    """把任意值压成短的、单行的摘要字符串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            value = str(value)
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit]


@dataclass
class TrajectoryStep:
    """轨迹中的一步（一次工具调用）。"""

    index: int
    tool: str
    args_digest: str = ""
    ok: bool = True
    observation_digest: str = ""
    latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "args_digest": self.args_digest,
            "ok": self.ok,
            "observation_digest": self.observation_digest,
            "latency_ms": self.latency_ms,
        }


@dataclass
class Trajectory:
    """一次请求的 Agent 轨迹。"""

    question: str = ""
    steps: List[TrajectoryStep] = field(default_factory=list)
    final_answer: str = ""
    total_latency_ms: int = 0
    token_usage: Dict[str, Any] = field(default_factory=dict)

    def tools(self) -> List[str]:
        """按出现顺序返回被调用的工具名（可重复）。"""
        return [s.tool for s in self.steps if s.tool]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "steps": [s.to_dict() for s in self.steps],
            "final_answer": self.final_answer,
            "total_latency_ms": self.total_latency_ms,
            "token_usage": self.token_usage,
        }

    def render(self) -> str:
        """渲染为可读的紧凑文本（诊断/日志用）。"""
        lines = [f"问题：{self.question}"]
        if not self.steps:
            lines.append("（无工具调用）")
        for s in self.steps:
            flag = "OK" if s.ok else "FAIL"
            lines.append(
                f"[{s.index}] {s.tool} [{flag}] args={s.args_digest} "
                f"obs={s.observation_digest} ({s.latency_ms}ms)"
            )
        lines.append(f"最终答案：{self.final_answer}")
        return "\n".join(lines)


def build_trajectory_from_tool_calls(
    question: str,
    tool_calls: Optional[Sequence[Dict[str, Any]]],
    final_answer: str = "",
    total_latency_ms: int = 0,
    token_usage: Optional[Dict[str, Any]] = None,
) -> Trajectory:
    """从 trace 的 `tool_calls`（`ToolResult.to_dict()` 列表）还原轨迹。"""
    steps: List[TrajectoryStep] = []
    for i, call in enumerate(tool_calls or [], 1):
        if not isinstance(call, dict):
            continue
        steps.append(
            TrajectoryStep(
                index=i,
                tool=str(call.get("tool_name") or call.get("tool") or "").strip(),
                args_digest=_digest(call.get("input_arguments") or call.get("args")),
                ok=bool(call.get("success", True)) and not call.get("error"),
                observation_digest=_digest(call.get("output") or call.get("error")),
                latency_ms=int(call.get("latency_ms") or 0),
            )
        )
    return Trajectory(
        question=question or "",
        steps=steps,
        final_answer=final_answer or "",
        total_latency_ms=int(total_latency_ms or 0),
        token_usage=token_usage or {},
    )


def build_trajectory_from_agent_loop(
    question: str,
    agent_loop: Optional[Sequence[Dict[str, Any]]],
    final_answer: str = "",
    total_latency_ms: int = 0,
    token_usage: Optional[Dict[str, Any]] = None,
) -> Trajectory:
    """从 reasoning 的 `agent_loop` 事件（`metadata.{step_index,tools}`）还原轨迹。

    该来源只有「每步调了哪些工具」的名称，没有 observation 正文——按步数展开为
    占位步骤（observation 留空），用于「工具选择 / 步数效率」类判分。
    """
    steps: List[TrajectoryStep] = []
    idx = 0
    for event in agent_loop or []:
        if not isinstance(event, dict):
            continue
        # 只取 agent_loop 事件（reasoning 流里还有其它 step）
        if event.get("step") not in (None, "agent_loop"):
            continue
        meta = event.get("metadata") or {}
        names = meta.get("tools") or []
        for raw in names if isinstance(names, list) else []:
            idx += 1
            name = raw.get("tool") or raw.get("name") if isinstance(raw, dict) else raw
            steps.append(TrajectoryStep(index=idx, tool=str(name or "").strip()))
    return Trajectory(
        question=question or "",
        steps=steps,
        final_answer=final_answer or "",
        total_latency_ms=int(total_latency_ms or 0),
        token_usage=token_usage or {},
    )


@dataclass
class TrajectoryEvalResult:
    """轨迹判分结果（四维 0-1 + 总分为均值 + pass）。"""

    tool_selection: float = 0.0
    step_efficiency: float = 0.0
    info_coverage: float = 0.0
    compliance: float = 0.0
    reason: str = ""
    passed: bool = False

    @property
    def score(self) -> float:
        dims = [self.tool_selection, self.step_efficiency, self.info_coverage, self.compliance]
        return sum(dims) / len(dims) if dims else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_selection": self.tool_selection,
            "step_efficiency": self.step_efficiency,
            "info_coverage": self.info_coverage,
            "compliance": self.compliance,
            "score": round(self.score, 4),
            "passed": self.passed,
            "reason": self.reason,
        }


class TrajectoryEvaluator:
    """Agent 轨迹评估器（**全维确定性**，不依赖 LLM）。

    四维均为确定性规则；`evaluate` 为 async 仅为保持调用方 `await` 签名一致。
    """

    def __init__(self, pass_threshold: Optional[float] = None):
        self.pass_threshold = (
            pass_threshold
            if pass_threshold is not None
            else getattr(settings.search, "AGENT_TRAJ_EVAL_PASS_THRESHOLD", 0.7)
        )

    @staticmethod
    def _rule_tool_selection(
        expected_tools: Optional[Sequence[str]], actual_tools: Sequence[str]
    ) -> float:
        """工具选择的确定性判定（弱约束：只罚「该调未调」）。

        - 无期望（未标注）→ 1.0（不约束）
        - 否则 = 期望工具中被实际调用的比例

        说明：该维曾交由 LLM 判定，但 3B judge 会误判「明明调了却说没调」
        （2026-10-20 基线实证），故改为规则判定。
        """
        expected = [t for t in (expected_tools or []) if t]
        if not expected:
            return 1.0
        actual = set(actual_tools or [])
        return sum(1 for t in expected if t in actual) / len(expected)

    @staticmethod
    def _rule_step_efficiency(n_steps: int, max_steps: int) -> float:
        """步数效率的确定性判定：≤上界 1.0；0 步 0.5（无轨迹）；超界按超出比例扣分。"""
        if max_steps <= 0:
            return 1.0
        if n_steps == 0:
            return 0.5
        if n_steps <= max_steps:
            return 1.0
        return max(0.0, 1.0 - (n_steps - max_steps) / max_steps)

    @staticmethod
    def _bigrams(text: str) -> set:
        """去空白后的字符二元组集合（无空白时退化为单字集）。"""
        t = re.sub(r"\s+", "", text or "")
        if len(t) < 2:
            return {t} if t else set()
        return {t[i : i + 2] for i in range(len(t) - 1)}

    @classmethod
    def _rule_info_coverage(
        cls, expected_points: Optional[Sequence[str]], final_answer: str
    ) -> float:
        """信息覆盖的确定性判定：期望要点字符 bigram 在答案中的覆盖率 ≥ 0.5 记为覆盖。

        - 未标注要点 → 1.0（不约束）
        - 否则 = 覆盖到的要点比例

        说明：该维曾交由 LLM 判定，但抽检发现 3B judge **在字面已出现时仍判
        「未提及」**（2026-10-20：hybrid_01/02/03、multi_03 均为误判），故改为
        确定性判定。
        """
        points = [str(p) for p in (expected_points or []) if p]
        if not points:
            return 1.0
        haystack = cls._bigrams(final_answer or "")
        covered = 0
        for point in points:
            grams = cls._bigrams(point)
            if not grams:
                continue
            if len(grams & haystack) / len(grams) >= 0.5:
                covered += 1
        return covered / len(points)

    @staticmethod
    def _rule_compliance(final_answer: str) -> float:
        """合规的确定性判定：默认合规，仅惩罚「无实质/无依据作答」。

        - 空答 → 0.0
        - 命中「拒答/无据」模式（无法找到 / 请先选择知识库…）→ 0.5
        - 其余 → 1.0

        说明：该维曾交由 LLM 判定，但 3B judge 会把「未使用 web_search」等
        **工具选择**问题误判为不合规，且常空输出为 0（2026-10-20 三轮实证），
        故改为确定性判定。「无依据作答」的细粒度检测留待更强 judge（受 4B 显存
        红線约束暂不可得）。
        """
        t = (final_answer or "").strip()
        if not t:
            return 0.0
        return 0.5 if _NON_ANSWER_RE.search(t) else 1.0

    @staticmethod
    def _build_reason(
        trajectory: Trajectory, result: "TrajectoryEvalResult", max_steps: int
    ) -> str:
        """由规则结果合成简短、确定的判分理由。"""
        parts: List[str] = []
        if not trajectory.steps:
            parts.append("无工具调用")
        elif result.step_efficiency < 1.0:
            parts.append(f"步数({len(trajectory.steps)})超上界({max_steps})")
        if result.tool_selection < 1.0:
            parts.append("未调用全部期望工具")
        if result.info_coverage < 1.0:
            parts.append("参考要点未完全覆盖")
        if result.compliance < 1.0:
            parts.append("疑似无实质/无依据作答")
        return "；".join(parts) if parts else "各维均达标"

    async def evaluate(
        self,
        trajectory: Trajectory,
        expected_tools: Optional[Sequence[str]] = None,
        expected_answer_points: Optional[Sequence[str]] = None,
        expected_max_steps: Optional[int] = None,
    ) -> TrajectoryEvalResult:
        """对单条轨迹判分（确定性，无外部依赖、不抛异常）。"""
        if trajectory is None:
            return TrajectoryEvalResult(reason="轨迹为空")
        max_steps = expected_max_steps or getattr(settings.search, "SEARCH_REACT_MAX_STEPS", 3)
        result = TrajectoryEvalResult(
            tool_selection=self._rule_tool_selection(expected_tools, trajectory.tools()),
            step_efficiency=self._rule_step_efficiency(len(trajectory.steps), max_steps),
            info_coverage=self._rule_info_coverage(expected_answer_points, trajectory.final_answer),
            compliance=self._rule_compliance(trajectory.final_answer),
        )
        result.passed = result.score >= self.pass_threshold
        result.reason = self._build_reason(trajectory, result, max_steps)
        return result

    async def evaluate_batch(
        self,
        samples: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """批量判分；`samples` 每项含 trajectory 与可选 expected_* 字段。"""
        results: List[Dict[str, Any]] = []
        for sample in samples:
            result = await self.evaluate(
                trajectory=sample.get("trajectory"),
                expected_tools=sample.get("expected_tools"),
                expected_answer_points=sample.get("expected_answer_points"),
                expected_max_steps=sample.get("expected_max_steps"),
            )
            results.append(result.to_dict())

        if not results:
            return {"samples": [], "aggregated": {}}
        aggregated = {
            "traj_score": sum(r["score"] for r in results) / len(results),
            "traj_pass_rate": sum(1 for r in results if r["passed"]) / len(results),
        }
        return {"samples": results, "aggregated": aggregated}
