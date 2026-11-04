"""一致性巡检周期调度单测（全离线）。

覆盖三件容易出错、且出错后很难从外面看出来的事：
1. `compute_delay` 的补齐逻辑——容器每天重启一次时，不能每轮都从头计时；
2. 报告落盘与轮转——保留份数算错会删掉刚写的那份，或无限堆积；
3. `run_once` 的失败语义——超时/异常必须被吞掉并留痕，否则后台循环一次异常就永久停摆。
"""

import asyncio
import json
import os
import time

import pytest

import src.services.consistency_scheduler as sched


class TestComputeDelay:
    def test_zero_interval_never_runs(self):
        assert sched.compute_delay(0, None) == float("inf")

    def test_no_history_runs_soon(self):
        """首次部署没有记录 → 60s 后补跑一次（而不是等满一个间隔）。"""
        assert sched.compute_delay(24, None) == 60

    def test_fresh_run_waits_remaining_time(self):
        now = 1_000_000.0
        # 1 小时前跑过，间隔 24h → 还要等 23h
        assert sched.compute_delay(24, now - 3600, now=now) == pytest.approx(23 * 3600)

    def test_overdue_runs_soon(self):
        now = 1_000_000.0
        assert sched.compute_delay(24, now - 25 * 3600, now=now) == 60

    def test_clock_skew_record_from_future_does_not_stall(self):
        """时钟回拨时记录可能「来自未来」：按已超期处理，不能算出几十小时的等待。"""
        now = 1_000_000.0
        assert sched.compute_delay(24, now + 5 * 3600, now=now) == 60

    def test_remaining_never_below_floor(self):
        """刚好差几秒到期时也要留出下限，避免 0.2s 的空转。"""
        now = 1_000_000.0
        assert sched.compute_delay(24, now - (24 * 3600 - 1), now=now) == 60


class TestReportFiles:
    def test_write_and_read_back(self, tmp_path):
        report = {"exit_code": 0, "issue_count": 0, "sections": {}}
        path = sched.write_report(report, directory=str(tmp_path))

        assert path is not None and os.path.exists(path)
        with open(path, encoding="utf-8") as fh:
            assert json.load(fh)["exit_code"] == 0

    def test_prune_keeps_newest(self, tmp_path):
        for i in range(5):
            (tmp_path / f"consistency_2026010{i}_000000.json").write_text("{}", encoding="utf-8")

        removed = sched.prune_reports(str(tmp_path), keep=2)

        left = sorted(os.listdir(tmp_path))
        assert len(left) == 2
        assert left == ["consistency_20260103_000000.json", "consistency_20260104_000000.json"]
        assert len(removed) == 3

    def test_prune_ignores_other_files(self, tmp_path):
        (tmp_path / "consistency_20260101_000000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "note.txt").write_text("keep me", encoding="utf-8")

        sched.prune_reports(str(tmp_path), keep=1)

        assert (tmp_path / "note.txt").exists(), "轮转不能误删非报告文件"

    def test_prune_disabled_when_keep_zero(self, tmp_path):
        for i in range(3):
            (tmp_path / f"consistency_2026010{i}_000000.json").write_text("{}", encoding="utf-8")

        assert sched.prune_reports(str(tmp_path), keep=0) == []
        assert len(os.listdir(tmp_path)) == 3

    def test_write_failure_is_not_fatal(self, tmp_path):
        """目录不可写时只告警：报告落不了盘不能把巡检本身判成失败。"""
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file", encoding="utf-8")

        assert sched.write_report({"exit_code": 0}, directory=str(blocker / "sub")) is None


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_clean_run_logs_info(self, monkeypatch, tmp_path, caplog):
        async def fake_audit():
            return {"exit_code": 0, "issue_count": 0, "db": {"documents": 3}, "sections": {}}

        monkeypatch.setattr("src.services.consistency_audit.run_audit", fake_audit)
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_OUTPUT_DIR", str(tmp_path))

        import logging

        with caplog.at_level(logging.INFO, logger="rag_system"):
            report = await sched.run_once()

        assert report["issue_count"] == 0
        assert any("一致性巡检通过" in r.getMessage() for r in caplog.records)
        assert len(os.listdir(tmp_path)) == 1

    @pytest.mark.asyncio
    async def test_issues_log_warning_with_sections(self, monkeypatch, tmp_path, caplog):
        """有差异必须走 WARNING——后台循环没有退出码，日志是唯一能被告警抓到的出口。"""

        async def fake_audit():
            return {
                "exit_code": 1,
                "issue_count": 2,
                "db": {"documents": 1},
                "sections": {"minio": {"issues": 2}, "redis": {"issues": 0}},
            }

        monkeypatch.setattr("src.services.consistency_audit.run_audit", fake_audit)
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_OUTPUT_DIR", str(tmp_path))

        import logging

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            await sched.run_once()

        hits = [r for r in caplog.records if "一致性巡检发现差异" in r.getMessage()]
        assert hits and hits[0].levelno == logging.WARNING
        assert "minio:2" in hits[0].getMessage()

    @pytest.mark.asyncio
    async def test_timeout_does_not_raise(self, monkeypatch):
        async def slow_audit():
            await asyncio.sleep(5)
            return {}

        monkeypatch.setattr("src.services.consistency_audit.run_audit", slow_audit)
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_TIMEOUT_SECONDS", 30)
        # 把 wait_for 的超时压到极小，避免测试真的等 30s
        monkeypatch.setattr(sched.asyncio, "wait_for", _fast_timeout)

        report = await sched.run_once()

        assert report["fatal"] == "timeout", "超时要落到报告里，不能静默返回空字典"

    @pytest.mark.asyncio
    async def test_exception_does_not_kill_loop(self, monkeypatch):
        async def boom():
            raise RuntimeError("milvus unreachable")

        monkeypatch.setattr("src.services.consistency_audit.run_audit", boom)

        report = await sched.run_once()

        assert report["exit_code"] == 2
        assert "milvus unreachable" in report["fatal"]


async def _fast_timeout(coro, timeout):  # noqa: ARG001 - 模拟超时，不实际等待
    coro.close()
    raise asyncio.TimeoutError


class TestStartStop:
    def test_disabled_when_interval_zero(self, monkeypatch):
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_INTERVAL_HOURS", 0)
        assert sched.start_scheduler() is None

    @pytest.mark.asyncio
    async def test_start_is_idempotent_and_stops(self, monkeypatch):
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_INTERVAL_HOURS", 24)
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_OUTPUT_DIR", "")
        # 让循环立刻进入长时间 sleep，避免真的触发巡检
        monkeypatch.setattr(sched, "_read_last_run", _no_history)
        monkeypatch.setattr(sched, "compute_delay", lambda *_a, **_k: 3600)

        first = sched.start_scheduler()
        try:
            assert first is not None
            assert sched.start_scheduler() is first, "重复启动必须幂等，不能起两个循环"
        finally:
            await sched.stop_scheduler()

        assert sched._task is None


async def _no_history():
    return None


class TestDefaultOutputDir:
    def test_default_dir_is_the_mounted_backend_data(self, monkeypatch):
        """默认目录必须是「后端根/data」，不能用 DATA_DIR。

        踩过的坑：`DATA_DIR` 是 `BASE_DIR/data`，而容器里 `BASE_DIR=/app/src`，
        于是报告写进镜像层 `/app/src/data`——容器一重建就没了，而日志显示「已落盘」。
        生产挂载的是 `/app/data`，这里必须与 `LOG_DIR` 同口径。
        """
        monkeypatch.setattr(sched.settings.consistency_audit, "CONSISTENCY_AUDIT_OUTPUT_DIR", "")

        path = sched.resolve_output_dir().replace("\\", "/")

        assert path.endswith("/backend/data/consistency") or path.endswith("/app/data/consistency")
        assert "/src/data" not in path

    def test_explicit_dir_wins(self, monkeypatch):
        monkeypatch.setattr(
            sched.settings.consistency_audit, "CONSISTENCY_AUDIT_OUTPUT_DIR", "/tmp/audit"
        )
        assert sched.resolve_output_dir() == "/tmp/audit"
