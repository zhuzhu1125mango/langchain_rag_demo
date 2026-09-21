"""A/B 评估环境准备：上传 eval_corpus 文档到测试知识库并生成 doc-id-map。

幂等：复用已有用户/知识库/文档（同名文档跳过）。用法：
    uv run python scripts/prepare_ab_kb.py
输出：
    backend/agent_ab_kb_id.txt        知识库 id
    backend/agent_ab_doc_id_map.json  {语义文档id: milvus document_id uuid}
"""

import json
import sys
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
BASE_URL = "http://localhost:8000"
USERNAME = "ab_eval"
PASSWORD = "ab_eval_2026"
KB_NAME = "ab_eval_kb"
CORPUS = BASE_DIR / "tests" / "evaluation" / "eval_corpus.jsonl"
KB_ID_FILE = BASE_DIR / "agent_ab_kb_id.txt"
ID_MAP_FILE = BASE_DIR / "agent_ab_doc_id_map.json"

WAIT_TIMEOUT_S = 900  # 12 篇短文档含 LLM 智能分析，预留 15 分钟


def _jsonl(path: Path) -> list:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _list_docs(client: httpx.Client, kb_id: str) -> list:
    """文档列表接口直接返回数组。"""
    raw = client.get("/api/documents/list", params={"kb_id": kb_id}).json()
    if isinstance(raw, dict):
        return raw.get("items") or raw.get("documents") or []
    return raw


def main() -> int:
    client = httpx.Client(base_url=BASE_URL, timeout=120, trust_env=False)

    # 1. 登录（不存在则注册）
    r = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    if r.status_code != 200:
        r = client.post("/api/auth/register", json={"username": USERNAME, "password": PASSWORD})
        if r.status_code not in (200, 201):
            print(f"[prepare-ab] 注册/登录失败: {r.status_code} {r.text[:200]}")
            return 1
    token = r.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    print("[prepare-ab] 用户就绪")

    # 2. 建库（存在则复用）
    kb_id = None
    r = client.get("/api/knowledge_bases/")
    if r.status_code == 200:
        items = r.json().get("items") or []
        for kb in items:
            if kb.get("name") == KB_NAME:
                kb_id = kb["id"]
                break
    if not kb_id:
        r = client.post("/api/knowledge_bases/", json={"name": KB_NAME})
        if r.status_code not in (200, 201):
            print(f"[prepare-ab] 建库失败: {r.status_code} {r.text[:200]}")
            return 1
        kb_id = r.json()["id"]
    KB_ID_FILE.write_text(str(kb_id), encoding="utf-8")
    print(f"[prepare-ab] 知识库就绪: {kb_id}")

    # 3. 上传 eval_corpus 文档（幂等：同名文档跳过）
    corpus = _jsonl(CORPUS)
    id_map = {}
    existing = _list_docs(client, kb_id)
    existing_names = {d.get("filename") for d in existing}
    for doc in corpus:
        fname = f"{doc['id']}.txt"
        if fname in existing_names:
            d = next(x for x in existing if x.get("filename") == fname)
            id_map[doc["id"]] = str(d["id"])
            print(f"[prepare-ab] 跳过已存在: {fname} -> {d['id']}")
            continue
        text = f"# {doc['title']}\n\n{doc['text']}"
        tmp = BASE_DIR / f".ab_upload_{doc['id']}.txt"
        tmp.write_text(text, encoding="utf-8")
        with open(tmp, "rb") as fh:
            r = client.post(
                "/api/documents/upload",
                params={"kb_id": kb_id},
                files={"file": (fname, fh, "text/plain")},
            )
        tmp.unlink(missing_ok=True)
        if r.status_code not in (200, 201):
            print(f"[prepare-ab] 上传失败 {fname}: {r.status_code} {r.text[:200]}")
            return 1
        doc_id = r.json()["id"]
        id_map[doc["id"]] = doc_id
        print(f"[prepare-ab] 已上传: {fname} -> {doc_id}")

    ID_MAP_FILE.write_text(
        json.dumps(id_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[prepare-ab] doc-id-map 已写入: {ID_MAP_FILE}")

    # 4. 轮询等待全部处理完成
    deadline = time.time() + WAIT_TIMEOUT_S
    while time.time() < deadline:
        items = _list_docs(client, kb_id)
        pending = [
            d for d in items
            if d.get("filename", "").endswith(".txt")
            and d.get("processing_status") not in ("completed", "failed")
        ]
        if not pending:
            break
        print(
            f"[prepare-ab] 等待处理完成... 剩余 {len(pending)} 篇"
            f"（当前: {[d['filename'] + ':' + str(d.get('processing_status')) for d in pending[:3]]}）"
        )
        time.sleep(8)

    items = _list_docs(client, kb_id)
    failed = [
        d for d in items
        if d.get("filename", "").endswith(".txt")
        and d.get("processing_status") == "failed"
    ]
    done = [
        d for d in items
        if d.get("filename", "").endswith(".txt")
        and d.get("processing_status") == "completed"
    ]
    print(f"[prepare-ab] 处理完成: {len(done)}/{len(corpus)} 篇")
    if failed:
        print(f"[prepare-ab] 处理失败 {len(failed)} 篇: {[d['filename'] for d in failed]}")
        return 1
    if len(done) < len(corpus):
        print("[prepare-ab] 超时：仍有文档未处理完成")
        return 1
    print("[prepare-ab] 环境准备完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
