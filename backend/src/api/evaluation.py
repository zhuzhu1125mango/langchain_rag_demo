"""RAG 评估 API。

提供检索评估与生成评估接口，支持单条与批量评估。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.auth import CurrentUser, get_current_user
from src.services.evaluation import GenerationEvaluator, RetrievalEvaluator

router = APIRouter(prefix="/evaluate", tags=["evaluation"])


class RetrievalEvalRequest(BaseModel):
    """检索评估请求。"""

    question: str
    retrieved_docs: List[Any]
    expected_doc_ids: Optional[List[str]] = None
    expected_contents: Optional[List[str]] = None


class RetrievalBatchRequest(BaseModel):
    """批量检索评估请求。"""

    samples: List[Dict[str, Any]]


class GenerationEvalRequest(BaseModel):
    """生成评估请求。"""

    answer: str
    question: str
    contexts: List[str]


class GenerationBatchRequest(BaseModel):
    """批量生成评估请求。"""

    samples: List[Dict[str, Any]]


@router.post("/retrieval")
async def evaluate_retrieval(
    data: RetrievalEvalRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """评估单次检索结果质量。"""
    evaluator = RetrievalEvaluator()
    result = await evaluator.evaluate(
        question=data.question,
        retrieved_docs=data.retrieved_docs,
        expected_doc_ids=data.expected_doc_ids,
        expected_contents=data.expected_contents,
    )
    return result.to_dict()


@router.post("/retrieval/batch")
async def evaluate_retrieval_batch(
    data: RetrievalBatchRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """批量评估检索结果质量。"""
    evaluator = RetrievalEvaluator()
    return await evaluator.evaluate_batch(data.samples)


@router.post("/generation")
async def evaluate_generation(
    data: GenerationEvalRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """评估单次生成答案质量（忠实度与相关度）。"""
    evaluator = GenerationEvaluator()
    result = await evaluator.evaluate(
        answer=data.answer,
        question=data.question,
        contexts=data.contexts,
    )
    return result.to_dict()


@router.post("/generation/batch")
async def evaluate_generation_batch(
    data: GenerationBatchRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """批量评估生成答案质量。"""
    evaluator = GenerationEvaluator()
    return await evaluator.evaluate_batch(data.samples)
