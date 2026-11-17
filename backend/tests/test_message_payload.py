"""消息落库契约的测试。

这些用例存在的理由：`sessions.messages` 是 JSONB，写错一个键名、少写一个语义，
数据库不会报错，只有人回看历史时才发现。上一版把 payload 拼在闭包里，
出了 NameError 而 2047 例单测全绿——所以契约必须拨到可测的边界上。
"""

import pytest
from pydantic import ValidationError

from src.services.message_payload import (
    GENERATION_SCHEMA_VERSION,
    GenerationInfo,
    build_assistant_message_payload,
    build_generation_info,
)


class TestGenerationInfo:
    def test_rejects_unknown_key(self):
        """JSONB 没有列校验，拼错键名必须当场炸，而不是悄悄写进库。"""
        with pytest.raises(ValidationError):
            GenerationInfo(model_id="m1", modle_name="typo")

    def test_rejects_unknown_selected_by(self):
        with pytest.raises(ValidationError):
            GenerationInfo(model_id="m1", selected_by="admin")

    def test_drops_empty_strings_but_keeps_fallback(self):
        info = build_generation_info(model_id="m1", model_name="", display_name="")

        assert "model_name" not in info
        assert "display_name" not in info
        # fallback 是显式布尔：块内出现 false 表示「确定没回落」，
        # 与「整块不存在 = 没记录」区分开
        assert info["fallback"] is False
        assert info["schema_version"] == GENERATION_SCHEMA_VERSION

    def test_fallback_from_only_when_fallback(self):
        assert "fallback_from" not in build_generation_info(model_id="a", fallback=False)
        assert build_generation_info(model_id="b", fallback=True, fallback_from="a")[
            "fallback_from"
        ] == "a"


class TestAssistantPayload:
    def _kwargs(self, **overrides):
        base = dict(
            message_id="msg-1",
            content="答案",
            sources=[],
            source_metadata=[],
            timestamp="2026-11-17T00:00:00",
        )
        base.update(overrides)
        return base

    def test_minimal_payload_has_no_state_keys(self):
        payload = build_assistant_message_payload(**self._kwargs())

        # 状态字段只在真发生时出现：JSONB 里没法靠列默认值表达「没发生」
        for absent in ("stopped", "error", "generation", "reasoning", "thinking"):
            assert absent not in payload
        assert payload["role"] == "assistant"

    def test_generation_block_attached_as_is(self):
        gen = build_generation_info(model_id="m1", model_name="qwen3:8b")
        payload = build_assistant_message_payload(**self._kwargs(generation=gen))

        assert payload["generation"] == gen

    def test_stopped_and_generation_coexist(self):
        """用户中途停止时，前面已经跑过的模型归因不能丢。"""
        gen = build_generation_info(model_id="m1", selected_by="user")
        payload = build_assistant_message_payload(
            **self._kwargs(content="半截", stopped=True, generation=gen)
        )

        assert payload["stopped"] is True
        assert payload["generation"]["model_id"] == "m1"

    def test_error_and_generation_coexist(self):
        gen = build_generation_info(model_id="m1")
        payload = build_assistant_message_payload(
            **self._kwargs(content="", error="生成失败", generation=gen)
        )

        assert payload["error"] == "生成失败"
        assert payload["generation"]["model_id"] == "m1"

    def test_execution_id_key_is_preserved_even_when_none(self):
        """刻意保留旧行为：execution_id 恒出现（历史数据里可能是 null）。

        这不是「好看的写法」，而是避免借一次重构改变已落库数据的语义——
        读的一方可能靠「键是否存在」判断是否走过学习引擎。
        """
        payload = build_assistant_message_payload(**self._kwargs())

        assert "execution_id" in payload
        assert payload["execution_id"] is None

    def test_key_set_is_stable(self):
        """键集合固定，防止「顺手多塞一个字段」让契约悄悄漂移。"""
        payload = build_assistant_message_payload(**self._kwargs())

        assert set(payload) == {
            "id",
            "role",
            "content",
            "sources",
            "source_metadata",
            "timestamp",
            "execution_id",
        }
