#!/usr/bin/env python3
"""回归：开机自启恢复（boot autostart → 模型加载中 → 自动启动检测）。

覆盖 v0.27 的真机问题：**autostart 把模型拉起来了，但检测不会自己开始**，
用户在开机后必须回来手点一次「启动检测」。更糟的是那 ~40s 加载窗口里
按钮还是可点的，一点就 502「功能未启用」，状态栏还写着「模型加载失败」。

根因链（三段都要挡住，所以这里三段都测）：
  1. ``get_session()`` 只认 ``status == "running"``，加载期与"没这个能力"
     抛的是同一个 ``RuntimeError("功能未启用")``；
  2. detector ``snapshot()`` 把它 catch 成 ``init_error`` → 前端报"失败"；
  3. 没人去启动检测。

对应实现：
  * ``registry.pending_models_for_capability()`` —— 区分"在加载"与"没能力"；
  * ``detector_base.snapshot()`` 新增 ``model_loading`` / ``loading_models``；
  * ``app/static/core/boot-restore.js`` —— 一次性 claim + 转圈 + 就绪后触发一次。

跑法：``.venv/bin/python scripts/verify_boot_restore.py``

**不占 NPU、不启停任何模型**：后端段用假 registry 替换 ``get_registry()``，
前端段在无头 Chromium 里跑纯 JS。真实整机链路仍需手测（见 AGENTS.md）。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


# ══════════════════════════════════════════════════════════════════════════
# 【1】registry.pending_models_for_capability —— 纯逻辑
# ══════════════════════════════════════════════════════════════════════════
print("\n【1】pending_models_for_capability：区分「在加载」与「没这个能力」")

from app.core.compute.registry import ComputeRegistry  # noqa: E402


class _Inst:
    """只带该方法用到的字段的假实例（真 ModelInstance 要 proc / proxy 等）。"""

    def __init__(self, model_id, status, source="spawned",
                 caps=("swcap_vision_chat",)):
        self.model_id = model_id
        self.npu_index = 0
        self.status = status
        self._source = source
        self.capability_types = list(caps)


class _Reg:
    """借用真方法，但用假实例表 —— 不碰真 registry，不启任何 worker。"""

    pending_models_for_capability = ComputeRegistry.pending_models_for_capability

    def __init__(self, insts):
        self._lock = threading.Lock()
        self._instances = {i.model_id: i for i in insts}


M = "qwen3-vl-4b"
CAP = "swcap_vision_chat"

check("loading → 算「正在加载」",
      _Reg([_Inst(M, "loading")]).pending_models_for_capability(CAP) == [M])
check("not_loaded → 也算「正在加载」（boot 带 --npu 时先停在这）",
      _Reg([_Inst(M, "not_loaded")]).pending_models_for_capability(CAP) == [M])
check("running → 不算（已经就绪，别再让前端转圈）",
      _Reg([_Inst(M, "running")]).pending_models_for_capability(CAP) == [])
check("压根没有这个能力 → 空",
      _Reg([]).pending_models_for_capability(CAP) == [])
check("error → 空（是真的失败，不该让用户对着不转的圈干等）",
      _Reg([_Inst(M, "error")]).pending_models_for_capability(CAP) == [])
check("peer(adopted) 在加载 → 不算",
      _Reg([_Inst(M, "loading", source="adopted")]).pending_models_for_capability(CAP) == [])
check("能力不匹配 → 空",
      _Reg([_Inst(M, "loading", caps=("swcap_audio_chat",))])
      .pending_models_for_capability(CAP) == [])
check("adopted_local 在加载 → 算（本机进程，本板等得起）",
      _Reg([_Inst(M, "loading", source="adopted_local")])
      .pending_models_for_capability(CAP) == [M])
check("一个 running + 一个 loading → helper 列出仍在加载的那个",
      _Reg([_Inst("a", "running"), _Inst(M, "loading")])
      .pending_models_for_capability(CAP) == [M])


# ══════════════════════════════════════════════════════════════════════════
# 【2】detector snapshot 的 model_loading —— 跑真 snapshot() 代码
# ══════════════════════════════════════════════════════════════════════════
print("\n【2】snapshot() 的 model_loading 字段")

from app.core.compute import registry as registry_mod  # noqa: E402
from app.core.llm.detector_base import WdnaiDetector  # noqa: E402

_ORIG_GET_REGISTRY = registry_mod.get_registry


class _FakeRegistry:
    def __init__(self, ready: bool, pending=()):
        self._ready = ready
        self._pending = list(pending)

    def get_session(self, cap):
        if self._ready:
            return object()
        raise RuntimeError(f"功能未启用: {cap}")

    def pending_models_for_capability(self, cap):
        return list(self._pending)

    def list_with_source(self):
        return []


class _FakeDetector:
    """借用真 ``WdnaiDetector.snapshot``，其余状态字段填成"干净的未启动"。

    snapshot() 内部 ``from ... import get_registry`` 是**调用时**解析模块属性，
    所以替换 ``registry_mod.get_registry`` 就能把真 registry 挡在外面 ——
    真 get_registry() 会启 Hub / 拉 discovery，测试里绝不能碰。
    """

    snapshot = WdnaiDetector.snapshot

    def __init__(self, capability):
        self._cap = capability
        self._lock = threading.Lock()
        self.available = True
        self._init_error = ""
        self._enabled = False
        self._prompt = ""
        self._every_n = 30
        self._worker = None
        self.slot_count = 1
        self._item_q = queue.Queue()
        self._enabled_tools = []
        self._image_size = None
        self._chosen_model_id = ""

    def _required_capability(self):
        return self._cap

    def slot_pins(self):
        return []


def snap_with(ready: bool, pending=(), capability=CAP):
    registry_mod.get_registry = lambda: _FakeRegistry(ready, pending)
    try:
        return _FakeDetector(capability).snapshot()
    finally:
        registry_mod.get_registry = _ORIG_GET_REGISTRY


s_loading = snap_with(ready=False, pending=[M])
check("加载中 → model_loading=True",
      s_loading.get("model_loading") is True,
      f"loading_models={s_loading.get('loading_models')}")
check("加载中 → init_error 仍是「功能未启用」（前端据此分支，不改语义）",
      "功能未启用" in (s_loading.get("init_error") or ""),
      s_loading.get("init_error", ""))
check("加载中 → session_ready=False",
      s_loading.get("session_ready") is False)

s_ready = snap_with(ready=True)
check("就绪 → model_loading=False",
      s_ready.get("model_loading") is False)
check("就绪 → session_ready=True",
      s_ready.get("session_ready") is True)

s_absent = snap_with(ready=False, pending=[])
check("真的没这个能力 → model_loading=False（前端走错误分支，不是转圈）",
      s_absent.get("model_loading") is False)
check("真的没这个能力 → 保留原 init_error 语义",
      "功能未启用" in (s_absent.get("init_error") or ""))


# ══════════════════════════════════════════════════════════════════════════
# 【3】core/boot-restore.js 状态机 —— 无头浏览器跑真 JS
# ══════════════════════════════════════════════════════════════════════════
print("\n【3】boot-restore.js：一次性 claim + 转圈 + 就绪后触发一次")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOT_JS = os.path.join(ROOT, "app", "static", "core", "boot-restore.js")

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    print("  ⚠️  未安装 playwright，跳过前端段")
    sync_playwright = None

CARD = "video-detection"


def js(ready: bool, loading: bool):
    """构造喂快照的 JS。**必须用 json** —— 直接 f-string 一个 Python dict
    会把 ``False`` / 单引号塞进 JS，那不是 JS，是 Python。"""
    payload = json.dumps({"ready": ready, "loading": loading})
    return f"""() => {{
  const s = {payload};
  const btn = document.getElementById('vd-detector-start');
  const snap = {{available: true, session_ready: s.ready,
                 model_loading: s.loading, enabled: false,
                 init_error: '功能未启用: swcap_vision_chat'}};
  const r = window.BootRestore.tick(window.__h, snap);
  window.__res = {{startNow: r.startNow,
                  spinning: btn.classList.contains('is-boot-wait'),
                  text: (btn.textContent||'').trim(),
                  disabled: btn.disabled,
                  pending: window.__h.pending}};
}}"""


def fresh(page, app_id: str, payload, claim: bool = True):
    """重建一个干净的 handle：先写存档（模拟上一次会话），再 arm。

    顺序不能反 —— ``begin()`` 在 claim 成立的那一刻就把存档**读进内存**了
    （见 boot-restore.js 的 saved 字段），之后再 save 是读不到的。
    """
    page.evaluate("""({app_id, payload, claim}) => {
      localStorage.removeItem('wdn.lastrun.' + app_id);
      if (payload) {
        window.BootRestore.save(app_id, payload);
      }
      if (claim) {
        window.__WDN_BOOT_RESTORE__ = {app_id: app_id, at: Date.now()};
      } else {
        try { delete window.__WDN_BOOT_RESTORE__; } catch (_) {}
      }
      window.__h = window.BootRestore.begin(app_id,
                     {startBtn: 'vd-detector-start'});
    }""", {"app_id": app_id, "payload": payload, "claim": claim})


if sync_playwright is not None:
    src = open(BOOT_JS, encoding="utf-8").read()
    VD_SAVED = {"sources": ["/tmp/a.mp4", "/tmp/b.mp4"],
                "prompt": "有人吗", "loop": True}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # **不能**用 page.set_content()：那是 about:blank（opaque origin），
        # localStorage 直接抛 SecurityError，而 BootRestore 的持久化全靠它。
        # route 拦截给一个真实 origin，不依赖被测服务是否在跑。
        page.route("http://boot-restore.test/", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8",
            body="<button id='vd-detector-start'>启动检测</button>"))
        page.goto("http://boot-restore.test/")
        page.add_script_tag(content=src)

        def run(ready, loading):
            page.evaluate(js(ready, loading))
            return page.evaluate("() => window.__res")

        # --- claim 的一次性 ---
        fresh(page, CARD, VD_SAVED, claim=True)
        claim1 = page.evaluate(
            "() => ({claimed: window.__h.claimed, saved: !!window.__h.saved})")
        check("被 arm 的卡片 → claimed=true", claim1["claimed"] is True)
        check("被 arm 的卡片 → 读到上次存档", claim1["saved"] is True)

        claim2 = page.evaluate("""() => {
          return window.BootRestore.begin('video-detection',
                     {startBtn: 'vd-detector-start'}).claimed;
        }""")
        check("claim 一次性：第二次 begin 不再被认领（手动进卡不会被误触发）",
              claim2 is False, f"claimed={claim2}")

        fresh(page, CARD, VD_SAVED, claim=False)
        claim3 = page.evaluate(
            "() => ({claimed: window.__h.claimed, saved: window.__h.saved})")
        check("没 arm → claimed=false", claim3["claimed"] is False)
        check("没 arm → 不读存档（不覆盖用户刚填的表单）",
              claim3["saved"] is None)

        # --- 状态机：加载中 → 就绪 ---
        fresh(page, CARD, VD_SAVED, claim=True)
        armed = page.evaluate("() => window.BootRestore.arm(window.__h)")
        check("arm → 返回 true", armed is True)
        check("arm → 进入等待态",
              page.evaluate("() => window.__h.pending") is True)

        r1 = run(ready=False, loading=True)
        check("加载中 → startNow=false", r1["startNow"] is False)
        check("加载中 → 按钮转圈", r1["spinning"] is True)
        check("加载中 → 文案含「正在等待模型加载…」",
              "等待模型加载" in r1["text"], r1["text"])
        check("加载中 → 按钮禁用（此刻点必 502，必须锁）", r1["disabled"] is True)

        r2 = run(ready=True, loading=False)
        check("就绪 → startNow=true（自动启动检测）", r2["startNow"] is True)
        check("就绪 → 按钮恢复原文案",
              r2["text"] == "启动检测", r2["text"])
        check("就绪 → 不再转圈", r2["spinning"] is False)
        check("就绪 → pending 已清（不会二次触发）", r2["pending"] is False)

        r3 = run(ready=True, loading=False)
        check("再来一张就绪快照 → 不重复触发（否则会重复启动）",
              r3["startNow"] is False)

        # 真失败：没有任何实例在加载
        fresh(page, CARD, VD_SAVED, claim=True)
        page.evaluate("() => window.BootRestore.arm(window.__h)")
        r4 = run(ready=False, loading=False)
        check("没实例在加载 + 没就绪 → 放弃等待（不挂一个永远不转的圈）",
              r4["pending"] is False and r4["startNow"] is False)
        check("放弃后按钮复原", r4["spinning"] is False, r4["text"])

        # 存档里没有 prompt → 不硬起
        fresh(page, CARD, {"sources": ["/tmp/a.mp4"], "prompt": "   "},
              claim=True)
        page.evaluate("() => window.BootRestore.arm(window.__h)")
        r5 = run(ready=True, loading=False)
        check("存档里 prompt 为空 → 不自动启动（不编提示词跑推理）",
              r5["startNow"] is False)

        # vtl 的 prompts[] 形态
        fresh(page, "video-target-localization",
              {"sources": ["/tmp/a.mp4"], "prompts": ["穿红衬衫的人", "", "  "],
               "loop": True}, claim=True)
        page.evaluate("() => window.BootRestore.arm(window.__h)")
        r6 = run(ready=True, loading=False)
        check("vtl 的 prompts[] 非空 → 自动启动", r6["startNow"] is True)

        # --- 标记**晚到**：卡片 IIFE 抢在 bootRestore 的 fetch 之前跑完 ---
        # 真机场景：刷新时 URL 已带 #<app_id>，卡片先加载，bootRestore 后到。
        # 早期实现只在 "标记先到" 时认领，这条路径整个不触发 —— 而它恰恰是
        # "重启服务后回来看一眼"最自然的动作。必须单独锁住。
        page.evaluate("""() => {
          try { delete window.__WDN_BOOT_RESTORE__; } catch (_) {}
          window.__restoreCalls = 0;
          window.__h = window.BootRestore.begin('video-detection',
                         {startBtn: 'vd-detector-start',
                          restore: (saved, h) => { window.__restoreCalls++; }});
        }""")
        late0 = page.evaluate(
            "() => ({claimed: window.__h.claimed, calls: window.__restoreCalls})")
        check("标记未到 → 不认领", late0["claimed"] is False)
        check("标记未到 → 不调 restore", late0["calls"] == 0)

        page.evaluate("""() => {
          window.__WDN_BOOT_RESTORE__ = {app_id: 'video-detection', at: Date.now()};
          window.dispatchEvent(new CustomEvent('compute:boot-restore',
                        {detail: {app_id: 'video-detection'}}));
        }""")
        late1 = page.evaluate(
            "() => ({claimed: window.__h.claimed, saved: !!window.__h.saved,"
            " calls: window.__restoreCalls})")
        check("标记晚到 → 事件把它捞回来认领", late1["claimed"] is True)
        check("标记晚到 → 补读存档", late1["saved"] is True)
        check("标记晚到 → restore 被调用一次", late1["calls"] == 1)

        # 持久化往返
        round_trip = page.evaluate("""() => {
          window.BootRestore.save('video-detection',
            {sources: ['/tmp/a.mp4', '/tmp/b.mp4'], prompt: '有人吗'});
          return window.BootRestore.load('video-detection');
        }""")
        check("持久化往返：源列表原样存回",
              round_trip["sources"] == ["/tmp/a.mp4", "/tmp/b.mp4"],
              str(round_trip["sources"]))
        check("持久化往返：提示词原样存回",
              round_trip["prompt"] == "有人吗", round_trip["prompt"])

        # localStorage 不可用时不能崩
        broken = page.evaluate("""() => {
          const real = window.localStorage;
          let threw = false;
          try {
            Object.defineProperty(window, 'localStorage',
              {get() { throw new Error('denied'); }, configurable: true});
          } catch (_) { return 'skip'; }
          try {
            window.BootRestore.save('video-detection', {prompt: 'x'});
            window.BootRestore.load('video-detection');
          } catch (e) { threw = true; }
          try {
            Object.defineProperty(window, 'localStorage',
              {value: real, configurable: true, writable: true});
          } catch (_) {}
          return threw;
        }""")
        if broken == "skip":
            print("  ⚠️  该浏览器拦了 localStorage 重定义，跳过该项")
        else:
            check("localStorage 不可用时不抛（退化成老行为）", broken is False)

        browser.close()


# ══════════════════════════════════════════════════════════════════════════
# 【4】三张卡的 app.js 语法 —— 纯解析，不执行
# ══════════════════════════════════════════════════════════════════════════
if sync_playwright is not None:
    print("\n【4】三张卡 app.js 语法（改了卡片脚本却没跑过页面是最常见的翻车方式）")
    import pathlib

    cards_dir = os.path.join(
        ROOT, ".venv/src/wdnapps_video_analyzer/src/wdnapps_video_analyzer/cards")
    for card in ("video_detection", "video_target_detection",
                 "video_target_localization"):
        f = pathlib.Path(cards_dir) / card / "static" / "app.js"
        if not f.is_file():
            check(f"{card}/app.js 存在", False, str(f))
            continue
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            try:
                pg.evaluate("s => { new Function(s); }",
                            f.read_text(encoding="utf-8"))
                check(f"{card}/app.js 可解析", True, f"{len(f.read_text())} 字符")
            except Exception as e:  # noqa: BLE001
                check(f"{card}/app.js 可解析", False, str(e)[:160])
            finally:
                b.close()


print("\n" + "═" * 70)
print(f"PASS={PASS}  FAIL={FAIL}")
print("═" * 70)
sys.exit(1 if FAIL else 0)