"""HuggingFace 传输内核 —— **curl 优先、可续传、能自愈**。

为什么不用 ``hf download``
--------------------------
本板上实测（同一文件、同一镜像、只切传输路径）：

* ``hf download`` 默认走 ``hf_xet``（Rust 扩展）：**突发式**（19~52 MB/s
  突刺）但**夹 40s 级停顿**，曾观测到 5 分钟零增长 —— 客户端只能干等。
  ⚠️ **不是体积门槛**：2.27 GiB 的文件也照样一次下成功。
* ``hf download`` + ``HF_HUB_DISABLE_XET=1``：退化成单流 HTTP，平稳但
  没有续传/看门狗，卡死就卡死。
* **curl**：平稳、可 Range 续传、``--speed-limit/--speed-time`` 能识别
  卡死的连接并中止重来。

所以**大批量下载一律走 curl**。另外两条实测结论直接写进本模块的默认行为：

1. **并发不放大带宽** —— 4 条并发 Range 聚合 6.19 MB/s，和单流峰值
   （8.08 MB/s）同量级。故本模块**只做单流串行**。
2. **速率不由客户端决定** —— 同一条命令重复测量拿到过 1.95 和 8.08 MB/s
   两种全程均值（边缘节点 / 镜像限速状态）。所以本模块**不承诺速率**，
   只保证"不卡死 + 可续传"。

本模块是**纯传输层**：不认识"模型"，只认识"仓库 + 文件 + 字节"。
模型语义（暂存目录、原子改名、进度语义）都在
:mod:`app.core.compute.model_download`。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

#: 镜像端点。hf-mirror 是国内可达的 HF 镜像；官方 huggingface.co 在本板
#: 上 DNS 被污染（连接被拒），所以默认走镜像。
HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")

USER_AGENT = "wdnwebui-hf-transfer/1.0"

#: 读盘分块（算 sha256 用）
CHUNK = 4 << 20

#: 速度看门狗 —— 低于 ``SPEED_FLOOR`` 字节/秒且持续 ``SPEED_TIME`` 秒，
#: curl 自己中止（rc=28），由上层重新解析签名 URL 后重连。
SPEED_FLOOR = 100_000
SPEED_TIME = 60

#: 单次 curl 的最长存活时间（兜底，防 --max-time 之外的怪状态）
MAX_TIME = 3600

#: 429 = 镜像配额打爆（5 分钟 10000 次 API 请求）。**不是失败**，要退避。
HTTP_TOO_MANY_REQUESTS = 429


# --------------------------------------------------------------------------- #
# HTTP 小工具
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 60) -> bytes:
    """GET 一次并返回响应体（失败抛异常）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_retry(url: str, *, timeout: int = 60, quota_retries: int = 3,
                   quota_wait: int = 60,
                   on_quota_wait=None) -> bytes:
    """GET + **429 退避重试**。

    镜像的配额是「5 分钟 10000 次请求」—— 一次大文件下载就足以打爆一个
    窗口（实测），所以 429 必须当「等一等」而不是「失败」。
    """
    last: Optional[Exception] = None
    for attempt in range(quota_retries + 1):
        try:
            return http_get(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last = exc
            if getattr(exc, "code", None) != HTTP_TOO_MANY_REQUESTS:
                raise
            if on_quota_wait is not None:
                try:
                    on_quota_wait(quota_wait, attempt + 1)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(quota_wait)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= quota_retries:
                raise
            time.sleep(5)
    raise last if last else RuntimeError("http_get_retry 失败")


def _no_redirect_opener():
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D102
            return None

    return urllib.request.build_opener(_NoRedirect)


def repo_tree(repo: str, *, quota_retries: int = 3,
              on_quota_wait=None) -> Dict[str, int]:
    """远端仓的文件清单 —— ``{相对路径: 字节数}``。

    走 tree API（``?recursive=true``）而不是 ``/api/models`` 的
    ``siblings`` —— **后者不带 size**（实测）。

    429（镜像配额耗尽）自动退避重试 —— 配额窗口是 5 分钟，等一等就好。
    """
    url = f"{HF_ENDPOINT}/api/models/{repo}/tree/main?recursive=true"
    data = json.loads(http_get_retry(url, quota_retries=quota_retries,
                                     on_quota_wait=on_quota_wait).decode())
    out: Dict[str, int] = {}
    for entry in data:
        if entry.get("type") != "file":
            continue
        size = entry.get("size")
        if size is None:
            size = (entry.get("lfs") or {}).get("size") or 0
        out[str(entry.get("path"))] = int(size)
    return out


def resolve_cdn_url(repo: str, path: str) -> str:
    """取真正的下载 URL。

    LFS 大文件：``resolve`` 返 302 → Location 是带签名的 CDN 直链。
    纯文本小文件（``README.md`` / ``config.json`` …）：``resolve`` 直接 200
    返回内容，**没有** redirect —— 这时要用原始 URL，别返回空串
    （空 URL 喂给 curl 会得到 rc=3 URL malformed）。

    ⚠️ hf-mirror 对小文件给的是**相对** redirect
    （``/api/resolve-cache/models/...``）—— 必须补上站点根，否则 curl
    拿到 ``/api/...`` 直接报 rc=3 URL malformed。
    """
    plain = f"{HF_ENDPOINT}/{repo}/resolve/main/{path}"
    opener = _no_redirect_opener()
    req = urllib.request.Request(plain, headers={"User-Agent": USER_AGENT})
    try:
        resp = opener.open(req, timeout=30)
        resp.close()
        return plain
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            loc = exc.headers.get("Location", "") or ""
            if loc.startswith("/"):
                loc = HF_ENDPOINT + loc
            return loc or plain
        raise


def parse_sha256sums_text(text: str) -> Dict[str, str]:
    """解析 ``SHA256SUMS`` 文本 → ``{文件名: sha256}``。"""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            out[parts[1].strip().lstrip("*")] = parts[0].strip()
    return out


def fetch_sha256sums(repo: str) -> Dict[str, str]:
    """取仓内 ``SHA256SUMS`` → ``{文件名: sha256}``。

    取不到就返回空 dict（下载照做，只是没有权威摘要可校验）。
    """
    try:
        text = http_get(f"{HF_ENDPOINT}/{repo}/resolve/main/SHA256SUMS").decode(
            errors="replace"
        )
    except Exception:  # noqa: BLE001 —— 拿不到摘要不该阻断下载
        return {}
    return parse_sha256sums_text(text)


def sha256_file(path: Path) -> str:
    """文件 sha256（分块读，不吃内存）。"""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def free_bytes(path: Path) -> int:
    """``path`` 所在分区的可用字节数。"""
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return 0
    return int(usage.free)


def dir_size(root: Path) -> int:
    """目录树里所有普通文件的字节总和（跳过 ``.cache``）。"""
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == ".cache":
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
#: 事件回调签名：``on_event(kind, **fields)``。
#: kind ∈ {file_start, file_skip, file_done, retry, quota_wait, notice, error}
EventHook = Optional[Callable[..., None]]


def _emit(on_event: EventHook, kind: str, **fields) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, **fields)
    except Exception:  # noqa: BLE001 —— 回调不该弄坏下载
        pass


def _curl_cmd(url: str, dest: Path, *, resume: bool = True) -> List[str]:
    """curl 命令行。

    * ``-f``：**HTTP 4xx/5xx 时不写 body**。没有它，一次 429 会把
      "We had to rate limit you..." 当成文件内容写进 dest，之后
      ``-C -`` 从错误偏移续传 → 文件永久损坏。
    * ``-C -``：Range 续传（服务器不支持时 curl rc=33，上层会退化成
      全新下载）。
    * ``--speed-limit/--speed-time``：卡死看门狗。

    ⚠️ **不要加 ``--retry`` / ``--retry-all-errors``** ——
    curl 的**内部重试会破坏 ``-C -`` 的续传语义**：内部重试时输出文件被
    截短到某个更小的偏移，于是同一段数据被反复重传。实测（同一个 2.27 GiB
    的文件，``--max-time 5`` 制造中途超时，3 次调用同一个目标文件）：
    带 ``--retry 5`` 只到 89.6 MiB / 每次 55.2 s；去掉后到 127.7 MiB /
    每次 5.0 s，curl 报错 offset 逐次累加零重传。

    重试交给**上层**的循环（``download_repo`` 里的 ``for attempt in range(tries)``）：
    那里每次重试都会**重新解析签名 URL**（CDN 签名会过期，curl 内部重试做不到），
    并且会用 ``-C -`` 从当前文件大小精确续传。
    """
    cmd = [
        "curl", "-sS", "-L", "-f",
        "--speed-limit", str(SPEED_FLOOR), "--speed-time", str(SPEED_TIME),
        "--connect-timeout", "20", "--max-time", str(MAX_TIME),
    ]
    if resume:
        cmd += ["-C", "-"]
    cmd += ["-o", str(dest), url]
    return cmd


def _run_curl(cmd: List[str], log_path: Optional[Path],
              should_cancel: Optional[Callable[[], bool]]) -> Tuple[int, str]:
    """跑一次 curl，返回 (rc, stderr 尾部)。

    ``should_cancel`` 为真时立刻杀掉子进程（返回 rc=-9）。

    ⚠️ **stderr 必须由独立线程读**：``curl -sS`` 在正常下载时**不产生任何
    stderr 输出**，主线程一旦 ``readline()`` 就会一直阻塞到 curl 结束 ——
    取消信号根本传不进去（实测：点了取消，任务还在下到文件结束）。
    所以改成「线程读日志 + 主线程轮询进程状态」。
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    tail: "deque[str]" = deque(maxlen=60)

    def _reader() -> None:
        assert proc.stderr is not None
        try:
            for line in proc.stderr:
                line = line.rstrip("\n")
                tail.append(line)
                if log_path is not None:
                    try:
                        with log_path.open("a") as fh:
                            fh.write(line + "\n")
                    except OSError:
                        pass
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if should_cancel is not None and should_cancel():
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                return -9, "\n".join(tail)
            time.sleep(0.3)
    finally:
        reader.join(timeout=2)
    return proc.returncode, "\n".join(tail)


def _http_status_of(tail: str) -> Optional[int]:
    """从 curl stderr 里认 HTTP 状态码（``-f`` 时会打 ``22 ... 429``）。"""
    for line in reversed(tail.splitlines()):
        for token in line.split():
            if token.isdigit() and len(token) == 3 and token.startswith("4"):
                return int(token)
    return None


def download_repo(repo: str,
                  local_dir: Path,
                  *,
                  log_path: Optional[Path] = None,
                  tries: int = 12,
                  on_event: EventHook = None,
                  should_cancel: Optional[Callable[[], bool]] = None,
                  quota_wait_seconds: int = 60) -> Tuple[bool, str]:
    """把一个远端仓逐文件拉到 ``local_dir``，并用仓内 ``SHA256SUMS`` 校验。

    返回 ``(ok, detail)``。

    * **串行**（并发不放大带宽，见模块 docstring）。
    * 每个文件**每次重试都重新解析签名 URL**（CDN 签名会过期）。
    * 命中 429 → 退避 ``quota_wait_seconds``（镜像配额是 5 分钟窗口，
      退避后重试，**不算失败**）。
    * 已存在且 sha256 匹配的文件**跳过**（断点续跑的落点）。
    * ``.cache/huggingface/download/*.incomplete`` 分片会被复用 —— 文件名
      里带 sha256，能对上号就挪成正式文件，省掉重复传输。

    ``tries`` 默认 12（不是 8）：每个失败尝试都要重传文件剩余部分，
    大文件代价高，值得多试几次。
    """
    try:
        sizes = repo_tree(repo)
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) == HTTP_TOO_MANY_REQUESTS:
            return False, "镜像配额已耗尽（429），请稍后重试"
        return False, f"取仓库清单失败: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"取仓库清单失败: {exc}"

    expected = fetch_sha256sums(repo)

    local_dir.mkdir(parents=True, exist_ok=True)

    # 复用 hf 留下的 .incomplete 分片
    cache_dir = local_dir / ".cache" / "huggingface" / "download"
    if cache_dir.is_dir():
        for part in list(cache_dir.glob("*.incomplete")):
            digest = part.name.rsplit(".", 2)[-2] if part.name.count(".") >= 2 else ""
            if not digest:
                continue
            for path, sha in expected.items():
                if sha == digest and not (local_dir / path).exists():
                    shutil.move(str(part), str(local_dir / path))
                    _emit(on_event, "notice", path=path, text="复用 .incomplete 分片")
                    break

    failures: List[str] = []
    for path in sorted(sizes):
        if should_cancel is not None and should_cancel():
            return False, "已取消"
        dest = local_dir / path
        want = expected.get(path)
        size = sizes.get(path, 0)

        # 已完整且摘要对得上 → 跳过
        if dest.exists() and want:
            try:
                if dest.stat().st_size == size and sha256_file(dest) == want:
                    _emit(on_event, "file_skip", path=path, size=size)
                    continue
            except OSError:
                pass
        if dest.exists() and not want:
            # 发布元数据没有权威摘要，但若大小已对得上也跳过
            try:
                if size and dest.stat().st_size == size:
                    _emit(on_event, "file_skip", path=path, size=size)
                    continue
            except OSError:
                pass

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not want:
            # 没法校验 → 直接重下，别让 -C - 去续一个可能已完整的文件
            try:
                dest.unlink()
            except OSError:
                pass

        ok = False
        for attempt in range(tries):
            if should_cancel is not None and should_cancel():
                return False, "已取消"
            try:
                url = resolve_cdn_url(repo, path)
            except urllib.error.HTTPError as exc:
                if getattr(exc, "code", None) == HTTP_TOO_MANY_REQUESTS:
                    _emit(on_event, "quota_wait", path=path, seconds=quota_wait_seconds)
                    time.sleep(quota_wait_seconds)
                    continue
                _emit(on_event, "error", path=path, text=f"解析 CDN URL 失败: {exc}")
                time.sleep(5)
                continue
            except Exception as exc:  # noqa: BLE001
                _emit(on_event, "error", path=path, text=f"解析 CDN URL 失败: {exc}")
                time.sleep(5)
                continue
            if not url:
                _emit(on_event, "quota_wait", path=path, seconds=20)
                time.sleep(20)
                continue

            _emit(on_event, "file_start", path=path, size=size, attempt=attempt + 1)
            cmd = _curl_cmd(url, dest, resume=dest.exists())
            rc, tail = _run_curl(cmd, log_path, should_cancel)

            if rc == 0:
                ok = True
                break

            status = _http_status_of(tail)
            if rc == -9:
                return False, "已取消"
            if status == HTTP_TOO_MANY_REQUESTS:
                _emit(on_event, "quota_wait", path=path, seconds=quota_wait_seconds)
                time.sleep(quota_wait_seconds)
                continue
            if rc == 33:
                # 服务器不支持 Range → 退化成全新下载
                _emit(on_event, "retry", path=path, text="服务器不支持 Range，改为全新下载")
                try:
                    dest.unlink()
                except OSError:
                    pass
                continue
            if rc == 22 and status == 416:
                # Range Not Satisfiable = 本地已完整
                try:
                    if dest.stat().st_size == size:
                        ok = True
                        break
                except OSError:
                    pass
            _emit(on_event, "retry", path=path,
                   text=f"curl rc={rc}{f' ({status})' if status else ''}，重试")
            time.sleep(min(5 * (attempt + 1), 30))

        if not ok:
            failures.append(path)
            continue

        if want:
            try:
                got = sha256_file(dest)
            except OSError as exc:
                failures.append(f"{path}(读盘失败: {exc})")
                continue
            if got != want:
                failures.append(f"{path}(sha256 不符)")
                _emit(on_event, "error", path=path, text="sha256 不符")
                # 摘要不符 → 文件是坏的，删掉让下次重下
                try:
                    dest.unlink()
                except OSError:
                    pass
                continue
        _emit(on_event, "file_done", path=path, size=size)

    if failures:
        return False, f"以下文件下载/校验失败: {failures}"
    return True, ""
