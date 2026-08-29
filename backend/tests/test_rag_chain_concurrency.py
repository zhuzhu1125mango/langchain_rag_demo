"""
RAGChain 请求级状态隔离测试（修复7）。

回归：RAGChain 为进程级单例，决策结果此前挂在 self.last_decision 上，
并发请求互相覆盖（A 的回答引用 B 的决策）。现改为 ContextVar 请求级隔离。
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.services.rag_chain import RAGChain, _request_decision, _request_retrieval_score


def _make_chain() -> RAGChain:
    """构造不触发外部依赖的 RAGChain，注入可区分结果的决策管道。"""
    chain = RAGChain(vector_store=None, strategy_manager=None)

    async def _decide(question, kb_ids=None, history=None, force_mode=None,
                      use_web_search=False, search_mode="simple"):
        # 让出事件循环，放大并发交错窗口
        await asyncio.sleep(0.01)
        return SimpleNamespace(
            should_use_kb=True,
            strategy_results={"marker": question},
            mode="kb",
        )

    chain.decision_pipeline = SimpleNamespace(decide=_decide)
    return chain


class TestRequestScopedDecision:
    """决策结果按请求（Task）隔离。"""

    async def test_concurrent_decisions_isolated(self):
        """并发 10 个请求：各自 get_last_decision 只能看到自己的结果。"""
        chain = _make_chain()
        N = 10

        async def one_request(i: int):
            await chain._make_decision(f"question-{i}")
            # 模拟 chat.py：决策之后、同一 Task 内读取
            await asyncio.sleep(0.02)
            decision = chain.get_last_decision()
            assert decision.strategy_results["marker"] == f"question-{i}"
            return decision

        results = await asyncio.gather(*(one_request(i) for i in range(N)))
        assert len(results) == N

    async def test_same_task_set_then_get(self):
        """同一 Task 内 set 后可读（非流式/流式路径的共同前提）。"""
        chain = _make_chain()
        assert chain.get_last_decision() is None
        await chain._make_decision("q1")
        assert chain.get_last_decision().strategy_results == {"marker": "q1"}

    async def test_set_after_task_creation_not_visible(self):
        """Task 创建后才 set 的值对子任务不可见（ContextVar 快照语义）。

        注：create_task 会复制创建时刻的上下文，因此创建前已 set 的值
        会被子任务继承——隔离边界以"创建时刻"为准。
        """
        chain = _make_chain()

        async def read_in_other_task():
            # 等待父 Task 完成 set，再读取
            await asyncio.sleep(0.03)
            return chain.get_last_decision()

        read_task = asyncio.create_task(read_in_other_task())
        await chain._make_decision("q1")  # set 发生在任务创建之后

        assert await read_task is None


class TestRequestScopedSideChannels:
    """置信度与检索分数同样请求级隔离。"""

    async def test_strategy_confidences_from_decision(self):
        chain = _make_chain()
        assert chain.get_last_strategy_confidences() == {}
        await chain._make_decision("q1")
        assert chain.get_last_strategy_confidences() == {"marker": "q1"}

    async def test_retrieval_score_isolated(self):
        chain = RAGChain(vector_store=None, strategy_manager=None)
        assert chain.get_last_retrieval_score() == 0.0

        async def one_request(score: float):
            _request_retrieval_score.set(score)
            await asyncio.sleep(0.01)
            return chain.get_last_retrieval_score()

        scores = await asyncio.gather(
            one_request(0.42), one_request(0.99), one_request(0.13)
        )
        assert sorted(scores) == [0.13, 0.42, 0.99]
