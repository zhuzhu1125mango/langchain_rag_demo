"""迁移脚本：sessions.messages 列 JSON → JSONB。

背景：消息追加已改为 jsonb || 原子拼接（src/services/session_service.py），
该运算符仅存在于 JSONB 类型。新建库由 create_all 直接建为 JSONB；
存量库需执行本脚本一次性迁移。

用法（在 backend 目录、激活虚拟环境后）：
    python scripts/migrate_session_messages_jsonb.py

脚本幂等：列已是 JSONB 时输出提示并退出。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from src.database import async_engine


CHECK_SQL = text(
    """
    SELECT data_type FROM information_schema.columns
    WHERE table_name = 'sessions' AND column_name = 'messages'
    """
)
ALTER_SQL = text("ALTER TABLE sessions ALTER COLUMN messages TYPE JSONB USING messages::jsonb")


async def main() -> None:
    async with async_engine.begin() as conn:
        result = await conn.execute(CHECK_SQL)
        row = result.scalar_one_or_none()
        if row is None:
            print("未找到 sessions.messages 列，请确认已执行建表（init_db）。")
            sys.exit(1)
        if row == "jsonb":
            print("sessions.messages 已是 JSONB，无需迁移。")
            return
        await conn.execute(ALTER_SQL)
        print(f"迁移完成: sessions.messages {row} -> jsonb")


if __name__ == "__main__":
    asyncio.run(main())
