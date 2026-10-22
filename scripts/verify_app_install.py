#!/usr/bin/env python3
"""应用包安装的预检逻辑验证（不碰 venv，不装任何东西）。

用户要求：「判断他是否包含 manifest，不是的话要提示非应用包」。这道闸门
必须在**安装之前**生效 —— 否则一个普通 wheel 拖进来就在这台板子上执行了
任意代码，事后才发现不是应用包就太晚了。

这里合成四种 wheel 走 ``_wheel_info`` + ``_reject_if_not_an_app``：

1. 既无 entry point 也无 manifest —— 普通库，拒绝
2. 有 entry_points.txt 但没有 wdnwebui.cards 组 —— 别的框架的插件，拒绝
3. 声明了组但没有 manifest.json —— 正是「装成功却不出现在列表」的元凶，拒绝
4. 声明了组 + 带 manifest.json —— 放行

另外验一下：真装的话会不会出事（--no-deps / --no-index 是否真的在命令里）、
以及 rescan 返回的字段是否够前端重绘网格。

用法::

    .venv/bin/python scripts/verify_app_install.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from app.system.api.apps import (  # noqa: E402
    CARD_EP_GROUP,
    _pip_install,
    _reject_if_not_an_app,
    _wheel_info,
)

_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


def make_wheel(path: Path, *, entry_points: str | None, manifest: bool,
               name: str = "demo", version: str = "0.1.0") -> None:
    """Build a minimal but structurally real wheel.

    ``RECORD`` is included because a wheel without it is **not installable**:
    pip refuses ("no RECORD file found") and, worse, leaves the files behind
    on failure so every retry hits the same wall. Real wheels always have it.
    """
    dist = f"{name}-{version}.dist-info"
    members: dict[str, str] = {
        f"{name}/__init__.py": "",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\n"
                         "Root-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if entry_points is not None:
        members[f"{dist}/entry_points.txt"] = entry_points
    if manifest:
        members[f"{name}/cards/demo_card/manifest.json"] = (
            '{"id": "demo-card", "title": "演示卡"}')

    record_lines = [
        f"{n},sha256=placeholder,{len(v.encode('utf-8'))}" for n, v in members.items()
    ]
    record_lines.append(f"{dist}/RECORD,,")
    members[f"{dist}/RECORD"] = "\n".join(record_lines) + "\n"

    with zipfile.ZipFile(path, "w") as zf:
        for n, v in members.items():
            zf.writestr(n, v)


def expect_reject(whl: Path, label: str, must_mention: str = "") -> None:
    info = _wheel_info(whl)
    try:
        _reject_if_not_an_app(info, whl.name)
    except HTTPException as e:
        detail = str(e.detail)
        ok = must_mention in detail if must_mention else True
        check(ok, label, f"拒绝理由：{detail}")
        return
    check(False, label, "却放行了 —— 这等于让任意包在本机执行代码")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="verify-appinstall-"))

    print("=== 1. 非应用包必须在安装前被拒 ===")
    # ① 普通库：什么都没有
    w = tmp / "plainlib-1.0-py3-none-any.whl"
    make_wheel(w, entry_points=None, manifest=False, name="plainlib")
    expect_reject(w, "普通库（无 entry point 无 manifest）→ 拒绝", "不是应用包")

    # ② 别家框架的插件：有 entry_points.txt，但不是我们的组
    w = tmp / "otherplug-1.0-py3-none-any.whl"
    make_wheel(w, entry_points="[someother.group]\nfoo = demo.foo\n",
               manifest=True, name="otherplug")
    expect_reject(w, "他框架插件（有 ep 文件但无 wdnwebui.cards 组）→ 拒绝",
                  "没有 wdnwebui.cards 组")

    # ③ 组对了但缺 manifest —— 这正是"装成功却不出现在列表"的元凶
    w = tmp / "nomanifest-1.0-py3-none-any.whl"
    make_wheel(w, entry_points=f"[{CARD_EP_GROUP}]\ndemo-card = demo.cards.demo_card\n",
               manifest=False, name="nomanifest")
    expect_reject(w, "声明了组但缺 manifest.json → 拒绝", "没有 manifest.json")

    print("\n=== 2. 真正的应用包要放行 ===")
    w = tmp / "wdnapps_demo-0.1.0-py3-none-any.whl"
    make_wheel(w, entry_points=f"[{CARD_EP_GROUP}]\ndemo-card = demo.cards.demo_card\n",
               manifest=True, name="wdnapps_demo")
    info = _wheel_info(w)
    check(info["package"] == "wdnapps_demo", "读出包名", str(info))
    check(info["version"] == "0.1.0", "读出版本", str(info))
    check(info["card_entry_points"] == ["demo-card"],
          "读出 entry point 名（= 卡片 id）", str(info["card_entry_points"]))
    check(len(info["manifests"]) == 1, "读出 manifest 路径", str(info["manifests"]))
    try:
        _reject_if_not_an_app(info, w.name)
        check(True, "应用包通过预检")
    except HTTPException as e:
        check(False, "应用包通过预检", str(e.detail))

    print("\n=== 3. 畸形输入 ===")
    # 非 zip
    w = tmp / "broken-1.0-py3-none-any.whl"
    w.write_bytes(b"this is not a zip at all")
    try:
        _wheel_info(w)
        check(False, "非 zip 内容 → 拒绝")
    except HTTPException as e:
        check("不是合法的 wheel" in str(e.detail),
              "非 zip 内容 → 拒绝", str(e.detail))

    # 有 dist-info 但没有 METADATA
    w = tmp / "nometa-1.0-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as zf:
        zf.writestr("nometa/__init__.py", "")
    try:
        _wheel_info(w)
        check(False, "缺 dist-info/METADATA → 拒绝")
    except HTTPException as e:
        check("METADATA" in str(e.detail),
              "缺 dist-info/METADATA → 拒绝", str(e.detail))

    print("\n=== 4. pip 命令的安全参数 ===")
    # 不真的跑（会动 venv），只验命令拼装：私有依赖必须不解析、不能碰索引
    import app.system.api.apps as A
    captured: list[list[str]] = []
    real_run = A.subprocess.run

    def fake_run(cmd, **kw):
        captured.append(cmd)
        class R:  # noqa: N801
            returncode = 0
            stdout = "Successfully installed"
            stderr = ""
        return R()

    A.subprocess.run = fake_run
    try:
        ok, _ = A._pip_install(Path("/tmp/demo-0.1-py3-none-any.whl"))
    finally:
        A.subprocess.run = real_run
    cmd = captured[0] if captured else []
    check(bool(cmd), "pip 命令已拼装", str(cmd))
    check("--no-deps" in cmd, "带 --no-deps（私有依赖 PyPI 上没有，且已在 venv 里）", str(cmd))
    check("--no-index" in cmd, "带 --no-index（断网板子也能装成功）", str(cmd))
    check("--force-reinstall" in cmd, "带 --force-reinstall（同版本重 build 的 wheel 能覆盖）", str(cmd))
    check(cmd and cmd[1:3] == ["-m", "pip"], "用本解释器的 pip，不依赖 PATH 里的 pip", str(cmd))

    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项未通过:")
        for f in _FAILED:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())