"""Agent 安全护栏（AM-2）。

包含四部分（全部为确定性规则，不调用 LLM）：
- InputGuard：提示注入/越权指令、超长、控制字符 → 拒答
- ToolGuard：工具参数长度、URL 安全、路径穿越 → 拒绝执行
- OutputGuard：凭证/系统提示泄漏 → 脱敏
- ApprovalGate：高危工具执行前的人工审批（HITL）

设计见 docs/design/agent-guardrails-hitl.md。
"""

from .approval import ApprovalGate, get_approval_gate
from .base import GuardrailHit, REDACTION_TEXT
from .input_guard import INPUT_BLOCK_MESSAGE, InputGuard, get_input_guard
from .output_guard import OutputGuard, get_output_guard
from .tool_guard import ToolGuard, get_tool_guard

__all__ = [
    "ApprovalGate",
    "GuardrailHit",
    "INPUT_BLOCK_MESSAGE",
    "InputGuard",
    "OutputGuard",
    "REDACTION_TEXT",
    "ToolGuard",
    "get_approval_gate",
    "get_input_guard",
    "get_output_guard",
    "get_tool_guard",
]
