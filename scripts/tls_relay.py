#!/usr/bin/env python3
# coding=utf-8
"""TLS 转发：``https://<板子IP>:8443`` → 解密后转给本机 ``:8000``。

为什么要有它
------------
浏览器只在**安全上下文**（https 或 http://localhost/127.0.0.1）里才暴露
``navigator.mediaDevices``。明文 ``http://172.16.9.173:8000`` 这种局域网
访问里它是 undefined，**点「开始录音」什么都不会发生**（实测）。

给主服务直接加 ``--ssl-keyfile`` 会把现有的明文访问一起废掉（板子上
别的人还在用 http://8000），所以这里另开一个 8443 端口做 TLS 终结：
原来的 8000 一行不改，多出来的只是「https 也能进」。

证书：``scripts/gen_tls_cert.sh`` 生成自签证书（CN/SAN = 板子 IP）。
浏览器第一次会告警「不是私密连接」，点「高级 → 继续前往」即可。

它得告诉后端「原来那个请求是 https」
------------------------------------
TLS 在这里终结，后端收到的是**明文** HTTP —— 它以为自己是 http://，
于是自己生成的绝对 URL 都带 http://，最要命的是 307 重定向：

    GET /api/tts  →  307 Location: http://<host>:<port>/api/tts/

https 页面拿到 http:// 的重定向目标 → 浏览器按 **mixed content** 拦掉
→ fetch() reject 成 TypeError: Failed to fetch。实测症状：语音生成向导点
「完成配置」卡 20~30 秒后报「模型还没就绪(或已被关闭)(Failed to fetch)」，
而模型明明已经起来了 —— 探测请求压根没送达后端。

所以这里给请求头注入 X-Forwarded-Proto: https（后端据此把 scope["scheme"]
设成 https，Location 就成了 https://）。**每个**请求都要注入：浏览器复用
keep-alive 连接，只注入第一个的话后面的又变回 http://（实测同一条连接上
第一个请求 https、第二个 http）。

帧怎么算：请求头到 ``\\r\\n\\r\\n`` 为止，请求体长度看 Content-Length。
帧不明确（chunked / 头超长）时补一个 Connection: close，退化成「一个连接
一个请求」—— 宁可少复用也不能把请求体算错，这里转发的是二进制（音色 WAV
上传）。WebSocket 升级请求原样透传：101 之后是隧道字节，不能再按 HTTP 解析。

跑法（板端）::

    /home/rockchip/work/wdnwebui/.venv/bin/python -u scripts/tls_relay.py
"""
import asyncio
import ssl
import sys
from pathlib import Path

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8443
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 8000

CERT_DIR = Path("/home/rockchip/.config/wdnwebui/tls")
CERT = CERT_DIR / "cert.pem"
KEY = CERT_DIR / "key.pem"

#: 请求头最大字节数。超过就放弃注入（头都不完整，猜不出帧边界）。
MAX_HEAD_BYTES = 64 * 1024

#: 转发给后端的 scheme —— TLS 在本进程终结，原始请求永远是 https。
FORWARDED_PROTO = "https"


class _RequestRewriter:
    """在**每个**请求头里插入 ``X-Forwarded-Proto``。

    对外只暴露 :meth:`feed`：喂进客户端发来的字节，返回可以原样发给后端
    的字节。请求体和隧道数据都是**逐字节透传**的，只有请求头被改写。
    """

    def __init__(self, scheme: str = FORWARDED_PROTO) -> None:
        self._scheme = scheme
        self._buf = bytearray()
        self._mode = "head"          # head | body | raw
        self._body_left = 0

    def feed(self, chunk: bytes) -> bytes:
        """喂一段客户端字节，返回可发给后端的字节。"""
        if self._mode == "raw":
            return chunk

        out = bytearray()
        self._buf += chunk

        while True:
            if self._mode == "body":
                take = min(self._body_left, len(self._buf))
                if take:
                    out += self._buf[:take]
                    del self._buf[:take]
                    self._body_left -= take
                if self._body_left:
                    break                       # 请求体没收全，等下一段
                self._mode = "head"
                continue

            idx = self._buf.find(b"\r\n\r\n")
            if idx < 0:
                if len(self._buf) > MAX_HEAD_BYTES:
                    print("[tls_relay] 请求头超过 {} 字节，放弃注入 "
                          "X-Forwarded-Proto".format(MAX_HEAD_BYTES), flush=True)
                    out += self._buf
                    self._buf.clear()
                    self._mode = "raw"
                break
            head = bytes(self._buf[:idx])
            del self._buf[:idx + 4]             # 头出队，余下的留在 buf

            new_head, body_len, raw = self._rewrite_head(head)
            out += new_head + b"\r\n\r\n"
            if raw:
                # 后面是隧道字节 / 帧不明的请求体 —— 原样透传，不再解析。
                out += self._buf
                self._buf.clear()
                self._mode = "raw"
                break
            if body_len > 0:
                self._mode = "body"
                self._body_left = body_len
                continue
            if not self._buf:
                break                           # 等下一个请求头

        return bytes(out)

    def _rewrite_head(self, head: bytes) -> tuple[bytes, int, bool]:
        """改写一个请求头，返回 ``(新请求头, 请求体长度, 是否转原样透传)``。"""
        lines = head.split(b"\r\n")
        request_line = lines[0]
        header_lines = lines[1:]

        def _name(line: bytes) -> bytes:
            return line.split(b":", 1)[0].strip().lower()

        body_len = 0
        chunked = False
        is_upgrade = False
        for line in header_lines:
            name = _name(line)
            if name == b"content-length":
                try:
                    body_len = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    body_len = 0
            elif name == b"transfer-encoding":
                chunked = b"chunked" in line.lower()
            elif name == b"upgrade" or (name == b"connection"
                                        and b"upgrade" in line.lower()):
                is_upgrade = True

        # 摘掉旧的转发头（免得叠成 "https, http"），再补上本次的。
        kept = [l for l in header_lines
                if _name(l) not in (b"x-forwarded-proto", b"x-forwarded-for")]
        kept.append(b"X-Forwarded-Proto: " + self._scheme.encode())
        if chunked:
            # chunked 的请求体边界要另写一套解析器。宁可让这条连接只跑一个
            # 请求，也不要把二进制请求体（音色 WAV）的帧算错。
            kept.append(b"Connection: close")
        new_head = b"\r\n".join([request_line] + kept)

        if is_upgrade or chunked:
            return new_head, 0, True
        return new_head, body_len, False


async def _pipe(reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter,
                 transform=None) -> None:
    """一个方向的字节搬运；transform 非空时先过一遍（改写请求头用）。"""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            out = transform(data) if transform else data
            if out:
                writer.write(out)
                await writer.drain()
    except (OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _handle(client_reader: asyncio.StreamReader,
                  client_writer: asyncio.StreamWriter) -> None:
    try:
        back_reader, back_writer = await asyncio.open_connection(
            TARGET_HOST, TARGET_PORT)
    except OSError as exc:
        print("[tls_relay] 连不上后端 {}:{} — {}".format(
            TARGET_HOST, TARGET_PORT, exc), flush=True)
        try:
            client_writer.close()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        await asyncio.gather(
            _pipe(client_reader, back_writer, _RequestRewriter().feed),
            _pipe(back_reader, client_writer),
        )
    finally:
        for w in (client_writer, back_writer):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass


async def main() -> int:
    if not CERT.is_file() or not KEY.is_file():
        print("[tls_relay] 缺证书: {} / {}".format(CERT, KEY), flush=True)
        print("        先跑 scripts/gen_tls_cert.sh", flush=True)
        return 1
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT), str(KEY))
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT,
                                        ssl=ctx)
    print("[tls_relay] https://{}:{} → http://{}:{}  (注入 X-Forwarded-Proto: "
          "{})".format(LISTEN_HOST, LISTEN_PORT, TARGET_HOST, TARGET_PORT,
                       FORWARDED_PROTO), flush=True)
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
