"""LLM-Wiki 文本工具：材料拼接、链接提取/推导、标题归一与 JSON 解析（CODE-1 拆分）。"""

import json
import re
from typing import List, Sequence, Union

from src.services.wiki_types import _THINK_RE
from src.services.wiki_types import _WIKI_LINK_RE

# ----------------------------------------------------------------------
# 模块级工具函数
# ----------------------------------------------------------------------

def normalize_doc_ids(doc_ids: Union[str, Sequence[str]]) -> List[str]:
    """doc_ids 归一化：单值 str / 单元素集合统一为去重后的字符串列表（保持顺序）。"""
    if isinstance(doc_ids, str):
        return [doc_ids]
    seen: List[str] = []
    for d in doc_ids:
        s = str(d)
        if s not in seen:
            seen.append(s)
    return seen

def build_material(chunks, max_chars: int) -> str:
    """拼接 chunks 正文为编译材料并按 max_chars 截断（compile_pages 与矛盾抽查共用）。"""
    return "\n\n".join(
        chunk.page_content for chunk in chunks if getattr(chunk, "page_content", "")
    )[:max_chars]

# Wiki 正文交叉链接语法：[[Title]]

def extract_links(content: str, self_title: str, valid_titles) -> List[str]:
    """提取正文 [[Title]] 链接：去重、排除自引用与不存在页。

    Args:
        content: 页面正文
        self_title: 本页标题（自引用排除，归一化比较）
        valid_titles: 有效目标标题集合/映射（归一化 key → 原标题）；
                      存在性校验后的链接写回原标题

    Returns:
        去重后的目标标题列表（保留首次出现顺序）
    """
    valid = (
        valid_titles
        if isinstance(valid_titles, dict)
        else {normalize_title(t): t for t in (valid_titles or [])}
    )
    self_norm = normalize_title(self_title)
    seen = set()
    links = []
    for raw in _WIKI_LINK_RE.findall(content or ""):
        title = raw.strip()[:256]
        norm = normalize_title(title)
        if not norm or norm == self_norm or norm in seen:
            continue
        target = valid.get(norm)
        if target is None:
            continue
        seen.add(norm)
        links.append(target)
    return links

def normalize_title(title: str) -> str:
    """标题归一化：去空白转小写（精确匹配快路径）。"""
    return re.sub(r"\s+", "", (title or "")).lower()

def derive_links_from_titles(
    content: str, self_title: str, valid_titles, max_links: int = 8
) -> List[str]:
    """E-1 图边元数据化（L1）：按「其他页标题出现在本页正文」建边。

    与 `extract_links` 的「正文 `[[Title]]` 语法」不同，本函数**不改正文**——
    直接把正文里出现的既有页标题识别为图边（红线：互链严禁内联正文）。生成
    的标题列表写入 `wiki_pages.links` JSONB，供 navigator 的 Related 导航。

    Args:
        content: 本页正文
        self_title: 本页标题（自引用排除，归一化比较）
        valid_titles: 有效目标标题映射（归一化 key → 原标题），或标题集合
        max_links: 单页图边数量上限

    Returns:
        去重后的目标标题列表（按目标标题在正文中的首次出现位置排序）
    """
    valid = (
        valid_titles
        if isinstance(valid_titles, dict)
        else {normalize_title(t): t for t in (valid_titles or [])}
    )
    self_norm = normalize_title(self_title)
    haystack = normalize_title(content or "")
    if not haystack:
        return []
    found = []
    for norm, title in valid.items():
        # 单字标题噪声大，跳过（如「会」「卡」）
        if not norm or norm == self_norm or len(norm) < 2:
            continue
        pos = haystack.find(norm)
        if pos >= 0:
            found.append((pos, title))
    found.sort(key=lambda item: item[0])
    return [title for _, title in found[:max_links]]

def _split_sentences(text: str) -> List[str]:
    """按中英文句末标点与换行切句（诊断探针用），去空句。"""
    parts = re.split(r"(?<=[。！？!?；;])|\n", text or "")
    return [p.strip() for p in parts if p and p.strip()]

def _strip_think(text: str) -> str:
    """剥离混合思考模型可能输出的 <think> 块。"""
    return _THINK_RE.sub("", text or "")

def _parse_json_dict(text: str) -> dict:
    """从 LLM 输出中解析 JSON 对象；容忍代码围栏、包裹文字与**尾随多余内容**。

    尾随多余内容（如 JSON 之后又续了说明/第二个对象）曾使贪心 `\\{.*\\}` 匹配
    把整段喂给 `json.loads` → `Extra data` 报错 → 该文档零页面。此处改用
    `raw_decode` 只消费**一个** JSON 值，忽略其后内容。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start == -1:
            raise
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    return data if isinstance(data, dict) else {}

def _cosine(vec_a: List[float], vec_b: List[float]) -> float:
    """余弦相似度；非法输入返回 0（对齐 semantic_cache_service 语义）。"""
    try:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = sum(a * a for a in vec_a) ** 0.5
        nb = sum(b * b for b in vec_b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
    except TypeError:
        return 0.0
