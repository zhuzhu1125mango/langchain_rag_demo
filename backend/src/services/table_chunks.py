"""行级表格块的识别与折叠（检索侧的「组合」修正）。

## 问题

生产切分器把 markdown 表格**按行切开**：每个块的正文是「heading_path + 表头 + 该行」。
这对点查（「X1 的重量是多少」）很友好，但对列举型问题（「监控哪些指标、有哪些告警」）
会出问题：同一张表的多行在重排里得分又平又高，把窗口占满，而答案在**另一张表**里。

实测（2026-11-11，24 题探针）该题 top-3 全是 `7.3 监控指标` 那张表的三行：
0.970 / 0.969 / 0.968，而 `4. 告警规则` 里的 `ScrapeTargetDown` 行只有 0.788 —— 名次 4，
结构上进不了上下文。

## 为什么折叠发生在融合之后、重排之前

生产 `search_hybrid` 里 `rerank_n = max(k, KB_HYBRID_RERANK_TOP_K)`，而线上
`KB_HYBRID_RERANK_TOP_K=3`、`k=3` —— **重排窗口等于最终块数，重排只能排序，不能换人**。
所以「谁进这 3 个名额」完全由 RRF 融合顺序决定。折叠必须插在 RRF 之后、截断之前，
才能改变候选身份；放在重排之后只是重新分配已有的 3 个名额（已实测为负收益）。

## 实测（离线 24 题，614 chunks）

- 基线（不折叠，窗口 3）：概览 0.556 / 细节 0.792，目标题覆盖 0.00
- 折叠（窗口 3）：概览 **0.598** / 细节 0.792，目标题覆盖 **0.50**，其余 23 题不变
- 放开窗口到 20：0.452（更差）—— 折叠腾出的名额会被「文档元信息块」这类
  字面相似但无答案的块占据，窗口越大越容易挤掉正确答案

## 边界

只折叠「表头 + 一行」形态的行级块。整表块不参与（没有可折叠的兄弟行），
普通段落不参与（相邻段落的重复是正当证据，例如优雅停机的连续步骤）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple


def table_header_of(text: str) -> str | None:
    """若片段是「heading_path + 表头 + 一行」的行级表格块，返回表头行；否则 None。

    判据与离线探针（`scripts/probe_retrieval_stages.py::_table_header_of`）保持一致：
    正文里恰好两行以 `|` 开头（表头 + 一行），且非空行不超过 3 行（第三行是 heading_path）。
    这个严格判据是有意的——放宽到「任意含表格的块」会把整表块也算进来，
    而整表块本来就没有可折叠的兄弟行。
    """
    if not text:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    table_lines = [ln for ln in lines if ln.strip().startswith("|")]
    if len(table_lines) == 2 and len(lines) <= 3:
        return table_lines[0]
    return None


def fold_same_table_rows(
    items: Sequence[Dict[str, Any]],
    *,
    content_key: str = "content",
    doc_key: str = "document_id",
) -> List[Dict[str, Any]]:
    """同一 (文档, 表头) 的行级表格块只保留第一个，其余顺序后移。

    保持原有相对顺序（输入是 RRF 融合后的降序，折叠即「同表只占一席，把名额让给后面的块」）。
    非表格块原样保留。

    Args:
        items: 融合后的候选列表（每项含正文与文档 id）。
        content_key: 正文所在字段名。
        doc_key: 文档标识所在字段名。

    Returns:
        折叠后的新列表（不修改入参，长度 ≤ 原长度）。
    """
    seen: set[Tuple[Any, str]] = set()
    out: List[Dict[str, Any]] = []
    for item in items:
        header = table_header_of(item.get(content_key) or "")
        if header:
            key = (item.get(doc_key), header)
            if key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out
