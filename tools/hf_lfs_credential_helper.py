#!/usr/bin/env python3
"""git 凭据助手 —— 按需向 HuggingFace 索取一份「LFS 上传」凭据。

为什么需要它
============

本板到 ``huggingface.co`` 的路是断的（DNS 污染 + SNI 阻断），只有
``hf-mirror.com`` 通。于是 git-lfs 的 LFS 端点被迫改成镜像::

    git config lfs.url https://hf-mirror.com/wardner/Qwen3.5-VL-9B.git/info/lfs

但**端点一旦被 ``lfs.url`` 接管，git-lfs 就不再走 SSH 认证那条路**了：
它只在「端点由 SSH 远端推导」时才去跑 ``git-lfs-authenticate``；否则
就改问 git 要用户名密码 —— 而 HTTPS 端点上没有可用凭据 ⇒ git 弹出
``Username for 'https://hf-mirror.com':``。

本助手把两段接起来：

1. 通过 **SSH**（``git@hf.co``）跑 ``git-lfs-authenticate``，拿到 HF 签发的
   LFS 凭据 —— 它是 ``Authorization: Basic base64(user:令牌)``；
2. 解出明文口令，以 git 凭据助手的格式交回 —— git 会把它重新编码成
   **一模一样**的 Basic 头发给 LFS API。

凭据是**短时效**的（实测 ``expires_in`` 约 3500 秒），所以每次取用都现取，
不落盘、不缓存。

用法::

    git config credential.helper /usr/local/bin/hf-lfs-cred

⚠️ 仓库名写死在下面 —— 换仓要改 ``REPO``。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys

#: HuggingFace 上的目标仓（``org/name``）。
REPO = "wardner/Qwen3.5-VL-9B"


def _repo_from_request(stdin_text: str) -> str:
    """从 git 的凭据请求里解析出目标仓。

    git 调凭据助手时会往 stdin 写::

        protocol=https
        host=hf-mirror.com
        path=wardner/Qwen3.5-VL-9B.git/info/lfs

    ``path`` 里就带着仓名 ⇒ **不用把仓名写死**。写死的后果是：给 A 仓推
    的时候拿的是 B 仓的凭据 —— 眼下能跑通（Hub 的 LFS 凭据没按仓收窄），
    但那是**碰巧**，不是设计。解析不出来时退回默认值。
    """
    for line in stdin_text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() != "path":
            continue
        val = value.strip()
        # `<org>/<name>.git/info/lfs` → `<org>/<name>`
        for suffix in (".git/info/lfs", "/info/lfs", ".git"):
            if val.endswith(suffix):
                return val[: -len(suffix)]
        return val
    return REPO


def main() -> int:
    # git 会把请求字段（protocol/host/path…）写到 stdin
    try:
        request = sys.stdin.read()
    except Exception:  # noqa: BLE001 —— 没有 stdin 也不该崩
        request = ""

    repo = _repo_from_request(request)
    if repo != REPO:
        print(f"hf-lfs-cred: 用请求里的仓 {repo}", file=sys.stderr)

    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
             "git@hf.co", f"git-lfs-authenticate {repo}.git", "upload"],
            capture_output=True, text=True, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print(f"hf-lfs-cred: 取凭据失败：{exc}", file=sys.stderr)
        return 1

    if proc.returncode != 0 or not proc.stdout.strip():
        print("hf-lfs-cred: SSH 认证失败：%s" % proc.stderr.strip()[:160],
              file=sys.stderr)
        return 1

    try:
        header = json.loads(proc.stdout)["header"]["Authorization"]
        user, _, token = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
    except Exception as exc:  # noqa: BLE001
        print(f"hf-lfs-cred: 凭据格式不对：{exc}", file=sys.stderr)
        return 1

    if not user or not token:
        print("hf-lfs-cred: 凭据内容为空", file=sys.stderr)
        return 1

    print(f"username={user}")
    print(f"password={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
