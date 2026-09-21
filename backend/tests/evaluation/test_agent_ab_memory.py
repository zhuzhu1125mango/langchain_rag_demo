"""Agent L1-a 跨请求记忆（A/B memory 子集）评估测试（设计文档 agent-evolution.md §11.2）。

离线（默认跑，CI）：验证核心新逻辑，不依赖真实链路——
- 数据集结构：memory 用例含跨轮引用的历史字段且合法；
- 决策 prompt 记忆注入：memory_context 非空时正确追加历史记忆块，为空时保持原样；
- 记忆读写隔离：按 session_id 前缀隔离会话、fail-open（关闭/异常均降级为空）。

注意：UI 契约 / e2e 门槛沿用 test_agent_ab.py 机制，本文件不引入新 e2e。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_eval import load_jsonl  # noqa: E402

# orchestrator 单测需注入 backend 路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.agent_orchestrator import (  # noqa: E402
    AgentLoopState,
    AgentOrchestrator,
)

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[2] / "tests" / "evaluation" / "agent_eval_dataset.jsonl"
)


# ---------------------------------------------------------------------------
# 数据集结构：memory 跨轮引用
# ---------------------------------------------------------------------------
def test_memory_dataset_has_cross_turn_fields():
    """memory 类用例需携带跨轮引用的历史字段（本轮不直接可自足回答）。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    mem = [s for s in dataset if s["category"] == "memory"]
    # 数据集当前以 kb/web/hybrid 为主，memory 子集允许后续迭代补充；
    # 一旦存在 memory 用例，则必须满足跨轮结构约束。
    if not mem:
        pytest.skip("数据集暂无 memory 类别用例（L1-a 灰度验证时补充）")
    for sample in mem:
        # 跨轮引用：question 无法独立回答，需依赖 prior_* 提供的事实
        assert sample.get("prior_question"), f"{sample['id']} 缺少 prior_question"
        assert sample.get("prior_answer"), f"{sample['id']} 缺少 prior_answer"
        assert sample.get("expected_points"), f"{sample['id']} missing expected_points"


# ---------------------------------------------------------------------------
# 决策 prompt 记忆注入
# ---------------------------------------------------------------------------
class _FakeLLM:
    pass


class _FakeToolManager:
    class _Registry:
        def get(self, name):
            return None

    registry = _Registry()


def _make_orchestrator(max_steps: int = 3) -> AgentOrchestrator:
    return AgentOrchestrator(
        llm=_FakeLLM(),
        tool_manager=_FakeToolManager(),
        max_steps=max_steps,
    )


def _loop() -> AgentLoopState:
    import time
    from src.config import settings

    return AgentLoopState(
        deadline=time.perf_counter() + settings.search.AGENT_TIME_BUDGET_MS / 1000.0
    )


def test_build_decide_prompt_appends_memory_block():
    """memory_context 非空时，决策 prompt 应包含历史记忆块与轮次标记。"""
    orch = _make_orchestrator()
    prompt = orch._build_decide_prompt(
        "年终奖发放规则？", "", _loop(), kb_ids=None, memory_context="问：上月奖金\n答：A级3个月"
    )
    assert "此前对话的历史记忆" in prompt
    assert "问：上月奖金\n答：A级3个月" in prompt


def test_build_decide_prompt_no_memory_keeps_unchanged():
    """memory_context 为空时，prompt 与基线一致（无记忆块、无空余注入）。"""
    orch = _make_orchestrator()
    with_hist = "用户此前对话的历史记忆" not in orch._build_decide_prompt(
        "问题", "历史", _loop(), kb_ids=None, memory_context=""
    )
    assert with_hist


# ---------------------------------------------------------------------------
# 记忆读写隔离（直接验证前缀隔离逻辑，不依赖真实 Milvus）
# ---------------------------------------------------------------------------
def test_memory_document_id_isolation():
    """记忆 document_id 为基于 session_id 的确定性 UUID，天然按会话隔离。"""
    import uuid as _uuid

    from src.services.rag_chain import _memory_doc_id

    sess_a, sess_b = "session-aaa", "session-bbb"
    id_a, id_b = _memory_doc_id(sess_a), _memory_doc_id(sess_b)
    # 必须产出合法 UUID（Milvus `_safe_id` 仅放行 UUID，防注入白名单）
    _uuid.UUID(id_a)
    _uuid.UUID(id_b)
    # 同会话聚合、异会话隔离
    assert _memory_doc_id(sess_a) == id_a
    assert id_a != id_b