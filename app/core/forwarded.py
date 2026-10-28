"""转发头处理：让后端知道自己收到的请求**原始 scheme** 是什么。

TLS 终结不在后端进程里 —— :mod:`scripts.tls_relay` 在 8443 端口终结 TLS，
再把**明文** HTTP 转给本机 8000。后端于是以为自己是 ``http://``，生成的
绝对 URL（307 重定向的 Location 等）就都带 ``http://``，https 页面按
**mixed content** 拦掉 → ``TypeError: Failed to fetch``。根因与症状写在
``scripts/tls_relay.py`` 的文件头。

relay 给每个请求头注入 ``X-Forwarded-Proto``，这一层负责把它落到
``scope["scheme"]`` 上。

为什么不直接依赖 uvicorn
------------------------
uvicorn 自带的 ``ProxyHeadersMiddleware`` 其实会处理这个头，但它默认
只信任 127.0.0.1（``forwarded_allow_ips``），**而且是可以被关掉的**
（``--no-proxy-headers``）。那是个 CLI 默认值，不是契约 —— 哪天有人改了
启动参数，scheme 会静默退回 ``http://``，症状一模一样。这里显式兜一次。
"""
from __future__ import annotations

#: 认定为「本机可信对端」的地址。只认回环是有意的：板子上 8000 端口是
#: 对局域网开的，任意客户端都能自己塞一个 ``X-Forwarded-Proto`` 进来。
#: 非回环对端塞了不认（跟 uvicorn 的默认口径一致）；伪造它最多让服务端把
#: 重定向写成 https://，影响面仅限那一个请求。
TRUSTED_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})


class XForwardedProtoMiddleware:
    """把 ``X-Forwarded-Proto`` 落到 ``scope["scheme"]``。

    只处理回环对端送来的头（见 :data:`TRUSTED_PEERS`）。websocket 的
    scheme 相应地映射成 ``ws`` / ``wss``（跟 uvicorn 的口径一致）。
    """

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] in ("http", "websocket"):
            client = scope.get("client")
            host = client[0] if client else ""
            if host in TRUSTED_PEERS:
                for name, value in scope.get("headers", ()):
                    if name == b"x-forwarded-proto":
                        proto = value.decode("latin-1").strip().lower()
                        if proto in ("http", "https"):
                            if scope["type"] == "websocket":
                                scope["scheme"] = "wss" if proto == "https" else "ws"
                            else:
                                scope["scheme"] = proto
                        break
        await self.app(scope, receive, send)
