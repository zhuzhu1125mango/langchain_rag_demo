"""三方一致性巡检 CLI（只读，不删任何数据）。

比对 Milvus ↔ DB、MinIO ↔ DB、Redis（语义缓存）↔ DB，输出人类可读摘要 +
可选 JSON 报告，供运维排查与 nightly 任务消费。

用法：
    cd backend
    uv run python scripts/consistency_audit.py
    uv run python scripts/consistency_audit.py --json-out /tmp/audit.json --sample 10
    uv run python scripts/consistency_audit.py --skip-minio --skip-redis

退出码：
    0  未发现差异
    1  发现差异（详见报告）
    2  巡检本身失败（连不上 Milvus/MinIO/Redis，或 DB 快照读取失败）

安全约定：本脚本不做任何写入/删除。孤儿向量与无主对象的清理是破坏性操作，
必须先人工确认，再按报告里的 subject 另行走既定清理流程。
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.consistency_audit import KIND_LABELS, run_audit  # noqa: E402


def _print_report(report: dict, sample: int) -> None:
    if report.get("fatal"):
        print(f"巡检失败：{report['fatal']}")
        return

    db = report.get("db") or {}
    print(
        f"一致性巡检 {report.get('generated_at', '')}  "
        f"用时 {report.get('duration_seconds', 0)}s"
    )
    print(
        f"DB 基线：文档 {db.get('documents', 0)}（已发布 {db.get('published', 0)}） / "
        f"知识库 {db.get('knowledge_bases', 0)} / Wiki 页 {db.get('wiki_pages', 0)} / "
        f"引用对象 {db.get('referenced_objects', 0)}"
    )

    sections = report.get("sections") or {}
    for name in ("milvus", "minio", "redis"):
        section = sections.get(name)
        if not section:
            print(f"\n[{name}] 已跳过")
            continue

        scanned, total = section.get("scanned", 0), section.get("total")
        head = f"\n[{name}] 扫描 {scanned}"
        if total is not None and total != scanned:
            head += f"/{total}"
        if section.get("truncated"):
            head += "（不完整）"
        if section.get("error"):
            head += f"  ✗ {section['error']}"
        print(head)

        kinds = section.get("kinds") or {}
        if not kinds:
            # 采集失败时不要打「未发现差异」：那是假阴性，会误导值班的人
            if not section.get("error"):
                print("  ✓ 未发现差异")
            continue
        for kind, info in kinds.items():
            print(f"  ✗ {kind:<28} {info['count']:>4}  {info['label']}")
            for item in info["samples"]:
                extras = ", ".join(
                    f"{k}={v}" for k, v in item.items() if k != "subject" and v not in ("", None)
                )
                print(f"      - {item['subject']}" + (f"  ({extras})" if extras else ""))

    issue_count = report.get("issue_count", 0)
    code = report.get("exit_code", 0)
    print()
    if code == 2:
        print(f"结论：巡检未完整执行（发现 {issue_count} 条差异，另有采集失败），退出码 2")
    elif issue_count:
        print(f"结论：发现 {issue_count} 条差异（只读报告，未做任何修改），退出码 1")
    else:
        print("结论：三方一致，退出码 0")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Milvus / MinIO / Redis 与 DB 的一致性巡检（只读）",
    )
    parser.add_argument("--json-out", default="", help="把完整报告写成 JSON 到该路径")
    parser.add_argument("--sample", type=int, default=5, help="每类问题在报告中保留的样本数")
    parser.add_argument("--milvus-batch", type=int, default=500, help="Milvus 分页大小")
    parser.add_argument(
        "--max-milvus-offset", type=int, default=16000,
        help="Milvus 深分页上限（超过则报告标记不完整）",
    )
    parser.add_argument("--max-objects", type=int, default=200000, help="MinIO 对象遍历上限")
    parser.add_argument("--max-cache-keys", type=int, default=0, help="语义缓存 scope 扫描上限（0=不限）")
    parser.add_argument("--skip-milvus", action="store_true", help="跳过向量库比对")
    parser.add_argument("--skip-minio", action="store_true", help="跳过对象存储比对")
    parser.add_argument("--skip-redis", action="store_true", help="跳过语义缓存比对")
    parser.add_argument("--quiet", action="store_true", help="只输出结论行")
    args = parser.parse_args(argv)

    report = asyncio.run(
        run_audit(
            skip_milvus=args.skip_milvus,
            skip_minio=args.skip_minio,
            skip_redis=args.skip_redis,
            milvus_batch=args.milvus_batch,
            max_milvus_offset=args.max_milvus_offset,
            max_objects=args.max_objects,
            max_cache_keys=args.max_cache_keys,
            sample=args.sample,
        )
    )

    if args.quiet:
        print(json.dumps({k: report.get(k) for k in ("exit_code", "issue_count")}, ensure_ascii=False))
    else:
        _print_report(report, args.sample)
        if report.get("issue_count"):
            print("\n可用问题类型：" + "、".join(sorted(KIND_LABELS)))

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        if not args.quiet:
            print(f"\nJSON 报告已写入：{args.json_out}")

    return int(report.get("exit_code", 2))


if __name__ == "__main__":
    sys.exit(main())
