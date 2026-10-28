"""文档管理 API（CODE-1 拆分后为包）。

子模块职责：
- ``common``  请求/响应模型、归属校验、序列化（无路由）
- ``upload``  上传、批量上传、分块配置、进度查询与 WebSocket
- ``query``   搜索、列表、预览、分块、来源定位、详情
- ``manage``  状态、重处理、分类/质量、更新、删除、查重

``router`` / ``ws_router`` 对外保持不变；**注册顺序敏感**：``/chunk-config``、
``/search`` 等静态单段路径必须先于 ``/{doc_id}`` 注册，故 include 顺序为
upload → query → manage。
"""

from fastapi import APIRouter

from . import manage, query, upload

router = APIRouter(prefix="/documents", tags=["documents"])
ws_router = APIRouter(prefix="/documents", tags=["documents"])

router.include_router(upload.router)
router.include_router(query.router)
router.include_router(manage.router)
ws_router.include_router(upload.ws_router)

# 兼容历史导入路径（单测与外部脚本可能直接引用这些符号）
from .common import (  # noqa: E402
    BatchDeleteRequest,
    DocumentResponse,
    DocumentSourceResponse,
    DocumentUpdate,
    DuplicateDetectionResult,
    SearchResult,
    get_status_display,
    serialize_document,
)
from .common import ensure_upload_size as _ensure_upload_size  # noqa: E402
from .common import get_owned_document as _get_owned_document  # noqa: E402
from .common import resolve_target_kb as _resolve_target_kb  # noqa: E402
from .manage import (  # noqa: E402
    batch_delete,
    classify_document,
    delete_document,
    detect_duplicates,
    evaluate_document_quality,
    reprocess_document,
    update_document,
    update_status,
)
from .query import (  # noqa: E402
    get_doc_chunks,
    get_document,
    get_document_source,
    list_documents,
    preview_doc,
    search_documents,
)
from .upload import (  # noqa: E402
    batch_upload,
    get_chunk_config,
    get_upload_progress_endpoint,
    upload_document,
    upload_progress_ws,
)
