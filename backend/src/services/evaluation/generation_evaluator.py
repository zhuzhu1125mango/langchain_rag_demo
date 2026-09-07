"""生成评估器。

使用 LLM-as-judge 评估生成答案的忠实度（Faithfulness）与问题相关度（Relevance）。
评分范围 0-1，并返回简要理由。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.services.model_manager import model_manager
from src.services.prompts.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


@dataclass
class GenerationEvalResult:
    """生成评估结果。"""

    faithfulness: float = 0.0
    relevance: float = 0.0
    faithfulness_reason: str = ""
    relevance_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "faithfulness": self.faithfulness,
            "relevance": self.relevance,
            "faithfulness_reason": self.faithfulness_reason,
            "relevance_reason": self.relevance_reason,
        }


class GenerationEvaluator:
    """基于 LLM 的生成质量评估器。"""

    def __init__(
        self,
        llm: Optional[Any] = None,
        prompt_loader: Optional[PromptLoader] = None,
    ):
        self.llm = llm
        self.prompt_loader = prompt_loader or PromptLoader()

    async def _get_llm(self) -> Any:
        """延迟初始化评估用 LLM。"""
        if self.llm is not None:
            return self.llm
        try:
            # B1 工厂：fast 角色 + temperature=0 + think=False（评分任务无需思考链）
            self.llm = await model_manager.get_chat_llm("fast", think=False, temperature=0.0)
        except Exception as e:
            logger.warning(f"生成评估器 LLM 初始化失败: {e}")
        return self.llm

    @staticmethod
    def _parse_score(text: str) -> float:
        """从模型输出中解析 0-1 浮点分数。"""
        if not text:
            return 0.0
        # 优先匹配 JSON 中的 score 字段
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "score" in data:
                return float(data["score"])
        except Exception:
            pass

        # 匹配文本中的分数模式：0.75 或 75%
        matches = re.findall(r"(\d+(?:\.\d+)?)", text)
        if matches:
            for m in matches:
                val = float(m)
                if 0 <= val <= 1:
                    return val
                if 0 <= val <= 100:
                    return val / 100.0
        return 0.0

    @staticmethod
    def _parse_reason(text: str) -> str:
        """从模型输出中解析理由。"""
        if not text:
            return ""
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "reason" in data:
                return str(data["reason"]).strip()
        except Exception:
            pass
        return text.strip()

    async def _invoke_prompt(self, prompt: str) -> str:
        """异步调用 LLM 并返回文本输出。"""
        llm = await self._get_llm()
        if llm is None:
            return ""
        try:
            response = await llm.ainvoke(prompt)
            return response.content if response else ""
        except Exception as e:
            logger.warning(f"生成评估 LLM 调用失败: {e}")
            return ""

    async def evaluate_faithfulness(
        self,
        answer: str,
        contexts: Sequence[str],
    ) -> Dict[str, Any]:
        """评估答案对参考上下文的忠实度。"""
        if not answer or not contexts:
            return {"score": 0.0, "reason": "答案或上下文为空"}

        context_text = "\n---\n".join(contexts)
        if self.prompt_loader.exists("eval_faithfulness"):
            prompt = await self.prompt_loader.load(
                "eval_faithfulness",
                context=context_text,
                answer=answer,
            )
        else:
            prompt = f"""你是一名严格的答案质量评估专家。请判断以下回答是否完全基于参考信息，未引入参考信息之外的内容。

参考信息：
{context_text}

回答：
{answer}

请输出 JSON 格式：{{"score": 0-1 之间的浮点数, "reason": "简短理由"}}
评分标准：
- 1.0：回答中所有事实性陈述均可在参考信息中找到依据。
- 0.0：回答包含参考信息中没有的臆测或错误信息。
- 中间值：部分事实有依据，部分无依据。
"""

        output = await self._invoke_prompt(prompt)
        return {
            "score": self._parse_score(output),
            "reason": self._parse_reason(output),
        }

    async def evaluate_relevance(
        self,
        answer: str,
        question: str,
    ) -> Dict[str, Any]:
        """评估答案对问题的相关度。"""
        if not answer or not question:
            return {"score": 0.0, "reason": "答案或问题为空"}

        if self.prompt_loader.exists("eval_relevance"):
            prompt = await self.prompt_loader.load(
                "eval_relevance",
                question=question,
                answer=answer,
            )
        else:
            prompt = f"""你是一名严格的答案质量评估专家。请判断以下回答是否直接、完整地回答了用户问题。

用户问题：
{question}

回答：
{answer}

请输出 JSON 格式：{{"score": 0-1 之间的浮点数, "reason": "简短理由"}}
评分标准：
- 1.0：回答直接、完整地回答了问题。
- 0.0：回答与问题无关或完全未回答问题。
- 中间值：回答部分相关或遗漏关键信息。
"""

        output = await self._invoke_prompt(prompt)
        return {
            "score": self._parse_score(output),
            "reason": self._parse_reason(output),
        }

    async def evaluate(
        self,
        answer: str,
        question: str,
        contexts: Sequence[str],
    ) -> GenerationEvalResult:
        """同时评估忠实度与相关度。

        Args:
            answer: 生成的答案。
            question: 用户问题。
            contexts: 参考上下文列表。

        Returns:
            GenerationEvalResult: 评估结果。
        """
        faithfulness_result = await self.evaluate_faithfulness(answer, contexts)
        relevance_result = await self.evaluate_relevance(answer, question)

        return GenerationEvalResult(
            faithfulness=faithfulness_result["score"],
            relevance=relevance_result["score"],
            faithfulness_reason=faithfulness_result["reason"],
            relevance_reason=relevance_result["reason"],
        )

    async def evaluate_batch(
        self,
        samples: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """批量评估多个答案。

        Args:
            samples: 每个样本包含 answer、question、contexts。

        Returns:
            聚合评估报告。
        """
        results = []
        for sample in samples:
            result = await self.evaluate(
                answer=sample.get("answer", ""),
                question=sample.get("question", ""),
                contexts=sample.get("contexts", []),
            )
            results.append(result.to_dict())

        if not results:
            return {"samples": [], "aggregated": {}}

        aggregated = {
            "faithfulness": sum(r["faithfulness"] for r in results) / len(results),
            "relevance": sum(r["relevance"] for r in results) / len(results),
        }
        return {"samples": results, "aggregated": aggregated}
