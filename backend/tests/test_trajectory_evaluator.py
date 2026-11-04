"""Agent 轨迹级评测（AM-1）单测：轨迹还原 + 确定性规则判分。

判分器已全维确定性（不依赖 LLM），故不再有 LLM 替身 / 解析相关用例。
"""

import pytest

from src.services.evaluation.trajectory_evaluator import (
    DIGEST_CHARS,
    Trajectory,
    TrajectoryEvalResult,
    TrajectoryEvaluator,
    TrajectoryStep,
    build_trajectory_from_agent_loop,
    build_trajectory_from_tool_calls,
)


# ----------------------------------------------------------------------
# 轨迹还原
# ----------------------------------------------------------------------
class TestBuildTrajectory:
    def test_from_tool_calls_maps_fields(self):
        calls = [
            {
                "tool_name": "kb_search",
                "input_arguments": {"query": "年终奖 计算"},
                "output": "命中 3 篇",
                "success": True,
                "error": None,
                "latency_ms": 120,
            },
            {
                "tool_name": "wiki_lookup",
                "input_arguments": {"title": "绩效等级"},
                "output": "",
                "success": False,
                "error": "not found",
                "latency_ms": 30,
            },
        ]
        traj = build_trajectory_from_tool_calls("年终奖怎么算", calls, final_answer="按绩效等级")
        assert traj.question == "年终奖怎么算"
        assert traj.tools() == ["kb_search", "wiki_lookup"]
        assert traj.steps[0].ok is True
        assert traj.steps[0].latency_ms == 120
        assert traj.steps[1].ok is False  # success=False
        assert "not found" in traj.steps[1].observation_digest
        assert traj.final_answer == "按绩效等级"

    def test_from_tool_calls_truncates_digest(self):
        traj = build_trajectory_from_tool_calls("q", [{"tool_name": "t", "output": "x" * 999}])
        assert len(traj.steps[0].observation_digest) == DIGEST_CHARS

    def test_from_agent_loop_expands_tools_per_step(self):
        loop = [
            {"step": "agent_loop", "metadata": {"step_index": 1, "tools": ["kb_search", "wiki_lookup"]}},
            {"step": "agent_loop", "metadata": {"step_index": 2, "tools": ["wiki_lookup"]}},
            {"step": "other", "metadata": {"tools": ["ignored"]}},
        ]
        traj = build_trajectory_from_agent_loop("跨文档问题", loop)
        assert traj.tools() == ["kb_search", "wiki_lookup", "wiki_lookup"]
        assert [s.index for s in traj.steps] == [1, 2, 3]

    def test_empty_inputs(self):
        assert build_trajectory_from_tool_calls("q", None).steps == []
        assert build_trajectory_from_agent_loop("q", []).steps == []


class TestRender:
    def test_render_contains_steps_and_answer(self):
        traj = Trajectory(
            question="Q",
            steps=[TrajectoryStep(index=1, tool="kb_search", ok=False, latency_ms=5)],
            final_answer="A",
        )
        text = traj.render()
        assert "问题：Q" in text
        assert "kb_search" in text and "FAIL" in text
        assert "最终答案：A" in text

    def test_render_no_steps(self):
        assert "（无工具调用）" in Trajectory(question="Q").render()


# ----------------------------------------------------------------------
# 判分执行（确定性）
# ----------------------------------------------------------------------
class TestEvaluate:
    async def test_evaluate_all_rules_pass(self):
        ev = TrajectoryEvaluator(pass_threshold=0.7)
        traj = build_trajectory_from_tool_calls(
            "Q", [{"tool_name": "kb_search", "success": True}], final_answer="要点已覆盖"
        )
        r = await ev.evaluate(
            traj, expected_tools=["kb_search"], expected_answer_points=["要点已覆盖"]
        )
        assert r.tool_selection == 1.0
        assert r.step_efficiency == 1.0
        assert r.info_coverage == 1.0
        assert r.compliance == 1.0
        assert r.passed is True
        assert r.reason == "各维均达标"

    async def test_evaluate_null_trajectory(self):
        ev = TrajectoryEvaluator()
        assert (await ev.evaluate(None)).reason == "轨迹为空"

    async def test_evaluate_tool_misuse_and_refusal_fails(self):
        # 工具选错 + 拒答式回答（近似 web_04）→ 综合不通过
        ev = TrajectoryEvaluator(pass_threshold=0.7)
        traj = build_trajectory_from_tool_calls(
            "Q", [{"tool_name": "kb_search", "success": True}],
            final_answer="请先选择知识库后再提问。",
        )
        r = await ev.evaluate(traj, expected_tools=["web_search"])
        assert r.tool_selection == 0.0  # 该调 web_search 未调
        assert r.compliance == 0.5  # 命中拒答模式
        assert r.passed is False
        assert "未调用全部期望工具" in r.reason

    async def test_evaluate_no_tools_half_step_efficiency(self):
        ev = TrajectoryEvaluator(pass_threshold=0.7)
        r = await ev.evaluate(Trajectory(question="Q", final_answer="答案"))
        assert r.step_efficiency == 0.5
        assert "无工具调用" in r.reason

    async def test_evaluate_batch_aggregates(self):
        ev = TrajectoryEvaluator(pass_threshold=0.7)
        good_samples = [
            {
                "trajectory": build_trajectory_from_tool_calls(
                    "Q1", [{"tool_name": "kb_search", "success": True}], final_answer="要点已覆盖"
                ),
                "expected_tools": ["kb_search"],
                "expected_answer_points": ["要点已覆盖"],
            }
        ]
        out = await ev.evaluate_batch(good_samples)
        assert out["aggregated"]["traj_score"] == pytest.approx(1.0)
        assert out["aggregated"]["traj_pass_rate"] == pytest.approx(1.0)

        bad_samples = [
            {
                "trajectory": Trajectory(question="Q2", final_answer="无关内容"),
                "expected_tools": ["kb_search"],
                "expected_answer_points": ["要点已覆盖"],
            }
        ]
        out2 = await ev.evaluate_batch(bad_samples)
        assert out2["aggregated"]["traj_pass_rate"] == 0.0

    async def test_evaluate_batch_empty(self):
        ev = TrajectoryEvaluator()
        assert await ev.evaluate_batch([]) == {"samples": [], "aggregated": {}}


# ----------------------------------------------------------------------
# 规则维度：确定性判定（不依赖 LLM）
# ----------------------------------------------------------------------
class TestRuleDimensions:
    def test_tool_selection_weak_constraint(self):
        assert TrajectoryEvaluator._rule_tool_selection(None, ["kb_search"]) == 1.0
        assert TrajectoryEvaluator._rule_tool_selection([], []) == 1.0
        assert TrajectoryEvaluator._rule_tool_selection(["kb_search"], ["kb_search"]) == 1.0
        assert TrajectoryEvaluator._rule_tool_selection(["kb_search"], ["web_search"]) == 0.0
        assert TrajectoryEvaluator._rule_tool_selection(
            ["kb_search", "web_search"], ["kb_search"]
        ) == 0.5
        # 弱约束：多调不罚
        assert TrajectoryEvaluator._rule_tool_selection(
            ["kb_search"], ["kb_search", "web_search"]
        ) == 1.0

    def test_step_efficiency(self):
        assert TrajectoryEvaluator._rule_step_efficiency(2, 3) == 1.0
        assert TrajectoryEvaluator._rule_step_efficiency(0, 3) == 0.5
        assert TrajectoryEvaluator._rule_step_efficiency(6, 3) == pytest.approx(0.0)
        assert TrajectoryEvaluator._rule_step_efficiency(4, 3) == pytest.approx(1 - 1 / 3)
        assert TrajectoryEvaluator._rule_step_efficiency(1, 0) == 1.0

    def test_info_coverage_rule(self):
        f = TrajectoryEvaluator._rule_info_coverage
        assert f(None, "任意") == 1.0  # 未标注 → 不约束
        assert f([], "任意") == 1.0
        assert f(["春节前发放"], "公司年终奖于春节前发放。") == 1.0
        assert f(["春节前发放", "两级审批"], "公司年终奖于春节前发放。") == pytest.approx(0.5)
        assert f(["两级审批"], "完全无关的答案") == 0.0

    def test_compliance_rule(self):
        f = TrajectoryEvaluator._rule_compliance
        assert f("公司办公时间为9:00至18:00。") == 1.0  # 正常作答 → 合规
        assert f("") == 0.0  # 空答
        assert f("   ") == 0.0
        assert f("无法找到相关信息。") == 0.5  # 拒答/无据
        assert f("请先选择知识库后再提问。") == 0.5


# ----------------------------------------------------------------------
# AGT-3：双源题的工具选择改为硬约束
# ----------------------------------------------------------------------
class TestHardToolConstraint:
    """`hybrid` 类题考察的是「KB 与 Web 两个来源都用到」。

    弱约束下只调一个得 0.5，四维均值里与「调对一个」几乎分不开，等于退化成任一源。
    这里钉住「硬约束 = 缺一即 0」，并保留弱约束行为不变（既有基线不受影响）。
    """

    def test_hard_requires_all(self):
        f = TrajectoryEvaluator._rule_tool_selection
        assert f(["kb_search", "web_search"], ["kb_search", "web_search"], require_all=True) == 1.0
        assert f(["kb_search", "web_search"], ["kb_search"], require_all=True) == 0.0
        assert f(["kb_search", "web_search"], ["web_search"], require_all=True) == 0.0
        assert f(["kb_search", "web_search"], [], require_all=True) == 0.0

    def test_hard_still_allows_extra_tools(self):
        """多调不罚这条对硬约束同样成立：只要期望的全在，额外工具不算错。"""
        assert TrajectoryEvaluator._rule_tool_selection(
            ["kb_search", "web_search"], ["kb_search", "weather", "web_search"], require_all=True
        ) == 1.0

    def test_hard_without_expectation_is_unconstrained(self):
        assert TrajectoryEvaluator._rule_tool_selection(None, ["kb_search"], require_all=True) == 1.0
        assert TrajectoryEvaluator._rule_tool_selection([], [], require_all=True) == 1.0

    def test_weak_constraint_unchanged_by_default(self):
        """默认仍走弱约束——这次改动不能悄悄改掉其它题的分。"""
        assert TrajectoryEvaluator._rule_tool_selection(
            ["kb_search", "web_search"], ["kb_search"]
        ) == 0.5

    def test_missing_tools_lists_what_is_absent(self):
        f = TrajectoryEvaluator._missing_tools
        assert f(["kb_search", "web_search"], ["kb_search"]) == ["web_search"]
        assert f(["kb_search", "web_search"], ["kb_search", "web_search"]) == []
        assert f(None, ["kb_search"]) == []

    @pytest.mark.asyncio
    async def test_evaluate_passes_require_all_through(self):
        evaluator = TrajectoryEvaluator()
        traj = Trajectory(
            question="公司弹性打卡制度与法定工作时间制度是否冲突？",
            steps=[TrajectoryStep(index=0, tool="kb_search", ok=True)],
            final_answer="公司实行弹性打卡，具体见制度。",
        )

        weak = await evaluator.evaluate(traj, expected_tools=["kb_search", "web_search"])
        hard = await evaluator.evaluate(
            traj, expected_tools=["kb_search", "web_search"], require_all_tools=True
        )

        assert weak.tool_selection == pytest.approx(0.5)
        assert hard.tool_selection == 0.0
        assert hard.missing_tools == ["web_search"]
        assert hard.score < weak.score  # 硬约束只可能更严

    @pytest.mark.asyncio
    async def test_evaluate_batch_honours_require_all_flag(self):
        evaluator = TrajectoryEvaluator()
        traj = Trajectory(
            steps=[TrajectoryStep(index=0, tool="kb_search", ok=True)],
            final_answer="答案。",
        )
        out = await evaluator.evaluate_batch([
            {"trajectory": traj, "expected_tools": ["kb_search", "web_search"],
             "require_all_tools": True},
            {"trajectory": traj, "expected_tools": ["kb_search", "web_search"]},
        ])
        assert out["samples"][0]["tool_selection"] == 0.0
        assert out["samples"][1]["tool_selection"] == pytest.approx(0.5)

    def test_to_dict_exposes_missing_tools(self):
        result = TrajectoryEvalResult(tool_selection=0.0, missing_tools=["web_search"])
        assert result.to_dict()["missing_tools"] == ["web_search"]
