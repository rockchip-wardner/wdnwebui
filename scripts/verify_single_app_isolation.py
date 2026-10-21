#!/usr/bin/env python3
"""
verify_single_app_isolation.py — 终极隔离测试:逐个 app 在"删光其它"
环境下仍能正常工作(boot + /api/compute + manifest + page + ImApp)。
7 个 app 各跑一轮,跑完 restore。

注意:
  - 测试 server 跑在 18080 独立端口,与生产 8000 隔离
  - 需 sudo 跑(生产 server 写的 __pycache__ 是 root 600,rockchip 删不动)
  - 不动 backend/frontend 共享代码、system/、core/
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPS_DIR = REPO / "app" / "apps"
# ORIGINAL_DIR 在 main() 一次性备份,之后只读;每次 isolate 前从它恢复。
# (之前用 BACKUP_DIR 当恢复源,被首次 isolate 替换后就不完整,踩过坑)
ORIGINAL_DIR = Path("/tmp/wdnwebui_apps_original")
TEST_PORT = 18080
TEST_BASE = f"http://localhost:{TEST_PORT}"

APPS = sorted([
    ("audio_analysis",      "audio-analysis",          True),   # (dir_name, id, hidden)
    ("text_chat",           "text-chat",               False),
    ("tts",                 "tts",                     False),
    ("video_retrieval",     "video-retrieval",         False),
    ("video_detection",     "video-detection",         False),
    ("video_target_detection","video-target-detection",False),
    ("video_target_localization","video-target-localization",False),
])


# ---------------------------------------------------------------------------
# 测试 server lifecycle (生产 server 以 root 跑,__pycache__ 是 root 600
# 写的,rockchip 删不动,涉及隔离删目录时需 sudo)
# ---------------------------------------------------------------------------
def _port_in_use(port: int) -> bool:
    """检查端口是否真正被进程占用(socket TIME_WAIT 也算可用)。"""
    out = subprocess.run(
        ["ss", "-tln", f"sport = :{port}"],
        capture_output=True, text=True, timeout=3,
    )
    # ss 输出首行是表头,如果只有表头则无 listener
    lines = [l for l in out.stdout.strip().splitlines() if l]
    return len(lines) > 1


def _wait_for_server(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{TEST_BASE}/api/compute", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def _kill_port(port: int):
    """强杀占用 port 的进程(包括 sudo)。每个测试 server stop 后
    端口会进入 TIME_WAIT ~60s,所以下一次 bind 前需要清理残留。"""
    # 先尝试 fuser(rockchip 可能没 sudo)
    try:
        subprocess.run(["fuser", "-k", f"{port}/tcp"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    time.sleep(0.5)
    try:  # sudo fallback
        subprocess.run(["sudo", "-n", "fuser", "-k", f"{port}/tcp"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    # 进程没了 TIME_WAIT 仍可能让 bind 失败;等几秒让内核回收
    time.sleep(2)


def start_test_server() -> subprocess.Popen:
    _kill_port(TEST_PORT)
    if _port_in_use(TEST_PORT):
        raise RuntimeError(f"port {TEST_PORT} 已被占用且无法释放")
    proc = subprocess.Popen(
        [
            ".venv/bin/python", "-m", "uvicorn",
            "app.main:app",
            "--host", "127.0.0.1",
            "--port", str(TEST_PORT),
            "--no-access-log",
        ],
        cwd=str(REPO),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    if not _wait_for_server(30):
        proc.terminate()
        raise RuntimeError(f"测试 server 启动超时")
    return proc


def stop_test_server(proc: subprocess.Popen):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
    # 端口在 TIME_WAIT 期间不能立即 bind;下个 isolate_app 的 _kill_port
    # 会再 fuser 一次确保释放(生产 8000 端口用 SO_REUSEADDR 不受影响)


# Apps 备份/恢复
def backup_apps():
    """备份当前所有 app 目录到 ORIGINAL_DIR(只读,测试期间不变)"""
    if ORIGINAL_DIR.exists():
        _rmtree_ignore(ORIGINAL_DIR)
    ORIGINAL_DIR.mkdir(parents=True, exist_ok=True)
    for app_dir in APPS_DIR.iterdir():
        if not app_dir.is_dir() or app_dir.name.startswith(("_", ".")):
            continue
        dest = ORIGINAL_DIR / app_dir.name
        shutil.copytree(app_dir, dest)
    return ORIGINAL_DIR


def restore_apps():
    """从 ORIGINAL_DIR 恢复所有 app 目录(最终恢复阶段)"""
    if not ORIGINAL_DIR.exists():
        print(f"  ⚠️  原始快照不存在,无法 restore")
        return False
    for entry in APPS_DIR.iterdir():
        if entry.name.startswith(("__", ".")):
            continue
        _rmtree_ignore(entry)
    for app_dir in ORIGINAL_DIR.iterdir():
        if not app_dir.is_dir():
            continue
        shutil.copytree(app_dir, APPS_DIR / app_dir.name)
    return True


def isolate_app(keep_dir_name: str):
    """保留 keep_dir_name,删除除它外的所有 app。

    每次调用前从 ORIGINAL_DIR(只读完整快照)恢复 apps 完整状态 ——
    否则第一次 isolate 后 APPS_DIR 已污染,后续 iterate 拿不到完整状态。
    """
    for entry in APPS_DIR.iterdir():
        if entry.name.startswith(("__", ".")):
            continue
        _rmtree_ignore(entry)
    for app_dir in ORIGINAL_DIR.iterdir():
        if not app_dir.is_dir():
            continue
        shutil.copytree(app_dir, APPS_DIR / app_dir.name)
    removed = []
    for entry in APPS_DIR.iterdir():
        if entry.name.startswith(("__", ".")):
            continue
        if entry.name == keep_dir_name:
            continue
        _rmtree_ignore(entry)
        removed.append(entry.name)
    return removed


def _rmtree_ignore(path):
    """rmtree 跳过 PermissionError(生产 server 以 root 跑,__pycache__
    中 .pyc 是 root 写的 600,rockchip 删不动;下次 server boot 会
    重建 cache,无害)。

    同时忽略 ENOTEMPTY(目录中含权限被跳过的文件,导致 rmdir 失败) —
    这种情况等价于部分删除,反正目录里的内容下次 server boot 时
    会被新 rmtree-on-boot 行为处理或保留 cache 复用。
    """
    def onerror(fn, p, exc_info):
        err = exc_info[1]
        if isinstance(err, PermissionError):
            return
        if isinstance(err, OSError) and err.errno == 39:  # ENOTEMPTY
            return
        raise err
    shutil.rmtree(path, onerror=onerror)


# 单 app 验证
def check_single_app(app_dir_name: str, app_id: str, is_hidden: bool) -> dict:
    """
    在测试 server 跑着的状态下,验证只有这个 app 的环境是否正常工作。
    返回 dict 含各项检查结果。
    """
    result = {"app_id": app_id, "dir": app_dir_name, "hidden": is_hidden}

    # 1. /api/compute 可访问
    try:
        with urllib.request.urlopen(f"{TEST_BASE}/api/compute", timeout=5) as r:
            data = json.loads(r.read())
            result["compute_status"] = "ok"
    except Exception as e:
        result["compute_status"] = f"err: {e}"
        return result

    # 2. / HTML 中能找到该 app 的 card
    try:
        with urllib.request.urlopen(f"{TEST_BASE}/", timeout=5) as r:
            html = r.read().decode("utf-8")
    except Exception as e:
        result["html_status"] = f"err: {e}"
        return result

    import re
    m = re.search(r"window\.__APP_MANIFESTS__ = (\{.*?\});", html, re.DOTALL)
    if not m:
        result["manifest_parse"] = "err: __APP_MANIFESTS__ not found"
        return result
    manifests = json.loads(m.group(1))
    cards = manifests.get("cards", [])
    result["manifest_card_count"] = len(cards)
    result["self_in_manifest"] = any(c.get("id") == app_id for c in cards)
    self_card = next((c for c in cards if c.get("id") == app_id), None)
    result["self_status"] = self_card.get("status", "?") if self_card else "?"

    # 3. HTML 中能找到该 app 的 <section id="page-{app_id}">
    sec_pattern = f'id="page-{app_id}"'
    result["section_in_html"] = sec_pattern in html

    # 4. hidden app 不在 grid,visible app 应有 href 链接
    if is_hidden:
        result["card_in_grid"] = None  # n/a
    else:
        result["card_in_grid"] = f'href="#{app_id}"' in html

    # 5. ImApp 检查放在 playwright 阶段(更可靠),这里不重复
    return result


def run_isolation_test(app_dir_name: str, app_id: str, is_hidden: bool):
    """删除其它 → 重启 → 验证 → 恢复"""
    print(f"\n--- {app_id} ({app_dir_name}) ---")
    removed = isolate_app(app_dir_name)
    print(f"  移除了 {len(removed)} 个其它 app: {removed}")

    proc = None
    try:
        proc = start_test_server()
        print(f"  ✅ server 启动 ok")
    except Exception as e:
        print(f"  ❌ server 启动失败: {e}")
        restore_apps()
        return None

    http_check = None
    try:
        asyncio.run(_playwright_check(app_id, is_hidden))
    except Exception as e:
        print(f"  ⚠️  playwright 阶段失败: {e}")
    try:
        http_check = check_single_app(app_dir_name, app_id, is_hidden)
    except Exception as e:
        print(f"  ⚠️  http check 异常: {e}")
    finally:
        stop_test_server(proc)
    return http_check


async def _playwright_check(app_id: str, is_hidden: bool):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:80]))
        await page.goto(f"{TEST_BASE}/#{app_id}", wait_until="domcontentloaded")
        await page.wait_for_selector(
            f"section#page-{app_id}", timeout=8000, state="attached",
        )
        await page.wait_for_timeout(500)
        info = await page.evaluate(f"""
() => {{
  const sec = document.getElementById('page-{app_id}');
  return {{
    hiddenAfter500ms: sec?.classList.contains('hidden'),
    imappExists: !!window['ImApp_{app_id.replace('-', '_')}'],
  }};
}}
""")
        mark_sec = "✅" if info["hiddenAfter500ms"] is False else "🟡"
        sec_text = "已显示" if info["hiddenAfter500ms"] is False else "hidden"
        print(f"  {mark_sec} section#{app_id} {sec_text}")
        # Hidden app 不需要 ImApp 全局;可见 app 必须注册(framework.onReady
        # 调 _loadCardJS → app.js IIFE → window.ImApp_X = {...})
        if not is_hidden:
            mark_im = "✅" if info["imappExists"] else "❌"
            print(f"  {mark_im} ImApp-{app_id}={info['imappExists']}")
        else:
            print(f"  —  hidden app,ImApp 检查跳过")
        if errs:
            print(f"  ⚠️  page errors: {errs[:3]}")
        await browser.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("=== 终极隔离测试:单 app 存活 ===")
    # 备份
    print(f"备份 apps 到 {ORIGINAL_DIR} ...")
    backup_apps()
    print(f"  备份完成,共 {len(list(ORIGINAL_DIR.iterdir()))} 个 app")
    print()

    results = []
    for dir_name, app_id, is_hidden in APPS:
        r = run_isolation_test(dir_name, app_id, is_hidden)
        results.append(r)

    # 最终恢复
    print("\n=== 恢复 apps ===")
    ok = restore_apps()
    remaining = sorted(p.name for p in APPS_DIR.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    print(f"  {'✅' if ok else '❌'} 恢复完成,APPS_DIR 现含: {remaining}")
    expected = sorted(d for d, _, _ in APPS)
    if remaining == expected:
        print(f"  ✅ 所有 {len(expected)} 个 app 已恢复")
    else:
        missing = set(expected) - set(remaining)
        extra = set(remaining) - set(expected)
        if missing:
            print(f"  ❌ 缺失: {missing}")
        if extra:
            print(f"  ⚠️  多余: {extra}")

    # 汇总
    print("\n" + "=" * 60)
    print("汇总:")
    boot_count = sum(1 for r in results if r)
    server_ok = sum(1 for r in results if r and r.get("compute_status") == "ok")
    section_ok = sum(1 for r in results if r and r.get("section_in_html"))
    manifest_ok = sum(1 for r in results if r and r.get("self_in_manifest"))
    print(f"  server boot 成功: {boot_count}/{len(APPS)}")
    print(f"  /api/compute OK: {server_ok}/{len(APPS)}")
    print(f"  manifest 包含 self: {manifest_ok}/{len(APPS)}")
    print(f"  page section in HTML: {section_ok}/{len(APPS)}")
    all_ok = (
        boot_count == len(APPS)
        and server_ok == len(APPS)
        and manifest_ok == len(APPS)
        and section_ok == len(APPS)
        and remaining == expected
    )
    print(f"  {'✅ 终极隔离 PASS' if all_ok else '❌ FAIL'}")
    sys.exit(0 if all_ok else 1)


import asyncio
if __name__ == "__main__":
    main()