#!/usr/bin/env python3
"""huggingface.co 的 SNI 绕行代理 —— 解决本板「DNS 污染 + TLS 握手被掐」。

问题
====

本板所在网络对 ``huggingface.co`` 做了**两层**封锁，只修 DNS 不够：

1. **DNS 污染**：``huggingface.co`` 的 A/AAAA 记录被换成 Facebook/Twitter
   段的假地址（实测每个上游解析器返回的假 IP 都不同：
   8.8.8.8→162.125.32.15、1.1.1.1→157.240.0.18、223.5.5.5→199.16.158.190 …）。
2. **SNI 阻断**：就算用 ``/etc/hosts`` 把名字钉到**真 IP**，TLS ClientHello
   里带着 ``SNI=huggingface.co`` 仍会被 RST —— 实测同一批 IP 下
   ``SNI=cloudflare.com`` 正常返回证书，``SNI=huggingface.co`` 直接被掐。

绕行原理
========

GFW 的 SNI 匹配串里**没有尾点**。把 ClientHello 的 SNI 发成
``huggingface.co.``（带尾点）��握手就能过；而 HTTP 的 ``Host`` 头保持
``huggingface.co``（不带尾点），服务端照常路由，证书也仍是
``CN=huggingface.co`` —— **证书校验依然成立**，没有降级成「关校验」。

.. code-block:: text

    hf CLI ──http://127.0.0.1:8787──► 本代理 ──TLS(SNI=huggingface.co.)──► 52.222.136.x:443
                                            │
                                            └─ 数据面落在 xet / CDN 主机上
                                               （transfer.xethub.hf.co、cas-bridge.xethub.hf.co
                                                 本身不被掐，直连即可）

用法
====

::

    # 1) 先修 DNS：把真 IP 钉进 /etc/hosts（见脚本 --print-hosts）
    .venv/bin/python tools/hf_sni_proxy.py --print-hosts

    # 2) 起代理
    sudo .venv/bin/python tools/hf_sni_proxy.py --port 8787

    # 3) 让 hf CLI 走它
    export HF_ENDPOINT=http://127.0.0.1:8787
    hf auth whoami
    hf upload wardner/Qwen3-ASR /userdata/models/Qwen3-ASR

只做 **TCP 层的 TLS 换腿**：请求体/响应体**按字节原样转发**（chunked 帧
保持不变），所以 1 GB 级的权重上传不会被缓冲进内存。
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import ssl
import sys
import threading
import time
from typing import List, Optional, Sequence, Tuple

LOG = logging.getLogger("hf_sni_proxy")

#: 被代理的目标域。
UPSTREAM_HOST = "huggingface.co"

#: 真实 IP（经境外 DoH ``dns.digitale-gesellschaft.ch`` 取得，HTTP/2）。
#: GFW 的污染答案落在 Facebook(157.240.x / 31.13.x)、Twitter(199.16.x)、
#: Dropbox(162.125.x) 段 —— 与下面这组 AWS 地址毫无关系。
DEFAULT_UPSTREAMS: Tuple[str, ...] = (
    "52.222.136.89",
    "52.222.136.92",
    "52.222.136.117",
    "52.222.136.38",
)

#: 刷新真实 IP 用的境外 DoH。境内解析器（alidns / doh.pub / 114114）
#: 对 huggingface.co 返回的是**污染答案**，不能用。
DOH_ENDPOINT = "https://dns.digitale-gesellschaft.ch/dns-query"

#: 请求/响应里要剥掉的逐跳头（RFC 9110 §7.6.1）。
#: ``Transfer-Encoding`` **不在此列** —— chunked 帧要原样透传。
HOP_BY_HOP = frozenset(
    {
        "connection",
        "proxy-connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)

#: 额外剥掉的请求头。
#:
#: ``Expect: 100-continue`` 必须剥：**客户端发完头就在等 100**，若代理
#: 把它透传给上游，上游回 100 之前客户端不会发请求体 —— 代理在等请求体、
#: 客户端在等 100，两边互等（curl 靠 1 秒超时兜底，但别的客户端会真死）。
#: 剥掉后上游看不到 Expect，直接按普通请求体收，链路不再有中间态。
STRIP_REQUEST_HEADERS = frozenset({"expect"})


# --------------------------------------------------------------------------- #
# 证书校验
# --------------------------------------------------------------------------- #
def cert_matches_hostname(cert: dict, hostname: str) -> bool:
    """判断证书是否覆盖 ``hostname``（含通配符）。

    不用 ``ssl.match_hostname``：它在 3.12 已被移除，且本项目的用法是
    「SNI 与证书名不同」的场景，需要自己显式表达。
    """
    names: List[str] = []
    for key, value in cert.get("subjectAltName", ()):
        if key == "DNS":
            names.append(value)
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                names.append(value)
    for name in names:
        if name.lower() == hostname.lower():
            return True
        if name.startswith("*."):
            suffix = name[1:]          # ".huggingface.co"
            if hostname.lower().endswith(suffix) and hostname.count(".") == suffix.count("."):
                return True
    return False


def open_upstream(ip: str, host: str = UPSTREAM_HOST, timeout: float = 30.0) -> ssl.SSLSocket:
    """连上游：SNI 带尾点，证书按不带尾点的名字校验。"""
    ctx = ssl.create_default_context()
    # SNI 用了尾点，内置的主机名校验必然不匹配 —— 关掉它，
    # 改由 cert_matches_hostname() 显式校验**不带尾点**的名字。
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED

    raw = socket.create_connection((ip, 443), timeout=timeout)
    try:
        sock = ctx.wrap_socket(raw, server_hostname=host + ".")
    except Exception:
        raw.close()
        raise
    cert = sock.getpeercert()
    if not cert_matches_hostname(cert, host):
        sock.close()
        raise ssl.SSLCertVerificationError(
            f"{ip} 返回的证书不覆盖 {host}: {cert.get('subject')}"
        )
    return sock


# --------------------------------------------------------------------------- #
# 逐字节转发（含 chunked 帧保持）
# --------------------------------------------------------------------------- #
def relay_chunked(f, dst: socket.socket) -> None:
    """原样转发 chunked 报文体的帧结构。

    ``f`` 必须是**读头部时用的那个缓冲流** —— 新开 ``makefile()`` 会把
    缓冲区里已经预读走的字节弄丢（头部后面紧跟请求体时必踩）。
    """
    while True:
        size_line = f.readline()
        if not size_line:
            return
        dst.sendall(size_line)
        size_txt = size_line.split(b";", 1)[0].strip()
        try:
            size = int(size_txt, 16)
        except ValueError:
            return
        if size == 0:
            # 终止块 + 可能的 trailer + 末尾 CRLF
            while True:
                line = f.readline()
                if not line:
                    break
                dst.sendall(line)
                if line in (b"\r\n", b"\n"):
                    break
            return
        remaining = size
        while remaining > 0:
            chunk = f.read(min(remaining, 1 << 16))
            if not chunk:
                return
            dst.sendall(chunk)
            remaining -= len(chunk)
        trailer = f.read(2)
        if trailer:
            dst.sendall(trailer)


def relay_body(f, dst: socket.socket, headers: Sequence[Tuple[str, str]]) -> None:
    """按 ``headers`` 描述的框架把请求体/响应体转发过去。"""
    hdr = {k.lower(): v for k, v in headers}
    if "transfer-encoding" in hdr and "chunked" in hdr["transfer-encoding"].lower():
        relay_chunked(f, dst)
        return
    length = hdr.get("content-length")
    if length is None:
        return
    try:
        remaining = int(length)
    except ValueError:
        return
    while remaining > 0:
        chunk = f.read(min(remaining, 1 << 16))
        if not chunk:
            return
        dst.sendall(chunk)
        remaining -= len(chunk)


def relay_until_eof(f, dst: socket.socket) -> None:
    """无 Content-Length 时的转发：读到 EOF 为止。"""
    while True:
        chunk = f.read(1 << 16)
        if not chunk:
            return
        dst.sendall(chunk)


def read_head(sock: socket.socket) -> Tuple[bytes, List[Tuple[str, str]], object]:
    """读起始行 + 头部。

    返回 ``(起始行, [(名, 值)], 缓冲流)``。**缓冲流必须留着给正文用** ——
    ``BufferedReader`` 会预读，丢掉它就等于丢掉请求体开头的一段。
    """
    f = sock.makefile("rb")
    start = f.readline()
    headers: List[Tuple[str, str]] = []
    while True:
        line = f.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers.append((name.strip(), value.strip()))
    return start, headers, f


# --------------------------------------------------------------------------- #
# 代理核心
# --------------------------------------------------------------------------- #
class Proxy:
    def __init__(self, upstreams: Sequence[str], host: str = UPSTREAM_HOST,
                 local_base: str = "", timeout: float = 120.0) -> None:
        self.upstreams = list(upstreams)
        self.host = host
        self.local_base = local_base
        self.timeout = timeout

    # ---- 上游选择 ---- #
    def connect(self) -> ssl.SSLSocket:
        last: Optional[Exception] = None
        for ip in self.upstreams:
            try:
                return open_upstream(ip, self.host, self.timeout)
            except Exception as exc:  # noqa: BLE001 —— 换下一个 IP 重试
                last = exc
                LOG.warning("上游 %s 失败: %s", ip, exc)
        raise RuntimeError(f"所有上游均不可用: {last}")

    # ---- Location 改写 ---- #
    def rewrite_location(self, value: str) -> str:
        if not self.local_base:
            return value
        for scheme in ("https://", "http://"):
            value = value.replace(scheme + self.host, self.local_base)
        return value

    def handle(self, client: socket.socket, addr) -> None:
        cfile = None
        try:
            start, headers, cfile = read_head(client)
            if not start:
                return
            method, path, *_ = start.decode("latin-1").split()
            hdr = {k.lower(): v for k, v in headers}

            upstream = self.connect()
            try:
                # Host 头保持不带尾点 —— 服务端按正常域名路由
                out = [f"{method} {path} HTTP/1.1\r\n".encode("latin-1")]
                for name, value in headers:
                    if name.lower() in HOP_BY_HOP or name.lower() == "host":
                        continue
                    if name.lower() in STRIP_REQUEST_HEADERS:
                        continue
                    out.append(f"{name}: {value}\r\n".encode("latin-1"))
                out.append(f"Host: {self.host}\r\n".encode("latin-1"))
                out.append(b"Connection: close\r\n\r\n")
                upstream.sendall(b"".join(out))

                # 请求体原样转发（用读头部时那个缓冲流）。
                # 上游可能**边收边拒**：例如路由不存在时它直接回 404 并
                # 关连接，而我们的请求体还在发 —— 此时 send() 抛
                # SSLEOFError。不能因此中断：上游的响应已经躺在缓冲区里，
                # 要把它转给客户端（否则客户端只看到 http=000，丢掉真实
                # 状态码与错误正文）。
                try:
                    relay_body(cfile, upstream, headers)
                except Exception as exc:  # noqa: BLE001
                    LOG.info("转发请求体时上游提前关闭（多半是上游已回错误响应）: %s", exc)
                    # 上游的响应可能已经躺在缓冲区里 —— 再给 5 秒把它捡回来；
                    # 捡不到就直接断链（别让客户端干等）。
                    try:
                        upstream.settimeout(5.0)
                    except Exception:
                        pass

                # 响应：起始行 + 头（改写 Location）+ 体。
                # 1xx 是**临时响应**（100 Continue / 103 Early Hints），
                # 必须原样转给客户端后继续读 —— 把它当最终响应，
                # 客户端会以为收完了（curl 报 http=100 就停）。
                while True:
                    rstart, rheaders, rfile = read_head(upstream)
                    if not rstart:
                        return
                    status = rstart.split(b" ", 2)
                    code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
                    if 100 <= code < 200:
                        client.sendall(rstart + b"".join(
                            f"{n}: {v}\r\n".encode("latin-1") for n, v in rheaders) + b"\r\n")
                        continue
                    break

                rout = [rstart]
                for name, value in rheaders:
                    if name.lower() == "location":
                        value = self.rewrite_location(value)
                    rout.append(f"{name}: {value}\r\n".encode("latin-1"))
                rout.append(b"\r\n")
                client.sendall(b"".join(rout))

                rhdr = {k.lower(): v for k, v in rheaders}
                if "chunked" in rhdr.get("transfer-encoding", "").lower():
                    relay_chunked(rfile, client)
                elif "content-length" in rhdr:
                    relay_body(rfile, client, rheaders)
                else:
                    # 既非 chunked 也无 Content-Length —— 上游用
                    # Connection: close 收尾，body 到 EOF 为止
                    relay_until_eof(rfile, client)
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001 —— 单个连接失败不能拖垮服务
            LOG.warning("连接 %s 处理失败: %s", addr, exc, exc_info=LOG.level <= logging.DEBUG)
        finally:
            try:
                if cfile is not None:
                    cfile.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass


def refresh_upstreams_via_doh(host: str = UPSTREAM_HOST) -> List[str]:
    """用境外 DoH 取真实 A 记录（RFC 8484 wire format，走 HTTP/2）。

    为什么要自己解析：huggingface.co 的 DNS 应答被 GFW 污染，**任何**
    境内解析器（含 DoH）给的都是 Facebook/Twitter 段的假地址。

    只依赖 stdlib + ``curl``（DoH 端点要求 HTTP/2，stdlib 不支持）。
    解析失败返回空列表 —— 调用方保留旧地址继续用。
    """
    import struct
    import subprocess
    import tempfile

    # 自己拼 DNS 查询报文（A 记录）
    tid = int.from_bytes(os.urandom(2), "big")
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00"
    packet = struct.pack("!HHHHHH", tid, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(packet)
        qpath = tmp.name
    rpath = qpath + ".resp"
    try:
        proc = subprocess.run(
            ["curl", "-s", "--http2", "--max-time", "15", "-X", "POST", DOH_ENDPOINT,
             "-H", "content-type: application/dns-message",
             "--data-binary", f"@{qpath}", "-o", rpath, "-w", "%{http_code}"],
            capture_output=True, text=True, timeout=25,
        )
        if proc.stdout.strip() != "200" or not os.path.exists(rpath):
            return []
        raw = open(rpath, "rb").read()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("DoH 刷新失败: %s", exc)
        return []
    finally:
        for p in (qpath, rpath):
            try:
                os.unlink(p)
            except OSError:
                pass

    if len(raw) < 12:
        return []
    _, _, qd, an, _, _ = struct.unpack("!HHHHHH", raw[:12])
    off = 12
    for _ in range(qd):
        while raw[off] != 0:
            off += 1 + raw[off]
        off += 5
    ips: List[str] = []
    for _ in range(an):
        if raw[off] & 0xC0 == 0xC0:
            off += 2
        else:
            while raw[off] != 0:
                off += 1 + raw[off]
            off += 1
        rtype, _, _, rdlen = struct.unpack("!HHIH", raw[off:off + 10])
        off += 10
        rdata = raw[off:off + rdlen]
        off += rdlen
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(rdata))
    return ips


def serve(listen_host: str, port: int, proxy: Proxy) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, port))
    srv.listen(64)
    LOG.info("代理已监听 http://%s:%d  →  %s（真 IP: %s）",
             listen_host, port, UPSTREAM_HOST, ", ".join(proxy.upstreams))
    while True:
        try:
            client, addr = srv.accept()
        except OSError:
            continue
        client.settimeout(proxy.timeout)
        threading.Thread(target=proxy.handle, args=(client, addr), daemon=True).start()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="huggingface.co SNI 绕行代理")
    ap.add_argument("--listen", default="127.0.0.1", help="监听地址（默认只听本机）")
    ap.add_argument("--port", type=int, default=8787, help="监听端口")
    ap.add_argument("--upstream", action="append", default=[],
                    help="上游真实 IP，可重复；缺省用内置的 4 个")
    ap.add_argument("--timeout", type=float, default=120.0, help="单连接超时（秒）")
    ap.add_argument("--print-hosts", action="store_true",
                    help="打印 /etc/hosts 要加的行，然后退出")
    ap.add_argument("--refresh-hours", type=float, default=6.0,
                    help="真实 IP 的刷新周期（小时）；<=0 表示不刷新")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    upstreams = args.upstream or list(DEFAULT_UPSTREAMS)
    if args.print_hosts:
        for ip in upstreams:
            print(f"{ip}\t{UPSTREAM_HOST}")
        print(f"# 把上面几行加进 /etc/hosts（sudo tee -a /etc/hosts）")
        return 0

    local_base = f"http://{args.listen}:{args.port}"
    proxy = Proxy(upstreams, UPSTREAM_HOST, local_base, args.timeout)

    if args.refresh_hours > 0:
        def _refresher() -> None:
            while True:
                time.sleep(args.refresh_hours * 3600)
                ips = refresh_upstreams_via_doh()
                if ips and ips != proxy.upstreams:
                    LOG.info("上游 IP 更新: %s -> %s", proxy.upstreams, ips)
                    proxy.upstreams = ips
                else:
                    LOG.debug("上游 IP 无变化（%s）", proxy.upstreams)

        threading.Thread(target=_refresher, name="hf-upstream-refresh", daemon=True).start()

    try:
        serve(args.listen, args.port, proxy)
    except KeyboardInterrupt:
        LOG.info("退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
