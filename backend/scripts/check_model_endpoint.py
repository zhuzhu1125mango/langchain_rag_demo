"""检查一个 OpenAI 兼容 / Ollama 端点是否够用来当本项目的数据源。

为什么单独一个脚本：把「provider 能用」拆成四个可分别失败的检查，而不是一句
「连不上」。切换模型服务时（Ollama → LM Studio / vLLM / 云端），失败原因通常
就是这四类之一，处置完全不同：

  1. reachable   —— 端口/网络/是否绑 0.0.0.0（Docker 里 host.docker.internal）
  2. models      —— 有没有你要的那个模型名（名字写错是最常见的错）
  3. chat        —— 能不能真的生成（有些端点列得出模型但没加载权重）
  4. embedding   —— 向量维度是否与 EMBEDDING_DIMENSION 一致（不一致要重灌语料）

第 4 项是本项目最容易踩的：语料里的向量是用 bge-m3（1024 维）建的，
换一个 768 维的 embedding 不会报错，只会**静默检索不到**。

用法（在容器里跑，才等价于后端看到的网络）：

    docker exec backend-prod python scripts/check_model_endpoint.py \
        --base-url http://host.docker.internal:1234/v1

    # 指定要校验的模型名
    docker exec backend-prod python scripts/check_model_endpoint.py \
        --base-url http://host.docker.internal:1234/v1 \
        --chat-model qwen2.5-7b-instruct --embedding-model text-embedding-bge-m3
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx

DEFAULT_TIMEOUT = 20.0


def _headers(api_key: Optional[str]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def list_models(client: httpx.AsyncClient, base: str, headers: Dict[str, str]) -> List[str]:
    """列模型。同时兼容 OpenAI 的 /v1/models 与 Ollama 的 /api/tags。"""
    resp = await client.get(f"{base}/models", headers=headers)
    if resp.status_code == 404:
        # 可能是 Ollama 原生端点
        root = base[:-3].rstrip("/") if base.endswith("/v1") else base
        resp = await client.get(f"{root}/api/tags", headers=headers)
        resp.raise_for_status()
        return [item.get("name", "") for item in resp.json().get("models", [])]
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload, dict) and "data" in payload:
        return [item.get("id", "") for item in payload["data"]]
    if isinstance(payload, dict) and "models" in payload:  # Ollama 形状
        return [item.get("name", "") for item in payload["models"]]
    return []


async def try_chat(
    client: httpx.AsyncClient, base: str, headers: Dict[str, str], model: str
) -> Tuple[bool, str]:
    """真发一次最小请求：列得出模型 ≠ 模型加载好了。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
    }
    try:
        resp = await client.post(f"{base}/chat/completions", headers=headers, json=body)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return False, f"返回里没有 choices: {str(data)[:200]}"
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 —— 这里就是要把异常翻译成人话
        return False, f"{type(exc).__name__}: {exc}"


async def try_embedding(
    client: httpx.AsyncClient, base: str, headers: Dict[str, str], model: str
) -> Tuple[bool, str, Optional[int]]:
    body = {"model": model, "input": ["维度探测"]}
    try:
        resp = await client.post(f"{base}/embeddings", headers=headers, json=body)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}", None
        data = resp.json()
        items = data.get("data") or []
        if not items:
            return False, f"返回里没有 data: {str(data)[:200]}", None
        vector = items[0].get("embedding") or []
        return True, "ok", len(vector)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", None


async def run(args: argparse.Namespace) -> int:
    base = args.base_url.rstrip("/")
    headers = _headers(args.api_key)
    expected_dim = args.expected_dim
    failures: List[str] = []

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        print(f"[1/4] 连通性  {base}")
        try:
            models = await list_models(client, base, headers)
        except Exception as exc:  # noqa: BLE001
            print(f"      ✗ 不可达：{type(exc).__name__}: {exc}")
            print("\n提示：Docker 里的后端走 host.docker.internal，")
            print("LM Studio 必须允许局域网访问（只绑 127.0.0.1 时容器连不上）。")
            return 2
        print(f"      ✓ 可达，模型 {len(models)} 个")

        print("[2/4] 模型列表")
        for name in models[:20]:
            print(f"      - {name}")
        if not models:
            print("      ✗ 端点可达但没有加载任何模型")

        chat_model = args.chat_model or next(
            (m for m in models if not any(k in m.lower() for k in ("embed", "bge", "gte"))), ""
        )
        embed_model = args.embedding_model or next(
            (m for m in models if any(k in m.lower() for k in ("embed", "bge", "gte"))), ""
        )

        print(f"[3/4] 生成  model={chat_model or '(未指定)'}")
        if not chat_model:
            print("      ✗ 没找到可用的 chat 模型，用 --chat-model 指定")
            failures.append("chat")
        else:
            ok, detail = await try_chat(client, base, headers, chat_model)
            print(f"      {'✓' if ok else '✗'} {detail}")
            if not ok:
                failures.append("chat")

        print(f"[4/4] 向量  model={embed_model or '(未指定)'}")
        if not embed_model:
            print("      ✗ 没找到 embedding 模型，用 --embedding-model 指定")
            failures.append("embedding")
        else:
            ok, detail, dim = await try_embedding(client, base, headers, embed_model)
            if not ok:
                print(f"      ✗ {detail}")
                failures.append("embedding")
            else:
                match = dim == expected_dim
                print(f"      {'✓' if match else '✗'} 维度 {dim}（期望 {expected_dim}）")
                if not match:
                    print(
                        "      ⚠ 维度不一致：现有语料是用 "
                        f"{expected_dim} 维建的，换了维度必须重灌，"
                        "否则检索会静默返回空结果。"
                    )
                    failures.append("embedding_dim")

    if failures:
        print(f"\n结论：不可直接切换（{', '.join(failures)}）")
        return 1
    print("\n结论：四项全过，可以作为 provider 接入。")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="检查模型端点是否可用作本项目 provider")
    ap.add_argument("--base-url", required=True, help="形如 http://host.docker.internal:1234/v1")
    ap.add_argument("--api-key", default="", help="多数本地服务不需要")
    ap.add_argument("--chat-model", default="", help="不填则自动挑一个非 embedding 的")
    ap.add_argument("--embedding-model", default="", help="不填则自动挑一个疑似 embedding 的")
    ap.add_argument("--expected-dim", type=int, default=1024, help="与 EMBEDDING_DIMENSION 对齐")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return asyncio.run(run(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
