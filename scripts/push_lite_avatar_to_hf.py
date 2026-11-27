#!/usr/bin/env python3
"""把 Lite-Avatar 模型包推到 HuggingFace（wardner/Lite-Avatar）。

本板到 huggingface.co 是断的（DNS 污染 + SNI 阻断），推送分两段：

* **控制面**（repo_info / preupload / commit）→ 走 ``tools/hf_sni_proxy.py``
  的 SNI 绕行代理（``http://127.0.0.1:8787``）。代理是**间歇性**不稳的
  —— 4 个上游会随机 SSL EOF ⇒ 每一步都要重试。
* **数据面**（≥10 MB 的文件）→ **xet** 协议。xet 的 CAS 端点在
  ``cas-server.xethub.hf.co`` / ``transfer.xethub.hf.co`` / ``cdn-lfs-us-1.hf.co``，
  这些域名**不含** ``huggingface.co`` ⇒ 不受 SNI 匹配串影响，且 DNS 正常 ⇒ 可直连。

  ⚠️ 走经典 LFS（S3 accelerate）是不行的：``hf-hub-lfs-us-east-1.s3-accelerate.amazonaws.com``
  的 TCP 在本板被掐（普通 ``s3.amazonaws.com`` 通，但证书不匹配 accelerate 域名）。

⚠️ **必须关掉 xet 是不行的** —— 那样大文件会退回 S3 LFS，直接失败。
   所以这里保持 xet **启用**（不要设 ``HF_HUB_DISABLE_XET=1``）。

用法::

    # 1) 起代理（SNI 绕行；需要 sudo 改 hosts 或直接用代理自己的解析）
    sudo .venv/bin/python tools/hf_sni_proxy.py --port 8787

    # 2) 干跑：只列清单 + 本地校验，不上传
    .venv/bin/python -u scripts/push_lite_avatar_to_hf.py --dry-run

    # 3) 真传
    .venv/bin/python -u scripts/push_lite_avatar_to_hf.py
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = "wardner/Lite-Avatar"
SRC = Path(os.path.expanduser("~/models/Lite-Avatar"))
TOKEN_FILE = Path(os.path.expanduser("~/.cache/huggingface/token"))

#: 大文件（≥10 MB）走 xet；小文件走普通 git blob。都直接用 upload_folder。
#: 单批文件数上限 —— 一次 commit 几千个文件会让 Hub 的 commit API 很慢，
#: 而且失败时整批要重来。分批后失败只影响一批。
BATCH_FILES = 200

#: 每个文件最多重试几次（代理间歇性断 ⇒ 必须重试）。
MAX_ATTEMPTS = 40

#: 忽略的东西（Hub 的 commit API 会硬拒 ``.cache/`` 下的路径）。
IGNORE_PATTERNS = [".cache/*", "*.pyc", "__pycache__/*"]


def log(msg: str) -> None:
    print(msg, flush=True)


def setup_backend() -> None:
    """给 HTTP 层挂 urllib3 重试（代理会随机 SSL EOF）。"""
    from huggingface_hub import configure_http_backend
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    def factory():
        import requests

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
    log("HTTP 后端：已挂 urllib3 重试（total=10, backoff=1.5）")


def local_files() -> list[Path]:
    """本地所有要传的文件（相对 SRC 的路径）。

    排序：**大文件在前** —— 大文件走 xet、最容易出问题（代理间歇断连 /
    CAS 端点抖动），让它们先传能尽早暴露问题，而不是攒到最后一批才炸。
    """
    out = []
    for p in SRC.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(SRC).as_posix()
        if rel.endswith(".pyc") or "__pycache__" in rel or rel.startswith(".cache/"):
            continue
        out.append(p)
    out.sort(key=lambda p: (-p.stat().st_size, p.relative_to(SRC).as_posix()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列清单，不上传")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8787",
                    help="控制面端点（SNI 绕行代理）")
    ap.add_argument("--repo", default=REPO)
    args = ap.parse_args()

    os.environ["HF_ENDPOINT"] = args.endpoint
    # ⚠️ 必须让 xet 保持启用（见模块 docstring）。
    os.environ.pop("HF_HUB_DISABLE_XET", None)

    from huggingface_hub import HfApi

    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    api = HfApi(token=token)
    setup_backend()

    files = local_files()
    total = sum(p.stat().st_size for p in files)
    big = [p for p in files if p.stat().st_size >= 10 * 1024 * 1024]
    log("端点    : %s" % args.endpoint)
    log("仓库    : %s" % args.repo)
    log("本地    : %s" % SRC)
    log("文件数  : %d（其中 ≥10MB 走 xet 的: %d）合计 %.1f MB"
        % (len(files), len(big), total / 1048576))
    if args.dry_run:
        for p in files[:20]:
            log("   %-64s %8.1f KB" % (p.relative_to(SRC).as_posix(),
                                       p.stat().st_size / 1024))
        log("   … 其余 %d 个" % max(0, len(files) - 20))
        return 0

    # ---- 远端已有清单（幂等：同样大小就跳过） --------------------------
    remote: dict[str, int] = {}
    for _ in range(8):
        try:
            info = api.repo_info(args.repo, files_metadata=True)
            remote = {s.rfilename: (s.size or 0) for s in info.siblings}
            break
        except Exception as exc:  # noqa: BLE001
            log("  读远端清单失败（重试）: %s" % str(exc)[:120])
            time.sleep(6)
    log("远端已有 %d 个文件" % len(remote))

    todo = [p for p in files
            if remote.get(p.relative_to(SRC).as_posix()) != p.stat().st_size]
    log("待传 %d 个（%.1f MB）"
        % (len(todo), sum(p.stat().st_size for p in todo) / 1048576))

    # ---- 分批上传 ------------------------------------------------------
    failed: list[str] = []
    for i in range(0, len(todo), BATCH_FILES):
        batch = todo[i:i + BATCH_FILES]
        rels = [p.relative_to(SRC).as_posix() for p in batch]
        bsize = sum(p.stat().st_size for p in batch) / 1048576
        ok = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            t0 = time.time()
            log("→ [%d/%d] 批次 %d..%d（%d 个, %.1f MB）"
                % (attempt, MAX_ATTEMPTS, i + 1, i + len(batch), len(batch), bsize))
            try:
                api.upload_folder(
                    repo_id=args.repo,
                    folder_path=str(SRC),
                    allow_patterns=rels,
                    commit_message="Upload Lite-Avatar batch %d" % (i // BATCH_FILES + 1),
                )
                dt = time.time() - t0
                log("   ✅ %.1fs（%.2f MB/s）" % (dt, bsize / max(dt, 1e-9)))
                ok = True
                break
            except Exception as exc:  # noqa: BLE001
                log("   ❌ %s" % str(exc)[:180])
                if attempt < MAX_ATTEMPTS:
                    wait = min(60, 3 * attempt)
                    log("   … %d 秒后重试" % wait)
                    time.sleep(wait)
        if not ok:
            failed.extend(rels)
            log("!! 批次 %d..%d 最终没传上去" % (i + 1, i + len(batch)))

    # ---- 复核 ----------------------------------------------------------
    log("")
    log("=== 复核远端清单 ===")
    info = api.repo_info(args.repo, files_metadata=True)
    remote_now = {s.rfilename: (s.size or 0) for s in info.siblings}
    rtotal = sum(remote_now.values())
    log("  远端 %d 个文件，合计 %.2f MB / 本地 %.2f MB"
        % (len(remote_now), rtotal / 1048576, total / 1048576))
    mismatched = []
    for p in files:
        rel = p.relative_to(SRC).as_posix()
        if remote_now.get(rel) != p.stat().st_size:
            mismatched.append(rel)
    if mismatched:
        log("⚠️ 大小对不上 %d 个:" % len(mismatched))
        for rel in mismatched[:20]:
            log("     %s" % rel)
    else:
        log("✅ 全部 %d 个文件大小一致" % len(files))
    if failed:
        log("未完成批次里的文件: %d" % len(failed))
        return 1
    log("全部完成 ✅")
    return 0 if not mismatched else 1


if __name__ == "__main__":
    sys.exit(main())
