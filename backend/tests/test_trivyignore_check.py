"""`.trivyignore` 豁免到期检查（#32）。

为什么要有测试：这个脚本的价值全在「到期那天真的会红」。如果日期解析或
比较写错，它会永远返回 OK —— 比没有检查更糟，因为看板上它是绿的。
"""

import datetime as dt
import importlib.util
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_trivyignore.py"

_spec = importlib.util.spec_from_file_location("check_trivyignore", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _text(review_by: str | None = "2026-11-30", cves: int = 2) -> str:
    lines = ["# Trivy 豁免清单"]
    if review_by:
        lines.append(f"# review-by: {review_by}")
    lines += [f"CVE-2026-{1000 + i}" for i in range(cves)]
    return "\n".join(lines) + "\n"


class TestCheckTrivyignore:
    def test_far_from_expiry_is_ok(self):
        code, msg = mod.check(_text(), dt.date(2026, 11, 1))
        assert code == 0
        assert "2026-11-30" in msg

    def test_within_warn_window_is_warn_not_fail(self):
        code, msg = mod.check(_text(), dt.date(2026, 11, 20), warn_days=14)
        assert code == 2, "还有 10 天应当是告警，不是失败"
        assert "10 天" in msg

    def test_expired_fails(self):
        code, msg = mod.check(_text(), dt.date(2026, 12, 1))
        assert code == 1
        assert "已过期" in msg

    def test_expiring_today_still_ok(self):
        """到期当天不算过期：写的是「复核截止日」，当天还有机会处理。"""
        code, _ = mod.check(_text(), dt.date(2026, 11, 30))
        assert code in (0, 2)

    def test_missing_review_by_fails(self):
        """允许「没写日期就跳过」等于让人删一行就能变绿。"""
        code, msg = mod.check(_text(review_by=None), dt.date(2026, 11, 1))
        assert code == 1
        assert "review-by" in msg

    def test_bad_date_format_fails(self):
        code, msg = mod.check(_text(review_by="2026/11/30"), dt.date(2026, 11, 1))
        assert code == 1
        assert "review-by" in msg

    def test_no_cve_entries_fails(self):
        """留个空壳文件会让人误以为「有豁免在管」。"""
        code, msg = mod.check(_text(cves=0), dt.date(2026, 11, 1))
        assert code == 1
        assert "CVE" in msg

    def test_repository_file_is_currently_valid(self):
        """仓库里那份必须当下有效：否则 CI 一上线就是红的。"""
        text = (_REPO_ROOT / ".trivyignore").read_text(encoding="utf-8")
        code, msg = mod.check(text, dt.date.today())
        assert code == 0, msg


class TestCliExitCode:
    def test_warn_exits_zero(self, capsys):
        """告警不能挡合入，否则会被习惯性绕过。"""
        code = mod.main(["--file", str(_REPO_ROOT / ".trivyignore"), "--today", "2026-11-20"])
        assert code == 0
        assert "WARN" in capsys.readouterr().out

    def test_expired_exits_one(self):
        assert mod.main(["--file", str(_REPO_ROOT / ".trivyignore"), "--today", "2026-12-01"]) == 1

    def test_missing_file_is_not_an_error(self, tmp_path):
        """豁免文件删掉说明不再需要豁免，不该拦。"""
        assert mod.main(["--file", str(tmp_path / "nope")]) == 0

    def test_bad_today_arg_raises(self):
        with pytest.raises(ValueError):
            mod.main(["--file", str(_REPO_ROOT / ".trivyignore"), "--today", "2026-13-01"])
