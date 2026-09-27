"""RAG 问答管线子包（从 rag_chain.py 按职责拆分，架构还债 2026-09-23）。

拆分原则：
- 行为与对外 API 完全不变：RAGChain 仍从 src.services.rag_chain 导入，
  历史符号（_PipelineState / should_think / _memory_doc_id / 请求级 ContextVar）
  经 rag_chain.py 再导出保持兼容。
- rag_chain.py 只保留类构造（__init__/_async_init）与生命周期（get_instance/close），
  其余方法按职责落入各 Mixin，由 RAGChain 多继承组装。
"""
