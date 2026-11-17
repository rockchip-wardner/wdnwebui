#!/usr/bin/env python3
"""把模型目录发布到 HuggingFace —— 建仓 + 上传 + 前后校验。

为什么需要这个脚本
==================

「把模型传上 Hub」看着就是一条 ``hf upload``，但真跑起来有四个坑：

1. **不能只 upload 就完事** —— 传完必须**回读校验**：Hub 上的
   ``SHA256SUMS`` 与本地逐字节相同才算数。否则传坏了（截断 / 传了
   旧文件 / LFS 指针没展开）只有用户下载时才发现。
2. **传之前也要校验** —— 本地 ``SHA256SUMS`` 不过就不该传
   （传上去的是坏文件，比不传更糟）。
3. **token 不能落盘** —— 只从环境变量 ``HF_TOKEN`` 读，绝不写进
   任何文件、命令行（会进 shell history 与进程列表）。
4. **本板到 huggingface.co 是断的** —— 必须走
   ``tools/hf_sni_proxy.py`` 那个 SNI 绕行代理（见
   ``docs/HF_SNI_PROXY.md``），即 ``HF_ENDPOINT=http://127.0.0.1:8787``。

用法
====

::

    # 1) 干跑：只做本地校验 + 网络探测，不建仓不上传
    export HF_TOKEN=hf_xxx
    python tools/publish_hf_model.py --repo wardner/Qwen3-ASR \
        --dir /userdata/models/Qwen3-ASR --dry-run

    # 2) 真传
    python tools/publish_hf_model.py --repo wardner/Qwen3-ASR \
        --dir /userdata/models/Qwen3-ASR

    # 3) 只校验 Hub 上已有的仓（回读 SHA256SUMS 对比）
    python tools/publish_hf_model.py --repo wardner/Qwen3-ASR \
        --dir /userdata/models/Qwen3-ASR --verify-only

私有仓加 ``--private``；提交信息默认 ``feat(asr): publish Qwen3-ASR 0.6B for RK182x``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: 默认的 SNI 绕行代理（本机）。见 docs/HF_SNI_PROXY.md。
DEFAULT_ENDPOINT = "http://127.0.0.1:8787"

#: 仓里除模型文件外的「发布元数据」—— 不在 config.json 的 files[] 里，
#: 但要一起传（README / LICENSE / NOTICE / .gitattributes 等）。
META_FILES = {
    "README.md", "README_CN.md", "LICENSE", "NOTICE", "SHA256SUMS",
    "config.json", ".gitattributes",
}

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


# --------------------------------------------------------------------------- #
# 本地校验
# --------------------------------------------------------------------------- #
def parse_sha256sums(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        out[name.strip()] = digest.strip()
    return out


def local_checks(model_dir: Path) -> bool:
    """本地一致性检查：SHA256SUMS 与 config.json 必须与磁盘上的文件对得上。"""
    good = True

    section("① 本地文件校验")
    sums_path = model_dir / "SHA256SUMS"
    if not sums_path.exists():
        fail("缺 SHA256SUMS")
        return False
    expected = parse_sha256sums(sums_path)
    for name, digest in expected.items():
        f = model_dir / name
        if not f.exists():
            fail(f"{name} —— SHA256SUMS 里有，磁盘上没有")
            good = False
            continue
        h = hashlib.sha256()
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if actual != digest:
            fail(f"{name} —— SHA-256 不符（{actual[:16]}… != {digest[:16]}…）")
            good = False
        else:
            ok(f"{name}  {digest[:16]}…")
    if good:
        ok(f"SHA256SUMS 全部匹配（{len(expected)} 个文件）")

    cfg_path = model_dir / "config.json"
    if not cfg_path.exists():
        warn("缺 config.json（跳过一致性检查）")
        return good
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    listed = {f["path"]: f for f in cfg.get("files", [])}
    on_disk = {p.name for p in model_dir.iterdir() if p.is_file()}
    missing = sorted(set(listed) - on_disk)
    if missing:
        fail(f"config.json 列了但磁盘上没有: {', '.join(missing)}")
        good = False
    else:
        ok(f"config.json 的 {len(listed)} 个文件全部在位")
    unlisted = sorted(on_disk - set(listed) - META_FILES)
    if unlisted:
        warn(f"磁盘上多出（不在 config.json 里）: {', '.join(unlisted)}")
    return good


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #
def http_json(url: str, token: Optional[str], timeout: float = 30.0):
    """GET 一个 JSON 接口，返回 ``(status, obj_or_None)``。"""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, None
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def http_text(url: str, token: Optional[str], timeout: float = 30.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def hub_base(endpoint: str) -> str:
    return endpoint.rstrip("/")


def check_endpoint(endpoint: str, token: Optional[str]) -> None:
    section("② 网络与鉴权")
    status, body = http_json(f"{hub_base(endpoint)}/api/whoami-v2", token)
    if status == 200 and isinstance(body, dict):
        who = body.get("name", "?")
        orgs = body.get("orgs") or {}
        perms = ""
        if isinstance(orgs, dict) and orgs:
            first = list(orgs.values())[0]
            if isinstance(first, dict):
                perms = f"，组织权限: {first.get('roleInOrg', '?')}"
        ok(f"鉴权成功：{who}{perms}")
    elif status == 401:
        fail("鉴权失败（401）—— token 无效或没传")
    elif status == 0:
        fail(f"连不上代理 {endpoint} —— 代理没起？"
             f"（sudo systemctl start hf-sni-proxy.service）")
    else:
        warn(f"whoami 返回 {status}: {str(body)[:160]}")


def repo_exists(endpoint: str, repo: str, token: Optional[str]) -> Tuple[Optional[bool], int]:
    """返回 ``(是否存在, HTTP 状态)``。状态非 200/404 时「是否存在」为 None。"""
    status, _ = http_json(f"{hub_base(endpoint)}/api/models/{repo}", token)
    if status == 200:
        return True, status
    if status == 404:
        return False, status
    return None, status


# --------------------------------------------------------------------------- #
# hf CLI
# --------------------------------------------------------------------------- #
def hf_cli() -> List[str]:
    """优先用本仓 venv 里的 hf，找不到就退回 PATH 上的。"""
    local = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "hf"
    return [str(local)] if local.exists() else ["hf"]


def run_hf(args: Sequence[str], endpoint: str, timeout: float = 3600.0) -> int:
    env = dict(os.environ)
    env["HF_ENDPOINT"] = endpoint
    env.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")
    # Xet 必须留着：HF 已弃用 cas-server.huggingface.co，
    # 关掉 Xet 会让上传走已经解析不到的旧 CAS 域名。
    print(f"  {DIM}$ hf {' '.join(args)}{RESET}")
    proc = subprocess.run(hf_cli() + list(args), env=env, timeout=timeout)
    return proc.returncode


# --------------------------------------------------------------------------- #
# 发布
# --------------------------------------------------------------------------- #
def resolve_token() -> str:
    """取 token：优先环境变量 ``HF_TOKEN``，否则读 hf CLI 的标准存放点。

    刻意**不**接受命令行传 token —— 那会进 shell history 与进程列表。
    ``~/.cache/huggingface/token`` 是 ``hf auth login`` 的标准落点，
    已经是「本机已登录」的事实，不需要再把 secret 抄一遍。
    """
    tok = os.environ.get("HF_TOKEN", "").strip()
    if tok:
        return tok
    for path in (
        Path.home() / ".cache" / "huggingface" / "token",
        Path("/root/.cache/huggingface/token"),
    ):
        try:
            if path.exists():
                tok = path.read_text(encoding="utf-8").strip()
                if tok:
                    return tok
        except OSError:
            continue
    return ""


def publish(repo: str, model_dir: Path, endpoint: str, private: bool,
            commit_message: str, dry_run: bool) -> int:
    token = resolve_token()
    if not token:
        fail("拿不到 HF token —— 设 HF_TOKEN 环境变量，"
             "或先跑一次 hf auth login（token 存在 ~/.cache/huggingface/token）")
        return 2

    print(f"{DIM}目标仓: {repo}    目录: {model_dir}    端点: {endpoint}{RESET}")

    if not local_checks(model_dir):
        fail("本地校验没过 —— 不上传（传上去的坏文件比不传更糟）")
        return 1

    check_endpoint(endpoint, token)

    exists, repo_status = repo_exists(endpoint, repo, token)
    section("③ 目标仓状态")
    if exists is True:
        ok(f"{repo} 已存在（HTTP {repo_status}）")
    elif exists is False:
        info(f"{repo} 不存在（HTTP 404）→ 需要建仓")
    elif repo_status in (401, 403):
        fail(f"查 {repo} 被拒（HTTP {repo_status}）—— token 无权限")
    else:
        warn(f"查询 {repo} 状态失败（HTTP {repo_status}）")

    if dry_run:
        section("④ 干跑结束")
        info("未做任何建仓/上传动作")
        files = sorted(p.name for p in model_dir.iterdir() if p.is_file())
        info(f"将上传 {len(files)} 个文件：" + ", ".join(files))
        return 0

    section("④ 建仓")
    if exists is True:
        info("已存在，跳过建仓")
    else:
        args = ["repo", "create", repo, "--repo-type", "model"]
        if private:
            args.append("--private")
        args.append("--exist-ok")
        if run_hf(args, endpoint) != 0:
            fail("建仓失败")
            return 1
        ok(f"{repo} 已创建" + ("（私有）" if private else "（公开）"))

    section("⑤ 上传")
    args = ["upload", repo, str(model_dir), "--repo-type", "model",
            "--commit-message", commit_message]
    if run_hf(args, endpoint) != 0:
        fail("上传失败")
        return 1
    ok("上传完成")

    section("⑥ 回读校验")
    return verify_remote(repo, model_dir, endpoint, token)


def verify_remote(repo: str, model_dir: Path, endpoint: str,
                  token: Optional[str]) -> int:
    """从 Hub 拉回 SHA256SUMS 与文件清单，和本地逐条对比。"""
    status, text = http_text(f"{hub_base(endpoint)}/{repo}/resolve/main/SHA256SUMS", token)
    if status != 200:
        fail(f"拉不到 Hub 上的 SHA256SUMS（HTTP {status}）")
        return 1
    remote = parse_sha256sums_from_text(text)
    local = parse_sha256sums(model_dir / "SHA256SUMS")
    if remote == local:
        ok(f"SHA256SUMS 与本地完全一致（{len(remote)} 项）")
    else:
        fail("SHA256SUMS 与本地不一致：")
        for name in sorted(set(local) | set(remote)):
            if local.get(name) != remote.get(name):
                fail(f"  {name}: 本地 {local.get(name, '-')[:16]}… / "
                     f"远端 {remote.get(name, '-')[:16]}…")
        return 1

    status, tree = http_json(f"{hub_base(endpoint)}/api/models/{repo}/tree/main?recursive=true", token)
    if status == 200 and isinstance(tree, list):
        remote_names = {e.get("path") for e in tree if e.get("type") == "file"}
        local_names = {p.name for p in model_dir.iterdir() if p.is_file()}
        missing = sorted(local_names - remote_names)
        if missing:
            fail("Hub 上缺这些文件: " + ", ".join(missing))
            return 1
        ok(f"Hub 上 {len(remote_names)} 个文件，与本地 {len(local_names)} 个一一对应")
    else:
        warn(f"取文件树失败（HTTP {status}），跳过清单核对")
    return 0


def parse_sha256sums_from_text(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        out[name.strip()] = digest.strip()
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="发布模型目录到 HuggingFace")
    ap.add_argument("--repo", default="wardner/Qwen3-ASR", help="目标仓 id")
    ap.add_argument("--dir", dest="model_dir", default="/userdata/models/Qwen3-ASR")
    ap.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT),
                    help="Hub 端点（默认走本机 SNI 绕行代理）")
    ap.add_argument("--private", action="store_true", help="建成私有仓")
    ap.add_argument("--commit-message",
                    default="feat(asr): publish Qwen3-ASR 0.6B streaming ASR for RK182x")
    ap.add_argument("--dry-run", action="store_true", help="只校验，不建仓不上传")
    ap.add_argument("--verify-only", action="store_true", help="只回读校验已有仓")
    args = ap.parse_args(argv)

    model_dir = Path(args.model_dir).resolve()
    if not model_dir.is_dir():
        fail(f"目录不存在: {model_dir}")
        return 2

    if args.verify_only:
        return verify_remote(args.repo, model_dir, args.endpoint, resolve_token())

    return publish(args.repo, model_dir, args.endpoint, args.private,
                   args.commit_message, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
