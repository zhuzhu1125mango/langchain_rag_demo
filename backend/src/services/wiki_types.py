"""LLM-Wiki 编译公共类型与阈值常量（CODE-1 拆分自 wiki_compiler）。"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

# 标题匹配的 embedding 余弦阈值（低于则视为新页）
TITLE_MATCH_THRESHOLD = 0.85
# 送入 LLM 的来源材料上限（字符），防止超出本地模型上下文
MAX_MATERIAL_CHARS = 8000
# 诊断探针：key_fact 与页面句子的余弦达到该值视为「事实保留」
FACT_RETENTION_THRESHOLD = 0.75
# 诊断探针：事实保留率低于该值记 warning（对齐评估上线门槛 0.9）
FACT_RETENTION_WARN = 0.9
# P4 迭代精炼：页面事实保留率低于该值视为弱页，触发重生成
FACT_RETENTION_TARGET = 0.9
# 编译输出 token 上限：本地小模型偶发重复生成（无上限时 Ollama 默认无限生成），
# 单槽被占满会拖垮全链路，故强制截断。
COMPILE_MAX_OUTPUT_TOKENS = 2048
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")

@dataclass
class ExistingPage:
    """既有 Wiki 页（编译时用于标题匹配与增量合并）。"""

    page_id: str
    title: str
    page_type: str
    content: str

@dataclass
class CompiledPage:
    """编译产出的页面（待持久化）。"""

    title: str
    page_type: str  # entity / topic
    content: str
    # 命中的既有页（None = 新建页）
    matched: Optional[ExistingPage] = None
    key_facts: List[str] = field(default_factory=list)

@dataclass
class WikiCompileResult:
    pages_created: int = 0
    pages_updated: int = 0
    chunks_indexed: int = 0
    # 诊断探针（WIKI_DIAGNOSTIC_PROBES 开启时）：key_facts 事实保留率，None=未探测
    fact_retention_rate: Optional[float] = None
