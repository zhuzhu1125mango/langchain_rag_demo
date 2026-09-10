"""环境变量一致性校验（纯 stdlib，纳入 CI env-consistency job）。

校验 A：.env.example 的键集合 ⊇ .env.dev ∪ .env.prod 的键集合
        （example 是模板与文档，两套环境实际使用的每个键都必须在模板中有记载）

校验 B：两份 compose 中 ${VAR} / ${VAR:-default} 的插值引用，必须能由对应
        env 文件提供（dev compose ← .env.dev，prod compose ← .env.prod），
        防止 compose 改动后变量漏配导致容器启动即失败。
        白名单中的键视为可选，允许 env 文件缺省。

任一校验失败：打印缺失键明细并以非零码退出。
用法：uv run python scripts/check_env_consistency.py
"""

import os
import re
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_EXAMPLE = os.path.join(PROJECT_ROOT, ".env.example")
# compose 文件 → 应提供插值变量的 env 文件
COMPOSE_ENV_PAIRS = [
    (os.path.join(PROJECT_ROOT, "docker-compose.dev.yml"), os.path.join(PROJECT_ROOT, ".env.dev")),
    (os.path.join(PROJECT_ROOT, "docker-compose.yml"), os.path.join(PROJECT_ROOT, ".env.prod")),
]

# compose 中允许 env 文件缺省的键（留空时 compose 报 warning 但可启动的可选项）
# 保持最小化：仅当确属可选且人工确认后再加入
OPTIONAL_COMPOSE_VARS: set = set()

# 匹配未注释的 KEY= 行（忽略内联注释；值允许为空）
ENV_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
# 匹配 compose 插值 ${VAR} / ${VAR:-default} / ${VAR-default}
COMPOSE_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-[^}]*)?\}")


def read_env_keys(path: str) -> set:
    """解析 env 文件中实际定义（未注释）的键集合。"""
    if not os.path.exists(path):
        print(f"错误: 文件不存在: {path}")
        sys.exit(2)
    keys = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = ENV_KEY_RE.match(line)
            if m:
                keys.add(m.group(1))
    return keys


def read_compose_vars(path: str) -> set:
    """提取 compose 文件中所有 ${VAR} 插值引用的键集合。"""
    if not os.path.exists(path):
        print(f"错误: 文件不存在: {path}")
        sys.exit(2)
    with open(path, encoding="utf-8") as f:
        return set(COMPOSE_VAR_RE.findall(f.read()))


def main() -> int:
    failures = []

    example_keys = read_env_keys(ENV_EXAMPLE)

    used_keys = set()
    for compose_path, env_path in COMPOSE_ENV_PAIRS:
        used_keys |= read_env_keys(env_path)

    # 校验 A：example 覆盖 dev ∪ prod 实际使用的键
    missing_in_example = sorted(used_keys - example_keys)
    if missing_in_example:
        failures.append(
            f"校验 A 失败: .env.example 缺少以下在用键 ({len(missing_in_example)} 个):\n  "
            + "\n  ".join(missing_in_example)
        )

    # 校验 B：compose 插值引用必须由对应 env 文件提供
    for compose_path, env_path in COMPOSE_ENV_PAIRS:
        env_keys = read_env_keys(env_path)
        compose_vars = read_compose_vars(compose_path)
        missing = sorted(v for v in compose_vars - env_keys if v not in OPTIONAL_COMPOSE_VARS)
        if missing:
            failures.append(
                f"校验 B 失败: {os.path.basename(compose_path)} 引用但 {os.path.basename(env_path)} 缺少 "
                f"({len(missing)} 个):\n  " + "\n  ".join(missing)
            )

    if failures:
        for failure in failures:
            print(failure)
            print()
        print(f"环境变量一致性校验未通过，共 {len(failures)} 项失败")
        return 1

    print("环境变量一致性校验通过:")
    print(f"  .env.example 覆盖 {len(used_keys)} 个在用键")
    for compose_path, env_path in COMPOSE_ENV_PAIRS:
        compose_vars = read_compose_vars(compose_path)
        print(
            f"  {os.path.basename(compose_path)}: {len(compose_vars)} 个插值引用均由 "
            f"{os.path.basename(env_path)} 提供"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
