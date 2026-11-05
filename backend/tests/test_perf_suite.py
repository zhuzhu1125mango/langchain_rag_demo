"""性能回归套件的判定逻辑单测（不跑真实负载，只验「会不会判错」）。

为什么这些测试值得写：这套东西唯一的产出是「红/绿」。判错的方向只有两种，
两种都很危险：

- **该红不红**：阈值写成 `>=`、计数器用集合比较、profile 缺失时当成通过 ——
  那么这条门禁永远是绿的，和没有一样。
- **不该红却红**：把「新测项」当退化、把缺 profile 当失败 ——
  第一次跑就红，人会直接把它注释掉。

真实负载跑一次要几十秒，所以这里只钉判定函数；数字由 `scripts/perf_suite.py`
在夜间工作流里产出。
"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from perf_suite import compare, render_markdown  # noqa: E402

BASELINE_PATH = Path(__file__).resolve().parents[2] / "docs" / "benchmarks" / "perf-baseline.json"


def _report(timings=None, counters=None):
    return {
        "profile": "local",
        "machine": {"platform": "test", "cpu_count": 8, "python": "3.12.0"},
        "timings": timings or {"rrf_fusion": {"min_ms": 0.07, "median_ms": 0.08, "p95_ms": 0.09, "samples": 3}},
        "counters": counters or {"chunk_count": 792},
    }


def _baseline():
    return {
        "version": 1,
        "counters": {"chunk_count": 792},
        "profiles": {"local": {"rrf_fusion": {"min_ms": 0.07}}},
    }


def test_missing_profile_only_records_never_fails():
    """profile 缺失时进记录模式：CI 首次运行不该因为「还没有基线」变红。"""
    failures, notes = compare(_report(), {"version": 1, "counters": {}, "profiles": {}}, 1.6, "ci")
    assert failures == []
    assert any("尚无基线" in n for n in notes)


def test_counter_drift_fails():
    """计数器是确定性指标，必须逐项相等——算法改了就得有人看一眼。"""
    failures, _ = compare(_report(counters={"chunk_count": 793}), _baseline(), 1.6, "local")
    assert len(failures) == 1
    assert "计数器变化 chunk_count" in failures[0]


def test_new_counter_is_noted_not_failed():
    """新增测项是好事，不该把门禁弄红。"""
    failures, notes = compare(_report(counters={"chunk_count": 792, "new_metric": 5}), _baseline(), 1.6, "local")
    assert failures == []
    assert any("new_metric" in n for n in notes)


def test_slower_than_threshold_fails():
    report = _report(timings={"rrf_fusion": {"min_ms": 0.12, "median_ms": 0.13, "p95_ms": 0.2, "samples": 3}})
    failures, _ = compare(report, _baseline(), 1.6, "local")
    assert len(failures) == 1
    assert "耗时退化 rrf_fusion" in failures[0]


def test_within_threshold_passes():
    report = _report(timings={"rrf_fusion": {"min_ms": 0.10, "median_ms": 0.11, "p95_ms": 0.2, "samples": 3}})
    failures, notes = compare(report, _baseline(), 1.6, "local")
    assert failures == []
    assert any("rrf_fusion" in n for n in notes)


@pytest.mark.parametrize(
    ("failures", "missing", "expect"),
    [
        ([], False, "✅ 判定：通过"),
        ([], True, "➖ 判定：记录模式"),
        (["耗时退化 x"], False, "❌ 判定：退化"),
    ],
)
def test_render_markdown_shows_verdict(failures, missing, expect):
    """人工看的就是这一行；判定与 failures/missing 必须一致。"""
    assert expect in render_markdown(_report(), failures, [], missing)


def test_committed_baseline_is_valid():
    """基线是门禁的锚点：坏了或被误删，夜间任务会静默进「记录模式」变成绿灯。"""
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["counters"], "计数器基线不能为空"
    assert "local" in data["profiles"]
    for item in ("rrf_fusion", "markdown_chunking", "bm25_query"):
        assert item in data["profiles"]["local"], f"缺少测项 {item}"
        assert data["profiles"]["local"][item]["min_ms"] > 0
