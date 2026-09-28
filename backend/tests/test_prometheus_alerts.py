"""Prometheus 告警规则校验（P2-5）。

校验 alerts.yml 结构合法、alert 名唯一，且 P2-5 新增告警引用的指标
均已在中后端 prometheus.py 定义（防拼写漂移导致规则恒不触发）。
"""

import re
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


# PromQL 关键字/函数名：提取 token 时排除，避免误当指标名
_PROMQL_KEYWORDS = {
    "rate", "irate", "sum", "avg", "min", "max", "count", "by", "without",
    "histogram_quantile", "clamp_min", "clamp_max", "le", "and", "or",
    "unless", "on", "group_left", "group_right", "ignoring", "offset",
    "bool", "topk", "bottomk", "avg_over_time", "sum_over_time",
    "increase", "deriv", "predict_linear", "absent", "label_replace",
    # 常见标签名（{job="..."} / by (model_name)）
    "job", "instance", "model_name", "name", "alertname",
}

# 非 prometheus.py 管理的外部 exporter 指标白名单（Milvus exporter 等基础
# 设施指标；新增外部指标告警时须显式加入，防止拼写漂移恒不触发）
_EXTERNAL_METRICS_WHITELIST = {
    "up",
    "milvus_proxy_sq_latency_milliseconds_sum",
    "milvus_proxy_sq_latency_milliseconds_count",
}


def _extract_metric_names(expr: str) -> set:
    """从 PromQL 表达式提取指标名 token（剔除 [5m] 持续时间，排除关键字与标签值）。

    负向后顾排除紧跟数字的字母（1e-9 的 e、5m 的 m 等数值字面量残段）。
    """
    cleaned = re.sub(r"\[\d+[a-z]+\]", " ", expr)
    cleaned = re.sub(r"\{[^}]*\}", " ", cleaned)  # 剔除 {job="..."} 标签选择器（含标签值）
    tokens = re.findall(r"(?<![0-9.eE])[a-zA-Z_][a-zA-Z0-9_]*", cleaned)
    return {t for t in tokens if t not in _PROMQL_KEYWORDS}


def _is_registered(metric: str, defined: set) -> bool:
    """指标名或其 histogram 变体（_bucket/_sum/_count 去后缀）已在定义集合。"""
    if metric in defined:
        return True
    for suffix in ("_bucket", "_sum", "_count"):
        if metric.endswith(suffix) and metric[: -len(suffix)] in defined:
            return True
    return False


def test_all_alert_metrics_registered():
    """W6 #66：alerts.yml 全部 expr 引用的指标可归属——应用指标必须在
    prometheus.py 注册，外部 exporter 指标必须在白名单内；两者都不满足
    即说明指标名拼写漂移，规则将恒不触发。"""
    data = _load_alerts()
    defined = _defined_metric_names()
    checked = 0
    for group in data["groups"]:
        for rule in group["rules"]:
            for metric in _extract_metric_names(rule["expr"]):
                checked += 1
                if metric.startswith(("rag_", "semantic_cache_", "wiki_")):
                    assert _is_registered(metric, defined), (
                        f"{rule['alert']} 引用的 {metric} 未在 prometheus.py 定义（拼写漂移？）"
                    )
                else:
                    assert metric in _EXTERNAL_METRICS_WHITELIST, (
                        f"{rule['alert']} 引用未登记的外部指标 {metric}；"
                        f"若为合法 exporter 指标请加入 _EXTERNAL_METRICS_WHITELIST"
                    )
    # 防御：提取逻辑失效（0 token）时测试形同虚设
    assert checked >= 10, f"应从规则中提取到足够多的指标 token，实际 {checked}"