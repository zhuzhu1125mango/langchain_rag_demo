"""SSRF 安全的 HTTP 客户端（SEC-9）。

在 httpcore 连接层固定 DNS 解析结果：TLS 仍使用原始主机名做 SNI/证书校验，
但 TCP 连接只允许落到校验通过的公开 IP，避免「校验后 DNS rebinding」。
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from typing import Any, Iterable, Optional

import httpcore
import httpx

from src.utils.security import UnsafeUrlError, is_public_ip


def _resolve_public_ip(host: str, port: int) -> str:
    """解析主机并返回一个公开 IP；任一结果为私网/保留地址即拒绝。"""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(str(literal)):
            raise UnsafeUrlError(f"连接目标为受限地址: {host}")
        return str(literal)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise UnsafeUrlError(f"域名解析失败: {host}") from exc
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise UnsafeUrlError(f"域名无可用地址: {host}")
    for address in addresses:
        if not is_public_ip(address):
            raise UnsafeUrlError(f"域名解析到受限地址: {address}")
    return addresses[0]


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """包装 AnyIO 后端，在 connect_tcp 时使用校验后的 IP。"""

    def __init__(self) -> None:
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        pinned_ip = _resolve_public_ip(host, port)
        return await self._backend.connect_tcp(
            pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: Optional[float] = None,
        socket_options: Optional[Iterable[Any]] = None,
    ) -> httpcore.AsyncNetworkStream:
        raise UnsafeUrlError("禁止通过 Unix Socket 发起外部请求")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def __aiter__(self):
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class PinnedHTTPTransport(httpx.AsyncBaseTransport):
    """基于 httpcore 连接池的 httpx transport（不支持代理，禁用 Unix Socket）。"""

    def __init__(
        self,
        *,
        verify: bool = True,
        max_connections: int = 20,
        keepalive_expiry: float = 30.0,
    ) -> None:
        ssl_context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
            http1=True,
            http2=False,
            network_backend=PinnedNetworkBackend(),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            resp = await self._pool.handle_async_request(req)
        except UnsafeUrlError:
            raise
        except Exception as exc:
            # 不在此处记录：包装后继续上抛，由调用方带请求上下文记录一次；
            # 这里只负责把底层异常统一成 httpx.TransportError（保留 cause）
            raise httpx.TransportError(str(exc)) from exc

        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=_ResponseStream(resp.stream),
            extensions=resp.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def create_safe_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """创建带 IP 固定 transport 的 AsyncClient（仅用于外部网页抓取）。"""
    kwargs.setdefault("transport", PinnedHTTPTransport())
    return httpx.AsyncClient(**kwargs)
