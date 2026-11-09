"""重建 BM25 稀疏索引：全量重拟合词表 + 重算存量 sparse + 原地 upsert。

为什么需要这个脚本
------------------
2026-11-09 排查「泛问命中率」时发现：生产 `documents` 集合有 532 条切片，但
持久化词表是在 **5 条删除演练文本** 上 fit 的（`corpus_size=5`，59 个词）。
后果不是报错，而是：

- 真实文档里的词几乎全部不在词表内 → `encode_queries` 得到全零向量 →
  稀疏通道整条跳过 → 混合检索静默退化成 dense 单通道；
- 存量 532 条切片的 sparse 向量也是用这份小词表编码的：平均只有 3.9 个非零维
  （新词表下是 38.1），且命中的多是「文档/内容」这类高频噪声词。
  注意**非空率是骗人的**——它们 100% 非空，只是没有区分度。

`convert_sparse_embeddings` 的负权重截断只解决了「请求被 Milvus 拒绝」，
没有解决「词表覆盖不到语料」。本脚本做后一半：

1. 读出集合里全部切片（含**原 dense 向量**）；
2. 用全部 content 重新 fit BM25（词表 / IDF / avgdl 全部换新）；
3. 用新词表重算每条切片的 sparse（负权重按生产同一函数截断）；
4. 按原 id 原地 upsert（Milvus 没有局部更新，必须带原 dense —— 重算 dense
   既慢，也会让「向量漂移」混进这次只该改稀疏通道的变更里）。

安全设计
--------
- 默认 **预演**：只读，只报「新旧词表的稀疏覆盖率」，`--commit` 才写；
- 写之前把旧词表备份成 `<path>.bak.<时间戳>`，便于回滚取证；
- 幂等：同一语料 fit 出同一词表，同 id upsert 覆盖，失败可原样重跑；
- 自检：flush 后用切片自身内容做一次稀疏检索，命中自己才算「写入端/查询端
  编码空间一致」；upsert 前后行数必须相等（upsert 不能变成 insert）。

用法（生产容器内）
------------------
    python scripts/rebuild_sparse_index.py                    # 预演
    python scripts/rebuild_sparse_index.py --commit           # 执行
    python scripts/rebuild_sparse_index.py --commit --probe "备份" --probe "重排序"

执行后必须重启 backend：运行中的进程仍在用旧词表编码查询，落盘的新词表要等
重启 `_load_bm25_vocabulary` 才会生效。

退出码：0 成功（含预演）/ 1 执行失败或自检不通过 / 2 前置条件不满足（拒绝执行）
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 将 backend 目录加入路径，以便导入 src 模块（沿用 scripts/ 下其他脚本的约定）
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from pymilvus import MilvusClient  # noqa: E402

from src.config import settings  # noqa: E402
from src.services.milvus_common import (  # noqa: E402
    BM25_VOCAB_SUSPECT_RATIO as SUSPECT_RATIO,
    BM25EmbeddingFunction,
    build_default_analyzer,
)
from src.services.milvus_writer import (  # noqa: E402
    bm25_vocab_path,
    convert_sparse_embeddings,
)

# upsert 需要写回的标量字段；不存在于集合里的字段会自动跳过（兼容旧集合）
SCALAR_FIELDS = (
    "id",
    "kb_id",
    "document_id",
    "content",
    "source",
    "chunk_index",
    "heading_path",
    "source_kind",
)

# 少于这个行数不做 fit：小语料上拟合正是本次事故的成因，脚本不该再生产一份
MIN_ROWS = 20
# 词表小于这个规模基本可以断定语料没读全或分词失效
MIN_VOCAB = 50

SEARCH_PARAMS = {"metric_type": "IP", "params": {"drop_ratio_search": 0.2}}


class PreconditionError(RuntimeError):
    """前置条件不满足：继续执行会写出比现状更差的索引，直接拒绝。"""


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #
def _connect() -> MilvusClient:
    return MilvusClient(
        uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
        db_name=settings.milvus.MILVUS_DATABASE,
    )


def count_rows(client: MilvusClient, collection: str) -> int:
    rows = client.query(collection_name=collection, filter="", output_fields=["count(*)"])
    return int(rows[0]["count(*)"] if rows else 0)


def collection_fields(client: MilvusClient, collection: str) -> List[str]:
    info = client.describe_collection(collection_name=collection)
    return [f["name"] for f in info.get("fields", [])]


def fetch_rows(
    client: MilvusClient,
    collection: str,
    available: Sequence[str],
    total: int,
    batch_size: int = 200,
) -> List[Dict[str, Any]]:
    """分页读出全部切片（含 dense 向量）。

    必须读全：只读一半会 fit 出偏小的词表，把「小语料词表」事故重演一遍。
    """
    output_fields = [f for f in SCALAR_FIELDS + ("embedding",) if f in available]
    rows: List[Dict[str, Any]] = []
    offset = 0
    while offset < total:
        part = client.query(
            collection_name=collection,
            filter="",
            output_fields=output_fields,
            limit=batch_size,
            offset=offset,
        )
        if not part:
            break
        rows.extend(part)
        offset += len(part)
    return rows


# --------------------------------------------------------------------------- #
# 词表与稀疏编码
# --------------------------------------------------------------------------- #
def read_vocab_meta(path: str) -> Optional[Dict[str, Any]]:
    """读取词表文件的元信息（不加载模型）。"""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return {
        "path": path,
        "corpus_size": int(payload.get("corpus_size") or 0),
        "vocab_size": len(payload.get("idf_word") or []),
        "avgdl": payload.get("avgdl"),
        "size_bytes": os.path.getsize(path),
    }


def load_vocab_ef(path: str) -> BM25EmbeddingFunction:
    ef = BM25EmbeddingFunction(build_default_analyzer(language="zh"))
    ef.load(path)
    return ef


def fit_vocab(texts: Sequence[str]) -> BM25EmbeddingFunction:
    ef = BM25EmbeddingFunction(build_default_analyzer(language="zh"))
    ef.fit(list(texts))
    return ef


def encode_sparse(ef: BM25EmbeddingFunction, texts: Sequence[str], batch_size: int = 128) -> List[Dict[int, float]]:
    """按生产同一规则编码：负权重截断（否则 Milvus 拒绝整条稀疏向量）。"""
    vectors: List[Dict[int, float]] = []
    for start in range(0, len(texts), batch_size):
        raw = ef.encode_documents(list(texts[start:start + batch_size]))
        vectors.extend(convert_sparse_embeddings(raw))
    return vectors


def query_coverage(ef: BM25EmbeddingFunction, queries: Sequence[str]) -> List[Dict[str, Any]]:
    """查询词在词表内的覆盖情况。

    这是比「存量向量非空率」更贴近症状的指标：稀疏通道失效时，请求仍然成功，
    只是查询向量全零、被 `search_sparse` 直接跳过。旧词表下真实提问的 nnz 基本为 0。
    """
    results = []
    for query in queries:
        vector = convert_sparse_embeddings(ef.encode_queries([query]))
        results.append({"query": query, "nnz": len(vector[0]) if vector and vector[0] else 0})
    return results


def sparse_stats(vectors: Sequence[Dict[int, float]]) -> Dict[str, Any]:
    nonempty = sum(1 for vec in vectors if vec)
    nnz = sum(len(vec) for vec in vectors)
    return {
        "rows": len(vectors),
        "nonempty": nonempty,
        "empty": len(vectors) - nonempty,
        "ratio": (nonempty / len(vectors)) if vectors else 0.0,
        "avg_nnz": (nnz / len(vectors)) if vectors else 0.0,
    }


def is_suspect(corpus_size: int, rows: int) -> bool:
    """词表是否明显欠拟合（用于启动告警与脚本报告，保持同一判据）。"""
    return bool(corpus_size) and rows > 0 and corpus_size * SUSPECT_RATIO < rows


# --------------------------------------------------------------------------- #
# 前置校验与写入
# --------------------------------------------------------------------------- #
def check_preconditions(rows: Sequence[Dict[str, Any]], available: Sequence[str], vocab_size: int) -> None:
    if "sparse_embedding" not in available:
        raise PreconditionError(
            "集合没有 sparse_embedding 字段，请先执行 scripts/migrate_hybrid_index.py 迁移集合"
        )
    if len(rows) < MIN_ROWS:
        raise PreconditionError(
            f"集合仅 {len(rows)} 条切片（阈值 {MIN_ROWS}），在小语料上重拟合会重演本次事故"
        )
    if vocab_size < MIN_VOCAB:
        raise PreconditionError(
            f"重拟合得到 {vocab_size} 个词（阈值 {MIN_VOCAB}），语料可能没读全或分词失效"
        )

    ids = [str(row.get("id")) for row in rows]
    if len(set(ids)) != len(ids):
        raise PreconditionError("集合内存在重复主键，upsert 会互相覆盖，请先排查数据")

    dims = {len(row["embedding"]) for row in rows if row.get("embedding")}
    if len(dims) != 1:
        raise PreconditionError(f"dense 向量维度不一致或缺失: {sorted(dims)}")


def backup_vocab(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    backup = f"{path}.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    return backup


def save_vocab(ef: BM25EmbeddingFunction, path: str) -> None:
    """原子落盘：先写临时文件再替换，避免服务重启时读到半个 JSON。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    ef.save(tmp_path)
    os.replace(tmp_path, path)


def upsert_rows(
    client: MilvusClient,
    collection: str,
    rows: Sequence[Dict[str, Any]],
    sparse_vectors: Sequence[Dict[int, float]],
    available: Sequence[str],
    batch_size: int = 100,
) -> int:
    written = 0
    for start in range(0, len(rows), batch_size):
        batch_rows = rows[start:start + batch_size]
        batch_sparse = sparse_vectors[start:start + batch_size]
        data = []
        for row, sparse in zip(batch_rows, batch_sparse):
            item: Dict[str, Any] = {
                "id": str(row["id"]),
                "content": row["content"],
                "embedding": row["embedding"],
            }
            if "kb_id" in available:
                item["kb_id"] = row.get("kb_id") or ""
            if "document_id" in available:
                item["document_id"] = row.get("document_id") or ""
            if "source" in available:
                item["source"] = row.get("source") or ""
            if "chunk_index" in available:
                item["chunk_index"] = int(row.get("chunk_index") or 0)
            if "heading_path" in available:
                item["heading_path"] = row.get("heading_path") or ""
            if "source_kind" in available:
                item["source_kind"] = row.get("source_kind") or "raw"
            item["sparse_embedding"] = sparse
            data.append(item)
        client.upsert(collection_name=collection, data=data)
        written += len(data)
        print(f"  upsert {written}/{len(rows)}")
    return written


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def pick_self_check_rows(rows: Sequence[Dict[str, Any]], sample: int) -> List[Dict[str, Any]]:
    """挑正文足够长的切片做自检索：太短的切片信息量不足，命中不了自己不代表索引坏了。"""
    candidates = [row for row in rows if len(str(row.get("content") or "")) >= 80]
    if not candidates or sample <= 0:
        return []
    step = max(1, len(candidates) // sample)
    return candidates[::step][:sample]


def self_check(
    client: MilvusClient,
    collection: str,
    ef: BM25EmbeddingFunction,
    rows: Sequence[Dict[str, Any]],
    k: int = 5,
) -> List[Dict[str, Any]]:
    results = []
    for row in rows:
        query = str(row.get("content") or "")[:200]
        query_vector = convert_sparse_embeddings(ef.encode_queries([query]))
        if not query_vector or not query_vector[0]:
            results.append({"id": str(row["id"]), "hit": False, "reason": "query_empty"})
            continue
        hits = client.search(
            collection_name=collection,
            data=query_vector,
            anns_field="sparse_embedding",
            search_params=SEARCH_PARAMS,
            limit=k,
            output_fields=["id"],
        )
        ids = [str(hit["id"]) for hit in (hits[0] if hits else [])]
        results.append({"id": str(row["id"]), "hit": str(row["id"]) in ids, "top": ids})
    return results


def run_probes(client: MilvusClient, collection: str, ef: BM25EmbeddingFunction, queries: Sequence[str], k: int = 5) -> List[Dict[str, Any]]:
    results = []
    for query in queries:
        query_vector = convert_sparse_embeddings(ef.encode_queries([query]))
        if not query_vector or not query_vector[0]:
            results.append({"query": query, "nnz": 0, "hits": [], "note": "查询词全部不在词表"})
            continue
        hits = client.search(
            collection_name=collection,
            data=query_vector,
            anns_field="sparse_embedding",
            search_params=SEARCH_PARAMS,
            limit=k,
            output_fields=["id", "document_id", "source"],
        )
        results.append({
            "query": query,
            "nnz": len(query_vector[0]),
            "hits": [
                {
                    "id": str(hit["id"]),
                    "score": round(float(hit.get("distance", 0.0)), 4),
                    "source": hit.get("source") or "",
                }
                for hit in (hits[0] if hits else [])
            ],
        })
    return results


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="重建 BM25 稀疏索引（全量重拟合 + 重算 + upsert）")
    parser.add_argument("--commit", action="store_true", help="真正写入；不加则只预演（只读）")
    parser.add_argument("--vocab-path", default="", help="覆盖词表落盘路径（默认与运行时同一规则）")
    parser.add_argument("--batch-size", type=int, default=100, help="upsert 批大小")
    parser.add_argument("--sample", type=int, default=3, help="自检索校验的切片数（0 关闭）")
    parser.add_argument("--probe", action="append", default=[], help="额外探针查询，可重复")
    parser.add_argument("--report", default="", help="把结果写成 JSON 报告")
    return parser


def _print_stats(label: str, stats: Dict[str, Any]) -> None:
    print(
        f"{label}: 非空 {stats['nonempty']}/{stats['rows']} "
        f"({stats['ratio']:.1%})，平均非零维 {stats['avg_nnz']:.1f}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    collection = settings.milvus.MILVUS_COLLECTION_NAME
    vocab_path = args.vocab_path or bm25_vocab_path()
    started = time.time()

    print(f"集合: {collection}")
    print(f"词表路径: {vocab_path}")
    print(f"模式: {'执行（--commit）' if args.commit else '预演（只读）'}")

    client = _connect()
    report: Dict[str, Any] = {
        "collection": collection,
        "vocab_path": vocab_path,
        "mode": "commit" if args.commit else "dry-run",
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        total = count_rows(client, collection)
        print(f"集合行数: {total}")
        if total == 0:
            raise PreconditionError("集合为空，无可重建内容")

        available = collection_fields(client, collection)
        rows = fetch_rows(client, collection, available, total)
        print(f"已读取: {len(rows)} 条（含 dense 向量）")
        if len(rows) != total:
            raise PreconditionError(
                f"只读到 {len(rows)}/{total} 条，分页读取不完整；在小语料上重拟合会把事故重演一遍"
            )

        texts = [str(row.get("content") or "") for row in rows]
        blank = sum(1 for text in texts if not text.strip())
        if blank:
            print(f"警告: {blank} 条切片正文为空，它们只能拿到空 sparse 向量")

        old_meta = read_vocab_meta(vocab_path)
        if old_meta:
            print(
                f"旧词表: corpus_size={old_meta['corpus_size']}, "
                f"vocab={old_meta['vocab_size']}, avgdl={old_meta['avgdl']}"
            )
            if is_suspect(old_meta["corpus_size"], total):
                print(
                    f"  ⚠ 旧词表明显欠拟合（corpus_size*{SUSPECT_RATIO} < 行数）："
                    "查询词大概率全部落在词表外，稀疏通道会被整条跳过"
                )
        else:
            print("旧词表: 不存在（服务当前无法做稀疏检索）")
        report["old_vocab"] = old_meta

        # 旧词表下的覆盖率：这是「病得多重」的基线，也是修复效果的对照
        old_ef = None
        old_stats = None
        if old_meta:
            try:
                old_ef = load_vocab_ef(vocab_path)
                old_stats = sparse_stats(encode_sparse(old_ef, texts))
                _print_stats("旧词表编码存量切片", old_stats)
            except Exception as exc:  # 旧词表损坏不应拦住修复
                print(f"警告: 旧词表无法加载，跳过基线统计: {exc}")
        report["old_sparse"] = old_stats

        ef = fit_vocab(texts)
        new_vocab_size = len(getattr(ef, "idf", {}) or {})
        print(
            f"新词表: corpus_size={getattr(ef, 'corpus_size', 0)}, "
            f"vocab={new_vocab_size}, avgdl={getattr(ef, 'avgdl', 0)}"
        )
        check_preconditions(rows, available, new_vocab_size)

        new_vectors = encode_sparse(ef, texts)
        new_stats = sparse_stats(new_vectors)
        _print_stats("新词表编码存量切片", new_stats)
        report["new_vocab"] = {
            "corpus_size": int(getattr(ef, "corpus_size", 0)),
            "vocab_size": new_vocab_size,
            "avgdl": getattr(ef, "avgdl", None),
        }
        report["new_sparse"] = new_stats

        if old_stats and new_stats["nonempty"] < old_stats["nonempty"]:
            raise PreconditionError(
                "新词表的非空稀疏向量反而更少，拒绝写入（先查分词与语料）"
            )

        # 查询侧覆盖率：这才是「稀疏通道到底参不参与」的直接证据。
        # 只报存量向量的非空率会得出「一切正常」的错误结论——它们 100% 非空。
        if args.probe:
            old_coverage = query_coverage(old_ef, args.probe) if old_ef is not None else None
            new_coverage = query_coverage(ef, args.probe)
            report["query_coverage"] = {"old": old_coverage, "new": new_coverage}
            for index, item in enumerate(new_coverage):
                old_nnz = "n/a" if old_coverage is None else old_coverage[index]["nnz"]
                print(f"  查询覆盖 {item['query']!r}: 旧词表 nnz={old_nnz} → 新词表 nnz={item['nnz']}")

        if not args.commit:
            print("\n预演结束，未写入任何数据。确认数字后加 --commit 执行。")
            report["elapsed_sec"] = round(time.time() - started, 2)
            _write_report(args.report, report)
            return 0

        backup = backup_vocab(vocab_path)
        if backup:
            print(f"旧词表已备份: {backup}")
        report["vocab_backup"] = backup
        save_vocab(ef, vocab_path)
        print(f"新词表已落盘: {vocab_path}")

        written = upsert_rows(client, collection, rows, new_vectors, available, batch_size=args.batch_size)
        client.flush(collection_name=collection)
        # flush 之后给索引一点可见性时间，避免自检读到旧段
        time.sleep(1.0)

        after = count_rows(client, collection)
        print(f"upsert 完成: {written} 条；集合行数 {total} → {after}")
        if after != total:
            raise RuntimeError(f"upsert 改变了行数（{total} → {after}），请立即检查是否误插入")

        checks = self_check(client, collection, ef, pick_self_check_rows(rows, args.sample))
        report["self_check"] = checks
        if checks:
            passed = sum(1 for item in checks if item["hit"])
            for item in checks:
                print(f"  自检索 {item['id']}: {'命中' if item['hit'] else '未命中'} {item.get('top', '')}")
            if passed * 2 <= len(checks):
                raise RuntimeError(
                    f"自检索仅 {passed}/{len(checks)} 命中：写入端与查询端的编码空间可能不一致"
                )

        if args.probe:
            probes = run_probes(client, collection, ef, args.probe)
            report["probes"] = probes
            for item in probes:
                print(f"  探针 {item['query']!r}: nnz={item['nnz']} → {item['hits'][:3]}")

        print("\n完成。请重启 backend 让运行中的进程加载新词表：")
        print("  docker restart backend-prod        # 生产")
        report["elapsed_sec"] = round(time.time() - started, 2)
        report["status"] = "ok"
        _write_report(args.report, report)
        return 0
    except PreconditionError as exc:
        print(f"\n拒绝执行: {exc}")
        report["status"] = "rejected"
        report["error"] = str(exc)
        _write_report(args.report, report)
        return 2
    except Exception as exc:  # noqa: BLE001 - 运维脚本需要把任何失败都转成可读输出
        print(f"\n执行失败: {exc}")
        report["status"] = "failed"
        report["error"] = str(exc)
        _write_report(args.report, report)
        return 1
    finally:
        client.close()


def _write_report(path: str, report: Dict[str, Any]) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"报告已写入: {path}")


if __name__ == "__main__":
    sys.exit(main())
