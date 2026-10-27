#!/usr/bin/env python3
# coding=utf-8
"""TLS 转发：``https://<板子IP>:8443`` → 解密后原样转给本机 ``:8000``。

为什么要有它
------------
浏览器只在**安全上下文**（https 或 http://localhost/127.0.0.1）里才暴露
``navigator.mediaDevices``。明文 ``http://172.16.9.173:8000`` 这种局域网
访问里它是 undefined，**点「开始录音」什么都不会发生**（实测）。

给主服务直接加 ``--ssl-keyfile`` 会把现有的明文访问一起废掉（板子上
别的人还在用 http://8000），所以这里另开一个 8443 端口做 TLS 终结：
原来的 8000 一行不改，多出来的只是「https 也能进」。

它是**字节级**转发（解密后原样 pipe），所以 HTTP 和 WebSocket 升级
（``wss://``）都能正常工作 —— 前端的 WS 地址本来就是按
``location.protocol`` 推出来的（``https:`` → ``wss:``），不用改。

证书：``scripts/gen_tls_cert.sh`` 生成自签证书（CN/SAN = 板子 IP）。
浏览器第一次会告警「不是私密连接」，点「高级 → 继续前往」即可 ——
**实测这样之后页面就是安全上下文，麦克风能正常拿到**。
想要连告警都没有，就用私有 CA 签发并把根证书装到客户端。

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


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """一个方向的字节搬运。"""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
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
    peer = client_writer.get_extra_info("peername")
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
        await asyncio.gather(_pipe(client_reader, back_writer),
                             _pipe(back_reader, client_writer))
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
    print("[tls_relay] https://{}:{} → http://{}:{}".format(
        LISTEN_HOST, LISTEN_PORT, TARGET_HOST, TARGET_PORT), flush=True)
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
