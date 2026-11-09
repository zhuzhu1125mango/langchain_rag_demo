#!/usr/bin/env python
"""创建或提升管理员账号（P0-2）。

背景：生产环境默认关闭注册（`AUTH_ALLOW_REGISTRATION=false`），而仓库此前
**没有任何建号手段**——空库部署完成后，无法产生一个可登录账号，管理页永远
打不开。本脚本补上这个缺口，也是「全局 ADMIN_KEY」之外的正常管理入口。

用法（容器内）::

    # 新建管理员（用户名不存在则创建）
    python scripts/create_admin.py --username admin --password 'Str0ng-Passw0rd!'

    # 已存在的账号提升为管理员（不重置密码）
    python scripts/create_admin.py --username alice --promote

    # 列出当前管理员
    python scripts/create_admin.py --list

    # 收回管理员权限
    python scripts/create_admin.py --username alice --demote

    # 重置密码（已有账号；这是唯一不需要手写 SQL 算 bcrypt 的通道）
    python scripts/create_admin.py --username admin --set-password

密码也可以从环境变量 ``CREATE_ADMIN_PASSWORD`` 读取，避免落在 shell 历史里::

    CREATE_ADMIN_PASSWORD='...' python scripts/create_admin.py --username admin

安全说明：
- 密码仍走 `validate_password_strength`，与注册接口同一套规则；
- 提权/降权都会写审计（`user.admin.grant` / `user.admin.revoke`），
  并记录操作者（本脚本以 ``cli:create_admin`` 标识，不伪装成某个用户）；
- 脚本只直接写库，不经过 HTTP 接口，因此不受限流与注册开关影响；
- `--set-password` **不会**打印密码，也不会吊销已签发的 JWT
  （`ACCESS_TOKEN_EXPIRE_MINUTES` 默认 60，最迟 1 小时后自然失效）。
  要立刻踢掉在线会话，当前只能重启后端或等过期——这是已知缺口，不是疏漏。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.auth import hash_password, verify_password  # noqa: E402
from src.database import async_session_maker  # noqa: E402
from src.models import User  # noqa: E402
from src.utils.security import WeakPasswordError, validate_password_strength  # noqa: E402

CLI_ACTOR = "cli:create_admin"


async def _record(action: str, user: User, detail: dict | None = None) -> None:
    """写审计（best-effort）。操作者记 CLI 标识，不假冒某个用户。"""
    from src.services.audit_service import record_audit

    await record_audit(
        action=action,
        resource_type="user",
        resource_id=str(user.id),
        detail={"username": user.username, **(detail or {})},
        actor_username=CLI_ACTOR,
    )


async def _list_admins() -> int:
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(User).where(User.is_admin.is_(True)).order_by(User.created_at)
            )
        ).scalars().all()

    if not rows:
        print("当前没有管理员账号。用 --username <名字> --password <密码> 创建一个。")
        return 0
    print(f"管理员共 {len(rows)} 个：")
    for row in rows:
        print(f"  - {row.username}  ({row.id})")
    return 0


async def _grant(username: str, password: str | None, promote_only: bool) -> int:
    async with async_session_maker() as db:
        user = await db.scalar(select(User).where(User.username == username))

        if user is None:
            if promote_only:
                print(f"用户 {username!r} 不存在，无法提升。去掉 --promote 可新建。", file=sys.stderr)
                return 2
            if not password:
                print("新建管理员需要 --password 或环境变量 CREATE_ADMIN_PASSWORD。", file=sys.stderr)
                return 2
            try:
                validate_password_strength(password, username)
            except WeakPasswordError as exc:
                print(f"密码强度不足：{exc}", file=sys.stderr)
                return 2
            user = User(
                username=username,
                password_hash=hash_password(password),
                is_admin=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            await _record("user.admin.create", user, {"bootstrap": True})
            print(f"已创建管理员：{username} ({user.id})")
            return 0

        if user.is_admin:
            print(f"{username} 已经是管理员，无需变更。")
            return 0

        user.is_admin = True
        await db.commit()
        await _record("user.admin.grant", user, {"via": CLI_ACTOR})
        print(f"已将 {username} 提升为管理员。")
        return 0


async def _revoke(username: str) -> int:
    async with async_session_maker() as db:
        user = await db.scalar(select(User).where(User.username == username))
        if user is None:
            print(f"用户 {username!r} 不存在。", file=sys.stderr)
            return 2
        if not user.is_admin:
            print(f"{username} 不是管理员，无需变更。")
            return 0

        remaining = (
            await db.execute(select(User.id).where(User.is_admin.is_(True)))
        ).scalars().all()
        if len(remaining) <= 1:
            # 没有管理员之后 create_admin 仍需能救场，但那要走 CLI；这里给提示而非静默
            print(
                "注意：这是最后一个管理员，降权后管理入口只能通过 scripts/create_admin.py 恢复。",
                file=sys.stderr,
            )

        user.is_admin = False
        await db.commit()
        await _record("user.admin.revoke", user, {"via": CLI_ACTOR})
        print(f"已收回 {username} 的管理员权限。")
        return 0


async def _set_password(username: str, password: str) -> int:
    """重置已有账号的密码。

    为什么要有这条路：此前全仓**没有任何改密能力**——既没有 `/change-password` 接口，
    脚本也只能建号/提权。于是「把初始密码换掉」这件事只能靠手写 SQL + 自己算 bcrypt，
    结果就是永远没人做（结项报告里挂了三条 owner 项，这是其中之一）。

    三重防呆，都是为了让「以为改了、其实没改」不可能发生：
    - 用户不存在 → 报错退出，不静默创建；
    - 新密码与当前密码相同 → 拒绝并明说（否则运维会以为已经轮换）；
    - 强度不足 → 与注册接口同一套 `validate_password_strength`。
    """
    async with async_session_maker() as db:
        user = await db.scalar(select(User).where(User.username == username))
        if user is None:
            print(f"用户 {username!r} 不存在。新建请去掉 --set-password。", file=sys.stderr)
            return 2

        try:
            validate_password_strength(password, username)
        except WeakPasswordError as exc:
            print(f"密码强度不足：{exc}", file=sys.stderr)
            return 2

        if verify_password(password, user.password_hash):
            print(
                f"新密码与 {username} 当前密码相同，未做任何修改。"
                "（轮换要用一个真正不同的密码）",
                file=sys.stderr,
            )
            return 2

        user.password_hash = hash_password(password)
        await db.commit()
        await _record("user.password.reset", user, {"via": CLI_ACTOR})

    print(f"已重置 {username} 的密码（不回显）。")
    print(
        "注意：已签发的 JWT 未吊销，最迟 60 分钟后自然失效"
        "（ACCESS_TOKEN_EXPIRE_MINUTES）。"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建 / 提升 / 收回管理员账号")
    parser.add_argument("--username", help="账号名")
    parser.add_argument("--password", help="仅新建时使用；建议改用 CREATE_ADMIN_PASSWORD 环境变量")
    parser.add_argument("--promote", action="store_true", help="只提升已存在的账号，不存在则报错")
    parser.add_argument("--demote", action="store_true", help="收回管理员权限")
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="重置已有账号的密码（管理员与其他账号都适用）",
    )
    parser.add_argument("--list", action="store_true", help="列出当前管理员")
    args = parser.parse_args(argv)

    if args.list:
        return asyncio.run(_list_admins())
    if not args.username:
        parser.error("需要 --username（或用 --list 查看）")
    if args.demote and args.set_password:
        parser.error("--demote 与 --set-password 不能同时用（一个收权限、一个改密码）")
    if args.demote:
        return asyncio.run(_revoke(args.username))

    password = args.password or os.environ.get("CREATE_ADMIN_PASSWORD")
    if args.set_password:
        if not password:
            # 交互式输入不回显；计划任务里请用环境变量
            password = getpass.getpass(f"{args.username} 的新密码（不回显）: ") or None
        if not password:
            print("需要 --password 或环境变量 CREATE_ADMIN_PASSWORD。", file=sys.stderr)
            return 2
        return asyncio.run(_set_password(args.username, password))

    if not password and not args.promote:
        # 交互式输入不回显；计划任务里请用环境变量
        password = getpass.getpass("管理员密码（不回显）: ") or None
    return asyncio.run(_grant(args.username, password, args.promote))


if __name__ == "__main__":
    raise SystemExit(main())
