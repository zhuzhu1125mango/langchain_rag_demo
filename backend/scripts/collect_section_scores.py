"""离线采集「整节回填」的判据数据：每节的父块分数 + 铺开后答案是否变好。

为什么需要这个脚本
------------------
`KB_SECTION_EXPAND_MIN_SECTION_SCORE=0.4` 目前只有**一道题**的两组数据支撑：
0.5 的正文小节该铺（7 步完整）、0.338 的回滚小节不该铺（16 块里 9 块是它，答案缩成 3 行）。
一条从单题分数分布里切出来的线，经不起换题验证——它可能只是那道题的巧合。

要让它站得住，需要的是同一批题上的三列数：小节父块分数、铺开后的上下文规模、
以及**答案覆盖率有没有变好**。这三列现在只能手工拼，本脚本把它变成一条命令。

采集什么
--------
对每道题（走与线上一致的检索入口，只是把回填补丁临时关掉取原始 top-k）：

1. **小节表**：每个命中块所属的二级小节、该节的父块分数（节内最高重排分）、
   该节在文档里的总块数与总字符（按 heading 前缀直接查，不是估算）；
2. **每个阈值的模拟结果**：阈值取自 `--thresholds`，记录铺开后的块数/字符/
   新增了哪些小节。阈值是 `min_section_score`，`skip_score` 用线上配置；
3. **（可选）答案评估**：给了 `--answer` 且题面带 `facts` 时，按每个阈值真跑一次
   问答，算 facts 的命中率。没有 facts 就不做评判——宁可缺一列，也不要用
   「答案看起来更长」冒充「答案更好」。

输出是一张逐题的分数表 + 一张跨题汇总表，`--out` 另存 JSON 供后续分析。

两个口径必须交代
----------------
- **检索是单查询**：脚本调 `KBRetrievalService.retrieve`，与线上同一条路径，但
  不做多查询改写（`KB_MULTI_QUERY_ENABLED` 开启时线上会多一步）。当前线上该开关
  关闭，两者一致；若哪天打开了，这里的分数分布会偏保守（少一个召回通道）。
- **只改内存里的 settings**：`--answer` 需要按不同阈值跑，脚本会临时改
  `KB_SECTION_EXPAND_MIN_SECTION_SCORE` 并在结束时还原。不写 `.env`、不影响
  正在运行的服务进程（各自独立的内存）。

用法（生产容器内）
------------------
    python scripts/collect_section_scores.py --questions /tmp/q.json
    python scripts/collect_section_scores.py --questions /tmp/q.json --answer --out /tmp/s.json
    python scripts/collect_section_scores.py --q "备份怎么恢复？" --kb <kb_id> --answer

题目文件：

    {
      "kb_ids": ["b3bad7b2-..."],
      "thresholds": [0.0, 0.3, 0.4, 0.5, 0.6],
      "questions": [
        {"question": "数据库备份怎么恢复？恢复后要确认什么？",
         "facts": ["restore.sh", "停写", "rag:semcache", "EMBEDDING_DIMENSION"]}
      ]
    }

`facts` 是「答对就必须出现」的短字符串（大小写、空白、标点不敏感），
一般取文档原文里的关键词或命令，不要写整句——整句只在逐字复述时命中。

退出码：0 全部完成 / 1 有题目失败（其余仍会输出）/ 2 输入或前置条件不满足
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 将 backend 目录加入路径，以便导入 src 模块（沿用 scripts/ 下其他脚本的约定）
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from src.config import settings  # noqa: E402
from src.services.chunk_expand import section_prefix  # noqa: E402

DEFAULT_THRESHOLDS = [0.0, 0.2, 0.3, 0.4, 0.5, 0.6]
# 样本少于这个数就不给「推荐阈值」——那是把观察冒充结论
MIN_SAMPLE_FOR_RECOMMENDATION = 5
# 覆盖率差距在这个范围内视为「一样好」，再比字符数
COVERAGE_TOLERANCE = 0.05

_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


class InputError(RuntimeError):
    """输入不成立：缺题、缺 facts、阈值写错等，继续跑只会产出误导性的数字。"""


# --------------------------------------------------------------------------- #
# 纯函数（可单测，不碰网络与数据库）
# --------------------------------------------------------------------------- #
def normalize(text: str) -> str:
    """归一化到「只比字面」的形式：小写 + 去掉标点空白。

    为什么要去掉标点：文档里写 `清空 `rag:semcache:*``，答案里常写成
    「清空 rag:semcache 缓存」，冒号星号的有无不该决定这条 fact 算不算命中。
    """
    return _NON_WORD.sub("", (text or "").lower())


def fact_coverage(
    answer: str,
    facts: Sequence[str],
) -> Dict[str, Any]:
    """答案命中了多少条 fact。

    Returns:
        `{"ratio": 0~1, "matched": [...], "missing": [...]}`；facts 为空时 ratio 为 0
        且 matched/missing 都为空 —— 调用方应据此跳过评判，而不是把 0 当成「答错」。
    """
    flat = normalize(answer)
    matched, missing = [], []
    for fact in facts:
        (matched if normalize(fact) and normalize(fact) in flat else missing).append(fact)
    total = len(matched) + len(missing)
    return {
        "ratio": round(len(matched) / total, 3) if total else 0.0,
        "matched": matched,
        "missing": missing,
    }


def load_questions(
    path: Optional[str],
    inline: Sequence[str],
    kb_ids: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[str], List[float]]:
    """读题目文件或命令行题目，返回 (题目列表, 默认 kb_ids, 阈值列表)。

    题目项统一成 `{"question", "kb_ids", "facts"}`；kb_ids 逐题覆盖文件级默认值，
    文件级再覆盖命令行。阈值以文件里的 `thresholds` 为准（没有就用默认扫描）。
    """
    payload: Dict[str, Any] = {}
    if path:
        raw = Path(path).read_text(encoding="utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            raise InputError(f"题目文件不是合法 JSON：{path}（{e}）") from e
        if isinstance(payload, list):  # 允许直接给数组
            payload = {"questions": payload}
        if not isinstance(payload, dict):
            raise InputError("题目文件应为对象或数组")

    items: List[Dict[str, Any]] = list(payload.get("questions") or [])
    for q in inline:
        items.append({"question": q})
    if not items:
        raise InputError("没有题目：用 --questions 给文件，或用 --q 逐条给")

    default_kb = [str(x) for x in (payload.get("kb_ids") or list(kb_ids))]
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, str):
            item = {"question": item}
        question = (item.get("question") or "").strip()
        if not question:
            raise InputError(f"第 {i} 题缺少 question")
        facts = item.get("facts") or []
        if not isinstance(facts, list) or any(not isinstance(f, str) for f in facts):
            raise InputError(f"第 {i} 题的 facts 必须是字符串数组")
        out.append(
            {
                "question": question,
                "kb_ids": [str(x) for x in (item.get("kb_ids") or default_kb)],
                "facts": [f for f in facts if f.strip()],
            }
        )

    thresholds = payload.get("thresholds") or DEFAULT_THRESHOLDS
    try:
        parsed = sorted({float(t) for t in thresholds})
    except (TypeError, ValueError) as e:
        raise InputError(f"thresholds 必须是数字数组：{thresholds}") from e
    if any(t < 0 or t > 1 for t in parsed):
        raise InputError("thresholds 应在 0~1 之间（它就是重排分的下限）")
    return out, default_kb, parsed


def section_table(docs: Sequence[Any]) -> Tuple[List[Dict[str, Any]], int]:
    """把命中块按「二级小节」归档，返回 (小节列表, 无法归类的块数)。

    父块分数取节内**原始命中块**的最高重排分：邻块回填会复制父块分数，让它一起
    参与取最大值不改变结果，但统计「命中几个块」时必须区分，否则会以为一节命中了
    五六块，其实只有一块带进来四个邻块。
    """
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
    unclassified = 0
    for d in docs:
        meta = d.metadata
        doc_id = str(meta.get("document_id") or "")
        prefix = section_prefix(str(meta.get("heading_path") or ""))
        if not doc_id or not prefix:
            unclassified += 1
            continue
        key = (doc_id, prefix)
        row = buckets.setdefault(
            key,
            {
                "document_id": doc_id,
                "prefix": prefix,
                # 名字取**小节**（H2）本身，不是命中块的 H3：这个桶代表整节，
                # 用 H3 命名会让人以为「4.1 恢复顺序」有 0.34 分，其实是「4. 恢复」这一节。
                "title": prefix.split(" > ")[-1],
                "children": set(),
                "score": 0.0,
                "hits": [],
                "originals": 0,
                "inherited": 0,
                "chars": 0,
            },
        )
        child = str(meta.get("heading_path") or "").split(" > ")[-1]
        if child and child != row["title"]:
            row["children"].add(child)
        idx = meta.get("chunk_index")
        if idx is not None:
            row["hits"].append(int(idx))
        row["chars"] += len(d.page_content or "")
        if meta.get("neighbor_expand") or meta.get("section_expand"):
            row["inherited"] += 1
        else:
            row["originals"] += 1
            row["score"] = max(row["score"], float(meta.get("rerank_score") or 0.0))

    rows = list(buckets.values())
    for row in rows:
        row["hits"].sort()
        row["children"] = sorted(row["children"])
        row["score"] = round(row["score"], 3)
    rows.sort(key=lambda r: (-r["score"], r["prefix"]))
    return rows, unclassified


def summarize(records: Sequence[Dict[str, Any]], thresholds: Sequence[float]) -> List[Dict[str, Any]]:
    """跨题汇总：每个阈值下「平均覆盖率 / 平均块数 / 平均字符」。

    覆盖率只在**有 facts 且跑过答案**的题上平均——把没评估的题按 0 计入，
    会让「题少」看起来像「答得差」。
    """
    out: List[Dict[str, Any]] = []
    for t in thresholds:
        sims = [r["simulations"].get(t) for r in records]
        sims = [s for s in sims if s]
        covered = [s for s in sims if s.get("coverage") is not None]
        spreads = [
            round((s["coverage_max"] - s["coverage_min"]), 3)
            for s in covered
            if s.get("coverage_max") is not None and s.get("coverage_min") is not None
        ]
        out.append(
            {
                "threshold": t,
                "questions": len(sims),
                "answered": len(covered),
                "avg_coverage": (
                    round(sum(s["coverage"] for s in covered) / len(covered), 3)
                    if covered else None
                ),
                "avg_spread": (
                    round(sum(spreads) / len(spreads), 3) if spreads else None
                ),
                "avg_chunks": (
                    round(sum(s["chunks"] for s in sims) / len(sims), 1) if sims else None
                ),
                "avg_chars": (
                    round(sum(s["chars"] for s in sims) / len(sims)) if sims else None
                ),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# 运行时（碰检索与模型）
# --------------------------------------------------------------------------- #
def clear_semantic_cache() -> int:
    """清语义缓存，返回删除条数。

    不做这一步会拿到**改配置前**的答案和来源（2026-11-23 实测被骗过一轮）：
    每条答案的 `reasoning` 只剩一条 `cache_hit`，看起来像「配置改了没用」。
    """
    try:
        import redis

        r = redis.Redis(
            host=settings.redis.REDIS_HOST,
            port=settings.redis.REDIS_PORT,
            db=settings.redis.REDIS_DB,
            password=settings.redis.REDIS_PASSWORD,
        )
        keys = list(r.scan_iter("rag:semcache:*"))
        return int(r.delete(*keys)) if keys else 0
    except Exception as e:  # noqa: BLE001 —— 清不掉就让用户知道，别静默
        print(f"[warn] 清语义缓存失败：{e}", file=sys.stderr)
        return 0


async def raw_hits(service, question: str, kb_ids: Sequence[str], top_k: int):
    """取「未经任何回填」的原始命中：临时关掉两个回填开关走线上检索入口。

    为什么不信手调 search_hybrid：线上还有混合/稠密回退、wiki 退池、TOP_K 截断
    这些行为，自己拼一遍就会和线上漂移。这里只把补丁关掉，其余完全同一条路。
    """
    p = settings.processing
    snapshot = (p.KB_NEIGHBOR_EXPAND_ENABLED, p.KB_SECTION_EXPAND_ENABLED, p.TOP_K)
    p.KB_NEIGHBOR_EXPAND_ENABLED = False
    p.KB_SECTION_EXPAND_ENABLED = False
    p.TOP_K = top_k
    try:
        return await service.retrieve(question, kb_ids=list(kb_ids) or None)
    finally:
        p.KB_NEIGHBOR_EXPAND_ENABLED, p.KB_SECTION_EXPAND_ENABLED, p.TOP_K = snapshot


async def section_sizes(vs, rows: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], Dict[str, int]]:
    """按 heading 前缀查每个小节在文档里的真实块数与字符数。

    这是「铺开会带来多少上下文」的分母：只统计命中块数会严重低估——实测 §4 命中
    2 块，整节却有 6 块 2255 字符。
    """
    pairs = [(r["document_id"], r["prefix"]) for r in rows]
    try:
        fetched = await vs.milvus_service.fetch_chunks_by_heading_prefix(pairs)
    except Exception as e:  # noqa: BLE001 —— 采集脚本，查不到就标缺失
        print(f"[warn] 查小节块数失败：{e}", file=sys.stderr)
        return {}
    tally: Dict[Tuple[str, str], Dict[str, int]] = {}
    for item in fetched:
        key = (
            str(item.get("document_id") or ""),
            section_prefix(str(item.get("heading_path") or "")),
        )
        slot = tally.setdefault(key, {"chunks": 0, "chars": 0})
        slot["chunks"] += 1
        slot["chars"] += len(item.get("content") or "")
    return tally


async def simulate(vs, docs, threshold: float, p) -> Dict[str, Any]:
    """按某个小节分数下限模拟一次整节回填，返回上下文规模与新增小节。"""
    out = await vs.expand_sections(
        list(docs),
        budget_chars=p.KB_SECTION_EXPAND_BUDGET_CHARS,
        max_sections=p.KB_SECTION_EXPAND_MAX_SECTIONS,
        max_distance=p.KB_SECTION_EXPAND_MAX_DISTANCE,
        skip_score=p.KB_SECTION_EXPAND_SKIP_SCORE,
        min_section_score=threshold,
    )
    added = [d for d in out if d.metadata.get("section_expand")]
    # 只留小节名（H2）：完整 heading_path 会把 H1 顶到前面，截断后看不出补的是哪一节
    sections = sorted(
        {
            (section_prefix(str(d.metadata.get("heading_path") or "")) or "（未分级）").split(" > ")[-1]
            for d in added
        }
    )
    return {
        "chunks": len(out),
        "chars": sum(len(d.page_content or "") for d in out),
        "added_chunks": len(added),
        "added_sections": sections,
        "coverage": None,
        "answer_len": None,
        "elapsed_s": None,
    }


def aggregate_runs(
    per_run: Sequence[Dict[str, Any]],
    facts: Sequence[str],
) -> Dict[str, Any]:
    """把同一格的多次运行合并成「平均覆盖率 + 每轮明细 + 每个 fact 的命中率」。

    为什么不能只留最后一次：同一份上下文连跑两次，覆盖率实测可以差 0.33
    （2026-11-25：12 块 / 2902 字符不变，0.4 得 0.67、0.5 得 1.00）。单次结果
    分不清「阈值有效」和「这一次运气好」。

    为什么按 fact 统计命中率而不是取某一次：`facts` 是「答对就必须出现」的点，
    「3 次里命中 2 次」本身就是信息——它说明这个点处于模型能力的边缘，
    换一次运行就可能掉。只报最后一次的 matched/missing 会把这种不稳定藏起来。
    """
    if not per_run:
        raise ValueError("aggregate_runs 需要至少一次运行结果")
    n = len(per_run)
    covs = [float(r.get("coverage") or 0.0) for r in per_run]
    rates = {
        f: round(sum(1 for r in per_run if f in (r.get("matched") or [])) / n, 2)
        for f in facts
    }
    return {
        "coverage": round(sum(covs) / n, 3),
        "coverage_runs": covs,
        "coverage_min": min(covs),
        "coverage_max": max(covs),
        "fact_hit_rate": rates,
        # 介于 0 和 1 之间 = 有时命中有时不命中，最该被看见的那类
        "unstable_facts": [f for f, v in rates.items() if 0 < v < 1],
        "answer_len": round(sum(int(r.get("answer_len") or 0) for r in per_run) / n),
        "elapsed_s": round(sum(float(r.get("elapsed_s") or 0.0) for r in per_run), 1),
    }


async def answer_with_threshold(
    chain,
    question: str,
    kb_ids: Sequence[str],
    threshold: Optional[float],
    facts: Sequence[str],
    *,
    expand: bool = True,
) -> Dict[str, Any]:
    """按指定阈值真跑一次问答（`expand=False` 时不回填，作为基准）。

    临时改内存里的 `KB_SECTION_EXPAND_MIN_SECTION_SCORE` 并强制开启整节回填：
    要衡量「这个阈值下的答案」，就得让线上那条路真的按这个阈值走。

    为什么必须有 `expand=False` 的基准：小节分数普遍在 0.8 以上时，0~0.5 的所有阈值
    都放行，四行结果一模一样——看起来「阈值无所谓」，其实是**对照组缺失**。
    没有基准就回答不了「回填让答案变好还是变坏」，只能看到「都一样」。
    """
    p = settings.processing
    snapshot = (p.KB_SECTION_EXPAND_ENABLED, p.KB_SECTION_EXPAND_MIN_SECTION_SCORE)
    p.KB_SECTION_EXPAND_ENABLED = expand
    if threshold is not None:
        p.KB_SECTION_EXPAND_MIN_SECTION_SCORE = threshold
    clear_semantic_cache()
    started = time.time()
    try:
        answer, _sources, _meta, _type = await chain.run(
            question, kb_ids=list(kb_ids) or None, deep_thinking="off"
        )
    finally:
        (p.KB_SECTION_EXPAND_ENABLED, p.KB_SECTION_EXPAND_MIN_SECTION_SCORE) = snapshot
    cov = fact_coverage(answer, facts)
    return {
        "coverage": cov["ratio"],
        "matched": cov["matched"],
        "missing": cov["missing"],
        "answer_len": len(answer or ""),
        "elapsed_s": round(time.time() - started, 1),
    }


async def answer_averaged(
    chain,
    question: str,
    kb_ids: Sequence[str],
    threshold: Optional[float],
    facts: Sequence[str],
    rounds: int,
    *,
    expand: bool = True,
) -> Dict[str, Any]:
    """同一格跑 `rounds` 次取平均；每次都先清语义缓存，否则第二次起拿到的是缓存答案。

    不清缓存时重复运行会得到**完全相同**的答案，方差恒为 0，
    「多次取平均」就变成了自欺欺人——所以清缓存这一步由 `answer_with_threshold` 负责，
    这里只是把同一格多跑几次再聚合。
    """
    per_run = [
        await answer_with_threshold(chain, question, kb_ids, threshold, facts, expand=expand)
        for _ in range(max(1, rounds))
    ]
    return aggregate_runs(per_run, facts)


def _cov_text(cell: Dict[str, Any]) -> str:
    """把覆盖率渲染成「均值(最小值~最大值)」；多次运行不一致时才显示区间。

    只显示均值会把「三次里有一次掉到 0.33」藏起来，而那种不稳定恰恰是
    选阈值时最该看的东西。
    """
    if cell.get("coverage") is None:
        return "-"
    text = f"{cell['coverage']:.2f}"
    lo, hi = cell.get("coverage_min"), cell.get("coverage_max")
    if lo is not None and hi is not None and abs(hi - lo) > 1e-9:
        text += f"({lo:.2f}~{hi:.2f})"
    return text


def print_question(idx: int, record: Dict[str, Any]) -> None:
    print(f"\n=== Q{idx} {record['question']}")
    print(
        f"    原始命中 {record['raw_hits']} 块 / 首块分 {record['top1']:.3f}"
        f" / 整节回填{'被跳过（≥%.2f）' % record['skip_score'] if record['skip_triggered'] else '会执行'}"
        f" / 无法归类 {record['unclassified']} 块"
    )
    if record["error"]:
        print(f"    [FAIL] {record['error']}")
        return
    print(f"    {'小节':<28} {'父块分':>6} {'命中':>4} {'节内块':>6} {'节内字符':>8}")
    for row in record["sections"]:
        size = row.get("section_chunks")
        chars = row.get("section_chars")
        print(
            f"    {row['title'][:26]:<28} {row['score']:>6.3f} {len(row['hits']):>4}"
            f" {(size if size is not None else '-'):>6} {(chars if chars is not None else '-'):>8}"
        )
        if row.get("children"):
            # 这一节里实际命中了哪些 H3：判定「该不该铺」时要看它，
            # 分数只说明整节相关，不说明铺开的是 4.1 还是 4.2
            print(f"      └ {'、'.join(row['children'])[:64]}")
    print(f"    {'阈值':>5} {'块':>4} {'字符':>6} {'新增':>4}  新增小节 / 覆盖率")
    bl = record.get("baseline")
    if bl:
        print(f"    {'基准':>5} {bl['chunks']:>4} {bl['chars']:>6} {0:>4}  （不回填） {_cov_text(bl)}")
    for t in record["threshold_order"]:
        s = record["simulations"][t]
        secs = "、".join(s["added_sections"]) if s["added_sections"] else "（无）"
        print(f"    {t:>5.2f} {s['chunks']:>4} {s['chars']:>6} {s['added_chunks']:>4}  {secs[:46]} {_cov_text(s)}")


async def main_async(args) -> int:
    questions, default_kb, thresholds = load_questions(args.questions, args.q, args.kb)
    if not default_kb and any(not q["kb_ids"] for q in questions):
        raise InputError("没有 kb_ids：用 --kb 或题目文件的 kb_ids 指定知识库，避免全库检索")
    if args.answer and any(not q["facts"] for q in questions):
        raise InputError("--answer 需要每题都给 facts（没有参照就无法判断「答案是否变好」）")

    from src.services.kb_retrieval_service import KBRetrievalService
    from src.services.model_manager import model_manager
    from src.services.vector_store import VectorStoreManager

    # 与 reingest 脚本同一个坑：脚本进程不复用 Web 进程的在线 registry，
    # 不先加载 DB 配置就会退回 legacy env（2026-11-24 因此灌空过一篇文档）。
    try:
        await model_manager.load_from_db()
    except Exception as e:  # noqa: BLE001
        raise InputError(f"加载数据库模型配置失败：{e}") from e

    vs = await VectorStoreManager.get_instance()
    service = KBRetrievalService(vs)
    p = settings.processing
    chain = None
    if args.answer:
        from src.services.rag_chain import RAGChain

        chain = await RAGChain.get_instance(vs)

    print("采集整节回填判据")
    print(
        f"  题数 {len(questions)} / 阈值 {thresholds} / "
        f"答案评估 {'开' if args.answer else '关'}"
        + (f"（每格 {args.rounds} 次取平均）" if args.answer else "")
    )
    print(
        f"  当前配置：TOP_K={args.top_k} 预算={p.KB_SECTION_EXPAND_BUDGET_CHARS} "
        f"skip={p.KB_SECTION_EXPAND_SKIP_SCORE} min_section={p.KB_SECTION_EXPAND_MIN_SECTION_SCORE} "
        f"max_distance={p.KB_SECTION_EXPAND_MAX_DISTANCE}"
    )
    if not args.answer:
        print("  提示：未开 --answer，只采集分数分布；「答案是否变好」这一列会是空的。")

    records: List[Dict[str, Any]] = []
    failures = 0
    for i, item in enumerate(questions, 1):
        record: Dict[str, Any] = {
            "question": item["question"],
            "kb_ids": item["kb_ids"],
            "facts": item["facts"],
            "raw_hits": 0,
            "top1": 0.0,
            "skip_score": p.KB_SECTION_EXPAND_SKIP_SCORE,
            "skip_triggered": False,
            "unclassified": 0,
            "sections": [],
            "simulations": {},
            "baseline": None,
            "threshold_order": list(thresholds),
            "error": None,
        }
        try:
            hits = await raw_hits(service, item["question"], item["kb_ids"], args.top_k)
            record["raw_hits"] = len(hits)
            if not hits:
                record["error"] = "检索无命中（这个阈值扫描没有意义）"
                failures += 1
                records.append(record)
                print_question(i, record)
                continue

            score0 = max(float(d.metadata.get("rerank_score") or 0.0) for d in hits)
            record["top1"] = round(score0, 3)
            record["skip_triggered"] = bool(
                p.KB_SECTION_EXPAND_SKIP_SCORE is not None
                and score0 >= p.KB_SECTION_EXPAND_SKIP_SCORE
            )

            # 复现线上顺序：邻块回填 → 整节回填。邻块也要在，否则小节归属和
            # 「命中几块」都与线上不同（线上先补 ±1，再按小节铺开）。
            docs = list(hits)
            if p.KB_NEIGHBOR_EXPAND_ENABLED:
                docs = await vs.expand_neighbors(
                    docs, budget_chars=p.KB_NEIGHBOR_EXPAND_BUDGET_CHARS
                )

            rows, unclassified = section_table(docs)
            record["unclassified"] = unclassified
            sizes = await section_sizes(vs, rows) if rows else {}
            for row in rows:
                size = sizes.get((row["document_id"], row["prefix"])) or {}
                row["section_chunks"] = size.get("chunks")
                row["section_chars"] = size.get("chars")
            record["sections"] = rows

            # 基准：完全不回填（等价于整节回填关闭）。没有这一行，当所有阈值都放行时
            # 四行结果一样，会被误读成「阈值无所谓」，其实是缺了对照组。
            baseline: Dict[str, Any] = {
                "chunks": len(docs),
                "chars": sum(len(d.page_content or "") for d in docs),
                "added_chunks": 0,
                "added_sections": [],
                "coverage": None,
                "answer_len": None,
                "elapsed_s": None,
            }
            if args.answer and item["facts"]:
                baseline.update(
                    await answer_averaged(
                        chain,
                        item["question"],
                        item["kb_ids"],
                        None,
                        item["facts"],
                        args.rounds,
                        expand=False,
                    )
                )
            record["baseline"] = baseline

            for t in thresholds:
                sim = await simulate(vs, docs, t, p)
                if args.answer and item["facts"] and not record["skip_triggered"]:
                    sim.update(
                        await answer_averaged(
                            chain,
                            item["question"],
                            item["kb_ids"],
                            t,
                            item["facts"],
                            args.rounds,
                        )
                    )
                elif args.answer and record["skip_triggered"]:
                    # 首块够高时线上根本不回填，阈值改成什么都不影响结果：
                    # 跑一次是浪费，跑多次还会把这个阈值包装成「有影响」。
                    sim["note"] = "首块分高于 skip，回填未触发，阈值不影响结果"
                record["simulations"][t] = sim
            print_question(i, record)
        except Exception as e:  # noqa: BLE001 —— 单题失败不拖垮整批
            record["error"] = f"{type(e).__name__}: {e}"
            failures += 1
            print_question(i, record)
        records.append(record)

    summary = summarize(records, thresholds)
    print("\n=== 汇总（按阈值）")
    print(
        f"  {'阈值':>5} {'题数':>4} {'评估':>4} {'平均覆盖':>8} {'波动':>6}"
        f" {'平均块':>6} {'平均字符':>8}"
    )
    for row in summary:
        cov = f"{row['avg_coverage']:.3f}" if row["avg_coverage"] is not None else "-"
        spread = f"{row['avg_spread']:.2f}" if row["avg_spread"] is not None else "-"
        print(
            f"  {row['threshold']:>5.2f} {row['questions']:>4} {row['answered']:>4}"
            f" {cov:>8} {spread:>6} {row['avg_chunks']:>6} {row['avg_chars']:>8}"
        )

    answered = [r for r in summary if r["answered"]]
    sample = max((r["answered"] for r in answered), default=0)

    # 基准行：不回填。必须**只和评估过阈值的题**比——基准跑的是全部题（含被 skip 的），
    # 阈值行只统计有覆盖率的题；分母不同的时候相减，等于把「题不一样」当成「效果不同」。
    evaluated = [
        r for r in records
        if any(s.get("coverage") is not None for s in r["simulations"].values())
    ]
    bl_rows = [
        r["baseline"] for r in evaluated
        if r.get("baseline") and r["baseline"].get("coverage") is not None
    ]
    if bl_rows:
        print(
            f"\n  基准（不回填，同 {len(bl_rows)} 题）: 平均覆盖 "
            f"{sum(r['coverage'] for r in bl_rows) / len(bl_rows):.3f}"
            f" / 平均块 {sum(r['chunks'] for r in bl_rows) / len(bl_rows):.1f}"
            f" / 平均字符 {round(sum(r['chars'] for r in bl_rows) / len(bl_rows))}"
        )
        worse = [
            r["question"][:18] for r in evaluated
            if r.get("baseline") and r["baseline"].get("coverage") is not None
            and any(
                s.get("coverage") is not None and s["coverage"] < r["baseline"]["coverage"]
                for s in r["simulations"].values()
            )
        ]
        if worse:
            print(
                "  回填后覆盖率低于基准的题：" + "、".join(worse)
                + "（分数高不等于该铺：小节块数多时，铺开只是把答案挤出注意力中心）"
            )
        else:
            print("  没有任何题因回填而低于基准。")

    if answered and args.rounds == 1:
        print(
            "\n  注意：每格只跑了 1 次。实测同一份上下文的覆盖率可差 0.33，"
            "小于这个幅度的差异不可信；要定论请加 --rounds 3。"
        )

    # 上下文等价分组：不同阈值可能在这批题上产出**完全相同**的检索结果。
    # 此时它们之间任何覆盖率差异都只可能来自生成噪声（实测：同一 12 块 2902 字符，
    # 0.4 覆盖 0.67、0.5 覆盖 1.00），拿它选阈值就是把噪声当信号。
    def _signature(t: float) -> tuple:
        return tuple(
            (r["question"], s["chunks"], s["chars"], tuple(s["added_sections"]))
            for r in records
            for s in [r["simulations"].get(t)]
            if s
        )

    groups: Dict[tuple, List[float]] = {}
    for t in thresholds:
        groups.setdefault(_signature(t), []).append(t)
    equivalent = {t: g for g in groups.values() if len(g) > 1 for t in g}
    for group in groups.values():
        if len(group) > 1:
            print(
                f"\n  阈值 {' / '.join(f'{t:.2f}' for t in group)} 在这批题上产出的上下文完全相同"
                "（块数、字符、新增小节一致），它们之间的覆盖率差异属于生成噪声，不能据此选阈值。"
            )

    if not answered:
        print("\n没有答案数据，得不出阈值；分数分布见上表。")
    elif sample < MIN_SAMPLE_FOR_RECOMMENDATION:
        print(
            f"\n样本只有 {sample} 题（< {MIN_SAMPLE_FOR_RECOMMENDATION}），**不给推荐阈值**。"
            "这个样本量只能看趋势，换个题就可能反过来。"
        )
    else:
        best_cov = max(r["avg_coverage"] for r in answered)
        near = [r for r in answered if r["avg_coverage"] >= best_cov - COVERAGE_TOLERANCE]
        # 等价组只留一个代表（组内取最小阈值，偏保守），避免用噪声挑出更严的那档
        seen: set = set()
        candidates = []
        for row in near:
            key = tuple(sorted(equivalent.get(row["threshold"], [row["threshold"]])))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(row)
        pick = min(candidates, key=lambda r: r["avg_chars"] or 0)
        same = equivalent.get(pick["threshold"])
        extra = f"；{' / '.join(f'{t:.2f}' for t in same)} 与本值等价" if same else ""
        print(
            f"\n覆盖率不差于最优 {best_cov:.3f}（容差 {COVERAGE_TOLERANCE}）的阈值里，"
            f"上下文最省的是 {pick['threshold']:.2f}"
            f"（覆盖 {pick['avg_coverage']:.3f}、{pick['avg_chars']} 字符）{extra}。"
        )

    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {"config": vars(args), "thresholds": thresholds, "records": records,
                 "summary": summary},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        print(f"\n报告已写入：{args.out}")

    if failures:
        print(f"\n{failures} 题失败，其余结果仍有效（见各题 [FAIL]）。", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="采集整节回填的判据：小节父块分数 + 各阈值下的上下文与答案覆盖率",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "题目文件示例：\n"
            '  {"kb_ids": ["<kb_id>"], "thresholds": [0, 0.3, 0.4, 0.5],\n'
            '   "questions": [{"question": "...", "facts": ["restore.sh", "停写"]}]}\n'
        ),
    )
    ap.add_argument("--questions", help="题目 JSON 文件（见上方格式）")
    ap.add_argument("--q", action="append", default=[], help="直接给一道题，可重复")
    ap.add_argument("--kb", action="append", default=[], help="知识库 id，可重复（题面未指定时用）")
    ap.add_argument("--top-k", type=int, default=None, help="检索窗口，默认取线上 TOP_K")
    ap.add_argument(
        "--answer",
        action="store_true",
        help="真跑问答并按 facts 算覆盖率（慢，每题每阈值一次生成）",
    )
    ap.add_argument(
        "--rounds",
        type=int,
        default=1,
        help=(
            "每格跑几次取平均。同一份上下文实测可差 0.33（生成噪声），"
            "要比较相差 0.33 以内的阈值必须 ≥3；每轮都会先清语义缓存"
        ),
    )
    ap.add_argument("--out", help="把完整结果另存为 JSON")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_k is None:
        args.top_k = settings.processing.TOP_K
    try:
        return asyncio.run(main_async(args))
    except InputError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
