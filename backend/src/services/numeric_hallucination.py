"""数值幻觉检测：抽取答案/来源中的数值并按量纲比对（CODE-1 拆分自 output_sanitizer）。"""

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Tuple


logger = logging.getLogger(__name__)

class NumericHallucinationDetector:
    """检测 LLM 输出中的数字是否与参考数据一致。"""

    _NUMBER_PATTERN = re.compile(
        r"(?:约|大概|大约|约为)?\s*[¥$€£]?\s*"
        r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
        r"\s*(?:元|美元|欧元|日元|港元|英镑|元/克|美元/盎司|%)?",
        re.UNICODE,
    )

    @classmethod
    def extract_numbers(cls, text: str) -> List[Dict[str, Any]]:
        """从文本中提取带上下文的数值。"""
        results = []
        for m in cls._NUMBER_PATTERN.finditer(text):
            raw = m.group(0)
            num_str = m.group(1).replace(",", "")
            try:
                value = Decimal(num_str)
            except InvalidOperation:
                continue
            start = max(0, m.start() - 20)
            end = min(len(text), m.end() + 20)
            context = text[start:end]
            results.append({
                "value": value,
                "raw": raw,
                "context": context,
                "position": m.start(),
            })
        return results

    @classmethod
    def detect(
        cls,
        answer: str,
        reference_numbers: List[Decimal],
        tolerance: float = 0.05,
    ) -> Tuple[bool, List[str]]:
        """检测答案中的数值是否超出参考范围。

        Args:
            answer: LLM 生成的答案。
            reference_numbers: 参考数值列表（如工具返回的价格）。
            tolerance: 相对偏差容忍度。

        Returns:
            (has_hallucination, warnings)
        """
        if not reference_numbers:
            numbers = cls.extract_numbers(answer)
            if numbers:
                return True, ["无权威参考数据，但答案包含具体数值，可能存在幻觉"]
            return False, []

        numbers = cls.extract_numbers(answer)
        hallucinated = []

        for num in numbers:
            value = num["value"]
            closest = min(reference_numbers, key=lambda v: abs(v - value))
            if closest == 0:
                deviation = float("inf")
            else:
                deviation = abs(value - closest) / closest
            if deviation > tolerance:
                hallucinated.append({
                    "value": value,
                    "closest_reference": closest,
                    "deviation": deviation,
                    "context": num["context"],
                })

        warnings = [
            f"答案中的数值 {h['value']} 与参考值 {h['closest_reference']} 偏差 {h['deviation']:.2%}"
            for h in hallucinated
        ]
        return bool(hallucinated), warnings
