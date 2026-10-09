"""Real-NPU parity check: LLMLocalSession vs LLMRPCSession, same weights.

Builds one real Qwen3.5-VL session, runs a prompt locally, then serves
that same session over DDS and runs the identical prompt through
LLMRPCSession. Because it is the same model instance and the same
process, any difference is the transport, not the model.

Usage: python -m scripts.verify_llm_session_parity
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import List, Optional

import numpy as np
from pydantic import BaseModel

import cv2
from wdnai import Field, Generable, ImageAttachment, LLMLocalSession
from wdndds import LLMRPCSession, serve_local_session

MODEL_DIR = os.environ.get("WDNAI_MODELS_DIR", "/mnt/oem/models")
SERVICE = f"wdndds/rpc/parity-{os.getpid()}"


@Generable
class Finding(BaseModel):
    """A schema the transport has no knowledge of."""
    summary: str = Field(description="one sentence summary")
    confidence: float = Field(default=0.0, description="0..1")


def build_session() -> LLMLocalSession:
    from wdnai.model import SystemLanguageModel
    # Same factory wdnwebui's registry uses, so this exercises the real
    # spec (vocab size, sampling options, vision variants) rather than a
    # hand-rolled approximation.
    spec = SystemLanguageModel.qwen3_5_vl_4b()
    return LLMLocalSession(model=spec, npu=0)


def frame() -> np.ndarray:
    """A simple synthetic scene — random noise makes the VLM echo the
    schema instead of answering it, which is a model artefact, not a
    transport one."""
    img = np.full((480, 640, 3), 200, dtype=np.uint8)
    img[:, :320] = (40, 60, 200)          # left half: a blue "sky"
    img[260:, :] = (60, 160, 70)          # bottom: a green "ground"
    cv2.rectangle(img, (420, 120), (560, 260), (30, 30, 200), -1)
    return img


async def main() -> int:
    print(f"[parity] loading real model from {MODEL_DIR} …", flush=True)
    t0 = time.perf_counter()
    session = build_session()
    served = serve_local_session(session, service_name=SERVICE)
    rpc = LLMRPCSession(
        model=session.spec, service_name=SERVICE,
        timeout_ms=10_000, infer_timeout_ms=120_000,
    )
    print(f"[parity] model ready in {time.perf_counter() - t0:.1f}s", flush=True)

    # Warm up DDS discovery (the first call always races it).
    for _ in range(60):
        try:
            rep = rpc._call_once({"action": "info", "op": "info"}, 5_000)
            if rep.get("ok"):
                break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
    print("[parity] replier reachable", flush=True)

    img = frame()
    failures: List[str] = []

    # ---- 1. plain text, local vs RPC ----------------------------------
    print("[parity] plain text (with image) …", flush=True)
    local_text = await session.respond(
        prompt="用一句话描述这张图片。", attachment=ImageAttachment(img))
    rpc_text = await rpc.respond(
        prompt="用一句话描述这张图片。", attachment=ImageAttachment(img))
    same = local_text == rpc_text
    print(f"  local: {local_text[:70]!r}")
    print(f"  rpc  : {rpc_text[:70]!r}")
    print(f"  identical: {same}")
    if not same:
        failures.append("plain-text output differs between local and RPC")

    # ---- 2. structured output, real schema instance --------------------
    # Run BOTH sides with the same image so a schema-parsing failure is
    # attributable: if the local call also falls back to str, the model
    # did not answer with the schema and the transport is not implicated.
    print("[parity] structured output (local, then RPC) …", flush=True)
    local_obj = await session.respond(
        prompt="这张图里有什么？", attachment=ImageAttachment(img),
        generating=Finding,
    )
    rpc_obj = await rpc.respond(
        prompt="这张图里有什么？", attachment=ImageAttachment(img),
        generating=Finding,
    )
    print(f"  local: {type(local_obj).__name__}  {local_obj!r}")
    print(f"  rpc  : {type(rpc_obj).__name__}  {rpc_obj!r}")
    if type(local_obj) is not type(rpc_obj) or local_obj != rpc_obj:
        failures.append(
            f"generating= local={type(local_obj).__name__}/{local_obj!r} "
            f"rpc={type(rpc_obj).__name__}/{rpc_obj!r}")
    elif isinstance(rpc_obj, Finding) and not rpc_obj.summary:
        failures.append("Finding.summary is empty")
    elif not isinstance(rpc_obj, Finding):
        # Both sides fell back to raw text — the model echoed the schema
        # instead of answering it. Parity still holds; note it so the run
        # isn't mistaken for a transport failure.
        print("  NOTE: model echoed the schema (both sides agree); "
              "type fidelity covered by wdndds unit tests")

    # ---- 3. streaming over the topic -----------------------------------
    print("[parity] streaming over RPC …", flush=True)
    deltas: List[str] = []
    streamed = await rpc.respond(
        prompt="用一句话描述这张图片。",
        attachment=ImageAttachment(img),
        on_delta=deltas.append,
    )
    joined = "".join(deltas)
    print(f"  deltas: {len(deltas)}  joined==return: {joined == streamed}")
    print(f"  text  : {streamed[:60]!r}")
    if not deltas:
        failures.append("no deltas crossed the delta topic")
    elif joined != streamed:
        failures.append("concatenated deltas != final text")

    # A second streaming call must route to the *new* callback, not the
    # one from the first call.
    second: List[str] = []
    streamed2 = await rpc.respond(
        prompt="用一句话描述这张图片。",
        attachment=ImageAttachment(img),
        on_delta=second.append,
    )
    print(f"  second call deltas: {len(second)}  joined==return: "
          f"{''.join(second) == streamed2}")
    if not second:
        failures.append("second streaming call received no deltas")
    elif "".join(second) != streamed2:
        failures.append("second call's deltas != its final text")

    # ---- 4. no side-file leakage ---------------------------------------
    leftovers = [f for f in os.listdir("/dev/shm") if f.startswith("wdndds_ai_")]
    print(f"[parity] shm leftovers: {leftovers}")
    if leftovers:
        failures.append(f"shm side-files leaked: {leftovers}")

    rpc.close()
    served.close()
    session.close()

    print("-" * 60)
    if failures:
        for f in failures:
            print("  FAIL:", f)
        return 1
    print("  ALL PARITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))