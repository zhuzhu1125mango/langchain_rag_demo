"""验证 Agent L1-a 跨请求记忆闭环（设计文档 agent-evolution.md §11.2）。

真实链路校验：预置记忆 → 读回校验 → 带同会话走 Agent 真实链路（确认注入不破坏流程并正常产出）。

用法（需 dev 栈 + Milvus 就绪 + AGENT_MEMORY_ENABLED=true 已注入）：
    cd backend
    $env:HF_HOME="...\\backend\\data\\model_cache\\huggingface"
    $env:PYTHONPATH="C:\\MyCode\\langchain_rag_demo\\backend"
    uv run python scripts/verify_agent_memory.py [--session SID] [--agent-off]

退出码：
    0 = 全部通过（写入 / 读回 / Agent 注入链路正常）
    1 = 写入失败
    2 = 读回失败（预置记忆未被检索到）
    3 = Agent 链路异常（注入后报错或超时）
    4 = AGENT_MEMORY_ENABLED 未开启（跳过，环境问题）
"""

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path


def _env_ready() -> None:
    """构造 RAGChain 前确保能解析 backend 包（settings 单例首次加载需 env 就绪）。"""
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))


def _build_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="验证 Agent 跨请求记忆闭环")
    p.add_argument("--session", default="", help="会话 ID（缺省随机生成）")
    p.add_argument(
        "--agent-off", action="store_true",
        help="跳过 Agent 真实链路（仅验证写入+读回，不依赖 Ollama 决策模型）",
    )
    return p.parse_args()


async def _main(args: argparse.Namespace) -> int:
    _env_ready()

    from src.config import settings  # noqa: E402

    if not settings.search.AGENT_MEMORY_ENABLED:
        print("[memory] AGENT_MEMORY_ENABLED=false，需经 env 置 true 后重跑（跳过）")
        return 4

    from src.services.rag_chain import RAGChain  # noqa: E402

    session_id = args.session or f"mem-verify-{uuid.uuid4().hex[:8]}"
    print(f"[memory] 会话: {session_id}")

    chain = await RAGChain.get_instance()
    if not chain._has_vector_store():
        print("[memory] 无向量库，无法验证（需 Milvus 就绪）")
        return 1

    # ---- 1) 写入 ----
    memory_text = "问：员工的年终奖发放规则？\n答：年终奖于春节前最后一个工作日，随当月工资发放，金额与绩效考核等级挂钩。"
    try:
        await chain._save_agent_memory(session_id, memory_text)
        print("[memory] 1) 写入成功")
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 1) 写入失败: {type(e).__name__}: {e}")
        return 1

    # 小等写入/建索引（Milvus 近实时，失败容忍时补一次）
    await asyncio.sleep(1.0)

    # ---- 2) 读回 ----
    q = "年终奖什么时候发？按什么挂钩？"
    try:
        got = await chain._load_agent_memory(q, session_id, top_k=3)
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 2) 读回异常: {type(e).__name__}: {e}")
        return 2
    if not got:
        print("[memory] 2) 读回失败：未检索到预置记忆（Milvus 可能尚未持久化）")
        return 2
    if "年终奖" not in got:
        print(f"[memory] 2) 读回内容不匹配:\n{got}")
        return 2
    print(f"[memory] 2) 读回成功（含目标记忆）:\n{got}")

    # ---- 3) Agent 真实链路（注入不破坏流程） ----
    if args.agent_off or os.environ.get("AGENT_ORCHESTRATOR_ENABLED", "false").lower() != "true":
        print("[memory] 3) 跳过 Agent 链路（--agent-off 或 AGENT_ORCHESTRATOR_ENABLED!=true）")
        return 0

    question = "那这个年终奖金额和绩效等级具体怎么对应？"
    chunks, reasoning = [], []
    start = time.perf_counter()

    async def _stream():
        async for chunk, _, _, atype in chain.arun_stream(
            question=question, kb_ids=[], history=[],
            use_web_search=False, search_mode="function_calling",
            user_id=None, session_id=session_id, deep_thinking="off",
        ):
            if atype == "reasoning":
                if chunk:
                    reasoning.append(chunk)
                continue
            if chunk:
                chunks.append(chunk)

    try:
        await asyncio.wait_for(_stream(), timeout=90)
    except asyncio.TimeoutError:
        print("[memory] 3) Agent 链路超时（90s）")
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 3) Agent 链路异常: {type(e).__name__}: {e}")
        return 3

    answer = "".join(chunks)
    print(f"[memory] 3) Agent 产出答案 {time.perf_counter() - start:.1f}s，"
          f"reasoning 步数={len(reasoning)}，答案 {len(answer)} 字:")
    print(answer[:300])
    if not answer:
        print("[memory] 3) Agent 链路未产出答案")
        return 3
    return 0


if __name__ == "__main__":
    rc = asyncio.run(_main(_build_args()))
    print(f"\n[memory] 结果: {'通过' if rc == 0 else f'失败(rc={rc})'}")
    sys.exit(rc)