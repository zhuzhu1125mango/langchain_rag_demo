"""重灌已入库文档：用新源文件替换对象存储正文，并重建该文档的全部切片。

为什么需要这个脚本
------------------
2026-11-10 盘点生产轨迹时发现：生产 `documents` 集合是 **11-04 入库的 532 块**，
而仓库里同样这 4 篇文档当前切出 **606 块**。逐篇对账后确认差异全部来自文档内容更新，
不是切分器漂移：

| 文档 | 生产(11-04) | 仓库现在 | 块数 |
|---|---|---|---|
| architecture.md | 19903 B | 19903 B | 71 → 71（未变） |
| monitoring.md | 18036 B | 18036 B | 67 → 67（未变） |
| deployment.md | 57681 B | 70654 B | 296 → 332 |
| backup-restore.md | 28275 B | 44635 B | 98 → 136 |

两篇未变文档在**同一镜像、同一参数**下块数逐一相同，是「切分器稳定」的直接证据；
另两篇字节数明显变大（新增 §7.8 等章节）。回答基于旧版文档是**正确性**问题，
不能靠调检索参数掩盖，所以需要重灌。

两个必须知道的实现事实
----------------------
1. **切片 id 是 `uuid.uuid4()`，不是内容哈希。** `insert_embeddings` 每次插入都生成
   新 id，`add_documents` 只追加不覆盖 —— 不先删旧向量就会叠加一整份重复块
   （同一段文字出现两次，检索命中后再去重也补不回来）。所以本脚本的固定顺序是：
   **备份 → 覆写对象存储 → 删向量并校验为 0 → 重新解析写入**。
2. **`process_document` 读的是 `doc.file_path`（`minio://...`），不是仓库磁盘。**
   只改仓库文件、重新触发索引，读到的还是旧正文。必须先覆写同一个对象键，
   正文才真的换掉。

安全设计
--------
- 默认**预演**：只解析新源文件、报块数对比，不写任何东西；`--commit` 才动手；
- 动手前先把旧对象备份到 `--backup-dir`（默认 `/app/data/reingest-backup`），
  备份失败或大小不符直接 abort —— 没有退路就不动生产；
- 新块数为 0、或比现状少一半以上（拿错文件的典型症状）→ 拒绝执行，
  确属删减时用 `--allow-shrink` 显式放行；
- 删除后仍有残留向量 → 立即停手，**不做写入**（否则就是「旧的没删掉、新的已加上」，
  比不重灌更糟）；
- 写完校验：该文档在集合内的行数 == 新块数，且 `documents.chunks_count` == 新块数。

执行之后
--------
必须再跑一次 `rebuild_sparse_index.py --commit`：新块里的新词不在旧词表内，
稀疏通道会漏掉它们（词表冻结设计的固有代价），并重启 backend 让新词表生效。

用法（生产容器内）
------------------
    python scripts/reingest_documents.py --doc deployment.md=/tmp/deployment.md              # 预演
    python scripts/reingest_documents.py --doc deployment.md=/tmp/deployment.md --commit
    python scripts/reingest_documents.py --doc a.md=/tmp/a.md --doc b.md=/tmp/b.md --commit

退出码：0 成功（含预演）/ 1 执行失败或自检不通过 / 2 前置条件不满足（拒绝执行）
"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# 将 backend 目录加入路径，以便导入 src 模块（沿用 scripts/ 下其他脚本的约定）
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from pymilvus import MilvusClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from src.config import settings  # noqa: E402
from src.database import async_session_maker  # noqa: E402
from src.models import Document  # noqa: E402
from src.services.document_processor import process_document  # noqa: E402
from src.services.minio_service import MinioService  # noqa: E402
from src.services.progress_manager import (  # noqa: E402
    create_upload_progress,
    update_upload_progress,
)

DEFAULT_BACKUP_DIR = "/app/data/reingest-backup"
MINIO_SCHEME = "minio://"

# 块数腰斩阈值：低于现状的这个比例就认为「拿错文件了」，默认拒绝
SHRINK_RATIO = 0.5


class PreconditionError(RuntimeError):
    """前置条件不满足：继续执行会把生产数据写坏，直接拒绝。"""


# --------------------------------------------------------------------------- #
# 纯函数（可单测，不碰网络与数据库）
# --------------------------------------------------------------------------- #
def minio_key(file_path: str) -> str:
    """``minio://bucket/documents/x/source.md`` → ``documents/x/source.md``。

    MinIO 客户端的方法只认对象键，不认带桶名的 URI；桶名由 `minio_service` 自己持有。
    """
    if not file_path.startswith(MINIO_SCHEME):
        raise PreconditionError(f"文件路径不是对象存储地址，无法覆写：{file_path}")
    rest = file_path[len(MINIO_SCHEME):]
    if "/" not in rest:
        raise PreconditionError(f"对象存储地址缺少桶名：{file_path}")
    _bucket, key = rest.split("/", 1)
    if not key:
        raise PreconditionError(f"对象存储地址缺少对象键：{file_path}")
    return key


def parse_doc_specs(specs: List[str]) -> List[Tuple[str, str]]:
    """``["deployment.md=/tmp/deployment.md"]`` → ``[("deployment.md", "/tmp/deployment.md")]``。

    用「文件名=路径」而不是两个平行参数，是为了多篇一起重灌时不至于错位配错。
    """
    parsed: List[Tuple[str, str]] = []
    for spec in specs:
        if "=" not in spec:
            raise PreconditionError(f"--doc 需要 `文件名=源路径` 形式，收到：{spec}")
        filename, source = spec.split("=", 1)
        filename, source = filename.strip(), source.strip()
        if not filename or not source:
            raise PreconditionError(f"--doc 的文件名或路径为空：{spec}")
        if not os.path.isfile(source):
            raise PreconditionError(f"源文件不存在：{source}")
        parsed.append((filename, source))
    if not parsed:
        raise PreconditionError("至少要给一个 --doc")
    return parsed


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_shrink(old_count: int, new_count: int, allow_shrink: bool) -> None:
    """块数异常变化的前置校验。

    重灌最常见的生产事故不是「写失败」，而是**拿错文件**：用一篇短文档覆盖了长文档，
    或者用 README 覆盖了详细手册。此时删除会成功、写入会成功、校验也自洽，
    唯一的信号就是「块数突然少了一大截」。所以把这个信号做成硬闸。
    """
    if new_count <= 0:
        raise PreconditionError("新源文件切出 0 个块（空文件或解析失败），拒绝执行")
    if old_count > 0 and new_count < old_count * SHRINK_RATIO and not allow_shrink:
        raise PreconditionError(
            f"新块数 {new_count} 不到现状 {old_count} 的 {SHRINK_RATIO:.0%}，"
            "疑似拿错源文件；确认是有意删减请加 --allow-shrink"
        )


# --------------------------------------------------------------------------- #
# Milvus
# --------------------------------------------------------------------------- #
def _connect() -> MilvusClient:
    return MilvusClient(
        uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
        db_name=settings.milvus.MILVUS_DATABASE,
    )


def count_by_document(client: MilvusClient, collection: str, doc_id: str) -> int:
    """该文档在集合内的切片数。"""
    uuid.UUID(str(doc_id))  # 非法 id 直接抛，避免拼出能匹配空串的表达式
    rows = client.query(
        collection_name=collection,
        filter=f"document_id == '{doc_id}'",
        output_fields=["count(*)"],
    )
    return int(rows[0]["count(*)"] if rows else 0)


def delete_by_document(client: MilvusClient, collection: str, doc_id: str) -> None:
    uuid.UUID(str(doc_id))
    client.delete(collection_name=collection, filter=f"document_id == '{doc_id}'")
    client.flush(collection_name=collection)


# --------------------------------------------------------------------------- #
# 单篇处理
# --------------------------------------------------------------------------- #
async def _fetch_document(filename: str) -> Document:
    async with async_session_maker() as db:
        rows = (
            await db.execute(select(Document).where(Document.filename == filename))
        ).scalars().all()
    if not rows:
        raise PreconditionError(f"数据库里没有文件名为 {filename} 的文档")
    if len(rows) > 1:
        raise PreconditionError(
            f"文件名 {filename} 对应 {len(rows)} 篇文档，无法确定重灌哪一篇"
            "（请先按 id 清理重复上传）"
        )
    return rows[0]


async def _set_document_size(doc_id: str, size: int) -> None:
    async with async_session_maker() as db:
        doc = (
            await db.execute(select(Document).where(Document.id == uuid.UUID(doc_id)))
        ).scalars().first()
        if doc:
            doc.size = size
            await db.commit()


async def _backup_object(key: str, dest_dir: str, filename: str) -> Tuple[str, int]:
    """把对象存储里的旧正文下载到本地备份目录，返回 (路径, 字节数)。"""
    os.makedirs(dest_dir, exist_ok=True)
    target = os.path.join(dest_dir, filename)
    svc = await MinioService.get_instance()
    # `open_object` 是同步生成器（逐块产出），不是带 .read() 的响应对象
    data = b"".join(svc.open_object(key))
    if not data:
        raise PreconditionError(f"旧对象为空，备份不可信，拒绝继续：{key}")
    with open(target, "wb") as fh:
        fh.write(data)
    written = os.path.getsize(target)
    if written != len(data):
        raise PreconditionError(f"备份大小不符（写 {written} / 读 {len(data)}），拒绝继续")
    return target, written


async def reingest_one(
    filename: str,
    source_path: str,
    *,
    commit: bool,
    backup_root: str,
    allow_shrink: bool,
) -> Dict[str, Any]:
    doc = await _fetch_document(filename)
    doc_id = str(doc.id)
    collection = settings.milvus.MILVUS_COLLECTION_NAME

    with open(source_path, "rb") as fh:
        new_bytes = fh.read()
    if not new_bytes.strip():
        raise PreconditionError(f"源文件为空：{source_path}")

    # 预演阶段就用**生产同一个解析函数**，避免「预估块数」和「实际块数」两套口径
    t0 = time.time()
    chunks = await asyncio.to_thread(process_document, source_path)
    parse_sec = time.time() - t0
    new_count = len(chunks)

    client = _connect()
    try:
        old_vectors = count_by_document(client, collection, doc_id)
    finally:
        client.close()

    old_chunks_count = int(doc.chunks_count or 0)
    old_size = int(doc.size or 0)
    check_shrink(old_vectors or old_chunks_count, new_count, allow_shrink)

    print(f"\n=== {filename} ===")
    print(f"  文档 id        : {doc_id}")
    print(f"  状态           : {doc.status}/{doc.processing_status}")
    print(f"  正文大小       : {old_size} B → {len(new_bytes)} B")
    print(f"  记录块数       : {old_chunks_count} → {new_count}（解析耗时 {parse_sec:.1f}s）")
    print(f"  集合内实际行数 : {old_vectors} → 预期 {new_count}")

    if old_size == len(new_bytes):
        print("  ⚠ 字节数与现状相同：源文件可能没更新，确认后再 --commit")

    result: Dict[str, Any] = {
        "filename": filename,
        "doc_id": doc_id,
        "old_size": old_size,
        "new_size": len(new_bytes),
        "old_chunks": old_chunks_count,
        "old_vectors": old_vectors,
        "new_chunks": new_count,
        "parse_sec": round(parse_sec, 2),
        "committed": False,
    }

    if not commit:
        print("  预演模式：未写入任何数据（加 --commit 执行）")
        return result

    # ---- 1. 备份旧正文（没有退路就不动生产） ----
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    key = minio_key(doc.file_path or "")
    backup_dir = os.path.join(backup_root, stamp, doc_id)
    backup_path, backup_size = await _backup_object(key, backup_dir, os.path.basename(key))
    print(f"  ① 旧正文已备份 : {backup_path}（{backup_size} B）")

    # ---- 2. 覆写对象存储（必须先于重新解析，否则读到的还是旧内容） ----
    svc = await MinioService.get_instance()
    await svc.upload_text_async(key, new_bytes.decode("utf-8"))
    uploaded = svc.stat_object(key)
    uploaded_size = int(uploaded.get("size") or uploaded.get("Size") or 0)
    if uploaded_size != len(new_bytes):
        raise PreconditionError(
            f"对象存储覆写后大小不符（{uploaded_size} != {len(new_bytes)}），"
            "已中止，旧正文在备份目录可还原"
        )
    await _set_document_size(doc_id, len(new_bytes))
    print(f"  ② 正文已覆写   : {key}（{uploaded_size} B）")

    # ---- 3. 删旧向量并校验归零（不能叠着写） ----
    client = _connect()
    try:
        delete_by_document(client, collection, doc_id)
        residual = count_by_document(client, collection, doc_id)
    finally:
        client.close()
    if residual:
        raise PreconditionError(
            f"删除后仍有 {residual} 条残留向量，已停手（未写入新切片）"
        )
    print("  ③ 旧向量已清空 : 残留 0")

    # ---- 4. 走生产同一条后台任务路径重灌 ----
    from src.services.document_tasks import process_document_async  # 延迟导入：避免与 api 成环

    task_id = f"reingest_{doc_id}_{uuid.uuid4().hex[:8]}"
    create_upload_progress(task_id, filename, len(new_bytes), owner_id=doc.owner_id)
    update_upload_progress(task_id, status="processing", message="正在重灌文档...")

    t1 = time.time()
    await process_document_async(
        doc_id,
        doc.file_path or "",
        str(doc.kb_id) if doc.kb_id else "",
        task_id,
        None,
        None,
    )
    ingest_sec = time.time() - t1

    # ---- 5. 校验：集合行数与 DB 记录都必须等于新块数 ----
    client = _connect()
    try:
        new_vectors = count_by_document(client, collection, doc_id)
    finally:
        client.close()

    fresh = await _fetch_document(filename)
    if new_vectors != new_count:
        raise PreconditionError(
            f"写入后行数 {new_vectors} != 预期 {new_count}（重复块或写入丢失）"
        )
    if int(fresh.chunks_count or 0) != new_count:
        raise PreconditionError(
            f"documents.chunks_count={fresh.chunks_count} != 预期 {new_count}"
        )
    if fresh.processing_status != "completed":
        raise PreconditionError(
            f"文档处理状态是 {fresh.processing_status}（{fresh.processing_message}），重灌未成功"
        )

    result.update(
        {
            "committed": True,
            "new_vectors": new_vectors,
            "ingest_sec": round(ingest_sec, 2),
            "backup_path": backup_path,
            "backup_sha256": sha256_bytes(open(backup_path, "rb").read()),
            "new_sha256": sha256_bytes(new_bytes),
        }
    )
    print(f"  ④ 重灌完成     : {new_vectors} 条切片（{ingest_sec:.1f}s），校验通过")
    return result


async def main_async(args) -> int:
    specs = parse_doc_specs(args.doc)
    print("重灌文档" + ("（执行模式）" if args.commit else "（预演模式，不写入）"))
    print(f"  集合   : {settings.milvus.MILVUS_COLLECTION_NAME}")
    print(f"  切分   : {settings.processing.CHUNK_SIZE}/{settings.processing.CHUNK_OVERLAP}")

    results = []
    for filename, source in specs:
        results.append(
            await reingest_one(
                filename,
                source,
                commit=args.commit,
                backup_root=args.backup_dir,
                allow_shrink=args.allow_shrink,
            )
        )

    client = _connect()
    try:
        rows = client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter="",
            output_fields=["count(*)"],
        )
        total = int(rows[0]["count(*)"] if rows else 0)
    finally:
        client.close()

    print(f"\n集合总行数：{total}")
    if args.commit:
        print(
            "下一步必须执行：\n"
            "  1) python scripts/rebuild_sparse_index.py --commit   # 新词进词表，否则稀疏通道漏召回\n"
            "  2) docker restart backend-prod                       # 让新词表生效"
        )

    if args.report:
        payload = {
            "mode": "commit" if args.commit else "dry-run",
            "at": datetime.now().isoformat(timespec="seconds"),
            "collection": settings.milvus.MILVUS_COLLECTION_NAME,
            "collection_rows": total,
            "documents": results,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"报告已写入：{args.report}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="重灌已入库文档（覆写对象存储正文 + 重建该文档切片）"
    )
    parser.add_argument(
        "--doc",
        action="append",
        default=[],
        metavar="文件名=源路径",
        help="要重灌的文档，可重复；如 deployment.md=/tmp/deployment.md",
    )
    parser.add_argument("--commit", action="store_true", help="真正写入（默认仅预演）")
    parser.add_argument(
        "--backup-dir", default=DEFAULT_BACKUP_DIR, help=f"旧正文备份目录（默认 {DEFAULT_BACKUP_DIR}）"
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="允许新块数明显少于现状（默认拒绝，防止拿错文件覆盖）",
    )
    parser.add_argument("--report", default="", help="把结果写成 JSON 报告")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except PreconditionError as exc:
        print(f"\n拒绝执行：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 脚本顶层，任何异常都要变成退出码
        print(f"\n执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
