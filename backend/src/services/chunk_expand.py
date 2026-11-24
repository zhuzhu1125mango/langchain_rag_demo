"""邻块回填：给选中的块补上同文档的 `chunk_index ± 1`。

## 为什么要（离线实测，2026-11-11）

生产切分是「标题路径 + 表头 + 一行」的行级块，一块的正文经常只有 100~200 字符。
答案需要连续性时，单块常常只覆盖了半句话：

- 「优雅停机怎么做」——步骤被切成 2 块，只拿到第 1 块，SIGTERM / SIGKILL 在后一块；
- 「监控哪些指标、有哪些告警」——折叠腾出名额后，回填把告警表其余行补齐，0.50 → 1.00。

24 题生产语料（606 块、窗口 3）：

| 配置 | 概览 | 细节 | 概览字符 |
|---|---|---|---|
| 不折叠 / 不回填 | 0.556 | 0.792 | 732 |
| 不折叠 / 回填 | 0.597 | 0.833 | 1942 |
| 折叠 / 不回填 | 0.597 | 0.792 | 793 |
| 折叠 / 回填 | **0.681** | **0.833** | 2028 |

**回填单独用对「同表占满窗口」那类题完全无效**（0.00 → 0.00）：邻块也是同一张表的行。
它必须和折叠一起才到 0.681 —— 两者作用在不同位置，一个换人、一个补全。

## 成本

无预算是 3 块 → 8 块、732 → 2028 字符；prefill 实测只多约 150~200ms
（`qwen3:4b`，1200 字符 196ms / 2000 字符 333ms）。真正的代价不是时间，是**噪声**：
补进来的邻块必然有与问题无关的内容，而覆盖率指标看不见这个。

所以这里强制带**字符预算**，不做无上限回填：

- 1200 字符拿到 0.639（比基线多 350 字符，性价比最高）
- 2000 字符拿到 0.681（多 968 字符，收益最高）
- 900 字符只有 0.597（回填基本没生效，块被预算挡在外面）

## 边界（由单测钉住）

1. **不跨文档**：只在同 `document_id` 内取邻居，绝不从别的文档补；
2. **不重复**：候选里已有的块不再补一遍（`id` 去重）；
3. **不超预算**：超预算的邻块跳过继续看下一个，而不是整批放弃；
4. **顺序稳定**：原地块按原顺序在前，邻块按「原地块顺序 → 先 -1 后 +1」追加；
5. **只做一层**：不递归补邻块的邻块（否则预算是唯一约束，会一路扩散）。

## 整节回填（2026-11-24 追加）

`chunk_index ± 1` 补不到「小节被切成 6 块、最相关的那块根本没排进 top_k」的情况。
实测（`backup-restore.md`，问「数据库备份怎么恢复？恢复后要确认什么？」）：

- §4.2「恢复后必做」= chunk 28，在候选池（20 条）里，被重排压到**第 12 名、0.064**；
- 原因不是召回失败：`bge-reranker-base` 偏字面重叠，chunk 28 是四条命令清单
  （`rag:semcache` / `EMBEDDING_DIMENSION` / `/index/status` / P95），不含
  「数据库/备份/恢复」这些字面词；而 chunk 113 是一张写着「PG 恢复 | MinIO 恢复」的
  演练记录表头，0.746 排第一；
- TOP_K 从 3 提到 5 也进不来（第 12 名比第 5 名远得多）。

所以整节回填按**文档结构**取数（`heading_path` 前缀 like），绕开重排判断。
三条边界与 ±1 不同，都已被单测钉住：

1. **要求三级标题**：只有两级时「去最后一段」会退化成整篇文档，直接拒绝；
2. **按小节轮转 + 距离上限**：高分小节常常是占满位置的表格行，让它先吃满预算
   就会把正文小节挤出去（实测 §7.3 先吃掉 2171 字符，§4.2 照旧进不来）；
3. **补进来的块不进相关性门禁**：沿用父块分数，参与平均分等于用复制分数投票。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

Position = Tuple[str, int]

# heading_path 的分段符（与入库时的拼接口径一致："H1 > H2 > H3"）
SECTION_SEP = " > "


def _pos(row: Dict[str, Any], doc_key: str, index_key: str) -> Position | None:
    doc = row.get(doc_key)
    idx = row.get(index_key)
    if doc in (None, "") or idx is None:
        return None
    try:
        return str(doc), int(idx)
    except (TypeError, ValueError):
        return None


def neighbor_positions(
    rows: Sequence[Dict[str, Any]],
    *,
    doc_key: str = "document_id",
    index_key: str = "chunk_index",
) -> List[Position]:
    """需要补查的 `(document_id, chunk_index)` 列表，顺序即「原地块顺序 → -1 → +1」。

    去重但保留首次出现顺序：同一个邻块被两个原地块同时需要时只查一次。
    """
    out: List[Position] = []
    seen: set[Position] = set()
    for row in rows:
        pos = _pos(row, doc_key, index_key)
        if pos is None:
            continue
        doc, idx = pos
        for delta in (-1, 1):
            key = (doc, idx + delta)
            if key[1] < 0 or key in seen:
                continue
            seen.add(key)
            out.append(key)
    return out


def same_heading_as_parent(
    neighbors: Iterable[Dict[str, Any]],
    base: Sequence[Dict[str, Any]],
    *,
    heading_key: str = "heading_path",
    doc_key: str = "document_id",
    index_key: str = "chunk_index",
) -> List[Dict[str, Any]]:
    """只保留与「带它进来的那个原地块」同章节的邻块。

    ## 为什么需要这一层

    `chunk_index ± 1` 是**物理相邻**，不等于**语义连续**。同一文档里，相邻两块可能分属
    完全不同的章节（上一节收尾 + 下一节开头的表格）。无差别回填的实测代价：

    - 「生产环境升级要注意什么」基线答案讲了 SIGTERM 与 graceful shutdown；
      回填后答案变成了「配置项清单」，SIGTERM 那条丢了 —— 邻块把注意力引到别处；
    - 「改完告警规则怎么生效」基线答 promtool 校验，回填后变成"跑测试脚本"。

    而真正需要回填的场景（步骤被切断、表格被按行切开）**必然同章节**：
    优雅停机的 3 个步骤、同一张表的兄弟行，heading_path 完全一致。

    ## 判据

    两侧 heading 都非空且不相等 → 丢弃；任一侧为空 → 保留（无从判断，宁可保留）。
    不做「前缀相同也算同章节」的放宽：`7.3 监控指标` 与 `7.4 告警规则` 前缀只有 `7`，
    而 `# 部署 > ## 4` 与 `# 部署 > ## 5` 前缀是 `# 部署`——前缀规则会把它们都判成同章节，
    等于没这道闸门。
    """
    parents: Dict[Position, str] = {}
    for row in base:
        pos = _pos(row, doc_key, index_key)
        if pos is None:
            continue
        parents[pos] = str(row.get(heading_key) or "")

    kept: List[Dict[str, Any]] = []
    for row in neighbors:
        pos = _pos(row, doc_key, index_key)
        if pos is None:
            continue
        doc, idx = pos
        child = str(row.get(heading_key) or "")
        if not child:
            kept.append(row)
            continue
        # 邻块可能同时是「前一个块的下邻」和「后一个块的上邻」，任一侧同章节即保留
        for parent_id in ((doc, idx - 1), (doc, idx + 1)):
            parent_heading = parents.get(parent_id)
            if parent_heading is None:  # 这个方向上没有入选的原地块
                continue
            if not parent_heading or parent_heading == child:  # 父块无 heading：无从判断
                kept.append(row)
                break
    return kept


def merge_with_neighbors(
    base: Sequence[Dict[str, Any]],
    neighbors: Iterable[Dict[str, Any]],
    *,
    budget_chars: int | None = None,
    content_key: str = "content",
    doc_key: str = "document_id",
    index_key: str = "chunk_index",
    id_key: str = "id",
) -> List[Dict[str, Any]]:
    """把邻块拼到原地块后面，按预算裁剪，返回新列表（不修改入参）。

    Args:
        base: 原地块（已按相关性排好序），原样保留并决定顺序。
        neighbors: 补查回来的邻块（顺序无关，按 `(doc, index)` 建索引）。
        budget_chars: 总字符上限（**含**原地块）。`None` 表示不限制。
            原地块本身超预算时仍然全部保留——预算只约束「补不补」，
            不能因为预算把已经选中的块丢掉（那会把检索结果变成更差的结果）。

    Returns:
        原地块在前、邻块按序追加的新列表。
    """
    index: Dict[Position, Dict[str, Any]] = {}
    for row in neighbors:
        pos = _pos(row, doc_key, index_key)
        if pos is not None and pos not in index:
            index[pos] = row

    out: List[Dict[str, Any]] = list(base)
    used_ids = {row.get(id_key) for row in out if row.get(id_key)}
    used_chars = sum(len(row.get(content_key) or "") for row in out)

    for row in base:
        pos = _pos(row, doc_key, index_key)
        if pos is None:
            continue
        doc, idx = pos
        for delta in (-1, 1):
            nb = index.get((doc, idx + delta))
            if nb is None:
                continue
            nb_id = nb.get(id_key)
            if nb_id and nb_id in used_ids:
                continue
            grow = len(nb.get(content_key) or "")
            if budget_chars is not None and used_chars + grow > budget_chars:
                continue
            out.append(nb)
            if nb_id:
                used_ids.add(nb_id)
            used_chars += grow
    return out


def section_prefix(
    heading_path: str,
    *,
    sep: str = SECTION_SEP,
    min_depth: int = 3,
) -> str:
    """返回小节前缀（父级 heading 路径），不满足条件时返回空串。

    例：`"部署 > 4. 恢复 > 4.1 恢复顺序"` → `"部署 > 4. 恢复"`。

    ## 为什么要求 `min_depth=3`

    只去掉**最后一段**，得到的是「入选块所属的二级小节」。若文档层级只有两层
    （`"部署 > 4. 恢复"`），去掉最后一段就退化成一级标题 —— 那等于「整篇文档」，
    预算会把上下文塞满、噪声盖过答案。这类层级不满足 `min_depth`，直接不回填：
    宁可少补，也不要把整篇文档拖进来。

    文档层级恰好是 `H1 > H2 > H3` 时（本项目 guide 文档的常见形态），
    返回的 `H2` 前缀正是「4. 恢复」这一节，含 4.1 / 4.2 / 4.3 全部子小节。
    """
    parts = [p.strip() for p in (heading_path or "").split(sep) if p.strip()]
    if len(parts) < min_depth:
        return ""
    return sep.join(parts[:-1])


def section_prefixes(
    rows: Sequence[Dict[str, Any]],
    *,
    heading_key: str = "heading_path",
    doc_key: str = "document_id",
    index_key: str = "chunk_index",
) -> List[Tuple[str, str]]:
    """原地块需要的 `(document_id, 小节前缀)` 列表，按首次出现顺序去重。

    `max_sections` 由调用方截断（避免一次请求回填十几个小节）。
    """
    out: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for row in rows:
        pos = _pos(row, doc_key, index_key)
        if pos is None:
            continue
        doc, _ = pos
        prefix = section_prefix(str(row.get(heading_key) or ""))
        if not prefix:
            continue
        key = (doc, prefix)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def merge_appended(
    base: Sequence[Dict[str, Any]],
    extras: Iterable[Dict[str, Any]],
    *,
    budget_chars: int | None = None,
    content_key: str = "content",
    id_key: str = "id",
) -> List[Dict[str, Any]]:
    """把「整节补查回来的块」按调用方给的顺序追加到原地块后面。

    与 `merge_with_neighbors` 的差别只在**追加顺序由调用方决定**（整节回填要按
    `chunk_index` 升序，让模型读到的是一节完整的正文，而不是打散的块），
    去重与预算语义完全一致：

    - 已在原地块里的不再追加；
    - 超预算的块跳过、继续看下一块（不是整批放弃）；
    - 原地块本身超预算也照留（预算只约束「补不补」）。
    """
    out: List[Dict[str, Any]] = list(base)
    used_ids = {row.get(id_key) for row in out if row.get(id_key)}
    used_chars = sum(len(row.get(content_key) or "") for row in out)
    for row in extras:
        row_id = row.get(id_key)
        if row_id and row_id in used_ids:
            continue
        grow = len(row.get(content_key) or "")
        if budget_chars is not None and used_chars + grow > budget_chars:
            continue
        out.append(row)
        if row_id:
            used_ids.add(row_id)
        used_chars += grow
    return out
