"""Agent 循环状态（CODE-1 拆分：供编排器与各 mixin 共用，避免循环导入）。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List

@dataclass
class AgentLoopState:
    """单次请求的循环状态（不跨请求复用）。"""

    scratchpad: List[Dict[str, Any]] = field(default_factory=list)
    seen_calls: set = field(default_factory=set)   # 同参数调用去重
    steps: int = 0
    deadline: float = 0.0
    plan: List[str] = field(default_factory=list)   # L2 分层规划：剩余待执行提纲（步骤描述）
    plan_done: List[str] = field(default_factory=list)  # 已完成规划步骤（推动计划修订）
    plan_dirty: bool = False                        # plan 是否已产出（首步规划后置 True）
    needs_web: bool = False                         # 混合问题：需 kb+web 双源（由路由 hybrid 意图注入）
