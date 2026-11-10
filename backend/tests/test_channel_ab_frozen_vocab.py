"""`--sparse-vocab` 的语义：复现「冻结词表」事故时，必须像生产一样**跳过**而不是排噪声。

为什么单独钉：用旧词表编码真实提问，得到的查询向量是全零。若照常参与排序，
所有文档并列 0 分，`argsort` 会按数组下标返回一堆无关文档——于是「稀疏通道坏了」
被算成「稀疏通道在乱给结果」，损失数字会失真。生产 `search_sparse` 对空向量
是直接返回 `[]` 的，脚本必须一致。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymilvus.model.sparse import BM25EmbeddingFunction  # noqa: E402
from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer  # noqa: E402

from scripts.run_channel_ab import arm_sparse_scored  # noqa: E402

CORPUS = [{"id": "d1", "title": "请假制度", "text": "员工请假需要提前三天申请"}]


def save_vocab(tmp_path, texts):
    """在给定语料上 fit 一份词表并落盘，模拟「生产词表来自别的语料」。"""
    ef = BM25EmbeddingFunction(build_default_analyzer(language="zh"))
    ef.fit(texts)
    path = tmp_path / "bm25_documents.json"
    ef.save(str(path))
    return path


class TestFrozenVocabArm:
    def test_oov_query_is_skipped_not_ranked(self, tmp_path):
        """演练词表里没有「请假/提前」，查询向量为空 → 必须返回 []。"""
        path = save_vocab(tmp_path, ["delete drill marker 20261107 删除链脚本"])
        rankings = arm_sparse_scored(CORPUS, [{"question": "请假要提前多久"}], 20, path)
        assert rankings["请假要提前多久"] == []

    def test_in_vocab_query_still_ranks(self, tmp_path):
        """词表覆盖到的查询要正常出结果，否则「跳过」会掩盖真实召回。"""
        path = save_vocab(
            tmp_path,
            ["员工请假需要提前三天申请", "报销流程与发票要求", "门禁卡补办流程"],
        )
        rankings = arm_sparse_scored(CORPUS, [{"question": "请假提前几天"}], 20, path)
        assert [doc_id for doc_id, _ in rankings["请假提前几天"]] == ["d1"]

    def test_default_path_fits_on_eval_corpus(self):
        """不传词表时仍走原来的「在评测语料上 fit」，行为不能变。"""
        rankings = arm_sparse_scored(CORPUS, [{"question": "请假"}], 20)
        assert [doc_id for doc_id, _ in rankings["请假"]] == ["d1"]
