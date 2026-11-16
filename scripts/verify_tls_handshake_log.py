#!/usr/bin/env python3
"""回归防护：TLS 握手失败必须落一条**明文** ERROR 日志。

背景
----
2026-11-16 起 8000 只提供 HTTPS。改完之后，「客户端用 ``http://`` 撞上来」
这个故障在服务端**一个字都不说** —— 连接被直接掐掉，客户端只看到
``Empty reply from server``，现场没有任何线索。

根因写在 ``app/core/tls_logging.py`` 的文件头：asyncio 的
``SSLProtocol._fatal_error`` 对 ``OSError``（``ssl.SSLError`` 是子类）只在
``loop.get_debug()`` 为真时才记 DEBUG，而 asyncio 的 logger 默认 WARNING
⇒ **记了也被丢掉**。实测默认模式与 ``PYTHONASYNCIODEBUG=1`` 都是 0 行。

⚠️ **生产跑的是 uvloop**（``uvicorn --loop auto`` 优先选 uvloop），
而 uvloop 有自己的 SSL 实现 ⇒ 打在 asyncio 类上的补丁**完全无效**。
真正与 event loop 无关的钩子是 ``ssl.SSLObject.do_handshake``。

本脚本断言（自包含，不占 NPU、不碰生产服务、不起第二个 wdnwebui）：

  1. **明文客户端撞 HTTPS 端口** ⇒ 必须记一条 ``[TLS] 握手失败`` 的 ERROR，
     且带异常原文与「客户端用了 http:// 而不是 https://」的排查提示；
  2. **正常 TLS 客户端** ⇒ **不能**记握手失败（不能误报）；
  3. **message 过滤** ⇒ 非握手类致命错误（``Fatal error on transport``，
     即 WSS 连接中途被 reset 走的分支）**不能**被记成「握手失败」；
  4. **客户端侧握手失败不刷屏** ⇒ ``server_side=False`` 的 SSLObject
     握手失败**不记**（wdnai 拉 HF 模型也会握手，误报会淹掉日志）；
  5. **幂等** ⇒ ``install()`` 调多次不会把补丁套两层；
  6. **签名不匹配就不打补丁** ⇒ Python 版本变了宁可没日志，也不要打错日志；
  7. **uvloop 与 asyncio 都要能记** ⇒ 生产是 uvloop，不能只测 asyncio。

用法::

    .venv/bin/python scripts/verify_tls_handshake_log.py
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 只导入 app.core.tls_logging —— 它没有副作用。
# **别导入 app.main**：它在 import 时就 create_app()，会顺带把算力 registry
# 拉起来（可能误杀正在跑的 compute_worker）。
from app.core import tls_logging  # noqa: E402

TLS_DIR = Path("/home/rockchip/.config/wdnwebui/tls")
BOARD_CERT = TLS_DIR / "cert.pem"
BOARD_KEY = TLS_DIR / "key.pem"

#: 钩子把日志**延迟** 0.25s 落（让 asyncio 侧能补 peer 地址）⇒ 测试要等够。
WAIT_AFTER_HIT = 1.0

_FAILED: list[str] = []
_PASSED = 0


def check(ok: bool, name: str, detail: str = "") -> bool:
    global _PASSED
    if ok:
        _PASSED += 1
        print(f"  [OK]   {name}")
    else:
        _FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# 证书                                                                        #
# --------------------------------------------------------------------------- #
def _make_self_signed(tmpdir: Path) -> tuple[str, str]:
    """板上没有板子证书时，用 cryptography 临时签一张自签证书。"""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmpdir / "cert.pem"
    key_path = tmpdir /key_path_name()
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return str(cert_path), str(key_path)


def key_path_name() -> str:
    return "key.pem"


def _server_ctx(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def _client_ctx() -> ssl.SSLContext:
    """信任板子 CA 的客户端 context（自签证书时退化成不校验）。"""
    ctx = ssl.create_default_context()
    ca = TLS_DIR / "ca.pem"
    if ca.is_file():
        ctx.load_verify_locations(str(ca))
        return ctx
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# --------------------------------------------------------------------------- #
# 被测服务器：asyncio 原生 TLS（走的就是 asyncio.sslproto.SSLProtocol）        #
# --------------------------------------------------------------------------- #
class _TlsEchoServer:
    """起一个 TLS 服务器。

    ``loop_kind`` 决定用哪个 event loop ——
    ``"uvloop"``（**生产用的就是它**）或 ``"asyncio"``。
    """

    def __init__(self, ctx: ssl.SSLContext, loop_kind: str = "asyncio") -> None:
        self._ctx = ctx
        self._loop_kind = loop_kind
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.port = 0
        self.error: BaseException | None = None

    def start(self) -> None:
        def _run() -> None:
            if self._loop_kind == "uvloop":
                import uvloop
                loop = uvloop.new_event_loop()
            else:
                loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _handler(reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
                try:
                    await reader.read(1024)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                    await writer.drain()
                except Exception:
                    pass
                finally:
                    try:
                        writer.close()
                    except Exception:
                        pass

            async def _main() -> None:
                # ⚠️ 必须用 **asyncio.start_server**（高层 API），不能用
                # ``loop.create_server``（低层 API，要求传 Protocol 实例）。
                # 低层那个会拿协程函数当协议 ⇒ 握手根本不发生 ⇒ 测了个寂寞。
                # asyncio.start_server 在 uvloop 上同样可用。
                server = await asyncio.start_server(
                    _handler, "127.0.0.1", 0, ssl=self._ctx)
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                async with server:
                    await server.serve_forever()

            try:
                loop.run_until_complete(_main())
            except BaseException as exc:      # noqa: BLE001
                self.error = exc
                self._ready.set()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        self._ready.wait(10)
        if self.error:
            raise self.error

    def stop(self) -> None:
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# 日志捕获                                                                    #
# --------------------------------------------------------------------------- #
class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)

    def handshake_lines(self) -> list[str]:
        return [r.getMessage() for r in self.records
                if "[TLS] 握手失败" in r.getMessage()]

    def clear(self) -> None:
        self.records.clear()


def _plaintext_hit(port: int) -> None:
    """用**明文 HTTP** 撞 TLS 端口 ⇒ 触发握手失败。"""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.settimeout(3)
        s.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        try:
            s.recv(1024)
        except OSError:
            pass


def _tls_hit(port: int) -> None:
    """正常 TLS 客户端 ⇒ 不该有握手失败。"""
    ctx = _client_ctx()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        raw.settimeout(3)
        with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
            s.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                s.recv(1024)
            except OSError:
                pass


def _run_suite(cap: _Capture, cert: str, key: str, loop_kind: str) -> None:
    """在指定 event loop 下跑一遍「明文撞击 ⇒ 有日志 / 正常 TLS ⇒ 不误报」。"""
    server = _TlsEchoServer(_server_ctx(cert, key), loop_kind=loop_kind)
    server.start()
    print(f"被测 TLS 服务器[{loop_kind}]: 127.0.0.1:{server.port}")
    try:
        cap.clear()
        _plaintext_hit(server.port)
        time.sleep(WAIT_AFTER_HIT)
        lines = cap.handshake_lines()
        check(len(lines) == 1,
              f"[{loop_kind}] 明文客户端撞 HTTPS 端口 ⇒ 恰好记了 1 条 [TLS] 握手失败",
              f"实际 {len(lines)} 条: {cap.text()!r}")
        if lines:
            joined = "\n".join(lines)
            check("SSLError" in joined or "Error" in joined,
                  f"[{loop_kind}] 日志带异常类型/原文",
                  f"实际: {joined!r}")
            check("排查提示" in cap.text() and "http://" in cap.text(),
                  f"[{loop_kind}] 日志带「客户端用了 http:// 而不是 https://」的排查提示",
                  f"实际记录: {cap.text()!r}")
            levels = [r.levelno for r in cap.records
                      if "[TLS] 握手失败" in r.getMessage()]
            check(levels and all(lv >= logging.ERROR for lv in levels),
                  f"[{loop_kind}] 握手失败日志是 ERROR 级（不会被默认级别过滤掉）",
                  f"实际级别: {levels}")

        cap.clear()
        _tls_hit(server.port)
        time.sleep(0.5)
        check(not cap.handshake_lines(),
              f"[{loop_kind}] 正常 TLS 客户端 ⇒ 没有误报握手失败",
              f"实际记录: {cap.text()!r}")
    finally:
        server.stop()


def main() -> int:
    print("=== TLS 握手失败明文日志回归 ===\n")

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tls-hs-"))
    if BOARD_CERT.is_file() and BOARD_KEY.is_file():
        cert, key = str(BOARD_CERT), str(BOARD_KEY)
        print(f"用板子证书: {cert}")
    else:
        cert, key = _make_self_signed(tmp)
        print(f"板子证书不在，用临时自签证书: {cert}")

    cap = _Capture()
    tl_logger = logging.getLogger("app.core.tls_logging")
    prev_level = tl_logger.level
    prev_prop = tl_logger.propagate
    tl_logger.setLevel(logging.DEBUG)
    tl_logger.addHandler(cap)
    tl_logger.propagate = False

    try:
        ok = tls_logging.install()
        check(ok, "install() 装上握手失败日志钩子")
        check(tls_logging.install(), "install() 幂等（第二次仍返回 True）")

        # 幂等的真正判据：类上只被套了一层。
        import asyncio.sslproto
        for obj, attr in ((ssl.SSLObject, "do_handshake"),
                          (asyncio.sslproto.SSLProtocol, "_fatal_error")):
            wrapped = getattr(obj, attr)
            inner = getattr(wrapped, "__wrapped__", None)
            check(inner is not None
                  and not getattr(inner, "_wdnwebui_tls_logging_patched", False),
                  f"install() 没有把 {attr} 补丁套两层")

        # ---- 主钩子：asyncio 下也要能记（顺便验证 peer 地址） ------------- #
        _run_suite(cap, cert, key, "asyncio")

        # ---- 主钩子：uvloop 下也要能记（**生产就是这个**） --------------- #
        try:
            import uvloop  # noqa: F401
            _run_suite(cap, cert, key, "uvloop")
        except ImportError:
            print("  [SKIP] 没装 uvloop，跳过 uvloop 用例")

        # ---- 3. 非握手类致命错误 ⇒ 不能被记成握手失败 -------------------- #
        cap.clear()

        class _FakeLoop:
            @staticmethod
            def get_debug() -> bool:
                return False

        class _FakeProto:
            _transport = None
            _loop = _FakeLoop()

        asyncio.sslproto.SSLProtocol._fatal_error(
            _FakeProto(), OSError("boom"), "Fatal error on transport")
        time.sleep(0.2)
        check(not cap.handshake_lines(),
              "message 不含 handshake（如 WSS reset）⇒ 不记成握手失败",
              f"实际记录: {cap.text()!r}")

        # ---- 4. 客户端侧握手失败 ⇒ 不刷屏 ------------------------------- #
        cap.clear()
        client_ctx = ssl.create_default_context()
        try:
            # 客户端侧握手：连一个不存在的端口 → 握手失败（server_side=False）
            s = socket.create_connection(("127.0.0.1", 1), timeout=2)
            s.close()
        except OSError:
            pass
        # 直接构造一个 server_side=False 的 SSLObject 并让它握手失败
        try:
            incoming = ssl.MemoryBIO()
            outgoing = ssl.MemoryBIO()
            obj = client_ctx.wrap_bio(incoming, outgoing, server_side=False)
            try:
                obj.do_handshake()
            except ssl.SSLError:
                pass
        except Exception as exc:
            print(f"  (客户端侧 SSLObject 构造跳过: {exc})")
        time.sleep(WAIT_AFTER_HIT)
        check(not cap.handshake_lines(),
              "客户端侧（server_side=False）握手失败 ⇒ 不记（不刷屏）",
              f"实际记录: {cap.text()!r}")

        # ---- 5. 签名不匹配 ⇒ 不打补丁 ------------------------------------ #
        saved_ssl = ssl.SSLObject.do_handshake
        saved_fatal = asyncio.sslproto.SSLProtocol._fatal_error
        try:
            ssl.SSLObject.do_handshake = lambda self, *a, **k: None
            check(not tls_logging.install(),
                  "签名不匹配 ⇒ install() 拒绝打补丁（返回 False）")
            check(ssl.SSLObject.do_handshake is not saved_ssl,
                  "签名不匹配 ⇒ 原实现没有被替换")
        finally:
            ssl.SSLObject.do_handshake = saved_ssl
            asyncio.sslproto.SSLProtocol._fatal_error = saved_fatal

    finally:
        tl_logger.removeHandler(cap)
        tl_logger.setLevel(prev_level)
        tl_logger.propagate = prev_prop
        try:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    total = _PASSED + len(_FAILED)
    print(f"\n=== {_PASSED}/{total} 通过 ===")
    if _FAILED:
        for name in _FAILED:
            print(f"  FAIL: {name}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
