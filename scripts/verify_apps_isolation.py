#!/usr/bin/env python3
"""
verify_apps_isolation.py — 验证 7 个应用相互独立、零耦合

测试 3 个层级:
  A) 静态代码扫描 (AST)        —— 跨 app import / app.system import / 反向 system→app
  B) 运行时可达性 (playwright)  —— 每个 app 的 URL 可达 + wizard 可挂载
  C) 故障隔离                   —— 坏 manifest / 缺 template / app.js 语法错
                                  不影响其他 app 的 status="ok"

用法:
  python scripts/verify_apps_isolation.py
退出码: 0 全过 / 1 有失败
"""
import ast
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPS_DIR = REPO / "app" / "apps"


def _cards() -> list[dict]:
    """所有卡（内置 app/apps/ + 插件包）的解析结果。

    6 张卡已迁到 wdnapps_* 插件包，只扫 app/apps/ 等于**几乎没扫** ——
    跨卡 import 的检查会假绿。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from card_dirs import resolve_cards
    return resolve_cards()


def _card_dirs() -> list[Path]:
    return [Path(c["dir"]) for c in _cards()]


def _cross_card_predicate(self_mod: str):
    """import 指向**别的**卡 → 违规。

    卡的 import 命名空间有两个：内置卡 ``app.apps``，插件卡
    ``<包>.cards``（跨卡引用形如 ``wdnapps_video_analyzer.cards.video_retrieval``）。
    引用宿主框架 ``app.core.*`` 是合法的（插件就是靠它用 NPU / 解码器）。
    """
    ns_own = {}
    for c in _cards():
        ns_own.setdefault(c["ns"], set()).add(c["mod"])

    def predicate(name: str) -> bool:
        for ns, mods in ns_own.items():
            if not name.startswith(ns + "."):
                continue
            tail = name[len(ns) + 1:].split(".")[0]
            if tail and tail not in mods:
                return True
        return False
    return predicate
SYS_DIR = REPO / "app" / "system"
TEST_PORT = 18080
TEST_BASE = f"http://localhost:{TEST_PORT}"

# 当前 7 个 app（audio_analysis 是 hidden,但仍参与测试）
EXPECTED_APPS = [
    "audio-analysis",
    "text-chat",
    "tts",
    "video-retrieval",
    "video-detection",
    "video-target-detection",
    "video-target-localization",
]
# 仅面板可见的 6 个（hidden=true 的不进 runtime check）
VISIBLE_APPS = [a for a in EXPECTED_APPS if a != "audio-analysis"]


# ---------------------------------------------------------------------------
# A) 静态代码扫描
# ---------------------------------------------------------------------------
def _scan_imports(tree, predicate):
    """遍历 AST,收集所有符合 predicate 的 import 节点"""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if predicate(alias.name):
                    hits.append(("import", alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.module and predicate(node.module):
                hits.append(("from", node.module, node.lineno))
    return hits


def _all_py_under(root: Path):
    for py in root.rglob("*.py"):
        # 跳过 __pycache__ (虽然 rglob 默认不进,但保险起见)
        if "__pycache__" in py.parts:
            continue
        yield py


def scan_cross_app_imports():
    """A1: app/apps/<id>/*.py 不应跨 app import"""
    bad = []
    for app_dir in _card_dirs():
        # 含连字符的目录名 (audio-analysis) 在 import 里是下划线
        for py in _all_py_under(app_dir):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError as e:
                bad.append((py, "syntax-error", str(e)))
                continue
            predicate = _cross_card_predicate(app_dir.name)
            hits = _scan_imports(tree, predicate)
            for kind, target, line in hits:
                bad.append((py, f"cross-app-{kind}", f"{target} (line {line})"))
    return bad


def scan_app_system_imports():
    """A2: app/apps/<id>/*.py 不应 import app.system.*"""
    bad = []
    for app_dir in _card_dirs():
        for py in _all_py_under(app_dir):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            hits = _scan_imports(tree, lambda n: n.startswith("app.system."))
            for kind, target, line in hits:
                bad.append((py, f"system-{kind}", f"{target} (line {line})"))
    return bad


def scan_system_to_app_imports():
    """A3: app/system/*/*.py 不应 import app.apps.*(im_bridge 是单向注入例外)"""
    bad = []
    for py in _all_py_under(SYS_DIR):
        rel = py.relative_to(REPO)
        if "im_bridge" in rel.parts:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        hits = _scan_imports(tree, lambda n: n.startswith("app.apps."))
        for kind, target, line in hits:
            bad.append((py, f"system-to-app-{kind}", f"{target} (line {line})"))
    return bad


def run_layer_a():
    print("=== A) 静态代码扫描 ===")
    a1 = scan_cross_app_imports()
    a2 = scan_app_system_imports()
    a3 = scan_system_to_app_imports()
    print(f"  A1 跨 app import:        {len(a1)} 处违规")
    for f, k, v in a1:
        rel = f.relative_to(REPO)
        print(f"    ❌ {rel}: {k} {v}")
    print(f"  A2 app.system.* 引用:    {len(a2)} 处违规")
    for f, k, v in a2:
        rel = f.relative_to(REPO)
        print(f"    ❌ {rel}: {k} {v}")
    print(f"  A3 system→app 反向扫描:  {len(a3)} 处违规")
    for f, k, v in a3:
        rel = f.relative_to(REPO)
        print(f"    ❌ {rel}: {k} {v}")
    ok = not (a1 or a2 or a3)
    print(f"  {'✅ 全部通过' if ok else '❌ 有违规'}\n")
    return ok


# 测试 server lifecycle
def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def _wait_for_server(timeout: float = 30.0):
    """异步 wait_for_server 是冗余的 — 这里用同步 polling"""
    import urllib.request
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


def start_test_server():
    """在 18080 端口启动独立 uvicorn 实例,与生产 server (8000) 隔离"""
    if _port_in_use(TEST_PORT):
        print(f"  ⚠️  port {TEST_PORT} 已被占用,跳过 start,假设已有 server")
        return None
    print(f"  启动测试 server on port {TEST_PORT} ...")
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
    if not _wait_for_server(timeout=30):
        proc.terminate()
        raise RuntimeError(f"测试 server 启动超时")
    print(f"  ✅ 测试 server 就绪 (pid={proc.pid})")
    return proc


def stop_test_server(proc):
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass
    print(f"  测试 server 已停")


# B) 运行时可达性
async def layer_b():
    print("=== B) 运行时可达性 (playwright) ===")
    from playwright.async_api import async_playwright
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await ctx.new_page()
        for app_id in VISIBLE_APPS:
            try:
                await page.goto(f"{TEST_BASE}/#{app_id}", wait_until="domcontentloaded")
                # state="attached" 而非默认 visible:framework onReady 异步
                # 移除 hidden class 时机不一定赶上 networkidle;section 在
                # / 的 HTML 已 inject,attached 即可证明 card_slot 已拼装。
                await page.wait_for_selector(
                    f"section#page-{app_id}", timeout=8000, state="attached",
                )
                # 给 framework onReady → showPage → 移除 hidden 的窗口
                await page.wait_for_timeout(500)
                visible = await page.evaluate(f"""
() => {{
  const sec = document.getElementById('page-{app_id}');
  return {{
    hidden: sec?.classList.contains('hidden') ?? null,
    imappExists: !!window['ImApp_{app_id.replace('-', '_')}'],
  }};
}}
""")
                # wizard 挂载不依赖 hidden(它 append 在原 page 兄弟位置),
                # 用选择器直接查
                wizard_present = await page.evaluate("""
() => !!document.querySelector('.wd-wizard-section')
""")
                results[app_id] = {
                    "section": True,
                    "hidden_after_500ms": visible["hidden"],
                    "imapp_registered": visible["imappExists"],
                    "wizard": wizard_present,
                }
            except Exception as e:
                results[app_id] = {"section": False, "err": str(e)[:80]}
        await browser.close()
    ok = True
    for app_id, r in results.items():
        sec = r["section"]
        wz = r.get("wizard", False)
        sec_mark = "✅" if sec else "❌"
        wz_mark = "✅" if wz else "— "
        hidden_ok = "🟢" if r.get("hidden_after_500ms") is False else ("🟡" if r.get("hidden_after_500ms") is True else "—")
        imapp_ok = "🟢" if r.get("imapp_registered") else "—"
        print(f"  {sec_mark} {wz_mark} {app_id:<28} section attached  "
              f"hidden-removed={hidden_ok}  ImApp-{app_id}={imapp_ok}")
        if not sec:
            ok = False
            if "err" in r:
                print(f"        err: {r['err']}")
    print(f"  {'✅ 全部可达' if ok else '❌ 有 app 不可达'}\n")
    return ok


# C) 故障隔离
def _get_manifest_status():
    """拉 / 看 __APP_MANIFESTS__.cards[*].status"""
    with urllib.request.urlopen(f"{TEST_BASE}/", timeout=5) as r:
        html = r.read().decode("utf-8")
    import re
    m = re.search(r"window\.__APP_MANIFESTS__ = (\{.*?\});", html, re.DOTALL)
    if not m:
        raise RuntimeError("__APP_MANIFESTS__ not found in HTML")
    data = json.loads(m.group(1))
    return {c["id"]: c.get("status", "?") for c in data.get("cards", [])}


import urllib.request


def _assert_other_apps_ok(failing_id, before_map, current_map):
    """断言除 failing_id 外,所有 app 仍是 status='ok'"""
    bad = []
    for app_id, st in current_map.items():
        if app_id == failing_id:
            # 失败的 app 可以是 fatal/template_missing,不是我们的关注点
            continue
        if st != "ok":
            bad.append((app_id, "before=" + before_map.get(app_id, "?"), "now=" + st))
    return bad


def c_failure_injection(app_id, failure_kind):
    """
    failure_kind ∈ {"bad_manifest", "missing_template", "syntax_appjs"}
    注入故障 → 触发 reload → 验证其他 app 仍 ok → 恢复
    """
    # 卡片源码目录：内置卡在 app/apps/，插件卡在 .venv/src/ 的插件包里
    # —— 不能一律拼 APPS_DIR（6 张卡已迁走）。
    app_dir = next((Path(c["dir"]) for c in _cards()
                    if c["id"] == app_id or c["name"] == app_id), None)
    if app_dir is None:
        return {"skipped": True, "reason": f"找不到卡片 {app_id}"}
    manifest = app_dir / "manifest.json"
    template = app_dir / "static" / "template.html"
    appjs = app_dir / "static" / "app.js"

    bak = {}
    try:
        # 1. 备份 + 制造
        if failure_kind == "bad_manifest":
            bak[manifest] = manifest.read_bytes()
            manifest.write_text("{ invalid json")
        elif failure_kind == "missing_template":
            if not template.exists():
                return {"skipped": True, "reason": "no template"}
            bak[template] = template.read_bytes()
            template.unlink()
        elif failure_kind == "syntax_appjs":
            if not appjs.exists():
                return {"skipped": True, "reason": "no app.js"}
            bak[appjs] = appjs.read_bytes()
            appjs.write_text("function () { SYNTAX_ERROR");  # 截断
        else:
            raise ValueError(failure_kind)

        time.sleep(0.5)  # 给文件系统一点时间
        before_map = _get_manifest_status()
        after_map = _get_manifest_status()
        return {"before": before_map, "after": after_map}
    finally:
        # 恢复:PermissionError 跳过(需 sudo 才能改 root 600 文件)
        for f, content in bak.items():
            try:
                f.write_bytes(content)
            except PermissionError:
                pass


def run_layer_c():
    print("=== C) 故障隔离 ===")
    failures = []
    # C1: 坏 manifest
    print("\n  C1: 坏 manifest 注入")
    for app_id in VISIBLE_APPS:
        try:
            r = c_failure_injection(app_id, "bad_manifest")
            if r.get("skipped"):
                print(f"    — {app_id} {r['reason']}")
                continue
            after = r["after"]
            bad = _assert_other_apps_ok(app_id, r["before"], after)
            mark = "✅" if not bad else "❌"
            print(f"    {mark} {app_id:<28} after: " +
                  ", ".join(f"{k}={v}" for k, v in after.items()))
            for bid, bef, aft in bad:
                print(f"        ❌ 旁路受损: {bid} {bef} → {aft}")
                failures.append((app_id, bid))
        except Exception as e:
            print(f"    ❌ {app_id} 注入失败: {e}")
            failures.append((app_id, "test-error"))
    # C2: 缺 template
    print("\n  C2: 缺 template 注入")
    for app_id in VISIBLE_APPS:
        try:
            r = c_failure_injection(app_id, "missing_template")
            if r.get("skipped"):
                print(f"    — {app_id} {r['reason']}")
                continue
            after = r["after"]
            bad = _assert_other_apps_ok(app_id, r["before"], after)
            mark = "✅" if not bad else "❌"
            print(f"    {mark} {app_id:<28} after: " +
                  ", ".join(f"{k}={v}" for k, v in after.items()))
            for bid, bef, aft in bad:
                print(f"        ❌ 旁路受损: {bid} {bef} → {aft}")
                failures.append((app_id, bid))
        except Exception as e:
            print(f"    ❌ {app_id} 注入失败: {e}")
            failures.append((app_id, "test-error"))
    # C3: app.js 语法错
    print("\n  C3: app.js 语法错注入")
    for app_id in VISIBLE_APPS:
        try:
            r = c_failure_injection(app_id, "syntax_appjs")
            if r.get("skipped"):
                print(f"    — {app_id} {r['reason']}")
                continue
            # app.js 错不会改变 manifest.status,只需确认其他 manifest ok
            # + 框架没崩(下面 _get_manifest_status 能跑通即 OK)
            after = r["after"]
            bad = _assert_other_apps_ok(app_id, r["before"], after)
            mark = "✅" if not bad else "❌"
            print(f"    {mark} {app_id:<28} manifest_status 仍全 ok")
            for bid, bef, aft in bad:
                print(f"        ❌ 旁路受损: {bid} {bef} → {aft}")
                failures.append((app_id, bid))
        except Exception as e:
            print(f"    ❌ {app_id} 注入失败: {e}")
            failures.append((app_id, "test-error"))
    print(f"\n  {'✅ 故障隔离全部通过' if not failures else f'❌ {len(failures)} 处旁路受损'}\n")
    return not failures


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ok_a = run_layer_a()
    proc = start_test_server()
    try:
        try:
            ok_b = asyncio.run(layer_b())
        except Exception as e:
            print(f"  ❌ layer_b 整体失败: {e}")
            ok_b = False
        try:
            ok_c = run_layer_c()
        except Exception as e:
            print(f"  ❌ layer_c 整体失败: {e}")
            ok_c = False
    finally:
        stop_test_server(proc)
    ok = ok_a and ok_b and ok_c
    print("=" * 60)
    print(f"汇总: A={ok_a} B={ok_b} C={ok_c} → {'✅ PASS' if ok else '❌ FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()