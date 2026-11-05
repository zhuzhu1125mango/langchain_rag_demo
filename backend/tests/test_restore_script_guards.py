"""`scripts/restore.sh` 的静态守卫（纯文本断言，不连库、不起容器）。

为什么用静态断言而不是集成测试：这条分支要「宿主机没有 psql + 有 docker」才走，
CI 里两样都不缺，跑不到；而它出错的形态是**静默的**——数据导进错的库，`psql` 仍 exit 0。
2026-11-04 回滚演练就是在这个前提下踩到的（详见 docs/guide/backup-restore.md §7.3 第六批）。
真实执行由 `HOST_RESTORE=1 ./scripts/restore_drill.sh` 覆盖，手工触发。
"""

from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "restore.sh").read_text(encoding="utf-8")


def test_plain_import_passes_target_db_into_container() -> None:
    """纯文本产物走容器时，库名/用户名必须由脚本显式传入。

    反例（已修）：`docker exec ... sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`
    —— 单引号让变量在**容器内**展开，取到容器的默认库；调用方指定别的库时会静默导错。
    """
    assert '-e "PGU=$POSTGRES_USER"' in SCRIPT
    assert '-e "PGD=$POSTGRES_DB"' in SCRIPT
    assert 'psql -U "$PGU" -d "$PGD"' in SCRIPT
    # 容器侧不能再出现这两个变量名，否则又回到了「让容器的 sh 去展开」的老路
    assert "$POSTGRES_DB" not in SCRIPT.split("sh -c '", 1)[1].split("'", 1)[0]


def test_magic_read_survives_gzip_null_byte() -> None:
    """gzip 头第 4 字节是 0x00，直接 $(head -c 5) 会让 bash 打 null byte 警告。

    恢复现场看到警告会以为是备份坏了，所以这里钉住「先过管道再进变量」的写法。
    """
    assert 'head -c 5 "$PG_FILE"' in SCRIPT
    assert "tr -d '\\000'" in SCRIPT


def test_import_output_is_muted() -> None:
    """pg_dump 里的 SELECT set_config(...) 会打出结果表，恢复现场容易被误判成报错。"""
    assert SCRIPT.count("-o /dev/null") >= 2


# ---------------------------------------------------------------------------
# 回滚演练脚本：Git Bash 下的静默失败模式
# ---------------------------------------------------------------------------

ROLLBACK = (Path(__file__).resolve().parents[2] / "scripts" / "rollback_drill.sh").read_text(
    encoding="utf-8"
)


def test_rollback_drill_never_writes_to_literal_dev_null() -> None:
    """原生 curl 在 MSYS_NO_PATHCONV=1 下不认 `/dev/null`。

    `-o /dev/null` 会变成写 `C://dev//null`：curl 退出码 23（写失败），**而 `-w` 照样打印 200**，
    脚本于是「看起来成功」直到下一步 grep 文件不存在才炸（排查花了一轮）。
    统一走 `$NULL_DEV`（`cygpath -w /dev/null`）。
    """
    assert "-o /dev/null" not in ROLLBACK
    assert 'NULL_DEV="$(cygpath -w /dev/null' in ROLLBACK
    assert ROLLBACK.count('"$NULL_DEV"') >= 2


def test_rollback_drill_converts_curl_output_path() -> None:
    """`curl -o` 的路径必须转成 Windows 形式，否则写不进去（同上，退出码 23）。"""
    assert '-o "$(winpath "$WORK/fe_index.html")"' in ROLLBACK


def test_rollback_drill_uses_a_separate_question_for_proxy_check() -> None:
    """经 nginx 的 SSE 不能复用刚问过的问题：命中语义缓存会「秒回且完整」，看起来像通过。"""
    assert "ASK_Q_PROXY=" in ROLLBACK
