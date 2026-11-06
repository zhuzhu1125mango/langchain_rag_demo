"""会话导出渲染的纯函数单测（不连库、不起服务）。

这些分支在集成测试里很难覆盖（要造特定的历史数据），但恰恰是最容易出错的：
标题为空、消息为空、来源是整段 chunk 原文、时间戳缺失或格式异常。
导出是「给人看」的功能，内容错了不会报错——只会安静地导出一份没法用的文件。
"""

from src.services.session_export import (
    FILENAME_MAX_CHARS,
    SOURCE_PREVIEW_CHARS,
    build_export_filename,
    render_session_markdown,
)


def _meta(**overrides):
    meta = {
        "id": "0b7f8c1e-3a4d-4c5e-9f00-112233445566",
        "title": "弹性打卡与法定工时",
        "created_at": "2026-11-04T09:12:29",
        "updated_at": "2026-11-04T09:13:00",
    }
    meta.update(overrides)
    return meta


def test_renders_header_metadata_and_messages():
    md = render_session_markdown(
        _meta(),
        [
            {"role": "user", "content": "两个制度冲突吗？", "timestamp": "2026-11-04T09:12:29"},
            {"role": "assistant", "content": "不冲突。", "timestamp": "2026-11-04T09:12:35"},
        ],
    )
    assert md.startswith("# 弹性打卡与法定工时")
    assert "- 会话 ID：0b7f8c1e-3a4d-4c5e-9f00-112233445566" in md
    assert "- 创建时间：2026-11-04 09:12" in md
    assert "- 消息数：2" in md
    assert "**用户**（2026-11-04 09:12）" in md
    assert "**助手**（2026-11-04 09:12）" in md
    assert "不冲突。" in md
    assert md.endswith("\n")


def test_empty_session_says_so_instead_of_rendering_nothing():
    md = render_session_markdown(_meta(), [])
    assert "_该会话还没有消息。_" in md
    assert "- 消息数：0" in md


def test_blank_title_falls_back():
    md = render_session_markdown(_meta(title="   "), [])
    assert md.startswith("# 未命名对话")


def test_missing_timestamp_renders_dash():
    md = render_session_markdown(_meta(created_at=""), [{"role": "user", "content": "hi"}])
    assert "- 创建时间：—" in md
    assert "**用户**（—）" in md


def test_unparsable_timestamp_is_kept_verbatim():
    """时间戳格式异常时不要把整条消息吞掉，原样展示比显示「—」更有用。"""
    md = render_session_markdown(_meta(), [{"role": "user", "content": "hi", "timestamp": "昨天下午"}])
    assert "**用户**（昨天下午）" in md


def test_empty_content_renders_placeholder():
    md = render_session_markdown(_meta(), [{"role": "assistant", "content": "   "}])
    assert "_（空消息）_" in md


def test_unknown_role_is_rendered_as_is():
    md = render_session_markdown(_meta(), [{"role": "critic", "content": "x"}])
    assert "**critic**" in md


def test_long_source_is_collapsed_and_truncated():
    """来源是整段 chunk 原文，直接贴会把几 KB 的导出撑到几百 KB。"""
    source = "办公时间安排\n\n办公时间：每周五天工作制。" * 50
    md = render_session_markdown(_meta(), [{"role": "assistant", "content": "a", "sources": [source]}])
    assert "> 1. 办公时间安排" in md
    assert "\n\n办公时间" not in md  # 换行被折叠
    preview_line = [line for line in md.splitlines() if line.startswith("> 1. ")][0]
    assert len(preview_line) <= SOURCE_PREVIEW_CHARS + len("> 1. ") + 1
    assert preview_line.endswith("…")


def test_blank_sources_are_skipped_and_numbering_stays_contiguous():
    md = render_session_markdown(
        _meta(),
        [{"role": "assistant", "content": "a", "sources": ["", "   ", "第二段有内容"]}],
    )
    assert "> 1. 第二段有内容" in md
    assert "> 2." not in md


def test_sources_that_are_not_a_list_are_ignored():
    md = render_session_markdown(_meta(), [{"role": "assistant", "content": "a", "sources": "oops"}])
    assert "参考来源" not in md


def test_reasoning_and_internal_metadata_are_not_exported():
    """思维链与打分元数据是调试信息，导出给外部看只会误导。"""
    md = render_session_markdown(
        _meta(),
        [
            {
                "role": "assistant",
                "content": "答案",
                "reasoning": [{"step": "intent_routing", "content": "内部推理"}],
                "source_metadata": [{"score": 0.87, "path": "dense"}],
            }
        ],
    )
    assert "内部推理" not in md
    assert "intent_routing" not in md
    assert "0.87" not in md


def test_filename_strips_path_separators_and_reserved_chars():
    name = build_export_filename('a/b\\c:d*e?f"g<h>i|j', "abcdef123456")
    assert name == "abcdefghij.md"


def test_filename_falls_back_to_session_id_when_title_is_empty():
    assert build_export_filename("   ", "abcdef123456") == "session-abcdef12.md"


def test_filename_is_length_capped():
    name = build_export_filename("很长的标题" * 50, "abcdef123456")
    assert name.endswith(".md")
    assert len(name) <= FILENAME_MAX_CHARS + len(".md")


def test_filename_keeps_chinese():
    assert build_export_filename("弹性打卡 制度", "x") == "弹性打卡 制度.md"
