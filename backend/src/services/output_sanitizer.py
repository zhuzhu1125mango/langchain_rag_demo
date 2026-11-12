"""输出清洗模块（CODE-1 拆分后保留清洗主链路）。

检测并清洗 LLM 输出中的工具调用 JSON 污染、代码块、思考标签等异常内容。

拆分子模块：
- `numeric_hallucination`      数值幻觉检测（答案数值不在来源中出现时告警）
- `answer_verification_types`  校验结果类型与正则常量
- `answer_verifier_checks`     数值/日期/引用/跨源一致性等具体检查项
- `answer_verifier`            校验器编排（本模块再导出，保持历史导入）
"""

import json
import logging
import re
from decimal import Decimal
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)

from src.services.numeric_hallucination import NumericHallucinationDetector

# 污染模式正则
_TOOL_CALL_POLLUTION_PATTERNS = [
    re.compile(r'\[\s*\{\s*"name"\s*:\s*"\w+"', re.IGNORECASE),
    re.compile(r'\{\s*"name"\s*:\s*"\w+"', re.IGNORECASE),
    re.compile(r'"tool"\s*:\s*"\w+"', re.IGNORECASE),
    re.compile(r'"arguments"\s*:\s*\{\s*"', re.IGNORECASE),
    re.compile(r'<tool_call>\s*\[', re.IGNORECASE),
    re.compile(r'<tool_call>\s*\{', re.IGNORECASE),
    re.compile(r'<RichMediaReference>', re.IGNORECASE),
]

# 工具调用「叙述」污染（非 JSON）：模型把工具调用过程/scratchpad 写进正文。
# 3B 模型在停止调用工具后偶发把「第 N 步调用 X(...) / 观察结果：... / 下一步操作：调用 X({...})」
# 当作最终答案输出（2026-10-20 基线实证：web_01/web_03）。
_TOOL_NARRATION_PATTERNS = [
    re.compile(r"^\s*第\s*\d+\s*步"),                 # 第 2 步调用 ...
    re.compile(r"^\s*观察结果\s*[：:]"),               # 观察结果：
    re.compile(r"^\s*下一步操作\s*[：:]"),             # 下一步操作：
    re.compile(r"^\s*调用\s*\w+\s*\("),               # 调用 web_search(...
    re.compile(r"调用\s*\w+\s*\(\s*\{"),              # 调用 web_search({...
]

# 需要过滤的思考/元数据标签
_THINKING_TAG_PATTERNS = [
    re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE),
    re.compile(r'<thinking>.*?</thinking>', re.DOTALL | re.IGNORECASE),
    re.compile(r'</think>', re.IGNORECASE),
]

# json 代码块
_JSON_CODE_BLOCK_PATTERN = re.compile(r'```json\s*([\s\S]*?)\s*```', re.IGNORECASE)

# 答案开头常见的搜索过程前缀（用于兜底过滤）
_ANSWER_PREFIX_PATTERNS = [
    re.compile(r'^(根据搜索结果[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(根据参考信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(根据提供的参考信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(资料显示[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(数据显示[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(经查询[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(我查询到[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(我认为[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(在我看来[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(根据以上信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(综上所述[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(结合参考信息[，：:；]?)\s*', re.IGNORECASE),
    re.compile(r'^(结合以上信息[，：:；]?)\s*', re.IGNORECASE),
]

class OutputSanitizer:
    """LLM 输出清洗器。"""

    @staticmethod
    def looks_like_tool_call(text: str) -> bool:
        """判断文本是否包含工具调用 JSON 污染。"""
        if not text:
            return False
        return any(p.search(text) for p in _TOOL_CALL_POLLUTION_PATTERNS)

    @staticmethod
    def looks_like_tool_narration(text: str) -> bool:
        """判断文本是否含「工具调用叙述」污染（非 JSON 的过程文本）。"""
        if not text:
            return False
        return any(p.search(text) for p in _TOOL_NARRATION_PATTERNS)

    @staticmethod
    def extract_json_code_block(text: str) -> Optional[str]:
        """从文本中提取 ```json ... ``` 代码块内容。"""
        match = _JSON_CODE_BLOCK_PATTERN.search(text)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def remove_thinking_tags(text: str, strip_edges: bool = True) -> str:
        """移除 <think> / <thinking> 等思考标签。

        `strip_edges=False` 用于**流式片段**：片段之间靠空格连接，逐片 strip 会把
        边界上的空格吃掉（见 `sanitize` 的说明）。
        """
        cleaned = text
        for pattern in _THINKING_TAG_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip() if strip_edges else cleaned

    @staticmethod
    def remove_answer_prefix(text: str, strip_edges: bool = True) -> str:
        """移除答案开头常见的搜索过程前缀（兜底过滤）。

        仅处理文本开头处的前缀，不修改正文内容。
        """
        if not text:
            return text
        cleaned = text
        for pattern in _ANSWER_PREFIX_PATTERNS:
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip() if strip_edges else cleaned

    @classmethod
    def sanitize(
        cls,
        text: str,
        reference_numbers: Optional[List[Decimal]] = None,
        low_confidence_threshold: float = 0.6,
        strip_edges: bool = True,
    ) -> Tuple[str, bool]:
        """清洗 LLM 输出。

        Args:
            text: 原始 LLM 输出。
            reference_numbers: 可选的参考数值列表，用于数字幻觉检测。
            low_confidence_threshold: 低置信度阈值，低于此值时对具体数字更严格。
            strip_edges: 是否剥掉首尾空白。**流式调用必须传 False。**

        Returns:
            (cleaned_text, was_polluted): 清洗后的文本，以及是否检测到污染。

        Note:
            `strip_edges` 存在的原因是一个真实的生产缺陷（2026-11-12）：本方法被
            **逐 chunk** 调用时，每个片段两端的空格都被剥掉，而流式 chunk 的边界
            经常正好落在空格上 —— 于是「docker logs backend-prod 2>&1 | grep -E」
            落库后变成「dockerlogsbackend-prod2>&1|grep-E」。中文看不出问题，
            代码块和英文则完全粘连、无法复制。

            清洗规则里只有「剥首尾空白」和「移除开头前缀」依赖整段语义，
            其余（思考标签、工具调用过滤）对片段是安全的。流式路径传 False 即可。
        """
        if not text:
            return text, False

        was_polluted = cls.looks_like_tool_call(text) or cls.looks_like_tool_narration(text)

        # 移除思考标签
        cleaned = cls.remove_thinking_tags(text, strip_edges=strip_edges)

        # 如果整段是 JSON 工具调用，直接清空
        stripped = cleaned.strip()
        if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict) and "name" in data and "arguments" in data:
                    return "", True
                if isinstance(data, list) and all(
                    isinstance(item, dict) and "name" in item for item in data
                ):
                    return "", True
            except Exception:
                pass

        # 移除 json 代码块（如果是工具调用）
        def replace_json_block(match):
            content = match.group(1).strip()
            if cls.looks_like_tool_call(content):
                return ""
            return content

        cleaned = _JSON_CODE_BLOCK_PATTERN.sub(replace_json_block, cleaned)

        # 移除 <tool_call> 标签块
        cleaned = re.sub(r'<tool_call>\s*[\s\S]*?\s*</tool_call>', '', cleaned, flags=re.IGNORECASE)

        # 移除独立的工具 JSON 行 / 工具调用叙述行
        #
        # 只在确实删掉行时才重建文本：`splitlines()` + `"\n".join()` 会顺手吃掉
        # 结尾的换行符，而流式片段经常正好以换行结尾——重建一次就丢一个换行，
        # 用户看到的是整段黏在一起。没有命中删除时保持原文不动。
        raw_lines = cleaned.splitlines(keepends=True)
        kept: List[str] = []
        dropped = False
        for line in raw_lines:
            probe = line.rstrip("\r\n")
            if cls.looks_like_tool_call(probe):
                dropped = True
                continue
            if cls.looks_like_tool_narration(probe):
                was_polluted = True
                dropped = True
                continue
            kept.append(line)
        if dropped:
            cleaned = "".join(kept)

        # 数字幻觉检测
        if reference_numbers:
            has_hallucination, warnings = NumericHallucinationDetector.detect(
                cleaned, reference_numbers, tolerance=low_confidence_threshold / 12
            )
            if has_hallucination:
                # 在答案末尾追加风险提示，不直接删除整段（避免误杀）
                cleaned += "\n\n[系统提示：以上回答中的部分数值与权威数据源存在偏差，建议您通过官方渠道核实。]"
                was_polluted = True

        # 兜底移除答案开头常见搜索过程前缀（整段语义：逐片调用只对首片有意义）
        cleaned = cls.remove_answer_prefix(cleaned, strip_edges=strip_edges)

        return (cleaned.strip() if strip_edges else cleaned), was_polluted

# 兼容历史导入（rag_chain 与评估脚本直接引用）
from src.services.answer_verification_types import VerificationResult  # noqa: F401
from src.services.answer_verifier import AnswerVerifier  # noqa: F401

__all__ = ["OutputSanitizer", "NumericHallucinationDetector", "AnswerVerifier", "VerificationResult"]
