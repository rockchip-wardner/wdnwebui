#!/usr/bin/env python3
"""把远端仓库的文件下载回来，和本地原件逐字节比对（sha256）。

为什么非下载不可
----------------
``repo_info(files_metadata=True)`` 在这条镜像链路上只给 ``lfs.size``、
不给 ``lfs.oid``；``paths-info`` 经镜像干脆没有 ``lfs`` 字段 ⇒
**拿不到服务端记录的 sha256**，没法和本地对账。

所以唯一可信的办法 = **把文件拉回来自己算 sha256**，跟本地原件比。

用法::

    export HF_ENDPOINT=https://hf-mirror.com
    .venv/bin/python -u scripts/verify_hf_repo_matches_local.py

与 ``tools/publish_hf_model.py`` 的分工
---------------------------------------
``tools/publish_hf_model.py`` 是本仓的通用发布工具（面向 transformers 布局的
模型目录，走 SNI 绕行代理，见 ``docs/HF_SNI_PROXY.md``）。本脚本是它的
**补充**，不是替代：

* 本脚本按**实际目录树**逐文件比对（含子目录）—— 通用工具只收顶层文件；
* 本脚本走**镜像端点**，不需要起 SNI 代理；
* 本脚本**不要求** ``SHA256SUMS`` / ``config.json``。

⇒ 非 transformers 布局的部署包（RKNN / 量化包）用本脚本对账。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time

ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
#: 仓库与本地目录。默认是 Intern_Decision-4B；**可用环境变量覆盖**去校验别的
#: 部署包（例：``HF_VERIFY_REPO=wardner/Lite-Avatar
#: HF_VERIFY_SRC=~/models/Lite-Avatar``）。
REPO = os.environ.get("HF_VERIFY_REPO", "wardner/Intern_Decision-4B")
SRC = os.path.expanduser(os.environ.get("HF_VERIFY_SRC",
                                        "~/models/Intern_Decision-4B"))
TOKEN_FILE = os.path.expanduser("~/.cache/huggingface/token")
#: 下载落地的根目录。⚠️ **别用 /tmp** —— 本机 /tmp 是 tmpfs，只有 3.1 GB，
#: 下 2 GB 的 .weight 会直接 "No space left on device"。/home 在 nvme 上。
DL_ROOT = os.path.expanduser("~/.cache/hf-verify")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def setup_backend() -> None:
    """HTTP 层重试 —— 这条链路会随机 TLS EOF（不带重试必挂）。"""
    import requests
    from huggingface_hub import configure_http_backend
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    def factory():
        session = requests.Session()
        retry = Retry(total=10, connect=10, read=10, backoff_factor=1.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=None)
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    configure_http_backend(backend_factory=factory)


def retry(fn, what: str, attempts: int = 12):
    """把任意调用包上退避重试。"""
    last = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            print("    ↻ %s 第 %d 次失败: %s" % (what, i, str(exc)[:90]), flush=True)
            time.sleep(min(30, 2 * i))
    raise last


def main() -> int:
    os.environ["HF_ENDPOINT"] = ENDPOINT
    from huggingface_hub import HfApi, hf_hub_download

    # 并发数：SNI 代理是**每连接一线程**（`threading.Thread(target=proxy.handle…)`）
    # ⇒ 支持并发。串行时每个文件 ~3.5 s（延迟主导），几千个文件要几小时；
    # 并发 12 把它压到十几分钟。走 xet 下载时数据面是直连 cas-server，
    # 不受代理的连接数限制。
    workers = int(os.environ.get("HF_VERIFY_WORKERS", "12"))
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    api = HfApi(token=token)
    setup_backend()

    remote = retry(lambda: [s.rfilename for s in api.repo_info(REPO).siblings],
                   "读远端清单")
    os.makedirs(DL_ROOT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="hfverify-", dir=DL_ROOT)
    bad, checked = [], 0
    lock = threading.Lock()

    def check_one(rel: str) -> None:
        nonlocal checked
        local = os.path.join(SRC, rel)
        if not os.path.isfile(local):
            print("  %-48s ⚠️ 本地没有这个文件（跳过）" % rel, flush=True)
            return
        t0 = time.perf_counter()
        try:
            got = retry(
                lambda r=rel: hf_hub_download(
                    REPO, r, token=token,
                    local_dir=os.path.join(tmp, "dl")),
                "下载 %s" % rel)
        except Exception as exc:  # noqa: BLE001
            print("  %-48s ❌ 下载失败: %s" % (rel, str(exc)[:120]), flush=True)
            with lock:
                bad.append(rel)
            return
        a, b = sha256(local), sha256(got)
        with lock:
            checked += 1
            if a == b:
                print("  %-48s ✅ sha256 一致  %s  (%.1fs)"
                      % (rel, a[:16], time.perf_counter() - t0), flush=True)
            else:
                print("  %-48s ❌ sha256 不一致\n      本地 %s\n      远端 %s"
                      % (rel, a, b), flush=True)
                bad.append(rel)

    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(check_one, sorted(remote)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("比对 %d 个文件，%s" % (checked, "✅ 全部一致" if not bad
                                 else "❌ %d 个异常: %s" % (len(bad), bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
