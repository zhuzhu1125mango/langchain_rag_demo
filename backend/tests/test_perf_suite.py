"""性能回归套件的判定逻辑单测（不跑真实负载，只验「会不会判错」）。

为什么这些测试值得写：这套东西唯一的产出是「红/绿」。判错的方向只有两种，
两种都很危险：

- **该红不红**：阈值写成 `>=`、计数器用集合比较、profile 缺失时当成通过 ——
  那么这条门禁永远是绿的，和没有一样。
- **不该红却红**：把「新测项」当退化、把缺 profile 当失败、或者拿 0.16ms 的
  测量噪声当退化 —— 第一次红完，第二次人就会把它注释掉。

第三种是最新踩到的（2026-11-08 夜间任务变红）：`bm25_query` 0.212ms → 0.374ms
被判 1.76× 退化，而绝对增量只有 0.162ms。用户感知不到，但红灯亮了，
于是「绿=真绿」的可信度又少一分。修法不是调大阈值，而是给耗时加第二条判据
（绝对增量下限）——因为**比例阈值对微基准天然不成立**。

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


def _timing(min_ms):
    return {"min_ms": min_ms, "median_ms": min_ms * 1.1, "p95_ms": min_ms * 1.2, "samples": 3}


def _report(timings=None, counters=None):
    return {
        "profile": "local",
        "machine": {"platform": "test", "cpu_count": 8, "python": "3.12.0"},
        "timings": timings or {"rrf_fusion": _timing(0.07)},
        "counters": counters or {"chunk_count": 792},
    }


def _baseline():
    return {
        "version": 1,
        "counters": {"chunk_count": 792},
        "profiles": {"local": {"rrf_fusion": {"min_ms": 0.07}}},
    }


def _ci_baseline():
    """`ci` 档位的真实基线（docs/benchmarks/perf-baseline.json）。"""
    return {
        "version": 1,
        "counters": {
            "rrf_channel_items": 150,
            "rrf_fused_items": 60,
            "chunk_input_chars": 40321,
            "chunk_count": 792,
            "bm25_questions": 90,
            "bm25_docs_scored_per_query": 78,
        },
        "profiles": {
            "ci": {
                "rrf_fusion": {"min_ms": 0.077},
                "markdown_chunking": {"min_ms": 3.49},
                "bm25_query": {"min_ms": 0.212},
            }
        },
    }


def test_missing_profile_only_records_never_fails():
    """profile 缺失时进记录模式：CI 首次运行不该因为「还没有基线」变红。"""
    failures, warnings, notes = compare(
        _report(), {"version": 1, "counters": {}, "profiles": {}}, 1.6, "ci"
    )
    assert failures == []
    assert warnings == []
    assert any("尚无基线" in n for n in notes)


def test_counter_drift_fails():
    """计数器是确定性指标，必须逐项相等——算法改了就得有人看一眼。"""
    failures, _, _ = compare(_report(counters={"chunk_count": 793}), _baseline(), 1.6, "local")
    assert len(failures) == 1
    assert "计数器变化 chunk_count" in failures[0]


def test_new_counter_is_noted_not_failed():
    """新增测项是好事，不该把门禁弄红。"""
    failures, _, notes = compare(
        _report(counters={"chunk_count": 792, "new_metric": 5}), _baseline(), 1.6, "local"
    )
    assert failures == []
    assert any("new_metric" in n for n in notes)


def test_real_regression_still_fails():
    """真退化必须红：切分慢到 3.4 倍、绝对增量 8.5ms，用户能感知。"""
    report = _report(timings={"markdown_chunking": _timing(12.0)})
    failures, warnings, _ = compare(report, _ci_baseline(), 1.6, "ci")
    assert len(failures) == 1
    assert "耗时退化 markdown_chunking" in failures[0]
    assert "绝对增量" in failures[0]
    assert warnings == []


def test_ratio_over_but_absolute_small_is_warning_not_failure():
    """比例超阈但增量不足下限：可见但不阻断（0.05ms 的差异没人能感知）。"""
    report = _report(timings={"rrf_fusion": _timing(0.12)})
    failures, warnings, _ = compare(report, _baseline(), 1.6, "local")
    assert failures == []
    assert len(warnings) == 1
    assert "测量噪声" in warnings[0]


def test_fast_item_needs_huge_ratio_to_fail():
    """0.077ms 的项即使慢 13 倍（+0.97ms），仍然不阻断——它离用户感知还很远。"""
    report = _report(timings={"rrf_fusion": _timing(1.05)})
    failures, warnings, _ = compare(report, _ci_baseline(), 1.6, "ci")
    assert failures == []
    assert len(warnings) == 1


def test_within_threshold_passes():
    report = _report(timings={"rrf_fusion": _timing(0.10)})
    failures, warnings, notes = compare(report, _baseline(), 1.6, "local")
    assert failures == []
    assert warnings == []
    assert any("rrf_fusion" in n for n in notes)


def test_20261108_run_is_no_longer_red():
    """回放 2026-11-08 那次真红的数字：当时的判定是 exit 1，修完必须判通过。

    这不是「调阈值把红变绿」——计数器那 6 项一模一样，说明工作量没变；
    变的只是测量噪声。如果没有这条断言，下次有人把下限调回 0，没人会发现。
    """
    report = _report(
        timings={
            "rrf_fusion": _timing(0.112),
            "markdown_chunking": _timing(4.712),
            "bm25_query": _timing(0.374),
        },
        counters=_ci_baseline()["counters"],
    )
    failures, warnings, _ = compare(report, _ci_baseline(), 1.6, "ci")
    assert failures == [], f"噪声不该判退化：{failures}"
    assert len(warnings) == 1, warnings
    assert "bm25_query" in warnings[0]


@pytest.mark.parametrize(
    ("failures", "warnings", "missing", "expect"),
    [
        ([], [], False, "✅ 判定：通过"),
        ([], ["耗时偏慢 x"], False, "✅ 判定：通过"),
        ([], [], True, "➖ 判定：记录模式"),
        (["耗时退化 x"], [], False, "❌ 判定：退化"),
    ],
)
def test_render_markdown_shows_verdict(failures, warnings, missing, expect):
    """人工看的就是这一行；判定必须与 failures/warnings/missing 一致。"""
    assert expect in render_markdown(_report(), failures, warnings, [], missing)


def test_warning_is_visible_but_verdict_is_pass():
    """告警必须同时满足两件事：看得见（❌ 不行，纯 ✅ 也不行），且不阻断。"""
    text = render_markdown(_report(), [], ["耗时偏慢 bm25_query"], [], False)
    assert "✅ 判定：通过" in text
    assert "⚠️" in text
    assert "❌" not in text


def test_committed_baseline_is_valid():
    """基线是门禁的锚点：坏了或被误删，夜间任务会静默进「记录模式」变成绿灯。"""
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["counters"], "计数器基线不能为空"
    assert "local" in data["profiles"]
    for item in ("rrf_fusion", "markdown_chunking", "bm25_query"):
        assert item in data["profiles"]["local"], f"缺少测项 {item}"
        assert data["profiles"]["local"][item]["min_ms"] > 0
