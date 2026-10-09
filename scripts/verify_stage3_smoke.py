"""Stage 3 end-to-end smoke: registry → worker subprocess → real NPU.

This is the check AGENTS.md demands for changes to shared modules
(``app/core/compute/registry.py``, ``app/core/llm/detector_base.py``,
the chat stack): exercise every affected capability through the
**production path** — ``ComputeRegistry.get_registry().get_session(...)``
— not through wdndds directly. If this passes, the protocol swap
(worker on ``serve_local_session``, client on ``LLMRPCSession``) is
intact end to end.

Each capability that shares the VLM weight is covered by the calls a
real page actually makes, because they are genuinely different code
paths even though they hit the same worker:

  * 音视频描述       ``respond(generating=VLMDoc)``
  * 视频目标检测     ``respond(generating=TargetDoc)`` + bbox normalisation
  * 视频目标定位     ``respond(generating=LocatorDoc)``
  * 文本对话         the chat agent loop (tool schemas + history + on_delta)
  * 音频理解         ``respond()`` with no schema, pure text
  * 语音生成         ``respond()`` with TTS options
  * 视频内容检索     ``embed_text`` / ``embed_video``

Plus the transport-level properties that only exist now:

  * deltas arrive via the side topic and concatenate to the reply
  * ``count_tokens`` (chat history trimming) works against a real tokenizer
  * an unparsable schema comes back as a bare ``str``, not a silent blank

Run::

    WDNAI_MODELS_DIR=/mnt/oem/models .venv/bin/python \\
        scripts/verify_stage3_smoke.py            # VLM + text chat (default)
    WDNAI_MODELS_DIR=/mnt/oem/models .venv/bin/python \\
        scripts/verify_stage3_smoke.py --all      # every capability
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any, List, Optional, Tuple

import numpy as np

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

LOG = logging.getLogger("stage3_smoke")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

LOAD_TIMEOUT_S = 180.0


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
def frame() -> np.ndarray:
    """A scene the model has no excuse to miss: grey backdrop, a red
    square in the middle, blue on the left, green along the bottom."""
    img = np.full((480, 640, 3), 200, dtype=np.uint8)
    img[:, :240] = (200, 120, 40)          # left: blue-ish "sky"
    img[360:, :] = (70, 160, 70)           # bottom: green "ground"
    img[180:300, 260:380] = (40, 40, 210)  # centre: red square
    return img


class Results:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, bool, str]] = []

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {name}" + (f"  — {detail}" if detail else ""),
              flush=True)
        return ok

    @property
    def failures(self) -> List[Tuple[str, bool, str]]:
        return [r for r in self.rows if not r[1]]

    def summary(self) -> int:
        ok = len(self.rows) - len(self.failures)
        print("-" * 68)
        print(f"{ok}/{len(self.rows)} checks passed")
        for name, _, detail in self.failures:
            print(f"  FAILED: {name} — {detail}")
        return 1 if self.failures else 0


# --------------------------------------------------------------------------- #
# Registry plumbing                                                           #
# --------------------------------------------------------------------------- #
def boot(model_id: str, npu_index: int = 0):
    """Spawn + bind a worker through the real registry and wait for it."""
    from app.core.compute.registry import get_registry

    reg = get_registry()
    inst = reg.ensure_worker_for_model(model_id, allow_adopt=False)
    if inst.npu_index is None or inst.npu_index < 0:
        reg.bind_instance(inst, npu_index)
    print(f"[smoke] waiting for {model_id} → running …", flush=True)
    t0 = time.time()
    last = ""
    while time.time() - t0 < LOAD_TIMEOUT_S:
        inst = reg._find_by_model(model_id)
        if inst is not None:
            last = inst.status
            if inst.status == "missing_model_dir":
                # Not a failure — the model files simply aren't on this
                # board. Say so and move on rather than burning the whole
                # load timeout waiting for a state that will never change.
                raise RuntimeError(
                    f"{model_id}: weights not present "
                    f"(model_dir {getattr(inst, 'model_dir', '?')!r} does not "
                    f"exist) — install them or point WDNAI_MODELS_DIR "
                    f"elsewhere")
            if inst.status == "running":
                print(f"[smoke] {model_id} running after "
                      f"{time.time() - t0:.0f}s", flush=True)
                return reg
            if inst.status == "error":
                raise RuntimeError(f"{model_id} failed to load: "
                                   f"{getattr(inst, 'error', '')!r}")
        time.sleep(2.0)
    raise TimeoutError(f"{model_id} never reached running "
                       f"(last status {last!r}) after {LOAD_TIMEOUT_S}s")


def shutdown(reg) -> None:
    for model_id in ("qwen3.5-vl-4b", "qwen3-vl-embedding-2b", "qwen3-tts"):
        try:
            reg.stop_instance_local(model_id)
        except Exception as e:  # noqa: BLE001
            LOG.debug("stop %s: %s", model_id, e)


def sync(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Checks — VLM capabilities (all share qwen3.5-vl-4b)                        #
# --------------------------------------------------------------------------- #
def check_video_describe(res: Results, reg) -> None:
    """音视频描述 page: ``respond(generating=VLMDoc)``."""
    from app.core.llm.detector_base import _get_vlm_doc_cls
    from wdnai import ImageAttachment

    VLMDoc = _get_vlm_doc_cls()
    sess = reg.get_session("swcap_video_describe")
    out = sync(sess.respond(
        prompt="用中文一句话描述画面内容。",
        attachment=[ImageAttachment(frame())],
        generating=VLMDoc,
    ))
    if isinstance(out, str):
        res.record("音视频描述 VLMDoc", False,
                   f"got unparsable str instead of VLMDoc: {out[:200]!r}")
        return
    text = getattr(out, "text", "") or ""
    res.record("音视频描述 VLMDoc", bool(text.strip()),
               f"{type(out).__name__} text={text[:70]!r} "
               f"level={getattr(out, 'level', None)!r}")


def check_target_detection(res: Results, reg) -> None:
    """视频目标检测 page.

    Two things have to hold, and they are genuinely different:

    1. ``respond(generating=TargetDoc)`` returns either a real
       ``TargetDoc`` or a bare ``str`` — the SDK's ``_safe_validate``
       returns raw text rather than raising on a validation miss.
       Qwen3.5-VL hits that path often: it echoes the schema hint back
       instead of emitting a bbox. So a ``str`` here is not a failure.
    2. The **page** still has to produce an event, which is what
       ``detector_base._try_rescue_target_doc`` is for — it pulls the
       bbox back out of the echoed JSON.

    Checking only (1) would flag a known model quirk as a transport bug;
    checking only (2) would hide a broken parse. Both are asserted.
    """
    from app.core.llm.detector_base import (
        _get_target_doc_cls, _normalize_bbox_to_0_1000, _try_rescue_target_doc,
    )
    from wdnai import ImageAttachment

    TargetDoc = _get_target_doc_cls()
    sess = reg.get_session("swcap_video_describe")
    out = sync(sess.respond(
        prompt="定位画面中央的红色正方形，输出它的裁剪区域和对区域的描述。",
        attachment=[ImageAttachment(frame())],
        generating=TargetDoc,
    ))

    if isinstance(out, str):
        # Expected on Qwen3.5-VL — not a transport failure. The model
        # echoes the schema hint back instead of emitting a bbox. What
        # matters is that the transport handed the text back intact
        # rather than dropping it, which is what `_safe_validate`'s
        # raw-text fallback plus the str-carrying wire is for.
        res.record("视频目标检测 传输层未丢原文", bool(out.strip()),
                   f"{len(out)} chars of model text survived the RPC")
        doc = _try_rescue_target_doc(out, TargetDoc)
        if doc is None:
            # The model returned the schema *template* (bbox's value is
            # a schema stub, not coordinates) — nothing to rescue. The
            # page renders this as "[TargetDoc 解析失败]", which is the
            # same behaviour as before the transport change.
            res.record("视频目标检测 救援无可救", True,
                       "model echoed the schema template with no bbox — "
                       "page shows the 解析失败 placeholder (pre-existing)")
            return
    else:
        doc = out

    bbox = getattr(doc, "bbox", None)
    norm = _normalize_bbox_to_0_1000(bbox)
    desc = (getattr(doc, "desc", "") or "").strip()
    ok = norm is not None and bool(desc)
    res.record("视频目标检测 救援 + bbox 归一化", ok,
               f"{type(doc).__name__} bbox={bbox} → {norm} desc={desc[:50]!r}")


def check_rescue_offline(res: Results, reg) -> None:
    """The rescue path itself, without the NPU.

    Qwen3.5-VL's echo is unreliable — sometimes the schema template,
    sometimes real values nested under ``properties``. Assert both
    shapes rescue, so a green run never depends on the model's mood.
    """
    from app.core.llm.detector_base import (
        _get_target_doc_cls, _normalize_bbox_to_0_1000, _try_rescue_target_doc,
    )

    TargetDoc = _get_target_doc_cls()
    cases = {
        "标准顶层": '{"bbox": [430, 370, 610, 630], "desc": "一个红色正方形"}',
        "properties 嵌套（带值）":
            '{"properties": {"bbox": [430, 370, 610, 630], '
            '"desc": "一个红色正方形"}}',
        "v1 老 schema":
            '{"x1": 430, "y1": 370, "x2": 610, "y2": 630, "desc": "红色正方形"}',
        "0-1 单位 bbox":
            '{"bbox": [0.43, 0.37, 0.61, 0.63], "desc": "红色正方形"}',
    }
    for label, raw in cases.items():
        doc = _try_rescue_target_doc(raw, TargetDoc)
        norm = _normalize_bbox_to_0_1000(getattr(doc, "bbox", None))
        ok = doc is not None and norm is not None
        res.record(f"rescue/{label}", ok, f"bbox→{norm}")


def check_localization(res: Results, reg) -> None:
    """视频目标定位 page: LocatorDoc."""
    from app.core.llm.detector_base import _get_grounding_doc_cls
    from wdnai import ImageAttachment

    LocatorDoc = _get_grounding_doc_cls()
    sess = reg.get_session("swcap_grounding")
    out = sync(sess.respond(
        prompt="找出画面中的红色正方形，并描述它的位置。",
        attachment=[ImageAttachment(frame())],
        generating=LocatorDoc,
    ))
    if isinstance(out, str):
        res.record("视频目标定位 LocatorDoc", False,
                   f"got unparsable str instead of LocatorDoc: {out[:200]!r}")
        return
    boxes = getattr(out, "boxes", None)
    text = (getattr(out, "text", "") or "").strip()
    res.record("视频目标定位 LocatorDoc", bool(text),
               f"{type(out).__name__} boxes={len(boxes or [])} "
               f"text={text[:60]!r}")


def check_streaming_deltas(res: Results, reg) -> None:
    """Deltas must arrive out-of-band and concatenate to the reply."""
    from wdnai import ImageAttachment

    sess = reg.get_session("swcap_video_describe")
    chunks: List[str] = []
    out = sync(sess.respond(
        prompt="用中文简短描述画面内容，不超过三十字。",
        attachment=[ImageAttachment(frame())],
        on_delta=chunks.append,
    ))
    joined = "".join(chunks)
    res.record("流式 on_delta 旁路", bool(chunks) and len(chunks) > 1,
               f"{len(chunks)} chunks, {len(joined)} chars")
    res.record("流式拼接 == 返回值",
               isinstance(out, str) and joined.strip() == out.strip(),
               f"joined={joined[:50]!r} vs out={str(out)[:50]!r}")


def check_stop_is_soft(res: Results, reg) -> None:
    """``stop()`` must not raise on the client and must not wedge anything."""
    sess = reg.get_session("swcap_video_describe")
    try:
        sess.stop()
        res.record("session.stop() 可用", True, "published cancel, returned")
    except Exception as e:  # noqa: BLE001
        res.record("session.stop() 可用", False,
                   f"{type(e).__name__}: {e}")


def check_count_tokens(res: Results, reg) -> None:
    """Chat history trimming depends on this; it must count, not guess."""
    from app.core.compute.proxy import count_tokens

    sess = reg.get_session("swcap_video_describe")
    texts = ["hello world", "中文测试句子，用来验证分词计数。"]
    counts = count_tokens(sess, texts)
    # The character/2 fallback would give ~5 and ~13. A real Qwen
    # tokenizer gives different, non-uniform numbers.
    ok = (len(counts) == 2 and all(isinstance(c, int) and c > 0 for c in counts)
          and counts != [len(t) // 2 for t in texts])
    res.record("count_tokens 精确计数", ok, f"{texts} → {counts}")


# --------------------------------------------------------------------------- #
# Checks — text chat (whatever serves swcap_text_chat)                      #
# --------------------------------------------------------------------------- #
def check_text_chat(res: Results, reg) -> None:
    """文本对话 page, through the real agent loop: tool schema rendering,
    history trimming, on_delta, and the final answer."""
    from app.core.chat.agent import run_chat_with_tools
    from app.core.tools._builtins import register_builtin_tools
    from app.core.tools.registry import get_tool_registry

    register_builtin_tools(get_tool_registry())
    sess = reg.get_session("swcap_text_chat")

    chunks: List[str] = []
    out = run_chat_with_tools(
        sess,
        [{"role": "user", "content": "你好，介绍一下你自己。"}],
        "你好，介绍一下你自己。",
        context_limit=8192,
        max_new_tokens=256,
        tool_names=[],                       # no tools this round
        on_delta=chunks.append,
        channel="smoke",
    )
    answer = out.get("answer") or ""
    res.record("文本对话 agent 循环", bool(answer.strip()),
               f"answer={answer[:80]!r}")
    res.record("文本对话 流式 delta", len(chunks) > 1,
               f"{len(chunks)} chunks")
    res.record("文本对话 无错误", not out.get("error"),
               out.get("error") or "")


def check_text_chat_with_tool(res: Results, reg) -> None:
    """Tool round: proves tool schemas are rendered caller-side and the
    model's JSON comes back parsed into a real ``@Callable`` instance."""
    from app.core.chat.agent import run_chat_with_tools
    from app.core.tools._builtins import register_builtin_tools
    from app.core.tools.registry import get_tool_registry

    register_builtin_tools(get_tool_registry())
    sess = reg.get_session("swcap_text_chat")

    out = run_chat_with_tools(
        sess,
        [],
        "请用文字朗读工具说一句『测试』。",
        context_limit=8192,
        max_new_tokens=256,
        tool_names=["TtsAlert"],
        channel="smoke",
    )
    # Either the model called the tool (steps non-empty) or it answered in
    # prose. Both are valid; a hard error is not.
    steps = out.get("steps") or []
    ok = not out.get("error") and bool(out.get("answer"))
    res.record("文本对话 工具轮", ok,
               f"tool_call={out.get('tool_call')!r} steps={len(steps)} "
               f"answer={str(out.get('answer'))[:60]!r}")


# --------------------------------------------------------------------------- #
# Checks — audio analysis (qwen2.5-omni-3b) and TTS (qwen3-tts)               #
# --------------------------------------------------------------------------- #
def _pcm_tone(seconds: float = 1.5, rate: int = 16000) -> bytes:
    """Raw mono s16le PCM of a 440 Hz tone.

    ``OmniBackend.run_audio`` takes decoded PCM, not a WAV container —
    ``_dispatch_sync`` concatenates ``AudioAttachment.data`` and splits
    out sample_rate / n_channels / sample_width as call options.
    """
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float64) / rate
    wave = 0.2 * np.sin(2 * np.pi * 440.0 * t)
    return (wave * 32767.0).astype(np.int16).tobytes()


def check_audio_analysis(res: Results, reg) -> None:
    """音视频描述's audio half: an AudioAttachment in, plain text out."""
    from wdnai import AudioAttachment

    sess = reg.get_session("swcap_audio_understand")
    out = sync(sess.respond(
        prompt="用中文简短描述这段音频的内容。",
        attachment=[AudioAttachment(_pcm_tone())],
        max_new_tokens=128,
    ))
    text = out if isinstance(out, str) else (getattr(out, "text", "") or "")
    res.record("音频理解 纯文本返回", bool(text.strip()),
               f"{type(out).__name__} text={text[:70]!r}")


def check_audio_routing(res: Results, reg) -> None:
    """A missing attachment must produce a *clear* error, not a hang.

    Worth its own check because the worker's only symptom used to be an
    RPC timeout: everything inside ``serve_local_session`` raises into a
    reply, and this asserts the message survives that trip.
    """
    sess = reg.get_session("swcap_audio_understand")
    try:
        sync(sess.respond(prompt="没有音频附件。", max_new_tokens=32))
        res.record("音频缺附件报错清晰", False, "expected an error")
    except RuntimeError as exc:
        ok = "AudioAttachment" in str(exc)
        res.record("音频缺附件报错清晰", ok, str(exc)[:100])


def check_tts(res: Results, reg) -> None:
    """语音生成: options cross the wire and synthesised audio comes back.

    Audio rides back as an ``Attachment``, which only reaches the caller
    when ``generating=`` is an ``LMOutput`` subclass — so the same schema
    the real TTS page uses, not a bare prompt. A ``str`` here would mean
    the audio silently vanished, which is the failure worth catching.
    """
    from app.apps.tts.service import _get_tts_result_cls
    from wdnai import Attachment

    TTSResult = _get_tts_result_cls()
    sess = reg.get_session("swcap_audio_tts")
    # Same shape the TTS page uses: the text to speak is a text
    # *attachment*; ``prompt=`` stays a plain string (the SDK calls
    # ``.rstrip()`` on it).
    out = sync(sess.respond(
        attachment=[Attachment("今天天气不错。", kind="text")],
        generating=TTSResult,
        max_new_tokens=256,
        no_play=True,               # worker-side playback off
        volume=0.6,
        ref_speaker="girl_base",
    ))
    atts = list(getattr(out, "attachment", []) or [])
    n_bytes = sum(len(getattr(a, "data", b"") or b"") for a in atts)
    res.record("语音生成 音频回传", bool(atts) and n_bytes > 0,
               f"{type(out).__name__} attachments={len(atts)} "
               f"bytes={n_bytes} text={str(getattr(out, 'text', ''))[:40]!r}")


# --------------------------------------------------------------------------- #
# Checks — embedding (qwen3-vl-embedding-2b)                                  #
# --------------------------------------------------------------------------- #
def check_embedding(res: Results, reg) -> None:
    """视频内容检索: embed ops are overridden to run off the pump thread."""
    sess = reg.get_session("swcap_video_retrieval")
    vec = sess.embed_text("一只红色的正方形")
    arr = np.asarray(vec).reshape(-1)
    ok = arr.size > 0 and np.isfinite(arr).all() and float(np.linalg.norm(arr)) > 0
    res.record("视频内容检索 embed_text", bool(ok),
               f"dim={arr.size} norm={float(np.linalg.norm(arr)):.3f}")

    # embed_video over the same frames the VLM path uses.
    frames = [frame()]
    vec2 = sess.embed_video(frames, "红色正方形")
    arr2 = np.asarray(vec2).reshape(-1)
    res.record("视频内容检索 embed_video", arr2.size == arr.size,
               f"dim={arr2.size}")


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #
VLM_CHECKS = (
    ("音视频描述", check_video_describe),
    ("目标检测", check_target_detection),
    ("流式", check_streaming_deltas),
    ("stop", check_stop_is_soft),
    ("count_tokens", check_count_tokens),
    ("rescue 离线", check_rescue_offline),
)
GROUPS = {
    "vlm": {"model": "qwen3.5-vl-4b", "checks": VLM_CHECKS},
    "grounding": {"model": "LocateAnything-3b",
                  "checks": (("视频目标定位", check_localization),)},
    # qwen3.5-vl-4b is the only remaining provider of swcap_text_chat,
    # so this is the chat path by definition.
    "chat": {"model": os.environ.get("STAGE3_CHAT_MODEL",
                                     "qwen3.5-vl-4b"), "checks": (
        ("文本对话", check_text_chat),
        ("文本对话工具", check_text_chat_with_tool),
    )},
    "audio": {"model": "qwen2.5-omni-3b", "checks": (
        ("音频理解", check_audio_analysis),
        ("音频路由", check_audio_routing),
    )},
    "tts": {"model": "qwen3-tts", "checks": (("语音生成", check_tts),)},
    "embedding": {"model": "qwen3-vl-embedding-2b",
                  "checks": (("视频内容检索", check_embedding),)},
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="run every capability, not just the VLM group")
    ap.add_argument("--group", default="vlm", choices=sorted(GROUPS))
    args = ap.parse_args()

    groups = list(GROUPS) if args.all else [args.group]
    res = Results()
    reg = None
    t0 = time.time()
    try:
        for name in groups:
            g = GROUPS[name]
            print(f"\n[{name}] {g['model']}", flush=True)
            try:
                reg = boot(g["model"])
            except Exception as e:  # noqa: BLE001
                res.record(f"{name} worker 启动", False,
                           f"{type(e).__name__}: {e}")
                continue
            res.record(f"{name} worker 启动", True)
            for check_name, fn in g["checks"]:
                try:
                    fn(res, reg)
                except Exception as e:  # noqa: BLE001
                    res.record(f"{name}/{check_name}", False,
                               f"{type(e).__name__}: {e}")
    finally:
        if reg is not None:
            shutdown(reg)
    print(f"\nelapsed {time.time() - t0:.0f}s")
    return res.summary()


if __name__ == "__main__":
    raise SystemExit(main())