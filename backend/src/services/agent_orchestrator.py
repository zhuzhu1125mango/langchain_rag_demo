"""Agent 循环编排器（CODE-1 拆分后为组合类）。"""

import logging
from typing import Optional

from src.config import settings

logger = logging.getLogger("rag_system")

from src.services.agent_actions import AgentActionsMixin
from src.services.agent_planner import AgentPlannerMixin
from src.services.agent_prompts import AgentPromptsMixin
from src.services.agent_state import AgentLoopState  # noqa: F401  兼容历史导入
from src.services.agent_stream import AgentStreamMixin


class AgentOrchestrator(
    AgentPromptsMixin,
    AgentPlannerMixin,
    AgentActionsMixin,
    AgentStreamMixin,
):
    """有界 Agent 循环编排器（由各 mixin 组合，对外接口不变）。"""

    def __init__(
        self,
        llm,
        tool_manager,
        answer_generator=None,
        metadata_converter=None,
        max_steps: Optional[int] = None,
    ):
        """
        Args:
            llm: 决策用 LLM（应已绑定 think=False，辅助任务不烧思考链）。
            tool_manager: 全局 ToolManager（工具注册与执行入口）。
            answer_generator: AnswerGenerator（纯 Agent 模式综合生成）。
            metadata_converter: 来源 → 管线统一 metadata 的转换函数
                                （rag_chain._web_sources_to_metadata），可选。
            max_steps: 步数上限，缺省取 SEARCH_REACT_MAX_STEPS。
        """
        self.llm = llm
        self.tool_manager = tool_manager
        self.answer_generator = answer_generator
        self.metadata_converter = metadata_converter
        self.max_steps = max_steps or settings.search.SEARCH_REACT_MAX_STEPS

    # ------------------------------------------------------------------
    # 工具面
    # ------------------------------------------------------------------

__all__ = ["AgentOrchestrator", "AgentLoopState"]
