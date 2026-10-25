"""知澜身份回答测试。

身份问题必须稳定返回产品口径，不能把小模型自带的 Qwen/阿里云身份
暴露给用户；一般问题不能误命中身份短路。
"""

import pytest

from src.services.identity import build_identity_answer


@pytest.mark.parametrize(
    "question",
    [
        "你是谁",
        "你是谁？",
        "您是谁",
        "请问你是谁",
        "你叫什么名字",
        "介绍一下你自己",
        "你能做什么",
        "你会什么",
        "你是什么AI",
        "你是机器人吗",
    ],
)
def test_general_identity_answers_as_zhilan(question):
    answer = build_identity_answer(question)

    assert answer is not None
    assert "知澜" in answer
    assert "知识库" in answer
    assert "Qwen" not in answer
    assert "阿里云" not in answer


@pytest.mark.parametrize(
    "question",
    [
        "你是什么模型",
        "你用的什么模型？",
        "你的底层模型是什么",
        "你是Qwen吗",
        "你是不是Qwen",
        "你用的是ChatGPT吗",
        "你使用的是什么模型",
        "你是阿里云的吗",
        "谁训练了你",
        "你由谁开发",
        "你背后的公司是谁",
        "你是哪个公司的",
        "你用的是什么大模型",
        "你用的是哪个大模型",
    ],
)
def test_model_identity_is_honest_but_product_first(question):
    answer = build_identity_answer(question)

    assert answer is not None
    assert "知澜" in answer
    assert "本地大模型" in answer
    assert "Qwen" not in answer
    assert "阿里云" not in answer


@pytest.mark.parametrize(
    "question",
    [
        "什么是RAG",
        "北京今天天气怎么样",
        "请根据文档说明这个流程",
        "Qwen模型有什么特点",
        "阿里巴巴是什么公司",
        "你好",
        "",
    ],
)
def test_normal_questions_do_not_short_circuit_identity(question):
    assert build_identity_answer(question) is None


@pytest.mark.parametrize(
    "question",
    [
        "你和ChatGPT有什么区别",
        "你比Qwen强吗",
        "你和通义千问相比怎么样",
    ],
)
def test_model_comparison_answers_product_positioning(question):
    answer = build_identity_answer(question)

    assert answer is not None
    assert "知澜" in answer
    assert "企业知识问答" in answer
    assert "阿里云" not in answer


@pytest.mark.parametrize(
    "question",
    [
        "你的训练数据截止到什么时候",
        "你训练数据到哪一年",
    ],
)
def test_training_data_answers_without_guessing_vendor(question):
    answer = build_identity_answer(question)

    assert answer is not None
    assert "知澜" in answer
    assert "模型提供方" in answer
    assert "阿里云" not in answer
