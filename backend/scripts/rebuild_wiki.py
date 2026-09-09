"""按知识库全量重编译 Wiki 页面（薄壳，核心逻辑在 src/services/wiki_rebuild.py）。

使用方法:
    cd backend && uv run python scripts/rebuild_wiki.py --kb-id <uuid>

注意：不受 WIKI_COMPILE_ENABLED 约束（该开关仅控制上传管线自动编译）；仅操作指定 KB。
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import async_session_maker
from src.services.wiki_rebuild import rebuild_kb_wiki


async def rebuild(kb_id: str) -> int:
    def progress(percent: int, message: str) -> None:
        print(f"[{percent:3d}%] {message}")

    print(f"=== Wiki 全量重编译: kb={kb_id} ===")
    async with async_session_maker() as db:
        stats = await rebuild_kb_wiki(db, kb_id, progress_cb=progress)
    print(
        f"=== 重编译完成: 文档 {stats['documents']} 篇, 清旧页 {stats['pages_wiped']}, "
        f"新建 {stats['pages_created']} 页, 更新 {stats['pages_updated']} 页, "
        f"入库 {stats['chunks_indexed']} 块 ==="
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="按知识库全量重编译 Wiki")
    parser.add_argument("--kb-id", required=True, help="知识库 ID")
    args = parser.parse_args()
    return asyncio.run(rebuild(args.kb_id))


if __name__ == "__main__":
    sys.exit(main())
