"""End-to-end check of the flat worker layout.

Boots a real worker as a subprocess exactly the way
``ComputeRegistry._spawn_compute_worker`` does, then drives it over DDS:
``info`` → ``load`` → ``infer``. Verifies the shared entry script, the
manifest-derived metadata, the ``/proc`` dedup needle and the real model
round trip.

Usage::

    WDNAI_MODELS_DIR=/mnt/oem/models python scripts/verify_worker_split.py [model_id]
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL_ID = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5-vl-4b"


def main() -> int:
    import numpy as np

    from app.core.compute.registry import _find_workers_by_name
    from app.core.compute.workers import discover, load_one
    from wdndds.dds._rpc import Hub

    failures = []

    # ---- 1. discovery -----------------------------------------------------
    manifests = discover()
    print(f"[split] discovered {len(manifests)} workers")
    bad = [m.id for m in manifests if m.status != "ok"]
    if bad:
        failures.append(f"manifest status != ok: {bad}")

    manifest = load_one(MODEL_ID)
    if manifest is None:
        failures.append(f"{MODEL_ID}: no manifest")
        print("FATAL: no manifest")
        return 1
    print(f"[split] {MODEL_ID}: swcap={manifest.capability_types} "
          f"comm_alias={manifest.worker_name}")
    print(f"[split] entry = {manifest.entry_path}")
    if not os.path.isfile(manifest.entry_path):
        failures.append("entry script missing")

    # ---- 1b. flat-layout invariants --------------------------------------
    # One manifest per model, one entry script for all of them. If either
    # drifts back to per-model scripts, the dedup needle below starts
    # matching more than it should and the per-model copy drift starts
    # again — so assert the shape, not just that it happens to boot.
    entries = {m.entry_path for m in manifests}
    if len(entries) != 1:
        failures.append(f"expected 1 shared entry script, got {len(entries)}: "
                        f"{sorted(entries)}")
    else:
        print(f"[split] all {len(manifests)} models share one entry script")

    from wdnai import SystemLanguageModel
    for m in manifests:
        if not m.spec_factory:
            failures.append(f"{m.id}: manifest has no spec_factory")
        elif not hasattr(SystemLanguageModel, m.spec_factory):
            # Would otherwise only surface as an AttributeError deep inside
            # a boot-time loader, long after the manifest looked fine.
            failures.append(
                f"{m.id}: wdnai.SystemLanguageModel has no "
                f"{m.spec_factory!r}")
    print(f"[split] spec_factory resolved for all {len(manifests)} manifests")

    # ---- 2. spawn exactly like the registry does --------------------------
    env = dict(os.environ)
    env.setdefault("WDNAI_MODELS_DIR", "/mnt/oem/models")
    # The worker registers BOTH a per-PID service (because --local-only)
    # and the standard one; the standard name is stable across launches,
    # so drive that one.
    service = f"wdndds/rpc/{MODEL_ID}"
    print(f"[split] spawning: {sys.executable} -u {manifest.entry_path} "
          f"--model {MODEL_ID} --local-only")
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, "-u", manifest.entry_path,
         "--model", MODEL_ID, "--local-only"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        # ---- 3. the /proc dedup needle must match this process ----------
        time.sleep(6.0)
        needle = f"compute_worker.py\x00--model\x00{MODEL_ID}\x00".encode()
        with open(f"/proc/{proc.pid}/cmdline", "rb") as fh:
            cmdline = fh.read()
        needle_ok = needle in cmdline
        comm = ""
        try:
            with open(f"/proc/{proc.pid}/comm") as fh:
                comm = fh.read().strip()
        except OSError:
            pass
        print(f"[split] /proc cmdline dedup needle matches: {needle_ok}")
        print(f"[split] /proc comm = {comm!r}")
        if not needle_ok:
            failures.append("dedup cmdline needle no longer matches the entry path")
        expected_comm = f"worker:{manifest.worker_name}"
        if comm != expected_comm:
            failures.append(f"comm is {comm!r}, expected {expected_comm!r}")

        # registry's own dedup helper must find the process we just spawned
        pids = _find_workers_by_name(MODEL_ID) or []
        print(f"[split] registry _find_workers_by_name -> {pids}")
        if proc.pid not in pids:
            failures.append(f"registry dedup missed pid {proc.pid} (found {pids})")

        # ---- 4. drive it over DDS ---------------------------------------
        hub = Hub(participant_name="verify-split")
        hub.start()
        req = hub.create_service(service).create_requester(timeout_ms=20_000)

        def rpc(payload, timeout_ms=20_000):
            return req.call(payload, timeout_ms=timeout_ms)

        # info
        info = None
        for _ in range(40):
            info = rpc({"action": "info", "op": "info"})
            if info.get("ok"):
                break
            time.sleep(0.5)
        if not (info and info.get("ok")):
            failures.append("info RPC never succeeded")
        else:
            d = info["data"]
            print(f"[split] info: status={d.get('status')} "
                  f"model={d.get('model')} comm={d.get('worker_name')!r}")
            if d.get("model") != MODEL_ID:
                failures.append(f"info reports model {d.get('model')!r}")

        # load is asynchronous by design: it kicks the model build off on a
        # background thread and returns "loading" straight away. Poll info
        # until it settles.
        print("[split] load (building real model, ~60s) …", flush=True)
        t0 = time.time()
        reply = rpc({"action": "load", "op": "load", "npu_index": 0},
                    timeout_ms=180_000)
        load_data = (reply.get("data") or {})
        print(f"[split] load -> status={load_data.get('status')} "
              f"in {time.time() - t0:.0f}s")
        if load_data.get("status") not in ("running", "loading"):
            failures.append(f"load rejected: {load_data}")

        status = "loading"
        while status in ("loading", "not_loaded") and time.time() - t0 < 300:
            time.sleep(3.0)
            probe = rpc({"action": "info", "op": "info"}, timeout_ms=20_000)
            d = probe.get("data") or {}
            status = d.get("status", "?")
            print(f"[split] info status={status} "
                  f"({time.time() - t0:.0f}s)", flush=True)
            if status == "error":
                failures.append(f"load ended in error: {d.get('error')}")
                break
        if status != "running":
            failures.append(f"load never reached running (status={status!r})")

        # infer. Stage 3 moved the worker onto
        # ``wdndds.serve_local_session``, so the reply is the transport's
        # envelope (``{"status": …, "text": …}``) and the request carries
        # a fully-composed prompt plus ``attachments`` (plural — the old
        # ``infer_handler`` took ``attachment``).
        #
        # Attachment form must be the production one: a /dev/shm path-list
        # descriptor. A bare base64 string is NOT raw BGR — the decoder
        # routes it through cv2.imdecode (needs a JPEG/PNG encoded blob)
        # and silently demotes a failed decode to a TextAttachment, which
        # would feed the whole base64 blob to the model as text.
        import uuid
        img = np.full((480, 640, 3), 180, np.uint8)
        img[200:400, 200:440] = (40, 40, 220)
        shm_path = f"/dev/shm/verify_split_{uuid.uuid4().hex}.bin"
        img.tofile(shm_path)
        try:
            reply = rpc({
                "action": "infer", "op": "respond", "v": 2,
                "prompt": "一句话描述这张图。",
                "attachments": [{
                    "data_path": shm_path,
                    "shape": [480, 640, 3],
                    "dtype": "uint8",
                }],
                "options": {},
            }, timeout_ms=120_000)
        finally:
            try:
                os.unlink(shm_path)
            except OSError:
                pass
        data = (reply.get("data") or {})
        if data.get("status") == "error":
            failures.append(f"infer failed: {data}")
        elif not (isinstance(data, dict) and data.get("text")):
            failures.append(f"infer returned no text: {data}")
        else:
            out = str(data.get("text"))
            print(f"[split] infer -> {out[:80]!r}")
            # A model that never saw the image can't describe "a red
            # square on a light grey background" — the exact content we
            # synthesised. Catch the silent "0 attachments decoded" case.
            if "红" not in out and "red" not in out.lower():
                failures.append(
                    f"infer output does not mention the red square — the "
                    f"frame probably did not reach the model: {out[:200]!r}")

        # count_tokens: the one session capability the worker still lends
        # out, because history trimming needs the model's own tokenizer.
        tok = rpc({"action": "info", "op": "count_tokens",
                   "texts": ["hello world", "中文测试"]},
                  timeout_ms=15_000)
        tdata = (tok.get("data") or {})
        counts = tdata.get("counts")
        if not (isinstance(counts, list) and len(counts) == 2
                and all(isinstance(c, int) and c > 0 for c in counts)):
            failures.append(f"count_tokens returned {tdata!r}")
        else:
            print(f"[split] count_tokens -> {counts}")

        hub.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("-" * 60)
    if failures:
        for f in failures:
            print("  FAIL:", f)
        return 1
    print("  WORKER SPLIT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())