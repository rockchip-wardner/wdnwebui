#!/usr/bin/env python3
"""Audit the SDK-declared dependencies against what is actually installed.

Why this exists
---------------
The internal SDKs are always installed with ``pip install --no-deps -e``
(``update_rkwhl.sh`` / ``bootstrap_wdndds_native.sh`` / README §4).  That
is deliberate, not legacy:

* the dependency graph of the private SDKs contains **private packages**
  (``wdndds`` → ``wdnai``, the card plugins → ``wdnhid`` / ``wdnav``) that
  do not exist on PyPI, so letting pip resolve them fails outright;
* the operator owns the dependency set — ``requirements.txt`` pins
  ``transformers`` / ``tokenizers`` / ``opencv-python-headless`` with
  upper bounds that pip would otherwise happily break;
* ``av`` needs a hand-picked build (wdnav's README pins PyAV's main
  branch until the fix ships in a release).

But ``--no-deps`` has a sharp edge: **the SDKs' declared dependencies never
land in the venv on their own**, and the symptom shows up far away from
the cause —

* ``v4l2`` missing → the HDMI-IN endpoints return 503 forever
  (``No module named 'v4l2'``);
* ``tokenizers`` missing → the compute page says 「模型启动失败」.

This script turns that class of failure into an install-time error:

* every ``[project].dependencies`` entry of every SDK under
  ``.venv/src/`` is checked against the **installed** distributions
  (``importlib.metadata`` — *never* the index, so it works offline);
* the version constraint is verified when it can be parsed;
* the module is **really imported**, because "installed but broken" is a
  real class here (upstream ``v4l2`` 0.2 raises ``TypeError`` on import
  under Python 3 — see ``scripts/fix_v4l2_py3.py``).

Usage::

    .venv/bin/python scripts/check_sdk_deps.py            # audit + exit 1 on problems
    .venv/bin/python scripts/check_sdk_deps.py --quiet     # only print problems
    .venv/bin/python scripts/check_sdk_deps.py --src-dir DIR   # audit another tree

It is wired into ``update_rkwhl.sh`` (after every package has been
processed — a failing audit fails the whole run) and into
``bootstrap_wdndds_native.sh`` (step 7, warns but does not abort).

Exit codes::

    0  every declared dependency is installed and importable
    1  at least one dependency is missing / version-mismatched / unimportable
    2  the SDK source tree or the venv could not be located
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata as md
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python < 3.11
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_DIR = REPO_ROOT / ".venv" / "src"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

#: 私有发行版 —— 由兄弟 SDK 的 editable 安装提供，**不在 PyPI 上**。
#: 缺了只能靠 ``update_rkwhl.sh`` 装 SDK 解决，``pip install <name>``
#: 是错的修法，所以提示语要分开写。
PRIVATE_DISTS = {
    "wdnav",
    "wdnai",
    "wdndds",
    "wdnhid",
    "wdnapps-hdmi-processor",
    "wdnapps-video-analyzer",
    "wdnapps_hdmi_processor",
    "wdnapps_video_analyzer",
}

#: 发行版名 → import 名（默认同名 + 下划线化）。
_IMPORT_NAME = {
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "av": "av",
    "pillow": "PIL",
    "python-multipart": "multipart",
    "pyyaml": "yaml",
    "protobuf": "google.protobuf",
    "sentencepiece": "sentencepiece",
}

#: 提供同一个 import 的等价发行版。headless 版即满足 ``opencv-python``
#: 声明 —— 强制解析会把 **GUI 版 opencv-python 一起装进来**，两份
#: ``cv2`` 互相覆盖（见 README §3）。
_EQUIVALENT = {
    "opencv-python": ("opencv-python-headless",),
    "opencv-python-headless": ("opencv-python",),
}

_REQ_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"\s*(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*(?P<spec>[^;]*)"
    r"(?:;\s*(?P<marker>.*))?$"
)

#: 状态
OK = "OK"
MISSING = "MISSING"
BAD_VERSION = "BAD_VERSION"
BROKEN = "BROKEN"
SKIPPED = "SKIPPED"


@dataclass
class Check:
    """One dependency of one SDK."""

    sdk: str
    raw: str
    name: str
    spec: str
    marker: str
    status: str = OK
    detail: str = ""


def _norm(name: str) -> str:
    """PEP 503 normalisation (lowercase, runs of -_. collapse to -)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_requirement(raw: str) -> tuple[str, str, str] | None:
    """Return ``(name, specifier, marker)`` or ``None`` when unparseable."""
    m = _REQ_RE.match(raw)
    if not m:
        return None
    return m.group("name"), (m.group("spec") or "").strip(), (m.group("marker") or "").strip()


def _marker_applies(marker: str) -> bool:
    """Evaluate an environment marker.

    Uses ``packaging`` when available; when it is not, a marker we cannot
    evaluate is treated as **applying** (conservative: report a problem
    rather than hide one).
    """
    if not marker:
        return True
    try:
        from packaging.markers import Marker

        return bool(Marker(marker).evaluate())
    except Exception:  # noqa: BLE001 — packaging missing or marker unsupported
        return True


def _version_ok(installed: str, spec: str) -> bool:
    """Check ``installed`` against a PEP 440 specifier set.

    An unparseable specifier is accepted — the audit's job is to catch
    *missing* dependencies, not to become a second version resolver.
    """
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet

        return installed in SpecifierSet(spec)
    except Exception:  # noqa: BLE001
        return True


def _import_name(dist: str) -> str:
    """Module to import for a distribution name."""
    if dist in _IMPORT_NAME:
        return _IMPORT_NAME[dist]
    return _norm(dist).replace("-", "_")


def _installed_version(dist: str) -> str | None:
    """Version of ``dist`` in this interpreter's environment, if installed."""
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        for alt in _EQUIVALENT.get(_norm(dist), ()):
            try:
                return md.version(alt)
            except md.PackageNotFoundError:
                continue
    return None


def _expand_equivalents(names: set[str]) -> set[str]:
    """把提供同一个 import 的等价发行版名互相算作「已 pin」。

    requirements.txt 里 pin 的是 ``opencv-python-headless``，而 SDK 声明的
    是 ``opencv-python`` —— 两者提供同一个 ``cv2`` 包。不做这层展开的话，
    提示语会错误地说「requirements.txt 里也没有」，把人引去装 GUI 版
    opencv（那会跟 headless 版抢同一个 ``cv2``）。
    """
    out = set(names)
    for name in list(out):
        out.update(_EQUIVALENT.get(name, ()))
    return out


def _requirements_pins() -> set[str]:
    """Distribution names pinned in ``requirements.txt``."""
    names: set[str] = set()
    if not REQUIREMENTS.is_file():
        return names
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        parsed = _parse_requirement(line)
        if parsed:
            names.add(_norm(parsed[0]))
    return _expand_equivalents(names)


def _read_dependencies(pyproject: Path) -> list[str]:
    """``[project].dependencies`` of a pyproject.toml (raw requirement strings)."""
    if tomllib is None:
        raise RuntimeError(
            "Python 3.11+ required (tomllib) — run with .venv/bin/python"
        )
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", []) or []
    return [d for d in deps if isinstance(d, str)]


@contextlib.contextmanager
def _captured_stdio():
    """Capture everything written to fd 1 / fd 2 while the block runs.

    Importing some of these modules is *noisy* — transformers prints
    "None of PyTorch, TensorFlow >= 2.0, or Flax have been found" and
    some modules print banners from C code.  Neither belongs in an
    audit report, but both are useful when an import *fails*.  So:
    swallow during the import, re-surface on failure.

    Redirecting the file descriptors (not just ``sys.stderr``) is what
    catches the C-level writes; ``logging`` handlers hold their own
    reference to the stream, so rebinding ``sys.stderr`` would not
    silence them.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    tmp = tempfile.TemporaryFile()
    try:
        os.dup2(tmp.fileno(), 1)
        os.dup2(tmp.fileno(), 2)
        yield tmp
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        tmp.close()


def _read_captured(tmp) -> str:
    try:
        tmp.seek(0)
        return tmp.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return ""


def _mute_stdout_at_teardown() -> None:
    """把 fd 1 指向 /dev/null，让解释器拆卸时那行 native banner 落进黑洞。

    ``librga`` 在**进程收尾**时（ctypes 释放 CDLL → dlclose）用 ``write(2)``
    直接往 fd 1 吐一行 ``rga_api version 1.10.5_[9]``。

    实测（strace）：那是一次**独立的 write(1, ...) 系统调用**，发生在脚本
    主体跑完、解释器开始收尾时 —— **比 Python 的 atexit 处理器还早**，
    所以「在 atexit 里静音」是来不及的（试过，banner 照样出现）。

    因此只能在 main() 返回前直接换掉 fd 1。stderr 不动：真正的报错
    还是要看得见。

    只在 ``__main__`` 下调用 —— 这个脚本被当模块 import 时不该抢走
    调用方的 stdout。
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
    except OSError:  # pragma: no cover — /dev/null always exists on Linux
        return
    sys.stdout.flush()
    os.dup2(devnull, 1)


def _check_one(sdk: str, raw: str, pins: set[str]) -> Check:
    parsed = _parse_requirement(raw)
    if parsed is None:
        return Check(sdk, raw, "", "", "", SKIPPED, "无法解析的依赖声明")
    name, spec, marker = parsed
    norm = _norm(name)

    if not _marker_applies(marker):
        return Check(sdk, raw, name, spec, marker, SKIPPED,
                     f"marker 不适用于当前环境（{marker}）")

    version = _installed_version(name)
    if version is None:
        if norm in PRIVATE_DISTS:
            detail = ("私有包不在 PyPI —— 用 ./update_rkwhl.sh 装 SDK，"
                      "不要 pip install")
        elif norm in pins:
            detail = ("requirements.txt 里已 pin —— 跑 "
                      ".venv/bin/pip install -r requirements.txt")
        else:
            detail = (f"requirements.txt 里也没有 —— 补进 requirements.txt "
                      f"（.venv/bin/pip install '{name}{spec}'）")
        return Check(sdk, raw, name, spec, marker, MISSING, detail)

    if not _version_ok(version, spec):
        return Check(sdk, raw, name, spec, marker, BAD_VERSION,
                     f"装的是 {version}，不满足 {spec}")

    module = _import_name(name)
    with _captured_stdio() as tmp:
        try:
            importlib.import_module(module)
        except Exception as e:  # noqa: BLE001 — 任何 import 失败都要报出来
            noise = _read_captured(tmp)
            detail = (f"装了（{version}）但 import 失败："
                      f"{type(e).__name__}: {e}")
            if noise:
                detail += f" | import 期间的输出：{noise[:300]}"
            return Check(sdk, raw, name, spec, marker, BROKEN, detail)
    return Check(sdk, raw, name, spec, marker, OK, version)


def _discover_sdks(src_dir: Path) -> list[tuple[str, Path]]:
    """``(name, pyproject)`` for every SDK source tree under ``src_dir``."""
    out: list[tuple[str, Path]] = []
    if not src_dir.is_dir():
        return out
    for entry in sorted(src_dir.iterdir()):
        pyproject = entry / "pyproject.toml"
        if entry.is_dir() and pyproject.is_file():
            out.append((entry.name, pyproject))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Audit SDK-declared dependencies against the venv.",
    )
    ap.add_argument(
        "--src-dir", default=str(DEFAULT_SRC_DIR),
        help="SDK 源码树目录（默认 .venv/src）",
    )
    ap.add_argument(
        "--quiet", action="store_true",
        help="只输出有问题的依赖",
    )
    args = ap.parse_args(argv)

    src_dir = Path(args.src_dir).resolve()
    sdks = _discover_sdks(src_dir)
    if not sdks:
        print(
            f"[deps] FATAL: {src_dir} 下没找到任何带 pyproject.toml 的 SDK",
            file=sys.stderr,
        )
        return 2

    pins = _requirements_pins()
    # transformers 在 import 时会打一条 "None of PyTorch..." 的 warning，
    # 与审计无关 —— 压掉，别污染输出。**必须在 import 之前设。**
    logging.getLogger("transformers").setLevel(logging.ERROR)

    checks: list[Check] = []
    for sdk_name, pyproject in sdks:
        try:
            deps = _read_dependencies(pyproject)
        except Exception as e:  # noqa: BLE001
            print(f"[deps] FATAL: 读 {pyproject} 失败: {e}", file=sys.stderr)
            return 2
        for raw in deps:
            checks.append(_check_one(sdk_name, raw, pins))

    problems = [c for c in checks if c.status not in (OK, SKIPPED)]

    name_w = max(len(n) for n, _ in sdks) + 1
    if not args.quiet:
        print(f"[deps] 扫描 {src_dir} 下 {len(sdks)} 个 SDK 的 "
              f"[project].dependencies（{len(checks)} 条）")
    for sdk_name, _ in sdks:
        mine = [c for c in checks if c.sdk == sdk_name]
        if not mine:
            continue
        bad = [c for c in mine if c.status not in (OK, SKIPPED)]
        skipped = [c for c in mine if c.status == SKIPPED]
        if not bad and args.quiet:
            continue
        head = f"[deps] {sdk_name:<{name_w}}"
        if not bad:
            applied = [c.name for c in mine if c.status == OK]
            extra = (f"（跳过 {len(skipped)} 条 marker 不适用）"
                     if skipped else "")
            print(f"{head}OK       {', '.join(applied)}{extra}")
            continue
        for c in bad:
            print(f"{head}{c.status:<11} {c.raw}  —— {c.detail}")
        if skipped:
            # 有问题时也要说清"另外还跳过了什么" —— 否则明细行数对不上
            # 依赖条数，读的人会以为审计漏了几条。
            print(f"{head}{'SKIPPED':<11} " +
                  "、".join(f"{c.raw}（{c.detail}）" for c in skipped))

    if not problems:
        if not args.quiet:
            print(f"[deps] 全部就绪（{len(checks)} 条依赖）")
        return 0

    # stdout 走管道时是块缓冲，stderr 是无缓冲的 —— 不显式 flush 的话
    # 汇总会跑到明细前面，读起来像"先报结论后给证据"。
    sys.stdout.flush()
    print("", file=sys.stderr)
    print(f"[deps] FATAL: {len(problems)} 条依赖不可用", file=sys.stderr)
    missing_pins = sorted({
        _norm(c.name) for c in problems
        if c.status == MISSING and _norm(c.name) not in pins
        and _norm(c.name) not in PRIVATE_DISTS
    })
    if missing_pins:
        print(f"       这些连 requirements.txt 里都没有，先补进去："
              f"{', '.join(missing_pins)}", file=sys.stderr)
    print("       修复： .venv/bin/pip install -r requirements.txt",
          file=sys.stderr)
    if any(c.status == BROKEN for c in problems):
        print("       import 失败的多半是「装了但版本不对/有 bug」，"
              "装完记得跑 scripts/fix_v4l2_py3.py 之类的补丁脚本",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    _code = main()
    # 报告已经落到 stdout 了 —— 现在把 fd 1 换成 /dev/null，好让解释器
    # 收尾时那行 native banner（librga 打的）落进黑洞。放在 raise 之前，
    # 也放在模块级 import 之后。
    _mute_stdout_at_teardown()
    raise SystemExit(_code)
