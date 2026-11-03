"""无检索上下文时的提示词分流。

背景（端到端验证发现的真实问题）：用真实项目文档测 10 个问题，问「系统架构」
和「优雅停机」两题返回的是模型自己编的通顺答案，末尾还写着「以上信息基于训练数据」。

根因不在检索——trace 显示两题都检索到了 3 条候选，rerank_score 分别是
0.077/0.011/0.006 与 0.009/0.000/0.000，全部远低于阈值 0.3，**相关性判定是对的**，
docs 也确实被清空了。

问题出在清空之后：生成阶段走 `LLM_DIRECT_TEMPLATE`，而那句模板开头写的是
「本次联网搜索未能返回有效结果，**请基于你已有的知识回答**」。等于系统已经
算出「知识库里没有」，却把这个结论翻译成「请你编一个」。

这组测试守住两条分支不再混用。
"""

from types import SimpleNamespace

from src.services.pipeline.generation_stages import _build_no_context_prompt
from src.services.pipeline.state import KB_MISS_TEMPLATE, LLM_DIRECT_TEMPLATE


def _state(attempted: bool) -> SimpleNamespace:
    return SimpleNamespace(
        kb_retrieval_attempted=attempted,
        history_context="",
        resolved_question="整个系统由哪些部分组成？",
    )


class TestPromptSelection:
    def test_kb_searched_but_empty_uses_miss_template(self):
        answer_type, prompt = _build_no_context_prompt(_state(attempted=True))

        assert answer_type == "kb_miss"
        # 必须要求「明说没找到」，而不是「用你自己的知识答」
        assert "没有找到" in prompt
        assert "请基于已有知识回答" not in prompt

    def test_pure_chat_uses_direct_template(self):
        answer_type, prompt = _build_no_context_prompt(_state(attempted=False))

        assert answer_type == "llm_direct"
        assert "基于你已有的知识" in prompt
        assert "没有找到" not in prompt

    def test_miss_template_carries_no_citation_slots(self):
        """没有可信来源时不能诱导模型标 [1][2]——那会把编造内容伪装成有出处的。"""
        _, prompt = _build_no_context_prompt(_state(attempted=True))

        assert "不要标注 [1][2]" in prompt
        assert "未经知识库验证" in prompt

    def test_question_and_history_are_interpolated(self):
        state = SimpleNamespace(
            kb_retrieval_attempted=True,
            history_context="上一轮问了部署",
            resolved_question="那监控呢？",
        )
        _, prompt = _build_no_context_prompt(state)

        assert "那监控呢？" in prompt
        assert "上一轮问了部署" in prompt


class TestTemplatesStayDistinct:
    def test_miss_template_never_asks_model_to_answer_from_memory(self):
        """反向守卫：任何人把「请基于已有知识回答」加回 KB_MISS_TEMPLATE 都要红。"""
        assert "请基于已有知识回答" not in KB_MISS_TEMPLATE
        assert "请基于你已有的知识回答" not in KB_MISS_TEMPLATE

    def test_direct_template_is_unchanged_for_chat(self):
        """纯聊天模板不该被这次改动波及。"""
        assert "本次联网搜索未能返回有效结果" in LLM_DIRECT_TEMPLATE
