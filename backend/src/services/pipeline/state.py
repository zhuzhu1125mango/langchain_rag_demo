"""问答管线共享状态、常量与工具函数（自 rag_chain.py 拆出）。

包含：提示词模板、深度思考开关决策、请求级 ContextVar、管线状态对象、
阶段计时器、Agent 记忆 doc_id 工具。
"""

import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.config import settings
from src.services.decision_pipeline import DecisionResult

logger = logging.getLogger("rag_system")

# 请求级决策/检索状态。RAGChain 是进程级单例，此前将决策结果挂在
# self 上，并发请求会互相覆盖（串号）。ContextVar 在每个 asyncio Task
# 中独立复制：并发请求互不可见；同一 Task 内 set 后可被后续代码读取。
# 注意：set 与 get 必须发生在同一 Task（不要跨 asyncio.create_task 读取）。
_request_decision: ContextVar[Optional[DecisionResult]] = ContextVar(
    "rag_request_decision", default=None
)
_request_retrieval_score: ContextVar[float] = ContextVar(
    "rag_request_retrieval_score", default=0.0
)

# P1-3 语义缓存：后台写入任务引用集，防止 fire-and-forget 任务被垃圾回收
_background_store_tasks: set = set()


# ----------------------------------------------------------------------
# 提示词模板（流式/非流式共用的单一事实来源，消除两套模板漂移）
# ----------------------------------------------------------------------
KB_ANSWER_TEMPLATE = """
你是一个严谨的智能助手，请基于以下参考信息回答用户问题。

回答要求：
1. 优先使用参考信息中的内容，禁止编造参考信息里不存在的信息。
2. 如果参考信息中没有答案，直接说明"无法找到相关信息"。
3. 关键事实必须标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
4. 如果同时包含知识库和联网搜索结果，优先以知识库内容为准，联网搜索作为补充。
5. 对于价格、日期、数据等时效性信息，优先提取具体数值并突出展示，
   标注数据来源；若多个来源数据不一致，给出范围而非随机选取。
6. **禁止输出任何工具调用 JSON、```json 代码块、<tool_call> 标签或 <RichMediaReference> 思考过程**。
7. **禁止在回答开头使用"根据搜索结果"、"根据参考信息"、"资料显示"、"我认为"等前缀，直接陈述答案**。
8. 当工具结果置信度低于 60% 时，不要以绝对语气给出具体数值，应说明"数据可能存在偏差，建议通过官方渠道核实"。

对话历史:
{history}

参考信息:
{context}

当前问题:
{question}

请结合参考信息给出准确、连贯的回答：
"""

LLM_DIRECT_TEMPLATE = """
你是一个智能助手。本次联网搜索未能返回有效结果。
请基于你已有的知识回答用户问题：
- 非时效性问题：正常回答。
- 涉及时效性信息（如价格、新闻、天气、实时数据等）：基于你的知识作答，
  并在回答末尾标注"以上信息基于训练数据，可能不具备实时性，建议核实最新情况"，
  不要简单拒绝或只说"无法获取实时信息"。

回答要求：
1. **禁止在回答开头使用"根据搜索结果"、"根据参考信息"、"资料显示"、"我认为"等前缀，直接陈述答案**。
2. 保持回答的连贯性和上下文一致性。

对话历史:
{history}

当前问题:
{question}

请结合历史对话进行回答：
"""


# 深度思考开关决策（on/off 两态，前端开关透传）
def should_think(deep_thinking: str = "off") -> Optional[bool]:
    """深度思考开关决策。

    Returns:
        True: 开启思考；False: 关闭思考；
        None: 不干预，使用模型默认行为（模型不支持思考时）。
    """
    if not settings.model.OLLAMA_SUPPORTS_THINKING:
        return None
    return deep_thinking == "on"


@dataclass
class _PipelineState:
    """问答管线各阶段的共享状态（单次请求内有效，不跨请求复用）。

    事件协议（_pipeline 及各阶段 yield 的元组，首元素为事件类型）：
    - ("reasoning", payload_str): reasoning 过程事件（流式对外转发，非流式忽略）
    - ("thinking", text): 模型原始思考增量（流式对外转发，非流式忽略）
    - ("chunk", text, source_texts, source_metadata, answer_type): 答案片段
    - ("final", state): 终态事件，携带完整状态供 run() 做非流式后处理
    """

    question: str
    kb_ids: Optional[List[str]]
    history: Optional[List[dict]]
    use_web_search: bool
    search_mode: str
    # 深度思考开关：on | off（前端开关透传）
    deep_thinking: str = "off"
    # 深度思考决策结果（True/False 强制，None 用模型默认）
    think: Optional[bool] = None
    # Trace 归属用户 ID（run()/arun_stream() 透传）
    user_id: Optional[str] = None
    # 会话 ID（arun_stream() 透传）：Agent L1-a 跨请求记忆按会话隔离注入/写入
    session_id: Optional[str] = None
    # 语义缓存：本次请求是否已命中（命中后跳过写缓存）、查询向量（finalize 时复用写入）
    semantic_cache_hit: bool = False
    semantic_cache_embedding: Optional[Any] = None
    # 终态清理是否已执行（SSE 断连/finally 兜底与正常路径共用，幂等防重）
    finalized: bool = False

    trace: Any = None
    resolved_question: str = ""
    history_context: str = ""

    intent_decision: Any = None
    decision: Optional[DecisionResult] = None
    use_kb: bool = False
    is_agent_mode: bool = False

    docs: List[Any] = field(default_factory=list)
    source_texts: List[str] = field(default_factory=list)
    source_metadata: List[Dict] = field(default_factory=list)
    search_context: str = ""
    cross_source_data: Optional[Dict] = None
    web_sources_for_citation: List[Dict] = field(default_factory=list)
    answer_type: str = "llm_direct"

    # 累积的最终答案（清洗后的全部 chunk 拼接），终态写入 trace
    final_answer: str = ""
    # 阶段置 True 表示流程已产出完整回答（datetime/tool_first/纯 Agent 短路）
    finished: bool = False

    # P1-2 可观测性：LLM 返回的 token 元数据（最后一个携带计数的 chunk 生效）
    llm_token_meta: Optional[Dict[str, Any]] = None
    # token 用量是否已写入 trace（避免重复估算）
    token_usage_recorded: bool = False


class _StageTimer:
    """阶段计时上下文管理器：退出时把阶段名/状态/耗时写入 Trace（P1-2）。"""

    def __init__(self, trace, name: str):
        self._trace = trace
        self._name = name
        self._t0 = 0.0

    async def __aenter__(self):
        self._t0 = time.time()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        status = "error" if exc_type else "done"
        self._trace.add_stage(self._name, status, int((time.time() - self._t0) * 1000))
        return False


def _memory_doc_id(session_id: str) -> str:
    """为跨请求记忆生成确定性 document_id（UUIDv5，基于 session_id）。

    同会话得到同一 document_id，使记忆切片聚合可统一检索；不同会话 UUID 不同，
    天然隔离。必须产出合法 UUID——Milvus 过滤表达式 `_safe_id` 仅放行 UUID，
    拒绝 `memory:{session_id}` 这类带冒号的字符串（防注入白名单）。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-memory:{session_id}"))
