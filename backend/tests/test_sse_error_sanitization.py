"""
P1-2 回归测试：SSE 错误脱敏（审查项 #15 残留）。

验证：
1. chat.py 流式路径两处 SSE error payload（生成失败/保存失败）
   不再拼接 str(e)，改为通用文案 + request_id（详细错误仅进日志）；
2. api 层无异常信息进入客户端响应路径
   （HTTPException detail / SSE error 字段中的 str(e) 插值）。

注：业务校验插值（文件扩展名、敏感词命中、批量上限）属用户输入回显，非异常泄露，
不在此断言范围内。流式端点行为级测试依赖 PostgreSQL，见 test_api_chat.py。
"""

import re
from pathlib import Path

API_DIR = Path(__file__).resolve().parent.parent / "src" / "api"


def _read_api_source(name: str) -> str:
    return (API_DIR / name).read_text(encoding="utf-8")


class TestChatSSEErrorSanitization:
    """chat.py SSE 错误事件脱敏。"""

    def test_sse_error_payloads_use_generic_message_and_request_id(self):
        """生成失败与保存失败的 error payload 均为通用文案并携带 request_id。"""
        source = _read_api_source("chat.py")
        assert "'error': '生成回答失败，请稍后重试', 'request_id': request_id" in source
        assert "'error': '回答已生成但保存失败，请稍后重试', 'request_id': request_id" in source

    def test_sse_error_payload_does_not_interpolate_exception(self):
        """SSE error 字段不允许 f-string 插值（防止异常细节回流）。"""
        source = _read_api_source("chat.py")
        assert not re.search(r"'error':\s*f['\"]", source), (
            "chat.py 中发现 f-string 拼接的 error 字段，疑似异常信息透传"
        )

    def test_stream_endpoint_reads_middleware_request_id(self):
        """流式端点从 request.state 读取中间件生成的 request_id。"""
        source = _read_api_source("chat.py")
        assert 'getattr(request.state, "request_id"' in source


class TestApiLayerExceptionLeakage:
    """api 层客户端响应路径的异常信息泄露扫描。"""

    def test_no_exception_detail_in_client_paths(self):
        """HTTPException detail 与 SSE error 字段不得插值 str(e)/异常变量。"""
        # 匹配 detail=f"...{str(e)}" 或 'error': f"...{str(e)}" 等透传模式
        pattern = re.compile(
            r"(detail\s*=|['\"]error['\"]\s*:)\s*f?['\"][^'\"]*\{\s*str\(\s*\w+"
        )
        offenders = []
        for path in sorted(API_DIR.rglob("*.py")):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(API_DIR)))
        assert offenders == [], f"以下文件在客户端响应路径透传异常信息: {offenders}"
