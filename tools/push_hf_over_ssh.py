#!/usr/bin/env python3
"""用 SSH（**不需要 API token**）把模型目录推到 HuggingFace。

为什么要有这个脚本
==================

HF 的模型仓可以直接用 SSH 推：账号注册了公钥就够，板子上不必有 write token。
但在本板这条路上有两个坑，每次都会撞：

1. **ssh 默认先试 ``id_rsa``** —— 那把若没注册，Hub 认到未注册的 key 就回
   ``Hi anonymous``，已注册的 ``id_ed25519`` 根本没机会被认。
   ⇒ 必须用 ``~/.ssh/config`` 把顺序钉死。
2. **HF 不支持 pure-SSH 的 LFS 协议** —— git-lfs 回退到 legacy 的
   ``git-lfs-authenticate``（经 SSH，能拿到凭据），但接着把 batch API 发到
   ``https://huggingface.co``，而本板那条路是断的（DNS 污染 + SNI 阻断）。
   ⇒ 把 LFS 端点指到 ``hf-mirror.com``；但端点一旦被 ``lfs.url`` 接管，
   git-lfs 就不再走 SSH 认证，改问 git 要用户名密码 ⇒ 终端弹
   ``Username for 'https://hf-mirror.com':``。
   ⇒ 用 ``tools/hf_lfs_credential_helper.py`` 把两段接起来。

还有一个尾巴：镜像在 batch 响应里回的是**真 Hub 的绝对地址**，
``verify`` 这一步于是又打到 ``huggingface.co`` ⇒ 推送在收尾时报错
（**对象其实已经传上去了**）。本脚本把 verify 也走镜像补一遍，
再 ``git push --no-verify`` 把提交推上去。

用法
====

::

    # 首次：装好前置（git-lfs / ssh config / 凭据助手）
    sudo apt-get install -y git-lfs
    sudo install -m 755 tools/hf_lfs_credential_helper.py /usr/local/bin/hf-lfs-cred

    # 推送
    python tools/push_hf_over_ssh.py --repo wardner/Qwen3.5-VL-9B \
        --dir /userdata/models/Qwen3.5-VL-9B

    # 只看会推什么
    python tools/push_hf_over_ssh.py --repo ... --dir ... --dry-run

模型目录需要已经是 git 仓（有 ``.git``、远端指向 ``git@hf.co:...``）。
首次使用可以先 ``git clone`` 下来再把新文件覆盖进去。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: 镜像端点（本板直连 huggingface.co 不通）。
MIRROR = "https://hf-mirror.com"
CRED_HELPER = "/usr/local/bin/hf-lfs-cred"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def info(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{title}")


def run(cmd: List[str], cwd: Optional[str] = None, check: bool = True,
        env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=3600, env=env)
    if check and proc.returncode != 0:
        fail(f"命令失败（rc={proc.returncode}）：{' '.join(cmd)}")
        for line in (proc.stderr or proc.stdout).strip().splitlines()[-12:]:
            print(f"      {line}")
    return proc


def ssh_greeting() -> str:
    """``ssh -T git@hf.co`` 的问候语 —— 用来确认身份（Hi <user> / Hi anonymous）。"""
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                           "git@hf.co"],
                          capture_output=True, text=True, timeout=60)
    lines = []
    for line in (proc.stdout + proc.stderr).splitlines():
        line = line.strip()
        # ssh 在没有 tty 时会打这句，与身份无关
        if not line or line.startswith("Pseudo-terminal will not be allocated"):
            continue
        lines.append(line)
    return " / ".join(lines)


def lfs_credential(repo: str) -> Optional[Tuple[str, str]]:
    """向 HF 要一份 LFS 凭据（经 SSH）。返回 ``(username, token)``。"""
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "git@hf.co",
         f"git-lfs-authenticate {repo}.git", "upload"],
        capture_output=True, text=True, timeout=90)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        header = json.loads(proc.stdout)["header"]["Authorization"]
        user, _, token = base64.b64decode(header.split(" ", 1)[1]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return None
    return (user, token) if user and token else None


def lfs_batch(repo: str, cred: Tuple[str, str], operation: str,
              objects: List[Tuple[str, int]]) -> Dict[str, dict]:
    """LFS batch API（走镜像）。返回 ``{oid: object}``。"""
    basic = base64.b64encode(("%s:%s" % cred).encode()).decode()
    payload = {
        "operation": operation, "transfers": ["basic"],
        "ref": {"name": "refs/heads/main"},
        "objects": [{"oid": o, "size": s} for o, s in objects],
    }
    req = urllib.request.Request(
        f"{MIRROR}/{repo}.git/info/lfs/objects/batch",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/vnd.git-lfs+json",
                 "Accept": "application/vnd.git-lfs+json",
                 "Authorization": "Basic " + basic},
        method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return {o["oid"]: o for o in data.get("objects", [])}


def lfs_verify(repo: str, cred: Tuple[str, str], oid: str, size: int) -> bool:
    """回执校验 —— **必须走镜像**（真 Hub 的绝对地址在本板不可达）。"""
    basic = base64.b64encode(("%s:%s" % cred).encode()).decode()
    req = urllib.request.Request(
        f"{MIRROR}/{repo}.git/info/lfs/objects/verify",
        data=json.dumps({"oid": oid, "size": size}).encode(),
        headers={"Content-Type": "application/vnd.git-lfs+json",
                 "Accept": "application/vnd.git-lfs+json",
                 "Authorization": "Basic " + basic},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False


def pending_lfs_objects(model_dir: Path, repo_dir: str) -> List[Tuple[str, int]]:
    """本地 commit 里引用、但远端还没有的 LFS 对象。"""
    proc = subprocess.run(["git", "lfs", "push", "--dry-run", "origin", "main"],
                          cwd=repo_dir, capture_output=True, text=True, timeout=900)
    out = []
    for line in proc.stdout.splitlines():
        if line.startswith("push "):
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[1], 0))
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="HF 仓 id（org/name）")
    ap.add_argument("--dir", dest="model_dir", required=True, help="模型目录（git 仓）")
    ap.add_argument("--dry-run", action="store_true", help="只报状态，不推送")
    args = ap.parse_args(argv)

    model_dir = Path(args.model_dir).expanduser().resolve()
    repo = args.repo.strip("/")
    if not model_dir.is_dir():
        fail(f"目录不存在：{model_dir}")
        return 2
    if not (model_dir / ".git").exists():
        fail(f"{model_dir} 不是 git 仓 —— 先 git clone 或 git init + fetch")
        return 2

    # 模型目录通常是 root 的 —— git 会以「可疑所有权」拒掉非 root 用户，
    # 而 git-lfs 还要往 .git/lfs 里写对象 ⇒ 非 root 时整体提权重来。
    if os.geteuid() != 0:
        info(f"当前用户不是 root —— 用 sudo 重跑（模型目录属 root，git 会拒）")
        cmd = ["sudo", "-n", sys.executable, os.path.abspath(__file__)]
        cmd += ["--repo", args.repo, "--dir", str(model_dir)]
        if args.dry_run:
            cmd.append("--dry-run")
        return subprocess.run(cmd).returncode

    section("① 前置检查")
    if shutil.which("git-lfs") is None:
        fail("没装 git-lfs —— sudo apt-get install -y git-lfs")
        return 2
    ok("git-lfs 已装")

    greeting = ssh_greeting()
    if "anonymous" in greeting.lower():
        fail(f"SSH 身份是匿名：{greeting}")
        fail("⇒ 公钥没被认，或 ssh 先试了未注册的 id_rsa。"
             "在 ~/.ssh/config 里钉死 IdentityFile 为已注册的 id_ed25519")
        return 2
    ok(f"SSH 身份：{greeting}")

    lfs_url = f"{MIRROR}/{repo}.git/info/lfs"
    want_remote = f"git@hf.co:{repo}.git"
    cur_remote = run(["git", "config", "--get", "remote.origin.url"],
                     cwd=str(model_dir), check=False).stdout.strip()
    if cur_remote != want_remote:
        if args.dry_run:
            warn(f"remote.origin.url 是 {cur_remote}，应为 {want_remote}")
        else:
            run(["git", "remote", "set-url", "origin", want_remote], cwd=str(model_dir))
            ok(f"remote.origin.url → {want_remote}")
    else:
        ok(f"remote.origin.url = {cur_remote}")

    for key, val in (("lfs.url", lfs_url),
                     ("credential.helper", CRED_HELPER)):
        cur = run(["git", "config", "--get", key], cwd=str(model_dir),
                  check=False).stdout.strip()
        if cur == val:
            ok(f"{key} 已配置")
        elif args.dry_run:
            warn(f"{key} 应为 {val}（当前 {cur or '未设置'}）")
        else:
            run(["git", "config", key, val], cwd=str(model_dir))
            ok(f"{key} → {val}")

    cred = lfs_credential(repo)
    if not cred:
        fail("取不到 LFS 凭据 —— SSH 认证可能有问题")
        return 2
    ok(f"LFS 凭据已取得（用户 {cred[0]}）")

    section("② 待推对象")
    proc = subprocess.run(["git", "lfs", "push", "--dry-run", "origin", "main"],
                          cwd=str(model_dir), capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        fail(f"git lfs --dry-run 失败（rc={proc.returncode}）")
        for line in (proc.stderr or proc.stdout).strip().splitlines()[-8:]:
            print(f"      {line}")
        return 1
    objs = []
    for line in proc.stdout.splitlines():
        if line.startswith("push "):
            _, oid, rest = line.split(" ", 2)
            fname = rest.split("=>")[-1].strip()
            size = (model_dir / fname).stat().st_size if (model_dir / fname).exists() else 0
            objs.append((oid, size, fname))
    if not objs:
        ok("没有待推的 LFS 对象")
    for oid, size, fname in objs:
        info(f"{oid[:12]}…  {fname}  {size / 1048576:.1f} MB")

    if args.dry_run:
        section("③ 干跑结束")
        info("未做任何推送动作")
        return 0

    section("③ 上传 LFS 对象（git push 的 pre-push 钩子会做）")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    push1 = run(["git", "push", "origin", "main"], cwd=str(model_dir),
                check=False, env=env)
    combined = push1.stdout + push1.stderr
    if push1.returncode == 0:
        ok("一次推完（含提交）")
        return 0
    # 预期失败点：verify 打到了 huggingface.co（真 Hub 的绝对地址）
    if "objects/verify" in combined:
        warn("提交在 verify 这一步失败（对象已上传，地址不通）—— 补齐校验后重推")
    else:
        warn(f"git push 返回 {push1.returncode}：{combined.strip()[-300:]}")

    section("④ 补齐回执校验（走镜像）")
    all_ok = True
    for oid, size, fname in objs:
        good = lfs_verify(repo, cred, oid, size)
        ok(f"{fname}  verify {'通过' if good else '失败'}")
        all_ok = all_ok and good
    if not all_ok:
        fail("有对象校验没过")
        return 1

    section("⑤ 推提交（--no-verify：对象已就位）")
    push2 = run(["git", "push", "--no-verify", "origin", "main"],
                cwd=str(model_dir), check=False, env=env)
    if push2.returncode != 0:
        fail("提交推送失败")
        for line in (push2.stderr or push2.stdout).strip().splitlines()[-12:]:
            print(f"      {line}")
        return 1
    ok(push2.stdout.strip() or push2.stderr.strip())

    section("⑥ 复核远端")
    remote = lfs_batch(repo, cred, "download", [(o, s) for o, s, _ in objs])
    for oid, size, fname in objs:
        present = bool(remote.get(oid, {}).get("actions", {}).get("download", {}).get("href"))
        ok(f"{fname}  远端{'已存在' if present else '缺失'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
