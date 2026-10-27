"""真实后端 E2E 烟测（SEC-15）。

在 docker compose 栈启动后执行：
    cd backend && uv run python scripts/smoke_e2e.py --base-url http://localhost:8000

覆盖：live/ready、根路径、管理员模型快照（可选 ADMIN_KEY）、未认证越权 401。
退出码非 0 表示烟测失败。
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.getenv("SMOKE_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--admin-key", default=os.getenv("ADMIN_KEY", ""))
    args = parser.parse_args()

    failures: list[str] = []
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=15.0) as client:
        live = client.get("/health/live")
        if live.status_code != 200:
            failures.append(f"/health/live -> {live.status_code}")
        ready = client.get("/health/ready")
        if ready.status_code not in (200, 503):
            failures.append(f"/health/ready -> {ready.status_code}")

        root = client.get("/")
        if root.status_code != 200:
            failures.append(f"/ -> {root.status_code}")

        unauth = client.get("/auth/me")
        if unauth.status_code not in (401, 403):
            failures.append(f"/auth/me 未认证应为 401/403，实际 {unauth.status_code}")

        if args.admin_key:
            snap = client.get("/api/admin/models/snapshot", headers={"X-Admin-Key": args.admin_key})
            if snap.status_code != 200:
                failures.append(f"/api/admin/models/snapshot -> {snap.status_code}")

    if failures:
        print("SMOKE FAILED:")
        for item in failures:
            print(f" - {item}")
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
