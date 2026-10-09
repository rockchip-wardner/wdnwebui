"""Two-worker / two-NPU-card real-inference smoke.

Distinct from ``verify_stage3_smoke.py``: that script *owns* the NPU and
defaults every model to ``npu_index=0``, so it aborts (rc=-6) whenever a
model is already resident on card 0 — which is exactly what happens when
the UI has a model enabled. This one is built for the board's normal
state (card 0 and card 1 each holding a running worker) and only ever
*borrows* those workers:

* adopt-only probing (``_adopt_existing_worker``), never
  ``ensure_worker_for_model`` — the latter falls through to a spawn and
  would add NPU pressure to an already-full board;
* skips anything that is not already ``running`` rather than loading it;
* dispatches on each instance's ``capability_types``, so it works for
  whatever model happens to sit on each card.

Run it while the UI has two models enabled on two different cards::

    python scripts/verify_dual_card_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def check(name: str):
    def deco(fn):
        def wrapper():
            t0 = time.time()
            try:
                detail = fn() or ""
                record(True, name, f"({time.time() - t0:.1f}s) {detail}")
            except Exception as exc:  # noqa: BLE001 - smoke reports everything
                record(False, name,
                       f"({time.time() - t0:.1f}s) {type(exc).__name__}: {exc}")
                traceback.print_exc()
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def _sync(fn, *args, **kwargs):
    """Call a session coroutine function from sync code."""
    return asyncio.run(fn(*args, **kwargs))


def _image_attachment(img):
    """Wrap a BGR ndarray the way the real pages do."""
    from wdnai import ImageAttachment

    return ImageAttachment(img)


def _result_text(res) -> str:
    """Pull text out of a ``respond`` result.

    Normally a schema doc; a bare ``str`` when the SDK could not parse
    the model's JSON — see ``detector_base._result_raw_text()``.
    """
    text = getattr(res, "text", None)
    if text is None and isinstance(res, str):
        text = res
    return (text or "").strip()


def _adopt_all(reg) -> None:
    """Adopt every worker that already answers — never spawn."""
    from app.core.compute.registry import MODEL_PRESETS

    for model_id in sorted(MODEL_PRESETS):
        try:
            reg._adopt_existing_worker(model_id)
        except Exception as exc:  # noqa: BLE001 - probe failures are expected
            print(f"  probe {model_id}: {type(exc).__name__}: {exc}", flush=True)


def running_instances():
    """Return ``(registry, {model_id: (npu_index, _source)})`` for running models."""
    from app.core.compute.registry import get_registry

    reg = get_registry()
    reg._ensure_hub()
    print("  probing existing workers (adopt-only, no spawn) …", flush=True)
    _adopt_all(reg)
    out: dict[str, tuple[int, str]] = {}
    for inst in reg._instances.values():
        if inst.status == "running":
            out[inst.model_id] = (int(inst.npu_index), inst._source)
    return reg, out


def adopt_running(model_id: str, npu_index: int):
    """Return the instance for an already-running model, loading nothing."""
    from app.core.compute.registry import get_registry

    reg = get_registry()
    inst = reg._find_by_model(model_id)
    if inst is None:
        raise RuntimeError(f"{model_id} 未被发现")
    if inst.status != "running" or int(inst.npu_index) != npu_index:
        raise RuntimeError(
            f"{model_id} 未在 npu={npu_index} running"
            f"（实际 npu={inst.npu_index} status={inst.status}）"
        )
    return inst


def _scene() -> "np.ndarray":
    """A red square on a grey field — nothing to miss."""
    import numpy as np

    img = np.full((480, 640, 3), 200, dtype=np.uint8)
    img[180:300, 260:380] = (40, 40, 210)
    return img


def main() -> int:
    os.environ.setdefault("WDNAI_MODELS_DIR", "/mnt/oem/models")

    print("=" * 72)
    print("dual-card smoke: 两个 worker 分别跑 npu 0 / npu 1")
    print("=" * 72)

    reg, live = running_instances()
    for mid, (npu, src) in sorted(live.items(), key=lambda kv: kv[1][0]):
        print(f"  live: {mid:24s} npu={npu}  src={src}")
    if len(live) < 2:
        print("\n需要至少 2 个 running 实例才验证得了双卡。先在算力页把两个模型"
              "分别绑到不同的 NPU 卡。")
        return 2
    cards = sorted({npu for npu, _ in live.values()})
    print(f"  -> 使用 NPU 卡片 {cards}\n")

    def check_cap(mid: str, npu: int, cap: str):
        """Build the check for one capability, then run it immediately."""
        label = f"[npu{npu}] {mid} {cap}"

        if cap == "swcap_video_describe":
            @check(label)
            def _run():
                from app.core.llm.detector_base import _get_vlm_doc_cls
                adopt_running(mid, npu)
                sess = reg.get_session(cap)
                res = _sync(sess.respond,
                            prompt="用中文一句话描述画面内容。",
                            attachment=[_image_attachment(_scene())],
                            generating=_get_vlm_doc_cls())
                text = _result_text(res)
                if not text:
                    raise RuntimeError(f"空描述，res 类型={type(res).__name__}")
                return f"desc={text[:60]!r}"

        elif cap == "swcap_audio_understand":
            @check(label)
            def _run():
                import numpy as np
                from wdnai import AudioAttachment
                adopt_running(mid, npu)
                # Raw mono s16le PCM — OmniBackend takes decoded PCM, not a
                # WAV container (sample_rate/n_channels are call options).
                seconds, rate = 1.5, 16000
                t = np.arange(int(seconds * rate), dtype=np.float64) / rate
                pcm = (0.2 * np.sin(2 * np.pi * 440.0 * t) * 32767.0)
                pcm = pcm.astype(np.int16).tobytes()
                sess = reg.get_session(cap)
                res = _sync(sess.respond,
                            prompt="用中文简短描述这段音频的内容。",
                            attachment=[AudioAttachment(pcm, sample_rate=rate,
                                                        n_channels=1)],
                            max_new_tokens=128)
                text = _result_text(res)
                if not text:
                    raise RuntimeError(f"空回复，res 类型={type(res).__name__}")
                return f"pcm={len(pcm)}B reply={text[:50]!r}"

        elif cap == "swcap_text_chat":
            @check(label)
            def _run():
                adopt_running(mid, npu)
                sess = reg.get_session(cap)
                res = _sync(sess.respond, prompt="用一句话介绍你自己。",
                            max_new_tokens=96)
                text = _result_text(res)
                if not text:
                    raise RuntimeError(f"空回复，res 类型={type(res).__name__}")
                return f"reply={text[:60]!r}"

        elif cap == "swcap_grounding":
            @check(label)
            def _run():
                from app.core.llm.detector_base import _get_grounding_doc_cls
                adopt_running(mid, npu)
                sess = reg.get_session(cap)
                res = _sync(sess.respond,
                            prompt="定位画面中央的红色方块。",
                            attachment=[_image_attachment(_scene())],
                            generating=_get_grounding_doc_cls())
                text = _result_text(res)
                if not text:
                    raise RuntimeError(f"空定位结果，res 类型={type(res).__name__}")
                return f"grounding={text[:60]!r}"

        else:
            record(True, label + " 跳过", "该 capability 不在双卡 smoke 覆盖范围")
            return

        # @check only builds the runner — call it so the check executes.
        _run()

    for mid, (npu, _src) in sorted(live.items(), key=lambda kv: (kv[1][0], kv[0])):
        inst = reg._find_by_model(mid)
        for cap in list(inst.capability_types or []):
            check_cap(mid, npu, cap)

    # ---------------------------------------------------------------- #
    # Both cards alive at the same time — the actual thing under test
    # ---------------------------------------------------------------- #
    @check("[双卡] 两实例并发存活")
    def _both():
        bad = []
        for mid, (npu, _src) in live.items():
            inst = reg._find_by_model(mid)
            if inst is None or inst.status != "running":
                bad.append((mid, npu, getattr(inst, "status", "GONE")))
        if bad:
            raise RuntimeError(f"有实例掉了：{bad}")
        return " + ".join(f"{m}@npu{n}" for m, (n, _) in sorted(live.items()))

    _both()

    print("-" * 72)
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    print(f"{passed}/{len(RESULTS)} checks passed")
    print("=" * 72)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
