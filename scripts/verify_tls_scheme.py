#!/usr/bin/env python3
"""回归防护：8000 的 **scheme 必须是 https**，且请求体不被搞坏。

背景与根因写在 ``scripts/tls_relay.py`` 的文件头：relay 时代后端以为自己是
http://，307 重定向的 Location 就带 ``http://``，https 页面按 mixed content
拦掉 → ``TypeError: Failed to fetch`` → 语音生成向导卡 20~30 秒后误报「模型
还没就绪(或已被关闭)」。

⚠️ **2026-11-16 起架构变了**：8000 改成 **uvicorn 原生 TLS**
（``--ssl-certfile`` / ``--ssl-keyfile``），**8443 的 relay 已退役**。

⇒ ``scope["scheme"]`` 现在由 uvicorn **直接**给出（``https``），
``app/core/forwarded.py`` 的中间件**不再命中**（保留它是为了将来加 TLS
代理时不用重写 —— 它的单元断言仍然有效）。

本脚本断言（都不占 NPU）：

  0. **中间件单元**：``XForwardedProtoMiddleware`` 对回环/非回环对端、
     http/websocket、乱填值的处理符合预期（中间件保留 ⇒ 断言也保留）；
  1. **relay 自身**：请求体经 relay 不被搅坏（``scripts/tls_relay.py`` 的
     文件还在仓里，留着这段是为了别把它的 body 处理改坏 —— 将来有人再
     拿它当中继时能用上）；
  2. **明文连 8000 必须被拒**（8000 只说 HTTPS）；
  3. **https 新连接**：307 Location 必须是 ``https://``；
  4. **https 复用连接**：同一条连接上的**第二个**请求也必须是 ``https://``
     —— 浏览器复用 keep-alive 连接（这条最容易被改回去）。

用法::

    python -m scripts.verify_tls_scheme
"""
from __future__ import annotations

import asyncio
import hashlib
import http.client
import importlib.util
import os
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 只导入 app.core.forwarded —— 它没有副作用。**别导入 app.main**：它在
# import 时就 create_app()，会顺带把算力 registry 拉起来（本机还会去扫
# 一遍孤儿 worker，可能把正在跑的 compute_worker 误杀）。
from app.core.forwarded import XForwardedProtoMiddleware  # noqa: E402

#: 少一个尾斜杠就会 307 的路由（FastAPI 的 @router.get("/") 挂在 /api/tts 下）。
REDIRECT_PATH = "/api/tts"


# --------------------------------------------------------------------------- #
# 载入 relay 模块（按路径加载，免得依赖 scripts 是不是一个包）                 #
# --------------------------------------------------------------------------- #
def _load_relay_module():
    spec = importlib.util.spec_from_file_location(
        "tls_relay", str(REPO / "scripts" / "tls_relay.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("载不到 scripts/tls_relay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _start_relay(listen_port: int, target_port: int) -> threading.Thread:
    """在后台线程里跑一份独立的 relay（不改线上那个）。"""
    mod = _load_relay_module()
    mod.LISTEN_HOST = "127.0.0.1"
    mod.LISTEN_PORT = listen_port
    mod.TARGET_HOST = "127.0.0.1"
    mod.TARGET_PORT = target_port

    def _run() -> None:
        try:
            asyncio.run(mod.main())
        except Exception:  # noqa: BLE001
            pass

    th = threading.Thread(target=_run, name="tls-relay-test", daemon=True)
    th.start()
    return th


# --------------------------------------------------------------------------- #
# 假的回显后端：把请求体原样回一份，用来验二进制体没被 relay 搅坏              #
# --------------------------------------------------------------------------- #
class _BufferedReader:
    """带缓冲的读取器 —— 头和体常常在同一次 read() 里到，不能只看 head。"""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader
        self._buf = bytearray()

    async def _fill(self) -> bool:
        chunk = await self._reader.read(65536)
        if not chunk:
            return False
        self._buf += chunk
        return True

    async def read_line(self) -> bytes | None:
        """读一行（含 CRLF）。返回 ``None`` = 对端关闭；``b""`` = 空行。"""
        while b"\r\n" not in self._buf:
            if not await self._fill():
                return None
        idx = self._buf.find(b"\r\n")
        line = bytes(self._buf[:idx])
        del self._buf[:idx + 2]
        return line

    async def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if not await self._fill():
                break
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


class _EchoBackend:
    """极简 HTTP/1.1 回显服务器（只为本脚本存在）。"""

    def __init__(self) -> None:
        self.port = _free_port()
        self._server = None
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        _wait_port(self.port)

    def _serve(self) -> None:
        async def _main() -> None:
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", self.port)

            async with self._server:
                await self._server.serve_forever()

        try:
            asyncio.run(_main())
        except Exception:  # noqa: BLE001
            pass

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        buf = _BufferedReader(reader)
        try:
            while True:
                lines: list[bytes] = []
                while True:
                    line = await buf.read_line()
                    if line is None:
                        return                       # 对端关闭
                    if not line:
                        break                        # 空行 = 头结束
                    lines.append(line)
                if not lines:
                    return
                head = b"\r\n".join(lines)
                te = self._header_value(head, "transfer-encoding").lower()
                cl = self._header_value(head, "content-length")
                if "chunked" in te:
                    body = await self._read_chunked(buf)
                else:
                    want = int(cl) if cl.isdigit() else 0
                    body = await buf.read_exact(want)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: keep-alive\r\n\r\n" + body)
                await writer.drain()
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _header_value(head: bytes, name: str) -> str:
        for line in head.split(b"\r\n"):
            if b":" not in line:
                continue
            key, _, val = line.partition(b":")
            if key.strip().lower() == name.encode():
                return val.strip().decode("latin-1")
        return ""

    @staticmethod
    async def _read_chunked(buf: _BufferedReader) -> bytes:
        out = b""
        while True:
            size_line = await buf.read_line()
            if not size_line:
                break
            try:
                size = int(size_line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                break
            if size == 0:
                await buf.read_line()             # 结尾的空行
                break
            out += await buf.read_exact(size)
            await buf.read_exact(2)               # chunk 后的 CRLF
        return out


# --------------------------------------------------------------------------- #
# 断言                                                                         #
# --------------------------------------------------------------------------- #
def _location(conn: http.client.HTTPConnection, path: str) -> tuple[int, str]:
    """发一个 GET，返回 ``(状态码, Location 头)``。"""
    conn.request("GET", path)
    resp = conn.getresponse()
    resp.read()
    return resp.status, resp.getheader("Location") or ""


def _post_body(port: int, payload: bytes, chunked: bool) -> bytes:
    """经 relay POST 一段二进制，取回回显的请求体。"""
    ctx = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=15)
    try:
        if chunked:
            conn.putrequest("POST", "/echo")
            conn.putheader("Transfer-Encoding", "chunked")
            conn.endheaders()
            step = 64 * 1024
            for i in range(0, len(payload), step):
                piece = payload[i:i + step]
                conn.send(b"%x\r\n" % len(piece) + piece + b"\r\n")
            conn.send(b"0\r\n\r\n")
        else:
            conn.request("POST", "/echo", body=payload,
                         headers={"Content-Length": str(len(payload))})
        resp = conn.getresponse()
        return resp.read()
    finally:
        conn.close()


async def _middleware_scheme(scope_type: str, peer: str | None,
                             header: bytes | None):
    """跑一次中间件，返回它落下的 ``scope["scheme"]``。"""
    seen: dict = {}

    async def _app(scope, receive, send):  # noqa: ANN001
        seen["scheme"] = scope.get("scheme")

    mw = XForwardedProtoMiddleware(_app)
    headers = [(b"host", b"x:8443")]
    if header is not None:
        headers.append((b"x-forwarded-proto", header))
    scope = {"type": scope_type, "headers": headers,
             "client": (peer, 1234) if peer else None}
    await mw(scope, None, None)
    return seen.get("scheme")


def _check_middleware(fails: list[str]) -> None:
    """app/core/forwarded.py 的单元断言。"""
    cases = [
        # (scope 类型, 对端, 头, 期望的 scheme)
        ("http", "127.0.0.1", b"https", "https"),
        ("http", "::1", b"https", "https"),
        ("http", "127.0.0.1", b"HTTP", "http"),
        ("websocket", "127.0.0.1", b"https", "wss"),
        # 非回环对端 → 不认（局域网客户端不能自己声明 scheme）
        ("http", "172.16.9.173", b"https", None),
        # 没这个头 → 不动
        ("http", "127.0.0.1", None, None),
        # 乱填的值 → 不动
        ("http", "127.0.0.1", b"gopher", None),
    ]

    async def _run() -> None:
        for scope_type, peer, header, want in cases:
            got = await _middleware_scheme(scope_type, peer, header)
            if got != want:
                fails.append(
                    f"XForwardedProtoMiddleware({scope_type}, peer={peer}, "
                    f"xfp={header!r}) → scheme={got!r}，期望 {want!r}")

    asyncio.run(_run())


def main() -> int:
    fails: list[str] = []
    notes: list[str] = []

    # ---- 0. 证书在不在 --------------------------------------------------- #
    cert_dir = Path(os.environ.get(
        "WDNAWEBUI_TLS_DIR", "/home/rockchip/.config/wdnwebui/tls"))
    if not (cert_dir / "cert.pem").is_file() or not (cert_dir / "key.pem").is_file():
        print(f"⚠️  缺证书 {cert_dir} —— 跳过 TLS 部分"
              f"（先跑 scripts/gen_tls_cert.sh）")
        return 0

    # ---- 0b. app 侧的 scheme 处理（纯单元，不碰运行中的服务）-------------- #
    try:
        _check_middleware(fails)
        print("[0] ✅ app 侧 X-Forwarded-Proto 处理符合预期")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"app/core/forwarded.py 的单元断言跑不起来: {exc}")

    # ---- 1. 一份独立 relay：只对假回显后端（验请求体）-------------------- #
    # ⚠️ **不再**起「relay → 真后端(8000)」那份：8000 自己终结 TLS，
    #    relay 转过去的是明文 ⇒ 必然连不上。
    #    relay 的文件（scripts/tls_relay.py）还在仓里（将来有人再拿它当
    #    中继时能用），所以这里保留它的 body 处理回归。
    echo = _EchoBackend()
    echo.start()
    echo_relay_port = _free_port()
    _start_relay(echo_relay_port, echo.port)
    if not _wait_port(echo_relay_port):
        print(f"❌ relay 没起来（端口 {echo_relay_port}）")
        return 1
    print(f"[1] 测试用 relay: 127.0.0.1:{echo_relay_port} → 127.0.0.1:{echo.port}"
          f"（假回显后端；relay 已退役，这里只验它自己的 body 处理）")

    # ---- 2. 请求体完整性（relay 改了请求头，不能把 body 搅坏）-------------- #
    payload = bytes((i * 37 + (i >> 8) * 11) % 256 for i in range(256 * 1024))
    want = hashlib.sha256(payload).hexdigest()
    got = hashlib.sha256(
        _post_body(echo_relay_port, payload, chunked=False)).hexdigest()
    if got != want:
        fails.append(f"Content-Length 请求体经 relay 后被改坏了 "
                     f"(want={want[:12]} got={got[:12]})")
    else:
        print(f"[2] ✅ 256 KB 二进制体（Content-Length）原样送达")

    got_c = hashlib.sha256(
        _post_body(echo_relay_port, payload, chunked=True)).hexdigest()
    if got_c != want:
        fails.append(f"chunked 请求体经 relay 后被改坏了 "
                     f"(want={want[:12]} got={got_c[:12]})")
    else:
        print(f"[3] ✅ 256 KB 二进制体（chunked）原样送达")

    # ---- 4. 明文路径：8000 只说 HTTPS ⇒ 明文连上去必须被拒 --------------- #
    # 2026-11-16 起 8000 是 uvicorn 原生 TLS，明文 HTTP 会被 TLS 层掐掉
    # （curl exit=52 / Empty reply）。
    # ⇒ 「明文路径的 Location 还是 http://」这个断言**已经不适用**
    #    （已经没有明文路径了）⇒ 改成断言「明文连不上」。
    refused = False
    try:
        plain = http.client.HTTPConnection("127.0.0.1", 8000, timeout=10)
        _, loc = _location(plain, REDIRECT_PATH)
        plain.close()
    except (OSError, http.client.HTTPException):
        # RemoteDisconnected / ConnectionResetError 都是 OSError 的子类
        refused = True
    if refused:
        print("[4] ✅ 明文连 8000 被拒（8000 只说 HTTPS）")
    else:
        fails.append("明文连 8000 居然成功了 —— 8000 应该只提供 HTTPS")

    # ---- 5. https 路径：新连接 / 复用连接，Location 都得是 https ---------- #
    # 8000 现在**自己**终结 TLS（不再经过 relay）⇒ 直接连 https://127.0.0.1:8000。
    ctx = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection("127.0.0.1", 8000,
                                        context=ctx, timeout=10)
    try:
        status, loc1 = _location(conn, REDIRECT_PATH)
    except (OSError, http.client.HTTPException) as exc:
        print(f"❌ https 连不上 8000: {exc}")
        return 1
    if status != 307:
        notes.append(f"后端没有对 {REDIRECT_PATH} 返回 307（实际 {status}）"
                     f"—— 换一条会重定向的路由再验")
    if not loc1.startswith("https://"):
        fails.append(f"https 新连接的 Location 不是 https://: {loc1!r}")
    else:
        print(f"[5] ✅ https 新连接 Location = {loc1}")

    # 复用同一条连接再要一次 —— 浏览器就是这么干的（keep-alive）
    _, loc2 = _location(conn, REDIRECT_PATH)
    if not loc2.startswith("https://"):
        fails.append(f"https **复用连接** 的 Location 不是 https://: {loc2!r} "
                     f"（只注入第一个请求头是不够的）")
    else:
        print(f"[6] ✅ https 复用连接 Location = {loc2}")
    conn.close()

    for n in notes:
        print(f"note: {n}")
    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\n✅ TLS 转发的 scheme 传递正常；请求体未损坏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
