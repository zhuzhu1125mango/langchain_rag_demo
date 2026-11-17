"""助手消息的落库契约（`sessions.messages` 里每条 assistant 消息的形状）。

**为什么需要这个模块**
会话消息存在 JSONB 数组里，没有列约束、没有 DB 层面的类型检查，字段完全靠写入方
自觉。字段散落在调用点上的必然结果是：同一条消息里出现三种时间戳命名、两个版本
的模型字段，读的人得靠猜。集中到一处之后：
- 形状由 Pydantic 模型保证（多一个键会被拒，而不是悄悄写进库）
- 缺省语义由构造函数保证（「未知」与「默认模型」是两件事）
- 读的人只需要看这一个文件

**为什么模型归因是快照，而不是外键**
管理员随时可能改掉 deployment 的 `model_name`，甚至删掉它。如果消息里只存一个 id，
配置一变，历史回看就会「说现在的话」——那不是记录，是重新解释。
所以同时存：`model_id`（用于回连配置）+ `model_name` / `display_name` / `provider_type`
（当时的快照，配置消失后仍然说得清「当时跑的是什么」）。

**为什么这里不承担分析职责**
「各模型的调用量/成本/延迟」由 `model_usage_daily` 按天聚合（provider × model × task，
含 tokens 与成本），那是另一个管道。在 `sessions.messages` 上做
`jsonb_array_elements` 聚合既慢又难写，还会把在线业务表变成报表库。
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# 归因块的版本号。以后要加字段（比如 cost、system_fingerprint）就升版本，
# 读侧按版本分支，而不是猜「这个键什么时候有的」。
GENERATION_SCHEMA_VERSION = 1


class GenerationInfo(BaseModel):
    """一条回答的生成归因。

    `extra="forbid"`：拼错字段名必须当场报错。JSONB 没有列校验，这是唯一的防线。
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=GENERATION_SCHEMA_VERSION)
    # 配置侧 id（可能指向已删除的 deployment），用于回连
    model_id: str
    # 上游真实模型名，如 `qwen3:8b`；与 id 的区别在于它直接对得上模型服务里的名字
    model_name: str = ""
    # 展示名，UI 用；为空时读侧应回退到 model_name
    display_name: str = ""
    # 协议族（ollama / openai_compatible），排查协议差异时用；
    # 刻意不存 provider_id——那是可被改名的内部标签，不是事实
    provider_type: str = ""
    # 这条回答是「用户选的」还是「走默认路由」
    selected_by: Literal["user", "default"] = "default"
    # 所选模型在生成时失败、由 fallback 接手
    fallback: bool = False
    # 发生回落时，原本请求的是哪个模型
    fallback_from: Optional[str] = None


def build_generation_info(
    *,
    model_id: str,
    model_name: str = "",
    display_name: str = "",
    provider_type: str = "",
    selected_by: str = "default",
    fallback: bool = False,
    fallback_from: Optional[str] = None,
) -> dict:
    """构造归因块。空字符串与 None 会被丢掉，`fallback` 始终保留（显式布尔更好读）。"""
    info = GenerationInfo(
        model_id=model_id,
        model_name=model_name,
        display_name=display_name,
        provider_type=provider_type,
        selected_by=selected_by,  # type: ignore[arg-type]  Literal 由模型校验
        fallback=fallback,
        fallback_from=fallback_from,
    )
    dumped = info.model_dump(exclude_none=True)
    return {key: value for key, value in dumped.items() if value != ""}


def build_assistant_message_payload(
    *,
    message_id: str,
    content: str,
    sources: Optional[list] = None,
    source_metadata: Optional[list] = None,
    timestamp: str,
    reasoning: Optional[list] = None,
    thinking: str = "",
    execution_id: Optional[str] = None,
    stopped: bool = False,
    error: Optional[str] = None,
    generation: Optional[dict] = None,
) -> dict:
    """组装一条助手消息。

    三条可选的「状态」字段只在真发生时出现（`stopped` / `error` / `generation`）：
    JSONB 里没法靠列默认值表达「没发生」，只能靠「键不存在」。反过来，
    如果写成 `fallback: false` 遍地都是，读的人就无法区分「没回落」和「没记录」。
    """
    payload: dict = {
        "id": message_id,
        "role": "assistant",
        "content": content,
        "sources": sources or [],
        "source_metadata": source_metadata or [],
        "timestamp": timestamp,
        # 保持既有行为：execution_id 恒出现（历史数据里可能有 null），不借这次改动改变语义
        "execution_id": execution_id,
    }
    if reasoning:
        payload["reasoning"] = reasoning
    if thinking:
        payload["thinking"] = thinking
    if stopped:
        payload["stopped"] = True
    if error:
        payload["error"] = error
    if generation:
        payload["generation"] = generation
    return payload
