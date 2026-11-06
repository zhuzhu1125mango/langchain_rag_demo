"""会话导出：把一条会话渲染成 Markdown。

为什么单独放服务层而不是写在路由里：渲染是纯函数，可以逐条覆盖
「没有消息」「来源很长」「标题为空」「正文里带 Markdown 标题」这些分支——
这些恰恰是在路由里最容易漏、又最难在集成测试里发现的（接口返回 200 不代表内容对）。

导出的定位是「给人看、能贴到别处」，所以：
- 用 Markdown 而不是 HTML：贴进文档、issue、邮件都能读；
- **不导出** `reasoning`（思维链）与 `source_metadata`（内部打分）：前者是调试信息，
  后者含分数与路径，给外部看只会误导；
- 来源只保留第一行并截断——`sources` 里存的是整段 chunk 原文，
  一条回答可能有 5 段，直接贴会把文件从几 KB 撑到几百 KB。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

# 来源摘要长度：够认出是哪一段（小标题 + 首句），又不至于把正文淹掉
SOURCE_PREVIEW_CHARS = 80

# 文件名长度上限：多数文件系统的单段上限是 255 字节，中文占 3 字节，
# 留出 `session-xxxxxxxx.md` 的余地
FILENAME_MAX_CHARS = 60

_ROLE_LABELS = {
    "user": "用户",
    "assistant": "助手",
    "system": "系统",
    "tool": "工具",
}


def _format_time(value: Any) -> str:
    """把 ISO 字符串渲染成 `YYYY-MM-DD HH:MM`；解析不了就原样返回。"""
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _preview(text: Any, limit: int = SOURCE_PREVIEW_CHARS) -> str:
    """把来源压成一行并截断。

    整段 chunk 常有换行与重复内容，直接贴会让导出文件难以阅读；但**只取首行**也不行——
    很多 chunk 的第一行是小标题（「办公时间安排」），信息量不足，看不出来源讲什么。
    所以是「折叠所有空白 + 截断」，既保留开头两句的语义，又只占一行。
    """
    if not isinstance(text, str):
        text = str(text)
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _source_lines(sources: Any) -> List[str]:
    """把来源列表渲染成有序列表；空条目跳过（历史数据里存在空字符串来源）。"""
    if not isinstance(sources, list):
        return []
    lines: List[str] = []
    for source in sources:
        preview = _preview(source)
        if preview:
            lines.append(f"{len(lines) + 1}. {preview}")
    return lines


def render_session_markdown(meta: Dict[str, Any], messages: List[Dict[str, Any]]) -> str:
    """渲染会话为 Markdown 文本。

    Args:
        meta: 会话元信息，需含 title / id，可选 created_at / updated_at
        messages: 消息列表，元素形如 {"role": "user", "content": "...", "sources": [...]}

    Returns:
        Markdown 文本（以换行结尾）
    """
    title = (meta.get("title") or "").strip() or "未命名对话"
    lines: List[str] = [
        f"# {title}",
        "",
        f"- 会话 ID：{meta.get('id') or '—'}",
        f"- 创建时间：{_format_time(meta.get('created_at'))}",
        f"- 最近活动：{_format_time(meta.get('updated_at'))}",
        f"- 消息数：{len(messages)}",
        "",
        "---",
        "",
    ]

    if not messages:
        lines += ["_该会话还没有消息。_", ""]
        return "\n".join(lines)

    for message in messages:
        role = str(message.get("role") or "unknown")
        label = _ROLE_LABELS.get(role, role)
        stamp = _format_time(message.get("timestamp"))
        lines.append(f"**{label}**（{stamp}）")
        lines.append("")
        content = message.get("content")
        text = content.strip() if isinstance(content, str) else ""
        lines.append(text or "_（空消息）_")
        lines.append("")

        sources = _source_lines(message.get("sources"))
        if sources:
            lines.append("> 参考来源")
            for source in sources:
                lines.append(f"> {source}")
            lines.append("")

        lines += ["---", ""]

    return "\n".join(lines)


def build_export_filename(title: str, session_id: str, suffix: str = ".md") -> str:
    """生成导出文件名。

    去掉路径分隔符与控制字符：这些字符会让浏览器下载变成「另存为」甚至失败；
    标题为空时回退到会话 ID 前 8 位，保证文件名一定有意义。
    """
    safe = "".join(ch for ch in (title or "") if ch not in '\\/:*?"<>|' and ord(ch) >= 32)
    safe = " ".join(safe.split())[:FILENAME_MAX_CHARS].strip(" .")
    if not safe:
        safe = f"session-{str(session_id)[:8]}"
    return f"{safe}{suffix}"
