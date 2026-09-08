#!/usr/bin/env python3
"""verify_target_doc_shm_path.py — regression test for the
"malformed reply" 视频目标检测 truncation bug.

Bug: worker stuffed the entire reply JSON dict (including a long
Chinese desc in ``structured`` + duplicate ``result_data``) into
ComputeReply IDL's single ``text`` string field, which has the
FastCDR default 255-byte per-string cap. When desc > ~30 chars the
serialized JSON exceeded the cap and FastCDR silently truncated it,
so the main process's JSON parse failed with
``malformed reply: '{"ok":true,...,"desc":"一辆停'`` (cut mid-character).

Fix: worker now routes heavy payloads (crop JPEG, large result_data)
through /dev/shm path-list, keeping the wire reply inside the IDL
field cap. Main process reads the file + inlines the bytes before
returning to detector.

This test:
  1. Spins up a Hub with a single service that returns a
     worker-shaped reply with a deliberately long Chinese desc.
  2. Calls the service via ``Requester.call`` (same wire path the
     proxy uses in production).
  3. Runs ``RpcSessionProxy._decode_reply_shm`` to reconstruct the
     inline ``crop_b64`` / ``result_data`` from path refs.
  4. Reconstructs TargetDoc via ``_get_target_doc_cls().model_validate``
     to confirm the proxy's data path is consumable by the detector.

No NPU / no wdnai LLMSession needed — this only exercises the wire
+ shm contract.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from typing import Any

LOG = logging.getLogger("verify.target_doc_shm")

# Ensure repo root is on sys.path so we can import app.services.* for the
# proxy / detector code under test (script may be invoked from anywhere).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wdndds.dds._rpc import Hub


def _build_target_doc_reply(desc: str, *, with_crop_b64: bool = True) -> dict:
    """Mimic the new worker reply shape (after the v0.12 fix)."""
    reply = {
        "text": None,
        "level": None,
        "tool_call": None,
        "bbox": None,
    }
    result_data = {
        "x1": 75.0,
        "y1": 410.0,
        "x2": 330.0,
        "y2": 500.0,
        "desc": desc,
    }
    reply["result_data"] = result_data
    if with_crop_b64:
        import base64 as _b64
        synthetic_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
        reply["crop_b64"] = _b64.b64encode(synthetic_jpeg).decode("ascii")
    return reply


def _write_reply_payload_to_shm(payload: bytes, suffix: str) -> str:
    name = f"wdnwebui_cr_{uuid.uuid4().hex}.{suffix}"
    path = os.path.join("/dev/shm", name)
    with open(path, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    return path


def _write_result_data_to_shm(result_data: dict) -> str:
    encoded = json.dumps(result_data, ensure_ascii=False).encode("utf-8")
    return _write_reply_payload_to_shm(encoded, "json")


def _build_target_doc_reply_via_shm(desc: str) -> dict:
    """Mimic what the fixed worker does: heavy crop + large result_data
    are routed through /dev/shm, only ~50-byte path strings go over wire."""
    reply = {
        "text": None,
        "level": None,
        "tool_call": None,
        "bbox": None,
    }
    synthetic_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 50_000 + b"\xff\xd9"
    crop_path = _write_reply_payload_to_shm(synthetic_jpeg, "jpg")
    reply["crop_path"] = crop_path
    result_data = {
        "x1": 75.0, "y1": 410.0, "x2": 330.0, "y2": 500.0,
        "desc": desc,
    }
    rd_path = _write_result_data_to_shm(result_data)
    reply["result_data_path"] = rd_path
    return reply


_state_lock = threading.Lock()


def _run_test(test_name: str, reply_factory, post_process=None) -> bool:
    """Set up Hub + service, swap in the reply factory as the callback,
    run one round-trip RPC, run post_process on the received data, tear
    everything down."""
    svc_name = f"wdndds/rpc/test-{uuid.uuid4().hex[:6]}"
    server_name = f"verify-server-{test_name}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
    state = {"next_reply": None}

    def router(_service_name: str, payload: Any) -> dict:
        # Mirrors compute_worker.infer_handler: returns the worker-shaped
        # dict directly (the cshim wraps it in {"ok":true,"data":<dict>}).
        with _state_lock:
            nxt = state["next_reply"]
        if nxt is None:
            return {"text": "no reply set"}
        return nxt

    hub = Hub(
        participant_name=server_name,
        domain_id=0,
        services=[svc_name],
        callback=router,
        discovery_servers=[],
    )
    try:
        hub.start()
    except Exception as e:  # noqa: BLE001
        LOG.error("hub.start() failed for %s: %s", test_name, e)
        return False

    caller_hub = None
    try:
        # Wait for discovery to find the server hub. Same dance as
        # verify_simple_discovery.py's run_client().
        found_server = threading.Event()

        def _on_peer_event(guid: str, name: str, ip: str, discovered: bool):
            if not discovered:
                return
            LOG.info("[%s] discovered peer name=%s guid=%s",
                     test_name, name, guid[:24])
            if name == server_name:
                found_server.set()

        caller_hub = Hub(
            participant_name=f"verify-caller-{test_name}-{os.getpid()}-{uuid.uuid4().hex[:4]}",
            domain_id=0,
            discovery_servers=[],
        )
        caller_hub.start()
        caller_hub.set_peer_listener(_on_peer_event)

        discovery_deadline = time.time() + 20.0
        while time.time() < discovery_deadline and not found_server.is_set():
            time.sleep(0.2)
        if not found_server.is_set():
            LOG.error("[%s] server peer not discovered within 20s", test_name)
            return False
        LOG.info("[%s] server discovered, sleeping 2s for SEDP endpoints",
                 test_name)
        time.sleep(2.0)

        next_reply = reply_factory()
        with _state_lock:
            state["next_reply"] = next_reply

        remote_svc = caller_hub.create_service(svc_name)
        req = remote_svc.create_requester(timeout_ms=10_000)
        result = req.call({"action": "info"})
        if not result.get("ok"):
            LOG.error("[%s] RPC failed: %r", test_name, result)
            return False
        data = result.get("data") or {}
        LOG.info("[%s] round-trip data keys: %s",
                 test_name, sorted(data.keys()))

        if post_process is not None:
            return post_process(data, next_reply)
        return True
    finally:
        if caller_hub is not None:
            try:
                caller_hub.close()
            except Exception:
                pass
        hub.close()


def test_short_reply_round_trip() -> bool:
    """Baseline: short reply dict passes through unchanged."""
    def factory():
        return {
            "text": "hello world",
            "level": "info",
            "tool_call": None,
            "bbox": None,
        }
    def check(data, sent):
        assert data == sent, f"reply mismatch:\n  got:  {data}\n  sent: {sent}"
        LOG.info("✓ test_short_reply_round_trip: data round-tripped cleanly")
        return True
    return _run_test("short", factory, check)


def test_long_desc_reply_via_shm() -> bool:
    """Reproduce the original bug pattern: long Chinese desc in
    TargetDoc reply. Pre-fix this would have hit the 255-byte IDL cap
    and truncated mid-character. Post-fix result_data_path keeps it
    under the cap and the proxy reconstructs the dict via shm read."""
    long_desc = (
        "一辆停在小路旁的白色轿车，前灯亮着，车窗反射出对面店铺的霓虹招牌。"
        "旁边站着一位身穿深色外套的中年男子，正在用手机通话，"
        "表情略显焦急。背景是一排商业店铺，招牌灯光明亮，"
        "可以看到中文和英文的店名，路上偶尔有行人走过。"
        "整体画面色调偏冷，呈现出夜晚的城市氛围。"
    )
    assert len(long_desc) > 80

    def factory():
        reply = _build_target_doc_reply_via_shm(long_desc)
        wire_size = len(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
        LOG.info("test_long_desc_reply_via_shm: wire reply size = %d bytes "
                 "(must be < 255 for IDL field cap)", wire_size)
        assert wire_size < 250, (
            f"reply too big after shm routing: {wire_size} bytes — "
            f"defeats the fix"
        )
        return reply

    def check(data, sent):
        # Reply arrived at the client with path refs still inline
        # (this is the cshim RPC level — the proxy's _decode_reply_shm
        # is what turns them back into crop_b64 / result_data).
        assert data.get("result_data_path"), "result_data_path missing in reply"
        assert data.get("crop_path"), "crop_path missing in reply"
        assert "result_data" not in data, (
            "raw RPC reply should NOT have inline result_data — "
            "the worker routes it via /dev/shm"
        )

        # Now run the proxy-side _decode_reply_shm to verify the
        # reconstruction path works end-to-end.
        from app.services.rpc_session_proxy import _RpcSessionProxy

        class _MiniProxy(_RpcSessionProxy):
            def __init__(self):
                pass

        mp = _MiniProxy()
        mp._model_id = "test-stub"
        decoded = mp._decode_reply_shm(dict(data))  # copy

        assert decoded.get("result_data"), (
            f"result_data missing after _decode_reply_shm: "
            f"keys={sorted(decoded.keys())}"
        )
        assert decoded["result_data"]["desc"] == long_desc, (
            f"desc mismatch after decode: {decoded['result_data']['desc']!r}"
        )
        assert decoded.get("crop_b64"), "crop_b64 missing after decode"
        assert not os.path.exists(data["crop_path"]), (
            f"crop_path {data['crop_path']} not cleaned up"
        )
        assert not os.path.exists(data["result_data_path"]), (
            f"result_data_path {data['result_data_path']} not cleaned up"
        )

        # Confirm the proxy's data path is consumable by the detector's
        # TargetDoc reconstruction (the exact branch used by the
        # 视频目标检测 page).
        from app.services.detector import _get_target_doc_cls
        TargetDoc = _get_target_doc_cls()
        target = TargetDoc.model_validate(decoded["result_data"])
        assert target.desc == long_desc
        assert target.x1 == 75.0 and target.y2 == 500.0
        LOG.info("✓ test_long_desc_reply_via_shm: TargetDoc(desc=%d chars) "
                 "reconstructed from shm round-trip", len(target.desc))
        return True

    return _run_test("long_shm", factory, check)


def test_inline_short_reply_unchanged() -> bool:
    """Small TargetDoc reply (desc < 30 chars) should NOT route through
    shm — worker keeps it inline. Confirms the cheap path isn't broken."""
    def factory():
        return _build_target_doc_reply("白色轿车", with_crop_b64=False)
    def check(data, sent):
        assert "result_data" in data, "short reply should keep inline result_data"
        assert data["result_data"]["desc"] == "白色轿车"
        from app.services.rpc_session_proxy import _RpcSessionProxy
        class _MiniProxy(_RpcSessionProxy):
            def __init__(self):
                pass
        mp = _MiniProxy()
        mp._model_id = "test-stub"
        decoded = mp._decode_reply_shm(dict(data))
        assert decoded == data, (
            "_decode_reply_shm must be a no-op for inline replies "
            f"(got {decoded} vs {data})"
        )
        LOG.info("✓ test_inline_short_reply_unchanged: no shm routing for short desc")
        return True
    return _run_test("inline_short", factory, check)


def test_old_style_oversized_reply_would_have_truncated() -> bool:
    """Reproduce the OLD buggy reply shape (inline structured + result_data
    with a long Chinese desc) and verify it would have exceeded the IDL
    text field cap. This documents WHY the fix was necessary."""
    long_desc = (
        "一辆停在小路旁的白色轿车，前灯亮着，车窗反射出对面店铺的霓虹招牌。"
        "旁边站着一位身穿深色外套的中年男子，正在用手机通话。"
    )

    def factory():
        reply = {
            "text": None,
            "level": None,
            "tool_call": None,
            "bbox": None,
            "structured": {
                "x1": 75.0, "y1": 410.0, "x2": 330.0, "y2": 500.0,
                "desc": long_desc,
            },
            "result_data": {
                "x1": 75.0, "y1": 410.0, "x2": 330.0, "y2": 500.0,
                "desc": long_desc,
            },
            "crop_b64": "a" * 80,
        }
        wire_size = len(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
        LOG.info("test_old_style_oversized_reply_would_have_truncated: "
                 "old-shape wire reply size = %d bytes (>> 255 byte cap)",
                 wire_size)
        assert wire_size > 255, (
            "test setup is wrong — the old-shape reply should exceed "
            "the IDL field cap to demonstrate the bug"
        )
        return reply

    def check(data, sent):
        wire_size = len(json.dumps(sent, ensure_ascii=False).encode("utf-8"))
        LOG.info("test_old_style_oversized_reply_would_have_truncated: "
                 "confirmed old-style reply would have hit FastCDR's "
                 "255-byte single-string cap (%d bytes > 255)", wire_size)
        return True

    return _run_test("old_style", factory, check)


def test_scene_frame_structured_via_shm() -> bool:
    """SceneFrame (used by VFA) has no ``result_data`` — its structured
    payload IS the path VFA reads. Same fix pattern: route the JSON dict
    through /dev/shm when it exceeds 100 bytes (SceneFrame fields are
    typically 30-100 chars each, total 200-400 bytes)."""
    def factory():
        structured = {
            "scene": "城市街道的夜景，路边停着几辆车，远处有高楼大厦的灯光。",
            "actors": ["一位身穿深色外套的中年男子", "一位穿白色T恤的年轻人"],
            "action": "中年男子正在用手机通话，表情略显焦急。",
            "objects": ["黑色轿车", "白色出租车", "红色霓虹招牌"],
            "note": "画面色调偏冷，呈现出夜晚的城市氛围。",
        }
        reply = {
            "text": None,
            "level": None,
            "tool_call": None,
            "bbox": None,
        }
        sd_path = _write_result_data_to_shm(structured)
        reply["structured_path"] = sd_path
        return reply

    def check(data, sent):
        assert "structured_path" in data, (
            "structured_path missing in reply (SceneFrame fix not applied?)"
        )
        assert "structured" not in data, (
            "raw RPC reply should NOT have inline structured — "
            "worker routes it via /dev/shm for SceneFrame too"
        )
        from app.services.rpc_session_proxy import _RpcSessionProxy
        class _MiniProxy(_RpcSessionProxy):
            def __init__(self):
                pass
        mp = _MiniProxy()
        mp._model_id = "test-stub"
        decoded = mp._decode_reply_shm(dict(data))
        assert decoded.get("structured"), (
            f"structured missing after _decode_reply_shm: "
            f"keys={sorted(decoded.keys())}"
        )
        s = decoded["structured"]
        assert s["scene"].startswith("城市街道"), f"scene mismatch: {s['scene']!r}"
        assert "黑色轿车" in s["objects"]
        assert not os.path.exists(data["structured_path"]), (
            f"structured_path {data['structured_path']} not cleaned up"
        )
        LOG.info("✓ test_scene_frame_structured_via_shm: SceneFrame.structured "
                 "reconstructed from shm round-trip")
        return True

    return _run_test("scene_frame_shm", factory, check)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("wdndds").setLevel(logging.WARNING)
    logging.getLogger("verify").setLevel(logging.INFO)

    LOG.info("=== verify_target_doc_shm_path ===")
    LOG.info("Regression test for the 视频目标检测 'malformed reply' bug")
    LOG.info("(" + os.path.realpath(__file__) + ")")
    LOG.info("")

    results = []
    for fn in (
        test_short_reply_round_trip,
        test_inline_short_reply_unchanged,
        test_long_desc_reply_via_shm,
        test_scene_frame_structured_via_shm,
        test_old_style_oversized_reply_would_have_truncated,
    ):
        try:
            ok = fn()
            results.append((fn.__name__, "PASS" if ok else "FAIL"))
        except Exception as e:  # noqa: BLE001
            LOG.exception("test %s raised", fn.__name__)
            results.append((fn.__name__, f"FAIL ({type(e).__name__}: {e})"))

    LOG.info("")
    LOG.info("=== summary ===")
    for name, status in results:
        LOG.info("  %s: %s", name, status)
    failed = [n for n, s in results if not s.startswith("PASS")]
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
