"""Prometheus 告警规则校验（P2-5）。

校验 alerts.yml 结构合法、alert 名唯一，且 P2-5 新增告警引用的指标
均已在中后端 prometheus.py 定义（防拼写漂移导致规则恒不触发）。
"""

import yaml
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "prometheus"
ALERTS_PATH = CONFIG_DIR / "alerts.yml"


def _load_alerts():
    with open(ALERTS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _defined_metric_names():
    """prometheus.py 中注册的 Counter/Histogram/Gauge 指标名集合。"""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "middleware" / "prometheus.py"
    text = src.read_text(encoding="utf-8")
    # 匹配 `"rag_xxx"` 或 `"semantic_cache_xxx"` / `"wiki_xxx"` 指标名常量定义
    return set(re.findall(r'"((?:rag|semantic_cache|wiki)_[a-z_0-9]+)"', text))


def test_alerts_yaml_structure():
    """alerts.yml 结构合法：含 groups，每条 rule 有 alert/expr/severity。"""
    data = _load_alerts()
    assert isinstance(data, dict) and "groups" in data and data["groups"]
    for group in data["groups"]:
        assert group["name"]
        for rule in group["rules"]:
            assert rule.get("alert")
            assert rule.get("expr")
            assert rule["labels"]["severity"] in ("warning", "critical")


def test_alert_names_unique():
    """alert 名在文件内唯一（避免去重/冲突）。"""
    data = _load_alerts()
    names = [r["alert"] for g in data["groups"] for r in g["rules"]]
    assert len(names) == len(set(names)), f"存在重复 alert 名: {names}"


def test_p25_metrics_defined():
    """P2-5 新增告警引用的指标均已在 prometheus.py 定义。"""
    data = _load_alerts()
    defined = _defined_metric_names()
    # 所有 rule expr 中出现的指标名，均应为已定义集合的子集
    all_expr = " ".join(r["expr"] for g in data["groups"] for r in g["rules"])
    # 至少：新计数器在 expr 出现，且被 prometheus.py 定义
    for metric in ("rag_retrieval_errors_total", "rag_sse_interrupted_total"):
        assert metric in all_expr, f"新增规则应引用 {metric}"
        assert metric in defined, f"{metric} 未在 prometheus.py 定义"