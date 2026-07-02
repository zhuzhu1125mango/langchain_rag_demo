"""Badcase 反馈闭环模块。

处理用户负面反馈，自动分类问题根因，并触发在线学习以改进后续回答质量。
"""

from .feedback_handler import BadcaseFeedbackHandler, BadcaseCategory

__all__ = [
    "BadcaseFeedbackHandler",
    "BadcaseCategory",
]
