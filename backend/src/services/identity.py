"""产品身份识别与固定回答。

小模型（qwen2.5:3b-instruct）在无上下文直答时会自称 Qwen/阿里云训练。
身份问题不需要走模型：命中后直接返回产品身份，既稳定又避免模型自曝。
其余回答提示词仍会追加身份约束，覆盖“你好”等未命中短路的场景。
"""

import re
from typing import Optional


_IDENTITY_ANSWER = (
    "我是**知澜**，企业知识问答助手。我可以帮你：\n\n"
    "1. 检索企业知识库，给出带来源的回答；\n"
    "2. 联网搜索实时信息；\n"
    "3. 总结文档、对比多个知识库，或基于上传资料回答问题。\n\n"
    "你可以直接提问，也可以在顶部选择知识库后开始。"
)

_MODEL_ANSWER = (
    "我是**知澜**，企业知识问答助手。"
    "底层由部署环境配置的本地大模型驱动，具体模型版本由管理员配置。"
    "我可以帮你检索知识库、联网搜索并给出带来源的回答。"
)

_COMPARISON_ANSWER = (
    "我是**知澜**，企业知识问答助手，主要面向企业知识库检索、联网搜索和带来源回答。"
    "不同模型和产品定位不同，我不以底层模型来定义自己；"
    "具体模型版本由管理员在部署配置中确认。"
)

_TRAINING_ANSWER = (
    "我是**知澜**，企业知识问答助手。"
    "我的回答基于当前部署的本地模型和检索到的资料；"
    "训练数据截止时间由模型提供方决定，具体模型版本可由管理员确认。"
    "涉及时效性问题时，建议开启联网搜索或查阅最新资料。"
)

# 注入所有回答提示词，覆盖“你好”等未命中身份短路的场景，避免模型自称 Qwen。
IDENTITY_PROMPT_RULE = (
    "身份规则：你叫知澜，是企业知识问答助手。"
    "被问到身份时以此自称；不要提及底层模型/厂商名称（Qwen、通义千问、阿里云等）；"
    "被追问模型时，说明由部署环境配置的本地大模型驱动。"
)

# 一般身份/能力询问（整句匹配，避免把正文里“你是谁”误判为身份问题）
_GENERAL_IDENTITY_RE = re.compile(
    r"^(请?问?)?\s*(你是谁|你叫什么(?:名字)?|你的名字(?:是什么)?|"
    r"介绍一下你自己|自我介绍(?:一下)?|你是什么(?:\s*(?:ai|人工智能))?|"
    r"你能做什么|你会什么|你的功能(?:是什么)?|"
    r"你是(?:机器人|真人|人类)吗|你是男是女)\s*(?:呀|啊)?\s*[?？!.。]*$",
    re.IGNORECASE,
)

# 开发者/厂商询问：同样只给产品口径，避免模型自曝底层厂商
_VENDOR_IDENTITY_RE = re.compile(
    r"^(请?问?)?\s*(你背后的公司(?:是谁)?|你的公司(?:是谁)?|你属于哪家公司|"
    r"你是哪家公司开发的|你由哪家公司开发|你的开发公司是谁|你的厂商是谁|"
    r"你是谁开发的|你由谁开发|你是哪个公司的|你的开发者是谁|"
    r"你来自哪里|你是哪家的)\s*[?？!.。]*$",
    re.IGNORECASE,
)

# 训练数据/知识截止询问：不猜测厂商与具体截止时间
_TRAINING_RE = re.compile(
    r"^(请?问?)?\s*你(?:的)?(?:训练数据|训练语料|知识截止|数据截止)"
    r".{0,20}(?:什么时候|何时|截止|时间|吗)?\s*[?？!.。]*$",
    re.IGNORECASE,
)

# 与其它模型/产品对比：只讲产品定位，不替底层模型背书
_COMPARISON_RE = re.compile(
    r"^你.{0,20}(?:和|与|跟|比|相比|对比).{0,20}"
    r"(?:qwen|通义千问|阿里云|chatgpt|gpt|deepseek|文心).{0,20}[?？!.。]*$",
    re.IGNORECASE,
)

# 底层模型/厂商询问：命中后仍然以知澜身份回答，并如实说明由部署配置驱动
_MODEL_IDENTITY_RE = re.compile(
    r"^(请?问?)?\s*(你是什么模型|你(?:用的|使用的)(?:是)?什么模型|"
    r"你(?:用的|使用的|用的是|使用的是|基于|背后).{0,8}(?:什么|哪个|哪种|啥).{0,4}(?:模型|大模型|ai|人工智能)|"
    r"你的底层模型(?:是什么)?|"
    r"你(?:是不是|是|用的是|使用的是|基于)\s*.{0,4}"
    r"(?:qwen|通义千问|阿里云|gpt|chatgpt|deepseek|文心)(?:吗|呢)?|"
    r"你是阿里云(?:的)?吗|谁训练了你|你是谁训练的)\s*[?？!.。]*$",
    re.IGNORECASE,
)

# “你基于什么模型/你是哪个模型”等更宽泛的问法（限制长度，避免误伤长问题）
_MODEL_HINT_RE = re.compile(
    r"(你|您).{0,8}(模型|训练|厂商|公司).{0,16}(qwen|通义|阿里|gpt|chatgpt|deepseek|文心|大模型)",
    re.IGNORECASE,
)


def build_identity_answer(question: str) -> Optional[str]:
    """命中身份问题时返回固定知澜回答；否则返回 None。"""
    text = (question or "").strip().replace("您", "你")
    if not text or len(text) > 80:
        return None

    if _TRAINING_RE.match(text):
        return _TRAINING_ANSWER
    if _COMPARISON_RE.match(text):
        return _COMPARISON_ANSWER
    if (
        _MODEL_IDENTITY_RE.match(text)
        or _MODEL_HINT_RE.search(text)
        or _VENDOR_IDENTITY_RE.match(text)
    ):
        return _MODEL_ANSWER
    if _GENERAL_IDENTITY_RE.match(text):
        return _IDENTITY_ANSWER
    return None
