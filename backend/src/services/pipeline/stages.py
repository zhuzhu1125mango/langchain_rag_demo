"""统一问答管线各阶段（自 rag_chain.py 拆出的 Mixin）。

CODE-1 拆分后本模块只负责组装：各阶段实现按「阶段分组」拆到同目录模块，
仍由 `RAGChain` 通过 `PipelineStagesMixin` 统一继承，方法名与调用顺序不变。

- `pipeline.orchestration`     `_pipeline` / `_pipeline_process`：流式与非流式共用编排
- `pipeline.pre_stages`        上下文增强、意图路由、工具优先、KB 决策
- `pipeline.cache_stages`      语义缓存查找 / 回放 / 写入
- `pipeline.kb_stage`          知识库检索（混合检索 + rerank + 来源归并）
- `pipeline.generation_stages` 答案生成（KB / 直答）与 token 用量记账
- `pipeline.finalize_stages`   终态副作用与 `final` 事件
"""

from src.services.pipeline.cache_stages import CacheStagesMixin
from src.services.pipeline.finalize_stages import FinalizeStagesMixin
from src.services.pipeline.generation_stages import (
    GenerationStagesMixin,
    _kb_source_to_doc,  # noqa: F401  兼容历史导入（测试直接引用）
)
from src.services.pipeline.kb_stage import KbStageMixin
from src.services.pipeline.orchestration import PipelineOrchestrationMixin
from src.services.pipeline.pre_stages import PreStagesMixin


class PipelineStagesMixin(
    PipelineOrchestrationMixin,
    PreStagesMixin,
    CacheStagesMixin,
    KbStageMixin,
    GenerationStagesMixin,
    FinalizeStagesMixin,
):
    """问答管线编排与各阶段实现（由 RAGChain 组装）。"""


__all__ = ["PipelineStagesMixin", "_kb_source_to_doc"]
