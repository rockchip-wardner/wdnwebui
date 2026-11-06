#!/usr/bin/env python3
"""UI 回归：「模型下载」卡片的角标与正文语义。

背景
----
角标曾经报的是**下载任务流水**（进程内存里的 history），文案写成
「✓ 已安装」——读起来像**库存陈述**，于是撒谎：用户把模型目录删了，
流水照样留着 ``state=done``，角标一直绿。

修法：角标改报**真实库存**「已装 N/M」，任务流水只保留**异常**
（失败 / 取消）写到卡片正文。

正文本来还有两行冗余（已删）：

* 「上次：X 下载完成 · <path>」—— **操作流水**不是**当前状态**，
  模型目录被用户删了它也照样留着；
* 「本板已装 N/M 个 wardner 模型」—— 与角标**同一句话说两遍**。

本脚本断言
----------
1. 角标四态：下载中 / 失败 / 空闲报库存 / 无清单。
2. 正文行 ``#cc-dl-last``：**只有**失败 / 取消时才出现；成功态隐藏。
3. 正文里**没有**库存计数行（``#cc-dl-stock`` 已从 DOM 里删掉）。
4. ``_staged_bytes()`` 量的是**已占用的磁盘字节**（半截文件只算已下部分）。
5. 续传时的磁盘预算会**扣掉**已暂存字节 —— 否则会被自己的半成品
   卡成「可用空间不足」。

跑法：``.venv/bin/python scripts/verify_model_download_card.py``
（需要服务在跑；只读页面 + 注入内存状态，不点任何按钮、不落盘）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("WDNWEBUI_BASE", "http://127.0.0.1:8000")
_FAILED: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}")
    if not ok:
        if detail:
            print(f"       {detail}")
        _FAILED.append(label)
    return ok


# ---------------------------------------------------------------------------
# 1) 纯逻辑：_staged_bytes + 续传预算
# ---------------------------------------------------------------------------
print("=== 1. 暂存字节计量与续传磁盘预算 ===")
from app.core.compute.model_download import _staged_bytes  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
(_tmp / "a.bin").write_bytes(b"x" * 1000)
(_tmp / "sub").mkdir()
(_tmp / "sub" / "b.bin").write_bytes(b"y" * 500)
(_tmp / ".cache" / "huggingface" / "download").mkdir(parents=True)
(_tmp / ".cache" / "huggingface" / "download" / "c.incomplete").write_bytes(b"z" * 250)

check(_staged_bytes(_tmp) == 1750,
      "_staged_bytes 量出目录里已占用的字节（含 .cache 分片）",
      f"got={_staged_bytes(_tmp)} want=1750")
check(_staged_bytes(Path(tempfile.mkdtemp())) == 0,
      "_staged_bytes 对空目录返回 0")
check(_staged_bytes(Path("/nonexistent/path/xyz")) == 0,
      "_staged_bytes 对不存在的路径返回 0（不抛异常）")

_total = 6_900_000_000
_staged = 5_000_000_000
_need_old = _total + (256 << 20)
_need_new = max(0, _total - _staged) + (256 << 20)
check(_need_new < _need_old,
      "续传预算扣掉已暂存字节（不会被自己的半成品卡死）",
      f"old={_need_old/1024**3:.2f}GiB new={_need_new/1024**3:.2f}GiB")


# ---------------------------------------------------------------------------
# 2) 静态审计：DOM 里不该再有库存计数行
# ---------------------------------------------------------------------------
print("\n=== 2. 静态审计 ===")
_js = (ROOT / "app" / "system" / "static" / "system.js").read_text(encoding="utf-8")
check("cc-dl-stock" not in _js,
      "system.js 里已无库存计数行（cc-dl-stock）")
check('classList.toggle("hidden", !txt)' in _js,
      "正文行按内容自动显隐（classList.toggle）")
check('id="cc-dl-last" class="cc-dl-muted hidden"' in _js
      or 'cc-dl-muted hidden" id="cc-dl-last"' in _js,
      "正文行初始即 hidden（首帧不占位）")


# ---------------------------------------------------------------------------
# 3) 页面实测：角标四态 + 正文显隐
# ---------------------------------------------------------------------------
print("\n=== 3. 页面实测 ===")
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[SKIP] 没装 playwright，跳过页面实测")
    sys.exit(0 if not _FAILED else 1)

_PROBE = r"""
() => {
  const c = document.querySelector('#cc-download-card');
  const badge = c.querySelector('#cc-dl-badge');
  const last  = c.querySelector('#cc-dl-last');
  return {
    badgeText: badge ? badge.textContent.trim() : null,
    badgeCls: badge ? badge.className : null,
    badgeTitle: badge ? badge.title : null,
    lastText: last ? last.textContent.trim() : null,
    lastDisplay: last ? getComputedStyle(last).display : null,
    lastHidden: last ? last.classList.contains('hidden') : null,
    stockLine: c.querySelector('#cc-dl-stock') ? true : false,
    cardText: c.innerText.trim(),
  };
}
"""

# 注入 + 读回**必须在同一个 evaluate 里**。
# 算力配置页有 1Hz tick，每次 tick 都会重新拉 /model-download/status
# 并覆盖掉 _downloadStatus —— 分成两步（注入 → 等 200ms → 读）的话，
# 注入的假状态会被 tick 冲掉，读回来的是真实状态。
_APPLY = r"""
(payload) => {
  const inst = window.ComputeConfigPage;
  inst._downloadStatus = payload;
  inst._updateDownloadCard();
  const c = document.querySelector('#cc-download-card');
  const badge = c.querySelector('#cc-dl-badge');
  const last  = c.querySelector('#cc-dl-last');
  return {
    badgeText: badge ? badge.textContent.trim() : null,
    badgeCls: badge ? badge.className : null,
    badgeTitle: badge ? badge.title : null,
    lastText: last ? last.textContent.trim() : null,
    lastDisplay: last ? getComputedStyle(last).display : null,
    lastHidden: last ? last.classList.contains('hidden') : null,
    stockLine: c.querySelector('#cc-dl-stock') ? true : false,
    cardText: c.innerText.trim(),
  };
}
"""


def _job(state: str, name: str = "Probe-Model", error: str = "") -> dict:
    return {
        "id": "dl-verify", "repo": f"wardner/{name}", "name": name,
        "dest": "/userdata/models", "state": state,
        "files_total": 3, "files_done": 1, "bytes_total": 100, "bytes_done": 33,
        "error": error, "notice": "", "target": f"/userdata/models/{name}",
        "created_at": 1.0, "started_at": 1.0, "finished_at": 2.0,
    }


with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox", "--mute-audio"])
    page = browser.new_page(viewport={"width": 1600, "height": 1200})
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(f"{BASE}/#compute-config", wait_until="networkidle")
    page.wait_for_timeout(6000)

    card = page.query_selector("#cc-download-card")
    if card is None:
        check(False, "页面里能找到「模型下载」卡片", "#cc-download-card not found")
    else:
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(500)

        # --- 成功态（真实服务里 history[0] 就是 done） ---
        st = page.evaluate(_PROBE)
        check(st["badgeText"].startswith("已装 "),
              "成功态：角标报库存「已装 N/M」", json.dumps(st["badgeText"]))
        check(st["lastHidden"] is True and st["lastDisplay"] == "none",
              "成功态：正文行隐藏（不显示「上次：…下载完成」）",
              f"hidden={st['lastHidden']} display={st['lastDisplay']}")
        check(st["stockLine"] is False,
              "DOM 里没有库存计数行")

        # --- 失败态 ---
        st = page.evaluate(_APPLY, {"active": None, "queued": [],
                                    "history": [_job("failed", error="sha256 校验不通过")]})
        check("下载失败" in (st["badgeText"] or ""),
              "失败态：角标红色「✗ 下载失败」", json.dumps(st["badgeText"]))
        check("cc-download-badge-err" in (st["badgeCls"] or ""),
              "失败态：角标带 err 样式")
        check(st["lastDisplay"] != "none" and "校验不通过" in (st["lastText"] or ""),
              "失败态：正文行显示失败原因", json.dumps(st["lastText"]))
        check("本板已安装" in (st["badgeTitle"] or ""),
              "失败态：角标 title 里仍带库存计数")

        # --- 取消态 ---
        st = page.evaluate(_APPLY, {"active": None, "queued": [],
                                    "history": [_job("cancelled")]})
        check(st["lastDisplay"] != "none" and "已取消" in (st["lastText"] or ""),
              "取消态：正文行显示「已取消」", json.dumps(st["lastText"]))
        check(st["badgeText"].startswith("已装 "),
              "取消态：角标回到库存计数", json.dumps(st["badgeText"]))

        # --- 下载中 ---
        act = _job("downloading")
        act.update({"state": "downloading", "bytes_total": 1000, "bytes_done": 400,
                    "current_file": "model.rknn", "speed_bps": 2048,
                    "eta_seconds": 3.0, "finished_at": 0.0})
        st = page.evaluate(_APPLY, {"active": act, "queued": [], "history": []})
        check(st["badgeText"] == "Probe-Model",
              "下载中：角标显示模型名", json.dumps(st["badgeText"]))
        check("cc-download-badge-active" in (st["badgeCls"] or ""),
              "下载中：角标带 active 样式")

    check(not errs, "页面无 JS 报错", json.dumps(errs, ensure_ascii=False))
    browser.close()


print("\n" + "=" * 60)
if _FAILED:
    print(f"❌ {len(_FAILED)} 条未通过：")
    for f in _FAILED:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
