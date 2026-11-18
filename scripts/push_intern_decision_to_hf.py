#!/usr/bin/env python3
"""把 Intern_Decision-4B 推到 HuggingFace（本板 huggingface.co DNS 解析不了，
只能走 hf-mirror.com 反向代理）。

⚠️ **这条链路很脆**
------------------
实测 hf-mirror.com 的 TLS 握手会随机 EOF（``UNEXPECTED_EOF_WHILE_READING``），
单连接带宽也只有 ~0.3 MB/s ⇒ 3.9 GB 要跑几个小时，且中途断链是常态。

所以这里做了三件事：

1. **HTTP 层重试**：:func:`configure_http_backend` 挂上 urllib3 的
   ``Retry``（含 POST —— preupload 是幂等的，重复注册无害）。
2. **每文件重试**：单个文件失败不放弃，退避后重来；已经落盘的小文件
   下次直接跳过（靠远端清单比对大小）。
3. **小文件先传**：先把 README / 清单 / 报告 / 小权重传上去（几分钟内
   就能在网页上看到），最后才啃两个大文件。

用法::

    export HF_ENDPOINT=https://hf-mirror.com
    .venv/bin/python -u scripts/push_intern_decision_to_hf.py

与 ``tools/publish_hf_model.py`` 的分工
---------------------------------------
本仓已有一套**通用**发布工具（``tools/publish_hf_model.py`` + SNI 绕行代理
``tools/hf_sni_proxy.py``，见 ``docs/HF_SNI_PROXY.md``）—— 它面向
**transformers 布局**的模型目录（有 ``config.json`` 的 ``files[]`` 和
``SHA256SUMS``），且走 SNI 代理直连 huggingface.co。

本脚本覆盖的是**另一种目录形状**：

* **不是** transformers 布局（没有 ``config.json`` / ``SHA256SUMS``）——
  例如 RKNN / 量化部署包这种「一堆二进制 + 一份清单」；
* 目录里有**子目录**（``*_report/`` 之类的转换报告）——
  ``tools/publish_hf_model.py`` 只收顶层文件（``iterdir()`` 且 ``is_file()``），
  子目录会被**静默漏掉**；
* 走**镜像端点**（``hf-mirror.com``）而不是 SNI 代理 —— 本板到
  huggingface.co 是 DNS 污染 + SNI 阻断两层封锁，镜像是另一条能走的路。

⇒ 两者不冲突：transformers 布局的模型用 ``tools/publish_hf_model.py``，
本脚本管「非 transformers 布局 / 带子目录 / 走镜像」这一类。
"""
from __future__ import annotations

import os
import sys
import time

ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
REPO = "wardner/Intern_Decision-4B"
SRC = os.path.expanduser("~/models/Intern_Decision-4B")
TOKEN_FILE = os.path.expanduser("~/.cache/huggingface/token")

#: 传输顺序 = 先小后大。小文件先落地，网页马上能看到东西。
ORDER = [
    "README.md",
    "intern-decision-4b.json",
    "Intern-Decision-4B-llm_report/model_report.html",
    "Intern-Decision-4B-vision_report/model_report.html",
    "Intern-Decision-4B-vision.rknn",
    "Intern-Decision-4B-llm.rknn",
    "Intern-Decision-4B-llm.tokenizer.gguf",
    "Intern-Decision-4B-vision.weight",
    "Intern-Decision-4B-llm.embed.bin",
    "Intern-Decision-4B-llm.weight",
]

#: 每个文件最多重试几次（每次之间退避）。
MAX_ATTEMPTS = 40


def _log(msg: str) -> None:
    print(msg, flush=True)


def setup_backend() -> None:
    """给 HTTP 层挂上重试（这条链路会随机 TLS EOF）。"""
    import requests
    from huggingface_hub import configure_http_backend
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    def factory():
        session = requests.Session()
        retry = Retry(
            total=10,
            connect=10,
            read=10,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            # POST 也要重试：preupload 是幂等的注册请求，重复无害。
            allowed_methods=None,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    configure_http_backend(backend_factory=factory)
    _log("HTTP 后端：已挂 urllib3 重试（total=10, backoff=1.5）")


def remote_sizes(api) -> dict:
    """远端清单 {路径: 大小}；读不到就当空。"""
    for _ in range(5):
        try:
            info = api.repo_info(REPO, files_metadata=True)
            return {s.rfilename: s.size for s in info.siblings}
        except Exception as exc:  # noqa: BLE001
            _log("  读远端清单失败（重试）: %s" % str(exc)[:120])
            time.sleep(5)
    return {}


def main() -> int:
    os.environ["HF_ENDPOINT"] = ENDPOINT
    from huggingface_hub import HfApi

    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    api = HfApi(token=token)

    _log("端点    : %s" % ENDPOINT)
    _log("仓库    : %s" % REPO)
    _log("本地目录: %s" % SRC)
    setup_backend()

    remote = remote_sizes(api)
    _log("远端已有 %d 个文件" % len(remote))

    failed = []
    for rel in ORDER:
        local = os.path.join(SRC, rel)
        if not os.path.isfile(local):
            _log("跳过（本地没有）: %s" % rel)
            continue
        size = os.path.getsize(local)
        if remote.get(rel) == size:
            _log("跳过（远端已同样大小 %.1f MB）: %s" % (size / 1048576, rel))
            continue

        ok = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            t0 = time.perf_counter()
            _log("→ [%d/%d] 上传 %s (%.1f MB)"
                 % (attempt, MAX_ATTEMPTS, rel, size / 1048576))
            try:
                api.upload_file(
                    path_or_fileobj=local,
                    path_in_repo=rel,
                    repo_id=REPO,
                    commit_message="Upload %s" % rel,
                )
                dt = time.perf_counter() - t0
                _log("   ✅ 完成 %.1fs（%.2f MB/s）"
                     % (dt, size / 1048576 / max(dt, 1e-9)))
                ok = True
                break
            except Exception as exc:  # noqa: BLE001
                _log("   ❌ 失败: %s" % str(exc)[:200])
                if attempt < MAX_ATTEMPTS:
                    wait = min(60, 3 * attempt)
                    _log("   … %d 秒后重试" % wait)
                    time.sleep(wait)
        if not ok:
            failed.append(rel)
            _log("!! %s 最终没传上去" % rel)

    _log("")
    _log("=== 复核远端清单 ===")
    info = api.repo_info(REPO, files_metadata=True)
    total = 0
    for s in sorted(info.siblings, key=lambda x: -(x.size or 0)):
        total += s.size or 0
        _log("  %-48s %9.1f MB  %s"
             % (s.rfilename, (s.size or 0) / 1048576,
                "LFS" if getattr(s, "lfs", None) else "普通"))
    _log("远端合计 %.2f GB / 本地 3.9 GB" % (total / 1073741824))
    if failed:
        _log("未完成: %s" % ", ".join(failed))
        return 1
    _log("全部完成 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
