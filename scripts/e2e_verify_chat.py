"""端到端验证：把真实问题打给生产 /api/chat/stream，收集答案与引用来源。

只做一件事：原样记录系统返回了什么，不做判断。判断由人来做。
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8001"
import os

# 注意：Windows 原生 Python 不认 Git Bash 的 /tmp 虚拟路径，
# 凭据一律走环境变量，避免脚本换个 shell 就失效。
TOKEN = os.environ["E2E_TOKEN"]
KB = os.environ["E2E_KB"]

QUESTIONS = [
    "生产环境怎么部署？",
    "数据库迁移怎么执行？",
    "系统会监控哪些指标、有哪些告警？",
    "备份怎么恢复？恢复后要做哪些确认？",
    "整个系统由哪些部分组成，请求是怎么流转的？",
    "怎么查看 PostgreSQL 的指标？",
    "优雅停机是怎么做的？为什么需要它？",
    "为什么要给 NLTK 语料单独放进镜像？",
    "ScrapeTargetDown 这条告警是什么意思？",
    "改完告警规则后怎么让它生效？",
]


def ask(question: str) -> dict:
    payload = json.dumps(
        {"question": question, "kb_ids": [KB], "search_mode": "simple"}
    ).encode()
    req = urllib.request.Request(
        f"{BASE}/api/chat/stream",
        data=payload,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    started = time.time()
    answer_parts: list[str] = []
    sources: list[dict] = []
    steps: list[str] = []
    event_types: set[str] = set()

    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            etype = evt.get("type")
            event_types.add(etype)
            if etype == "reasoning" and evt.get("status") == "done":
                steps.append(f"{evt.get('step')}:{evt.get('duration_ms')}ms")
            elif etype in ("token", "content", "delta"):
                answer_parts.append(evt.get("content") or evt.get("text") or "")
            elif etype in ("answer", "final"):
                text = evt.get("content") or evt.get("text") or ""
                if text:
                    answer_parts.append(text)
            elif etype in ("sources", "citations"):
                sources.extend(evt.get("sources") or evt.get("data") or [])
            elif etype == "done":
                if evt.get("content"):
                    answer_parts.append(evt["content"])
                if evt.get("sources"):
                    sources.extend(evt["sources"])

    return {
        "question": question,
        "answer": "".join(answer_parts).strip(),
        "sources": sources,
        "steps": steps,
        "event_types": sorted(x for x in event_types if x),
        "elapsed_s": round(time.time() - started, 1),
    }


results = []
for i, q in enumerate(QUESTIONS, 1):
    print(f"[{i}/{len(QUESTIONS)}] {q}", flush=True)
    try:
        r = ask(q)
        results.append(r)
        print(f"    {r['elapsed_s']}s | 答案 {len(r['answer'])} 字 | 引用 {len(r['sources'])} 条", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"    失败: {exc}", flush=True)
        results.append({"question": q, "error": str(exc)})

out = os.environ.get("E2E_OUT", "e2e_results.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n已写入 {out}")
if results and results[0].get("event_types"):
    print("事件类型:", results[0]["event_types"])
