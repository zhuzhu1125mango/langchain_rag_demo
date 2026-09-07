"""P0-2a 结构化分块 vs recursive 基线的检索质量对比（全离线）。

用含多级标题、表格、代码块的样例文档，分别以 recursive 与结构化策略分块，
用与生产一致的 BM25 稀疏检索栈（run_eval.bm25_rank）对同一组问题排序，
对比命中率（hit@3）、MRR 与命中 chunk 精度。

判定标准：
- 结构化 hit@3 / mrr 不得低于 recursive 基线（非退化）
- 表格行问题：结构化的命中 chunk 应排第一（行级分块收益）
- 命中 chunk 平均长度应更短（上下文更聚焦，精度提升）
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from langchain_core.documents import Document

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_eval import bm25_rank  # noqa: E402

from src.services.document_processor import ChunkingFactory, process_document  # noqa: E402

SAMPLE_MD = """# 员工手册

本手册适用于全体正式员工，请各部门组织学习并遵守各项制度。本手册由人事部负责解释与修订，如有疑问请联系人事部咨询。

## 考勤制度

员工每日需通过企业微信打卡两次，上班一次下班一次。迟到30分钟以内记为迟到，每月3次以内不处罚，超过则记为旷工半天。因公外出需提前在系统登记外出事由与返回时间，忘记打卡的可在当月内由主管补签，每月最多补签3次。

## 请假制度

事假需提前3个工作日在OA系统申请。年假规定：入职满一年的员工每年享受5天带薪年假，工作满十年增至10天。病假需提供医院证明并事后2个工作日内补办。婚假、产假、丧假按国家有关规定执行，需附相关证明材料提交人事部备案。

## 差旅标准

出差住宿与交通费用报销参照下表执行，跨城市出差按住宿发生地标准执行。

| 城市 | 住宿上限 | 高铁座席 |
| --- | --- | --- |
| 北京 | 500 | 二等座 |
| 上海 | 500 | 二等座 |
| 成都 | 350 | 二等座 |

## 报销流程

差旅报销需在出差结束后10个工作日内提交OA申请单，住宿费用按职级标准上限报销，超出部分自理。报销时须上传机票、酒店等正规发票原件照片，经直属主管与财务经理两级审批后，报销款在5个工作日内发放至工资卡。

## 信息安全

员工不得将公司代码与客户数据上传至个人网盘，办公电脑须安装公司统一的杀毒软件并开启全盘加密。系统密码须每季度更换一次，不得与个人账户密码重复，离职时须配合IT部门完成账号与权限回收。
"""

# (问题, 答案必须出现的文本, 是否表格行问题)
EVAL_QUERIES = [
    ("带薪年假有几天", "每年享受5天带薪年假", False),
    ("去成都出差住宿上限是多少", "| 成都 | 350 |", True),
    ("电脑需要装什么安全软件", "杀毒软件", False),
    ("迟到多久会被记为旷工", "记为旷工半天", False),
    ("报销申请的截止期限", "10个工作日内", False),
]

TOP_K = 3


def _chunks_to_corpus(chunks):
    return [{"id": f"chunk_{i}", "title": "", "text": c.page_content} for i, c in enumerate(chunks)]


def _evaluate(chunks):
    """返回 hit@3、mrr@3、命中 chunk 平均长度。"""
    corpus = _chunks_to_corpus(chunks)
    hits, mrrs, hit_lens = [], [], []
    for question, answer_text, _ in EVAL_QUERIES:
        ranked = bm25_rank(corpus, question, top_k=TOP_K)
        hit_rank = None
        for rank, cid in enumerate(ranked, start=1):
            idx = int(cid.split("_")[1])
            if answer_text in chunks[idx].page_content:
                hit_rank = rank
                hit_lens.append(len(chunks[idx].page_content))
                break
        hits.append(1.0 if hit_rank else 0.0)
        mrrs.append(1.0 / hit_rank if hit_rank else 0.0)
    n = len(EVAL_QUERIES)
    return {
        "hit": sum(hits) / n,
        "mrr": sum(mrrs) / n,
        "avg_hit_len": sum(hit_lens) / len(hit_lens) if hit_lens else 0,
    }


def _table_query_top1(chunks) -> bool:
    """表格行问题的第一名 chunk 是否命中答案。"""
    corpus = _chunks_to_corpus(chunks)
    question, answer_text = EVAL_QUERIES[1][0], EVAL_QUERIES[1][1]
    ranked = bm25_rank(corpus, question, top_k=1)
    if not ranked:
        return False
    return answer_text in chunks[int(ranked[0].split("_")[1])].page_content


@pytest.fixture(scope="module")
def strategies_chunks():
    """同一文档的 recursive 与结构化分块结果。"""
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(SAMPLE_MD)
        path = f.name
    try:
        recursive = ChunkingFactory.split(
            [Document(page_content=SAMPLE_MD)],
            file_path="x.md", chunk_strategy="recursive", chunk_size=500, chunk_overlap=50,
        )
        structured = process_document(path)
        return recursive, structured
    finally:
        os.unlink(path)


def test_structured_not_worse_than_recursive(strategies_chunks):
    """结构化分块的命中率与 MRR 不得低于 recursive 基线。"""
    recursive, structured = strategies_chunks
    m_rec, m_str = _evaluate(recursive), _evaluate(structured)
    assert m_str["hit"] >= m_rec["hit"], f"命中率退化: {m_str} vs {m_rec}"
    assert m_str["mrr"] >= m_rec["mrr"], f"MRR 退化: {m_str} vs {m_rec}"


def test_structured_table_row_advantage(strategies_chunks):
    """表格行问题：结构化分块的第一名应直接命中目标行。"""
    _, structured = strategies_chunks
    assert _table_query_top1(structured), "表格行问题第一名未命中目标行"


def test_structured_hit_chunks_more_focused(strategies_chunks):
    """结构化分块命中 chunk 平均长度应显著短于 recursive（上下文更聚焦）。"""
    recursive, structured = strategies_chunks
    m_rec, m_str = _evaluate(recursive), _evaluate(structured)
    assert m_str["avg_hit_len"] < m_rec["avg_hit_len"], (
        f"命中 chunk 长度未缩短: structured={m_str['avg_hit_len']:.0f} "
        f"recursive={m_rec['avg_hit_len']:.0f}"
    )


def test_structured_chunks_carry_heading_path(strategies_chunks):
    """结构化 chunk 应全部携带标题路径，recursive 不具备该信息。"""
    _, structured = strategies_chunks
    assert all("heading_path" in c.metadata for c in structured)
