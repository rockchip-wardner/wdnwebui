#!/usr/bin/env python3
"""回归防护：微信图片 / 图文 / 最近一张图片检索（不占 NPU）。

覆盖：
1. 图片 caption 提取。
2. ``video_manager.search`` 的图 / 文 / 图+文编码入口与 query_type。
3. 「最近一张」缓存：覆盖删旧图、文件丢失自动清空、启动清空。
4. ``VideoSearch.use_last_image``（有缓存图 / 无缓存图）+ 描述包含
   「刚发送的那张图片」（让模型据此判断追问 vs 独立问题）。
5. ``_process_image``：成功 → 保留图片 + 组织回答（注记含追问提示）；
   质量/无视频 → 回错误并删图；text_chat 未启用 → 回原始命中。
6. ``handle_incoming`` 分流：图片 → image worker；引用消息 / 普通文字 →
   文字队列（微信协议无法区分引用图/文字，不自动绑定图片）。

用法::

    python -m scripts.verify_wechat_image_search
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, ".")

from app.system.im_bridge import llm as L
from app.system.im_bridge import video_manager as VM
from app.system.im_bridge.video_search import VideoSearch

TMP = Path("/tmp/opencode/wechat_image_test")
TMP.mkdir(parents=True, exist_ok=True)

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("[PASS] " if cond else "[FAIL] ") + name
          + (f" — {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    # ---------------- 1. caption ----------------
    msg = SimpleNamespace(
        text="https://cdn/x.jpg 一只猫", images=[SimpleNamespace(url="https://cdn/x.jpg")])
    check("[1] 图片 caption 去掉 URL",
          L._LlmBridge._image_caption(msg) == "一只猫",
          L._LlmBridge._image_caption(msg))
    msg_url_only = SimpleNamespace(
        text="https://cdn/x.jpg", images=[SimpleNamespace(url="https://cdn/x.jpg")])
    check("[2] 仅 URL → 无 caption",
          L._LlmBridge._image_caption(msg_url_only) == "")

    # ---------------- 2. video_manager.search ----------------
    mgr = VM.WechatVideoManager.__new__(VM.WechatVideoManager)
    mgr._lock = threading.RLock()
    mgr._last_images = {}
    mgr._entries = {"u1": SimpleNamespace(
        state="ready", filename="v.mp4", duration_s=10.0,
        index=[{"start_time": 0.0, "end_time": 4.0,
                "vec": np.array([1.0, 0.0], np.float32)}])}

    class FakeSearch:
        def __init__(self):
            self.calls = []

        def encode_query(self, text, image_path="", check_image_quality=True):
            self.calls.append((text, image_path, check_image_quality))
            return np.array([1.0, 0.0], np.float32)

    fake = FakeSearch()
    with patch("app.core.video.search.get_service", return_value=fake):
        r = mgr.search("u1", "火灾", image_path="/tmp/x.jpg")
    check("[3] 图+文 → query_type=image+text",
          r.get("query_type") == "image+text", str(r.get("query_type")))
    check("[4] encode_query 收到图+文与质量检查默认开",
          fake.calls[-1] == ("火灾", "/tmp/x.jpg", True), str(fake.calls[-1]))
    with patch("app.core.video.search.get_service", return_value=fake):
        r = mgr.search("u1", "", image_path="/tmp/x.jpg")
    check("[5] 纯图 → query_type=image", r.get("query_type") == "image")
    with patch("app.core.video.search.get_service", return_value=fake):
        r = mgr.search("u1", "火灾")
    check("[6] 纯文字 → query_type=text", r.get("query_type") == "text")
    try:
        mgr.search("u1", "", image_path="")
        ok = False
    except ValueError:
        ok = True
    check("[7] 图/文都为空 → ValueError", ok)

    # ---------------- 3. 最近一张缓存 ----------------
    p1 = TMP / "a.jpg"
    p1.write_bytes(b"a")
    mgr.set_last_image("u1", str(p1), "cap")
    check("[8] set/get 最近一张", (mgr.get_last_image("u1") or {}).get("path") == str(p1))
    p2 = TMP / "b.jpg"
    p2.write_bytes(b"b")
    mgr.set_last_image("u1", str(p2))
    check("[9] 覆盖时删除旧图", not p1.exists())
    p2.unlink()
    check("[10] 文件丢失 → get_last_image 返回 None", mgr.get_last_image("u1") is None)

    root = TMP / "imgroot"
    root.mkdir(parents=True, exist_ok=True)
    (root / "old.jpg").write_bytes(b"x")
    with patch.object(VM, "_IMAGE_ROOT", root):
        VM.clear_image_cache()
    check("[11] clear_image_cache 清空目录且重建",
          root.is_dir() and not list(root.iterdir()))

    # ---------------- 4. VideoSearch.use_last_image ----------------
    class FakeToolMgr:
        def __init__(self, last=None):
            self.last = last
            self.calls = []

        def entry_status(self, uid):
            return {"state": "ready", "filename": "v.mp4"}

        def get_last_image(self, uid):
            return self.last

        def search(self, uid, text, top_k=5, image_path=""):
            self.calls.append((text, image_path))
            return {"status": "ok", "video": "v.mp4", "hits": [],
                    "all_below_threshold": False, "query_type": "image"}

    with patch("app.system.im_bridge.video_manager.get_wechat_video_manager",
               return_value=FakeToolMgr(last={"path": "/tmp/x.jpg"})):
        out = VideoSearch(query="图片里有什么", use_last_image=True)(
            None, SimpleNamespace(user_id="u1"))
    check("[12] use_last_image 有图 → 传 image_path",
          out.get("use_last_image") is True, str(out))
    with patch("app.system.im_bridge.video_manager.get_wechat_video_manager",
               return_value=FakeToolMgr(last=None)):
        out = VideoSearch(query="图片里有什么", use_last_image=True)(
            None, SimpleNamespace(user_id="u1"))
    check("[13] use_last_image 无图 → no_image", out.get("status") == "no_image",
          str(out))
    desc = VideoSearch.model_fields["use_last_image"].description or ""
    check("[14] use_last_image 描述包含「刚发送的那张图片」",
          "刚发送的那张图片" in desc, desc[:80])

    # ---------------- 5. _process_image ----------------
    class FakeWc:
        def download_message(self, msg):
            return SimpleNamespace(data=b"img")

    class FakeMgr2:
        def __init__(self):
            self.image_dir = TMP / "mgr2"
            self.image_dir.mkdir(parents=True, exist_ok=True)
            self.last = None
            self.err = None
            self.calls = []

        def search(self, uid, text, image_path="", **kw):
            self.calls.append((uid, text, image_path))
            if self.err:
                raise self.err
            return {"status": "ok", "video": "v.mp4",
                    "hits": [{"time": "00:00-00:04", "similarity": 0.8}],
                    "all_below_threshold": False,
                    "query_type": "image" if not text else "image+text"}

        def set_last_image(self, uid, path, caption=""):
            self.last = (uid, path, caption)

    def make_bridge(available=True):
        b = L._LlmBridge.__new__(L._LlmBridge)
        b._lock = threading.RLock()
        b._busy = False
        b._active_sessions = 0
        b.replies = []
        b.processed = []
        b.available = lambda: available
        b._reply_no_context = lambda uid, text: b.replies.append(text)
        b._process = lambda uid, text, system_note="", tool_names=None: \
            b.processed.append((uid, text, system_note, tool_names))
        return b

    fake_wc, fake_mgr2 = FakeWc(), FakeMgr2()
    bridge = make_bridge()
    with patch("app.system.im_bridge.service_wechat.get_wechat", return_value=fake_wc), \
            patch("app.system.im_bridge.video_manager.get_wechat_video_manager",
                  return_value=fake_mgr2):
        bridge._process_image("u1", SimpleNamespace(), "")
    check("[15] 图片检索成功 → 保留最近一张",
          fake_mgr2.last is not None, str(fake_mgr2.last))
    check("[16] 成功 → 交给 LLM 组织回答且 tool_names=[]",
          bool(bridge.processed) and bridge.processed[0][3] == []
          and "命中片段" in bridge.processed[0][2], str(bridge.processed[:1]))
    check("[17] 注记包含 use_last_image 追问提示",
          bool(bridge.processed)
          and "use_last_image" in bridge.processed[0][2], str(bridge.processed[:1])[:160])
    check("[18] 成功时文件保留",
          bool(fake_mgr2.last and os.path.isfile(fake_mgr2.last[1])))

    # 质量不合格 → 回错误、删临时图、不调 LLM
    import shutil
    shutil.rmtree(fake_mgr2.image_dir, ignore_errors=True)
    fake_mgr2.image_dir.mkdir(parents=True, exist_ok=True)
    before = {str(p) for p in fake_mgr2.image_dir.rglob("*") if p.is_file()}
    fake_mgr2.err = ValueError("图片几乎是纯色（无法检索），请换一张")
    bridge = make_bridge()
    with patch("app.system.im_bridge.service_wechat.get_wechat", return_value=fake_wc), \
            patch("app.system.im_bridge.video_manager.get_wechat_video_manager",
                  return_value=fake_mgr2):
        bridge._process_image("u1", SimpleNamespace(), "")
    after = {str(p) for p in fake_mgr2.image_dir.rglob("*") if p.is_file()}
    check("[19] 质量不合格 → 直接回错误文案",
          any("纯色" in r for r in bridge.replies), str(bridge.replies))
    check("[20] 质量不合格 → 删除临时图 + 不调 LLM",
          before == after and not bridge.processed, f"{before} -> {after}")
    fake_mgr2.err = None

    # 无视频
    fake_mgr2.err = ValueError("该用户没有已就绪的视频索引")
    bridge = make_bridge()
    with patch("app.system.im_bridge.service_wechat.get_wechat", return_value=fake_wc), \
            patch("app.system.im_bridge.video_manager.get_wechat_video_manager",
                  return_value=fake_mgr2):
        bridge._process_image("u1", SimpleNamespace(), "")
    check("[21] 无视频 → 回提示", any("没有已就绪" in r for r in bridge.replies),
          str(bridge.replies))
    fake_mgr2.err = None

    # text_chat 未启用 → 回原始命中
    bridge = make_bridge(available=False)
    with patch("app.system.im_bridge.service_wechat.get_wechat", return_value=fake_wc), \
            patch("app.system.im_bridge.video_manager.get_wechat_video_manager",
                  return_value=fake_mgr2):
        bridge._process_image("u1", SimpleNamespace(), "")
    check("[22] text_chat 未启用 → 回原始命中",
          any("文本对话模型未启用" in r and "00:00-00:04" in r
              for r in bridge.replies), str(bridge.replies))
    check("[23] text_chat 未启用 → 不调 LLM", not bridge.processed)

    # ---------------- 6. handle_incoming 分流 ----------------
    import queue as _q

    def make_router():
        b = L._LlmBridge.__new__(L._LlmBridge)
        b._lock = threading.RLock()
        b._enabled = True
        b._queue = _q.Queue()
        b._image_queue = _q.Queue()
        b._wake = threading.Event()
        b.replies = []
        b._reply_no_context = lambda uid, text: b.replies.append(text)
        return b

    b = make_router()
    b.handle_incoming(SimpleNamespace(
        type="image", text="", images=[], raw={}, user_id="u2"))
    job = b._image_queue.get_nowait()
    check("[24] 图片消息 → image worker", job[0] == "u2" and job[2] == "",
          str(job[:1] + job[2:]))

    b = make_router()
    ref_raw = {"item_list": [{"ref_msg": {"message_item": {
        "msg_id": "1", "type": 0}}}]}
    b.handle_incoming(SimpleNamespace(
        type="text", text="引用文字提问", images=[], raw=ref_raw, user_id="u2"))
    check("[25] 引用消息 → 文字队列（不自动绑定图片）",
          b._queue.get_nowait()[0] == "u2" and b._image_queue.empty(),
          str(b.replies))

    b = make_router()
    b.handle_incoming(SimpleNamespace(
        type="text", text="普通提问", images=[], raw={}, user_id="u2"))
    check("[26] 普通文字 → 文字队列",
          b._queue.get_nowait()[0] == "u2" and b._image_queue.empty())

    print()
    print(f"{len(fails)} 项失败")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
