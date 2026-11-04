#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""演练用的最小问答探针：向指定后端流式提一个问题，报告「流是否完整」。

为什么不用 curl：`curl -N` 只能看到文本，没法判断 SSE 有没有收到 `end` 帧。
演练要验的是「回答完整」，不是「有字打出来」——被中途硬杀时 curl 一样有输出。

用法：
    python scripts/ask_once.py <base_url> <token> <kb_id> <question> [--raw 输出文件] [--read-delay 秒]

`--read-delay` 是给停机演练用的慢消费者：每读一行 sleep 一下，连接就会一直开着，
这样「生成中停服务」才是确定的场景，而不是靠猜答案要写多久。

退出码：0 = 流完整且答案非空；2 = 流被截断（没有 end 帧）；1 = 请求本身失败。
输出：一行 JSON（answer_type / sources / answer_len / frames / finished / answer 摘要）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 300


def _opt(argv: list[str], name: str) -> str | None:
    if name in argv:
        idx = argv.index(name)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


def main(argv: list[str]) -> int:
    if len(argv) < 5:
        print(__doc__, file=sys.stderr)
        return 1
    base, token, kb_id, question = argv[1], argv[2], argv[3], argv[4]
    raw_path = _opt(argv, "--raw")
    read_delay = float(_opt(argv, "--read-delay") or 0)

    body = json.dumps({"question": question, "kb_ids": [kb_id]}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/api/chat/stream",
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )

    answer: list[str] = []
    end_frame: dict = {}
    frames = 0
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            for line in resp:
                if read_delay:
                    time.sleep(read_delay)
                line = line.decode("utf-8", "ignore").strip()
                if not line.startswith("data: "):
                    continue
                frames += 1
                try:
                    ev = json.loads(line[6:])
                except Exception:
                    continue
                kind = ev.get("type")
                if kind == "content":
                    answer.append(ev.get("content") or "")
                elif kind == "error":
                    print(json.dumps({"error": ev}, ensure_ascii=False))
                    return 1
                elif kind == "end":
                    end_frame = ev
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1

    text = "".join(answer)
    if raw_path:
        with open(raw_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    finished = bool(end_frame)
    print(
        json.dumps(
            {
                "question": question,
                "answer_type": end_frame.get("answer_type"),
                "sources": len(end_frame.get("sources") or []),
                "answer_len": len(text),
                "frames": frames,
                "finished": finished,
                "answer": text[:240].replace("\n", " "),
            },
            ensure_ascii=False,
        )
    )
    return 0 if (finished and text) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
