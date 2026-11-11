"""表格诊断脚本的边界（2026-11-11）。

这个脚本的结论直接影响「要不要改生产」，所以它自己的口径必须钉住：

1. 回填**不跨文档、不跨表**——跨了就是把无关内容算成收益；
2. 字符预算**不超**——超了就会得出「回填很便宜」的错误结论；
3. 只回填 top1 时**不补其它选中块的表**——这是「省钱写法」的对照臂，语义不能漂。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_table_selection import refill  # noqa: E402

HDR_A = "| 指标名称 | 类型 | 说明 |"
HDR_B = "| 组 | 告警 | 表达式 |"


def _chunk(cid, header, row):
    return {"id": cid, "text": f"某文档 > 7. 监控告警 > 7.3 监控指标\n{header}\n{row}"}


CORPUS = [
    _chunk("d1#1", HDR_A, "| a | gauge | 甲 |"),
    _chunk("d1#2", HDR_A, "| b | gauge | 乙 |"),
    _chunk("d1#3", HDR_B, "| scrape | X | up == 0 |"),
    _chunk("d2#1", HDR_A, "| c | gauge | 丙 |"),
    {"id": "d1#4", "text": "某文档 > 7.4 纯文本段落\n这里没有表格"},
]
BY_ID = {c["id"]: c for c in CORPUS}
text_of = lambda i: BY_ID[i]["text"]


class TestRefillScope:
    def test_fills_same_table_only(self):
        ids, _chars, added = refill(["d1#1"], CORPUS, text_of, cap_chars=5000)
        assert "d1#2" in ids, "同表其它行应补进来"
        assert "d1#3" not in ids, "不同表不能补"
        assert "d2#1" not in ids, "不同文档不能补"
        assert added == 1

    def test_non_table_chunk_is_untouched(self):
        ids, _chars, added = refill(["d1#4"], CORPUS, text_of, cap_chars=5000)
        assert ids == ["d1#4"] and added == 0

    def test_only_top1_drops_other_seeds(self):
        """`only_top1` 是「1 块换 3 块」的对照臂：只留第一块再补它的表，其余两块被丢弃。

        这不是笔误——正是这个语义解释了它为什么只有 0.250：
        把另外两块的事实点一起扔了。若要「保留三块、只补 top1 的表」，
        那需要另一个变体（实测 0.556，无收益）。
        """
        ids, _chars, added = refill(["d1#1", "d1#3"], CORPUS, text_of, cap_chars=5000, only_top1=True)
        assert ids[0] == "d1#1"
        assert "d1#2" in ids, "top1 所在的表要补全"
        assert "d1#3" not in ids, "其余选中块不保留——这就是该对照臂的定义"
        assert added == 1


class TestCharBudget:
    def test_budget_is_not_exceeded(self):
        ids, used, _added = refill(["d1#1"], CORPUS, text_of, cap_chars=10)
        assert ids == ["d1#1"], "预算不够时一块都不补"
        assert used == len(text_of("d1#1"))

    def test_budget_counts_seed_text(self):
        _, used, _ = refill(["d1#1"], CORPUS, text_of, cap_chars=5000)
        assert used == sum(len(text_of(i)) for i in ("d1#1", "d1#2"))
