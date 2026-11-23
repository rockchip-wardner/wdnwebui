"""TLS 握手失败的**明文**日志钩子。

背景
----
2026-11-16 起 8000 只提供 HTTPS（uvicorn 原生 TLS）。改完之后有一个
**看不见的故障模式**：客户端用 ``http://`` 撞上 8000，服务端**一个字都不说**
—— 连接被直接掐掉，客户端只看到「Empty reply from server」。

为什么服务端是哑的（实测）
--------------------------
Python 3.11 ``/usr/lib/python3.11/asyncio/sslproto.py:908``::

    def _fatal_error(self, exc, message='Fatal error on transport'):
        if self._transport:
            self._transport._force_close(exc)

        if isinstance(exc, OSError):
            if self._loop.get_debug():
                logger.debug("%r: %s", self, message, exc_info=True)
        elif not isinstance(exc, exceptions.CancelledError):
            self._loop.call_exception_handler({...})

三个坑叠在一起：

1. ``ssl.SSLError`` 是 ``OSError`` 的子类 ⇒ 走**第一个**分支；
2. 那个分支只有 ``self._loop.get_debug()`` 为真时才打日志 ⇒ 默认
   ``loop.get_debug()`` 是 False ⇒ **不打**；
3. 就算把 loop debug 打开（``PYTHONASYNCIODEBUG=1``），记的是
   ``logger.debug``，而 asyncio 的 logger 默认级别是 WARNING ⇒
   **DEBUG 记录被丢弃**。

实测两种模式都抓不到任何握手错误（journalctl 里 5 行输入、0 行错误）。
uvicorn 自己也没有「握手失败」的钩子。

⚠️ **生产跑的是 uvloop，不是 asyncio**
------------------------------------
``uvicorn`` 的 ``--loop auto`` 会**优先选 uvloop**（板上装了 uvloop 0.22.1
⇒ 生产进程里确实加载了 uvloop）。而 **uvloop 有自己的 SSL 实现**
（``uvloop/sslproto.pyx``），它**不经过** ``asyncio.sslproto.SSLProtocol``
⇒ 打在 asyncio 那个类上的补丁在生产上**完全无效**（2026-11-16 实测：
钩子装了、明文撞 8000、日志里一行都没有）。

而且 uvloop 那边的类型**全是不可变的**（Cython ``cdef class``）：

* ``uvloop.loop.SSLProtocol`` → ``cannot set 'connection_lost' attribute of
  immutable type``
* ``uvloop.loop.Loop`` / ``uvloop.loop.TCPTransport`` → 同样

⇒ uvloop 侧**没有任何可打补丁的类**。

所以真正能用的钩子只有一个：**``ssl.SSLObject.do_handshake``**。
``ssl.SSLObject`` 是 ``ssl.py`` 里的**纯 Python 类**（不是 ``_ssl._SSLObject``
的子类），它的 ``do_handshake()`` 就是 Python 方法 ⇒ **可打补丁**，
而且 **asyncio 与 uvloop 都走它**（两边都是
``self._sslobj.do_handshake()``）⇒ **与 event loop 无关**。

两条钩子
--------
======================  ==========================================  ==========
钩子                    作用                                        覆盖面
======================  ==========================================  ==========
``_patch_ssl_object``   包 ``ssl.SSLObject.do_handshake`` ⇒ 握手抛   **所有**
                        ``SSLError`` 时记一条明文 ERROR            event loop
``_patch_asyncio``      包 ``asyncio.sslproto.SSLProtocol.           仅 asyncio
                        _fatal_error`` ⇒ **补上对端地址**          （生产是
                                                                   uvloop ⇒ 不
                                                                   触发）
======================  ==========================================  ==========

对端地址只有 asyncio 侧拿得到（uvloop 的 ``SSLProtocol`` 不可打补丁）。
所以 ssl_object 钩子**延迟 0.25 秒**再落日志：asyncio 侧
``_fatal_error`` 会在这之前先记一条**带 peer 的**；uvloop 侧没人抢，
延迟后就落一条**不带 peer 的**。⇒ 任何一种 loop 下都只记一条。

``do_handshake`` 的过滤条件
---------------------------
``ssl.SSLObject.do_handshake`` **客户端也会调**（wdnai 拉 HF 模型、
websockets 客户端……）⇒ 只记 **server_side=True** 的对象。

三重保护
--------
这个补丁动的是**标准库方法**，所以必须自己兜住：

1. **签名不匹配就不打** —— Python 升级改了签名就放弃，宁可没有日志也
   不要打错日志；
2. **钩子自身的异常全吞** —— 记日志失败绝不能把连接搞崩；
3. **装不上只留一条 WARNING** —— 服务照常跑，不因为「装不上钩子」而
   拒绝启动。

日志格式
--------
**明文**（不加密、不压缩，journald 的 ``Storage=`` / ``Seal=`` 都是空的），
前缀固定 ``[TLS]`` ⇒ ``journalctl -u wdnwebui | grep '\\[TLS\\]'`` 就能捞到。
"""
from __future__ import annotations

import asyncio
import asyncio.sslproto
import functools
import inspect
import logging
import ssl
import time

LOG = logging.getLogger("app.core.tls_logging")

#: 打上这个标记的 wrapper 表示「已经打过补丁」⇒ 幂等。
_PATCHED_ATTR = "_wdnwebui_tls_logging_patched"

#: ssl_object 钩子的落日志延迟（秒）。asyncio 的 ``_fatal_error`` 会抢在
#: 前面记一条带 peer 的；这里只等它一小会儿。
_EMIT_DELAY = 0.25

#: 已经记过的异常（id → 记录时刻）。用来保证「同一个握手失败只记一条」。
_REPORTED: dict[int, float] = {}

#: 已排期、等会儿要记的异常（id → 异常对象）。持有引用 ⇒ id 不会被复用。
_PENDING: dict[int, BaseException] = {}


def _describe_peer(proto: object) -> str:
    """尽力取出对端的 ``ip:port``；取不到就返回空串（**不是** ``?``）。"""
    try:
        transport = getattr(proto, "_transport", None)
        if transport is None:
            return ""
        info = transport.get_extra_info("peername")
    except Exception:
        return ""
    if not info:
        return ""
    if isinstance(info, (tuple, list)) and len(info) >= 2:
        return f"{info[0]}:{info[1]}"
    return str(info)


def _hint(exc: BaseException) -> str:
    """按异常特征给一条**明文**的排查提示（猜不准就返回空串）。"""
    try:
        text = f"{type(exc).__name__}: {exc}".lower()
    except Exception:
        return ""
    # 明文客户端撞 HTTPS 端口 —— 两种报错形态都见过：
    #   * ``[SSL: HTTP_REQUEST] http request``：OpenSSL 直接认出「这是个
    #     HTTP 请求」（板上是这个）；
    #   * ``[SSL: WRONG_VERSION_NUMBER] wrong version number``：老一点
    #     OpenSSL / 客户端只发了几个字节时的形态。
    if ("http request" in text or "wrong version number" in text
            or "unknown protocol" in text):
        return ("looks like a plaintext client hitting the HTTPS port: the "
                "client used http:// instead of https://. The server serves "
                "HTTPS only on port 8000 — the client must use "
                "https://<host>:8000")
    if "certificate verify failed" in text or "certificate_verify_failed" in text:
        return ("the client does not trust the board's CA root — the client "
                "must install /home/rockchip/.config/wdnwebui/tls/ca.pem")
    if "timed out" in text or "timeout" in text:
        return ("the client handshake timed out — common causes: the network "
                "is down, an intermediate device blocks it, or the client "
                "certificate chain is incomplete")
    return ""


def _emit(exc: BaseException, key: int, peer: str = "") -> None:
    """真正落日志。同一个异常只落一次。"""
    if key in _REPORTED:
        return
    _REPORTED[key] = time.monotonic()
    _PENDING.pop(key, None)
    try:
        detail = f"{type(exc).__name__}: {exc}"
        peer_part = f"peer={peer}  " if peer else ""
        LOG.error("[TLS] handshake failed  %s%s", peer_part, detail)
        hint = _hint(exc)
        if hint:
            LOG.error("[TLS] hint: %s", hint)
    except Exception:
        try:
            LOG.warning("[TLS] the handshake-failure log hook itself raised "
                        "(ignored)", exc_info=True)
        except Exception:
            pass


def _schedule(exc: BaseException) -> None:
    """把一次握手失败排期落日志（延迟一点点，让 asyncio 侧能补 peer）。"""
    key = id(exc)
    if key in _REPORTED or key in _PENDING:
        return
    _PENDING[key] = exc
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        _emit(exc, key)
        return
    try:
        loop.call_later(_EMIT_DELAY, _emit, exc, key)
    except Exception:
        _emit(exc, key)


# --------------------------------------------------------------------------- #
# 钩子 1：ssl.SSLObject.do_handshake（**与 event loop 无关**）                 #
# --------------------------------------------------------------------------- #
#: 「还要更多数据 / 再试一次」的异常集合 —— **不是失败**。
#:
#: Python 3.11 的 asyncio 用的是同一组（``asyncio/sslproto.py:16``）：
#: ``SSLAgainErrors = (ssl.SSLWantReadError, ssl.SSLSyscallError)``。
#: 这里再加上 ``SSLWantWriteError``（同样是「再来一次」的中间态）。
#:
#: ⚠️ 它们都是 ``ssl.SSLError`` 的子类 ⇒ **必须**在 ``except ssl.SSLError``
#: 之前排除，否则**每一次正常的 TLS 握手**都会刷一条「握手失败」。
_SSL_AGAIN_ERRORS: tuple = tuple(
    exc for exc in (
        getattr(ssl, "SSLWantReadError", None),
        getattr(ssl, "SSLWantWriteError", None),
        getattr(ssl, "SSLSyscallError", None),
    ) if exc is not None
)


def _make_ssl_object_wrapper(orig):
    @functools.wraps(orig)
    def do_handshake(self):
        try:
            return orig(self)
        except _SSL_AGAIN_ERRORS:
            # 「还要更多数据」—— **不是失败**。非阻塞握手的正常中间态，
            # 每一次正常的 TLS 握手都会抛好几次（asyncio 把它 catch 掉
            # 然后等更多数据）。它是 ``ssl.SSLError`` 的子类，**必须**
            # 排在下面那个 except 前面，否则每次正常握手都会刷一条
            # 「握手失败」。
            raise
        except ssl.SSLError as exc:
            try:
                # 只记**服务端**。客户端侧的握手失败（wdnai 拉 HF 模型、
                # websockets 客户端）跟「明文撞 HTTPS 端口」无关，别刷屏。
                if getattr(self, "server_side", False):
                    _schedule(exc)
            except Exception:
                pass
            raise

    return do_handshake


def _patch_ssl_object() -> bool:
    """包 ``ssl.SSLObject.do_handshake`` ⇒ 握手失败落明文 ERROR。"""
    orig = getattr(ssl.SSLObject, "do_handshake", None)
    if orig is None:
        LOG.warning("[TLS] cannot install the handshake-failure log hook: ssl.SSLObject has no do_handshake")
        return False
    if getattr(orig, _PATCHED_ATTR, False):
        return True
    try:
        params = list(inspect.signature(orig).parameters)
    except (TypeError, ValueError):
        params = []
    if params != ["self"]:
        LOG.warning("[TLS] cannot install the handshake-failure log hook: "
                    "the do_handshake signature is not the expected one -> %s",
                    params)
        return False

    wrapper = _make_ssl_object_wrapper(orig)
    setattr(wrapper, _PATCHED_ATTR, True)
    try:
        ssl.SSLObject.do_handshake = wrapper
    except Exception:
        LOG.warning("[TLS] cannot install the handshake-failure log hook: patching failed", exc_info=True)
        return False
    LOG.info("[TLS] the ssl.SSLObject.do_handshake hook is in place "
             "(plain ERROR level, independent of the event loop)")
    return True


# --------------------------------------------------------------------------- #
# 钩子 2：asyncio.sslproto.SSLProtocol._fatal_error（**补 peer 地址**）        #
# --------------------------------------------------------------------------- #
def _make_fatal_error_wrapper(orig):
    @functools.wraps(orig)
    def _fatal_error(self, exc, message="Fatal error on transport"):
        try:
            msg = str(message or "")
            if "handshake" in msg.lower():
                _emit(exc, id(exc), peer=_describe_peer(self))
        except Exception:
            try:
                LOG.warning("[TLS] the handshake-failure log hook itself "
                            "raised (ignored)", exc_info=True)
            except Exception:
                pass
        return orig(self, exc, message)

    return _fatal_error


def _patch_asyncio() -> bool:
    """包 ``asyncio.sslproto.SSLProtocol._fatal_error`` ⇒ 补对端地址。

    只在**纯 asyncio** 下有用（生产跑 uvloop ⇒ uvloop 有自己的 SSL
    实现，这个类根本不会被用到）。装不上不算失败。
    """
    proto_cls = asyncio.sslproto.SSLProtocol
    orig = getattr(proto_cls, "_fatal_error", None)
    if orig is None:
        return False
    if getattr(orig, _PATCHED_ATTR, False):
        return True
    try:
        params = list(inspect.signature(orig).parameters)
    except (TypeError, ValueError):
        params = []
    if params[:3] != ["self", "exc", "message"]:
        LOG.warning("[TLS] cannot install the asyncio peer-address hook: "
                    "the _fatal_error signature is not the expected one -> %s",
                    params)
        return False

    wrapper = _make_fatal_error_wrapper(orig)
    setattr(wrapper, _PATCHED_ATTR, True)
    try:
        proto_cls._fatal_error = wrapper
    except Exception:
        LOG.warning("[TLS] cannot install the asyncio peer-address hook: patching failed", exc_info=True)
        return False
    LOG.info("[TLS] the asyncio _fatal_error hook is in place "
             "(fills in the peer address under plain asyncio; it does not "
             "fire under uvloop)")
    return True


def install() -> bool:
    """装上「握手失败 ⇒ 明文日志」的钩子。返回 True 表示主钩子装上了。

    **幂等** —— 重复调用不会把补丁套两层。
    """
    ok = _patch_ssl_object()
    _patch_asyncio()
    return ok


def uninstall() -> bool:
    """卸掉钩子（还原成标准库原实现）。主要用于测试。"""
    ok = False
    orig = getattr(ssl.SSLObject, "do_handshake", None)
    if orig is not None and getattr(orig, _PATCHED_ATTR, False):
        inner = getattr(orig, "__wrapped__", None)
        if inner is not None:
            ssl.SSLObject.do_handshake = inner
            ok = True
    proto_cls = asyncio.sslproto.SSLProtocol
    orig = getattr(proto_cls, "_fatal_error", None)
    if orig is not None and getattr(orig, _PATCHED_ATTR, False):
        inner = getattr(orig, "__wrapped__", None)
        if inner is not None:
            proto_cls._fatal_error = inner
            ok = True
    return ok
