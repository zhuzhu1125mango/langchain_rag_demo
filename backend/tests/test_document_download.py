"""原文下载接口（P1-2）单元测试。

覆盖三件容易出错、又不容易被人肉发现的事：
1. `Content-Disposition` 的中文文件名与响应头注入（换行/引号）；
2. 「对象存储里没了」与「存储不可用」必须报不同的状态码（404 / 503）；
3. 流式响应真的把字节吐出来、且 Content-Length 与 stat 一致。
"""

import uuid

import pytest
from fastapi import HTTPException
from minio.error import S3Error
from starlette.responses import FileResponse, StreamingResponse

from src.api.document import query as doc_query
from src.auth import CurrentUser
from src.services.minio_service import MinioService


class _Doc:
    def __init__(self, file_path="minio://documents/x/source.pdf", filename="体检报告 v2.pdf", file_type="pdf"):
        self.id = uuid.uuid4()
        self.filename = filename
        self.file_type = file_type
        self.file_path = file_path
        self.owner_id = "u1"
        self.knowledge_base = None
        self.category = None
        self.size = 11


class _Result:
    def __init__(self, doc):
        self._doc = doc

    def scalar_one_or_none(self):
        return self._doc


class _DB:
    def __init__(self, doc):
        self._doc = doc

    async def execute(self, *_a, **_kw):
        return _Result(self._doc)


class _FakeMinio:
    def __init__(self, chunks=(b"hello ", b"world"), stat_exc=None):
        self._chunks = chunks
        self._stat_exc = stat_exc
        self.closed = False

    def stat_object(self, file_key):
        if self._stat_exc:
            raise self._stat_exc
        return {"size": sum(len(c) for c in self._chunks), "content_type": "application/pdf", "last_modified": None}

    def open_object(self, file_key, chunk_size=65536):
        try:
            for chunk in self._chunks:
                yield chunk
        finally:
            self.closed = True


def _user(user_id="u1"):
    return CurrentUser(user_id=user_id, is_authenticated=True)


def _patch_minio(monkeypatch, fake):
    async def _get_instance(*_a, **_kw):
        return fake

    monkeypatch.setattr(MinioService, "get_instance", _get_instance)


async def _drain(response) -> bytes:
    body = b""
    async for chunk in response.body_iterator:
        body += chunk
    return body


# ---------------------------------------------------------------------------
# 文件名与 MIME
# ---------------------------------------------------------------------------

def test_content_disposition_keeps_chinese_and_adds_fallback():
    value = doc_query._content_disposition("体检报告 v2.pdf")
    assert value.startswith('attachment; filename="')
    assert "filename*=UTF-8''" in value
    # UTF-8 百分号编码后的中文
    assert "%E4%BD%93%E6%A3%80" in value


def test_content_disposition_strips_header_injection():
    value = doc_query._content_disposition('a\r\nX-Evil: 1".pdf')
    assert "\r" not in value and "\n" not in value
    assert "X-Evil" in value  # 文本保留，但已不可能成为独立响应头
    assert value.count('"') == 2  # 只剩包裹 fallback 的那一对引号


def test_safe_filename_strips_path_separators():
    assert doc_query._safe_filename("../../etc/passwd") == ".._.._etc_passwd"


@pytest.mark.parametrize(
    "filename,file_type,expected",
    [
        ("a.pdf", "pdf", "application/pdf"),
        ("a.docx", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("a.md", "md", "text/markdown"),
        ("noext", "unknownext", "application/octet-stream"),
    ],
)
def test_guess_media_type(filename, file_type, expected):
    assert doc_query._guess_media_type(filename, file_type) == expected


# ---------------------------------------------------------------------------
# 端点：成功路径
# ---------------------------------------------------------------------------

async def test_streams_object_with_headers(monkeypatch):
    fake = _FakeMinio()
    _patch_minio(monkeypatch, fake)

    response = await doc_query.download_doc_file(str(uuid.uuid4()), _DB(_Doc()), _user())

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "application/pdf"
    assert response.headers["content-length"] == "11"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert await _drain(response) == b"hello world"
    assert fake.closed is True, "生成器必须释放 MinIO 连接"


# ---------------------------------------------------------------------------
# 端点：失败路径必须可区分
# ---------------------------------------------------------------------------

async def test_missing_object_is_404_and_reportable(monkeypatch):
    # minio 7.2 的签名是 S3Error(response, code, message, ...)——第一个参数不是 code
    err = S3Error(None, "NoSuchKey", "not found", "res", "req", "host")
    _patch_minio(monkeypatch, _FakeMinio(stat_exc=err))
    logged = []
    monkeypatch.setattr(doc_query, "log_exception", lambda *a, **kw: logged.append(kw))

    with pytest.raises(HTTPException) as exc:
        await doc_query.download_doc_file(str(uuid.uuid4()), _DB(_Doc()), _user())

    assert exc.value.status_code == 404
    assert logged and logged[0]["severity"].name == "REPORTABLE"
    assert "consistency_audit" in logged[0]["message"]


async def test_storage_error_is_503_not_404(monkeypatch):
    err = S3Error(None, "InternalError", "boom", "res", "req", "host")
    _patch_minio(monkeypatch, _FakeMinio(stat_exc=err))
    monkeypatch.setattr(doc_query, "log_exception", lambda *a, **kw: None)

    with pytest.raises(HTTPException) as exc:
        await doc_query.download_doc_file(str(uuid.uuid4()), _DB(_Doc()), _user())

    assert exc.value.status_code == 503


async def test_document_without_file_is_404(monkeypatch):
    _patch_minio(monkeypatch, _FakeMinio())

    with pytest.raises(HTTPException) as exc:
        await doc_query.download_doc_file(str(uuid.uuid4()), _DB(_Doc(file_path="")), _user())

    assert exc.value.status_code == 404


async def test_non_owner_is_403(monkeypatch):
    _patch_minio(monkeypatch, _FakeMinio())

    with pytest.raises(HTTPException) as exc:
        await doc_query.download_doc_file(str(uuid.uuid4()), _DB(_Doc()), _user("someone-else"))

    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# 端点：本地路径兜底（历史导入）
# ---------------------------------------------------------------------------

async def test_local_path_falls_back_to_file_response(tmp_path):
    local = tmp_path / "note.md"
    local.write_text("hello", encoding="utf-8")

    response = await doc_query.download_doc_file(
        str(uuid.uuid4()), _DB(_Doc(file_path=str(local), filename="note.md", file_type="md")), _user()
    )

    assert isinstance(response, FileResponse)
    assert response.media_type == "text/markdown"
