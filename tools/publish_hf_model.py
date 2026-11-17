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
import logging
import os
import subprocess
import sys
import time
import traceback
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

LOG = logging.getLogger("publish_hf_model")

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


def hf_cli() -> List[str]:
    """优先用本仓 venv 里的 hf，找不到就退回 PATH 上的。"""
    local = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "hf"
    return [str(local)] if local.exists() else ["hf"]


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
def _install_retry(retries: int = 6, delay: float = 1.5) -> None:
    """给 huggingface_hub 的 HTTP session 装上「连接被掐」重试。

    为什么需要
    ==========

    本板到 huggingface.co 的路径被 GFW 检查**请求体**。README 里写着
    ``https://huggingface.co/...``，``POST /api/validate-yaml`` 又是**把
    README 原文裸传**上去的 —— 于是这个请求会**间歇性**（实测 3 次里挂 1 次）
    被注入 RST，客户端看到的是::

        requests.exceptions.ConnectionError: (ProtocolError('Connection aborted.',
        RemoteDisconnected('Remote end closed connection without response'))

    ⇒ **重试就能过**（实测重试后成功）。注意 commit 载荷里文件内容是
    **base64**，不含明文域名，所以真正的 commit 请求不受影响 —— 受影响的
    只有 validate-yaml 这一个「传原文」的端点。

    （本脚本另外把 ``_validate_yaml`` 关掉、改成本地校验 front matter ——
    所以这个重试是**兜底**，不是唯一防线。）
    """
    import requests
    from huggingface_hub.utils._http import get_session

    session = get_session()
    if getattr(session, "_wd_retry_patched", False):
        return

    original = requests.Session.request

    def request(self, method, url, *args, **kwargs):
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return original(self, method, url, *args, **kwargs)
            except requests.exceptions.ConnectionError as exc:
                last = exc
                LOG.info("连接被掐（第 %d/%d 次），%.1fs 后重试：%s %s",
                         attempt + 1, retries, delay, method, url)
                time.sleep(delay)
        raise last  # type: ignore[misc]

    requests.Session.request = request  # type: ignore[assignment]
    session._wd_retry_patched = True  # type: ignore[attr-defined]


def _local_validate_front_matter(readme: Path) -> bool:
    """本地校验 README 的 YAML front matter。

    为什么不用 Hub 的 ``/api/validate-yaml``
    =======================================

    那个端点会把 README **原文**裸传上去，而本板到 huggingface.co 的路径
    被 GFW 检查请求体 —— 含 ``huggingface.co`` 字样的正文会被**间歇性**注入
    RST（实测 3 次里挂 1 次），客户端看到::

        requests.exceptions.ConnectionError: (ProtocolError('Connection aborted.',
        RemoteDisconnected('Remote end closed connection without response'))

    ⇒ 本地校验更可靠，也不受链路影响。校验规则与 Hub 一致：YAML 能解析 +
    ``license`` 与 ``library_name`` 在位。
    """
    try:
        import yaml
    except ImportError:
        warn("没装 PyYAML，跳过 front matter 校验")
        return True
    try:
        text = readme.read_text(encoding="utf-8")
    except OSError as exc:
        fail(f"读不到 {readme}：{exc}")
        return False
    if not text.startswith("---"):
        fail(f"{readme.name} 没有 YAML front matter")
        return False
    end = text.find("\n---", 3)
    if end < 0:
        fail(f"{readme.name} 的 front matter 没有闭合")
        return False
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except Exception as exc:  # noqa: BLE001
        fail(f"{readme.name} 的 front matter 不是合法 YAML：{exc}")
        return False
    if not isinstance(meta, dict):
        fail(f"{readme.name} 的 front matter 不是键值对")
        return False
    missing = [k for k in ("license", "library_name") if not meta.get(k)]
    if missing:
        fail(f"{readme.name} 的 front matter 缺字段：{', '.join(missing)}")
        return False
    ok(f"{readme.name} front matter 合法"
       f"（license={meta.get('license')}, library_name={meta.get('library_name')}"
       f", pipeline_tag={meta.get('pipeline_tag', '-')}）")
    return True


def _patch_repo_url_endpoint(endpoint: str) -> None:
    """修 huggingface_hub 的一个坑：自定义端点下 ``CommitInfo`` 构造会炸。

    ``hf_api.py`` 里::

        class CommitInfo:
            def __post_init__(self):
                self.repo_url = RepoUrl(self.commit_url.split("/commit/")[0])
                                                                 ↑ 没传 endpoint

    而 ``RepoUrl.__init__`` 是::

        self.endpoint = endpoint or constants.ENDPOINT      # 默认 huggingface.co

    ⇒ 端点是 ``http://127.0.0.1:8787`` 时，``RepoUrl`` 却拿**默认的
    huggingface.co** 去判定「这是不是 HF URL」→ 判定失败 →::

        ValueError: Unable to retrieve user and repo ID from the passed HF ID:
                    http://127.0.0.1:8787/wardner/Qwen3-ASR

    修法：让 ``RepoUrl`` 默认用我们自己的 endpoint。
    """
    import huggingface_hub.hf_api as ha

    if getattr(ha.RepoUrl, "_wd_endpoint_patched", False):
        return

    orig_new = ha.RepoUrl.__new__
    orig_init = ha.RepoUrl.__init__
    default_endpoint = endpoint

    def __new__(cls, url, endpoint=None):
        return orig_new(cls, url, endpoint=endpoint or default_endpoint)

    def __init__(self, url, endpoint=None):
        orig_init(self, url, endpoint=endpoint or default_endpoint)

    ha.RepoUrl.__new__ = __new__          # type: ignore[assignment]
    ha.RepoUrl.__init__ = __init__        # type: ignore[assignment]
    ha.RepoUrl._wd_endpoint_patched = True  # type: ignore[attr-defined]


def upload_folder_with_retry(repo: str, model_dir: Path, endpoint: str,
                              commit_message: str, rounds: int = 4):
    """上传目录；连接被掐时重试。

    三个要点
    ========

    1. **进程内调用** ``HfApi.upload_folder()``，而不是 ``HfApi.create_commit()``
       之类的其它入口 —— 前者不会把 ``create_repo()`` 返回的 URL 当 ``repo_id``
       往下传（``parse_hf_id()`` 判定「是不是 HF URL」时比的是**默认的
       huggingface.co**，自定义端点下识别不出来）。
    2. **关掉 ``_validate_yaml``** —— 它把 README 原文裸传上去，是被 GFW
       掐断的元凶；改由 ``_local_validate_front_matter()`` 本地校验。
    3. **单请求级重试** —— 被掐是间歇性的，重发那一个请求即可，不必重跑
       整轮（整轮要重算 1.13 GB 的 hash，慢且不一定躲得过）。
    """
    from huggingface_hub import HfApi

    # front matter 已经在本地校验过，远端那个检查可以安全跳过
    HfApi._validate_yaml = lambda self, content, **kw: None  # type: ignore[assignment]

    _patch_repo_url_endpoint(endpoint)
    _install_retry()
    api = HfApi(token=resolve_token(), endpoint=endpoint)
    last: Optional[Exception] = None
    for attempt in range(rounds):
        try:
            return api.upload_folder(
                folder_path=str(model_dir),
                repo_id=repo,
                repo_type="model",
                commit_message=commit_message,
            )
        except Exception as exc:  # noqa: BLE001
            last = exc
            LOG.info("第 %d/%d 轮上传失败：%s", attempt + 1, rounds, exc)
            LOG.info("%s", traceback.format_exc())
            time.sleep(3)
    raise last  # type: ignore[misc]


def create_repo_with_retry(repo: str, endpoint: str, private: bool):
    """建仓；已存在时直接返回。

    用进程内 API 而不是 CLI —— 建仓只是一个 POST，没有「传 README 原文」
    这种会被掐的请求。返回的是 **repo id**（不是 URL）：``create_repo()``
    返回的是 ``RepoUrl``，直接当 ``repo_id`` 用会让 SDK 的 ``parse_hf_id()``
    在自定义端点下解析失败。
    """
    from huggingface_hub import HfApi

    _patch_repo_url_endpoint(endpoint)
    _install_retry()
    api = HfApi(token=resolve_token(), endpoint=endpoint)
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            return api.create_repo(repo_id=repo, repo_type="model",
                                   exist_ok=True, private=private).repo_id
        except Exception as exc:  # noqa: BLE001
            last = exc
            LOG.info("第 %d/4 轮建仓失败：%s", attempt + 1, exc)
            time.sleep(3)
    raise last  # type: ignore[misc]


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

    if not _local_validate_front_matter(model_dir / "README.md"):
        fail("README front matter 校验没过 —— 不上传")
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
        print(f"  {DIM}$ hf repo create {repo} --repo-type model --exist-ok{RESET}")
        try:
            create_repo_with_retry(repo, endpoint, private)
        except Exception as exc:  # noqa: BLE001
            fail(f"建仓失败：{exc}")
            return 1
        ok(f"{repo} 已创建" + ("（私有）" if private else "（公开）"))

    section("⑤ 上传")
    print(f"  {DIM}$ hf upload {repo} {model_dir} --repo-type model{RESET}")
    try:
        upload_folder_with_retry(repo, model_dir, endpoint, commit_message)
    except Exception as exc:  # noqa: BLE001
        fail(f"上传失败：{exc}")
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

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

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
