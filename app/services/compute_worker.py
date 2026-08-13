"""compute_worker — wdndds cshim RPC server, one process per model.

Spawned by ``ComputeRegistry._spawn_compute_worker`` via
``subprocess.Popen``. Each worker holds:

  * its own DomainParticipant (via wdndds Hub)
  * one RPC service endpoint (request topic + reply topic)

Wire protocol
-------------
A request is one JSON object published to the request topic; a
reply is one JSON object published to the reply topic. The wdndds
cshim layer wraps this in a ``{wdndds_reply_suffix, data}`` envelope
so the client can match replies to in-flight calls.

Lifecycle (v0.7):
  1. Worker boots, brings up Hub + Service + Replier, registers
     discovery on the LAN daemon. Does **not** instantiate LLMSession.
  2. State = ``not_loaded``. ``info`` RPC reports this so the main
     process's compute-config page can show "未启用".
  3. User selects NPU on the compute-config page → main process sends
     RPC ``{"action":"load","npu_index":N}`` → worker instantiates
     ``LLMSession(model=spec, npu=N)`` → state ``loading`` → ``running``.
  4. ``{"action":"unload"}`` releases the LLMSession handle but keeps
     the worker process alive (returns to ``not_loaded``).
  5. ``{"action":"shutdown"}`` sets ``_WORKER_STOP_EVENT``; main loop
     exits; process self-terminates. After a 3 s grace period the
     caller may ``kill -9`` as a fallback.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any

LOG = logging.getLogger("compute_worker")


def _setup_logging(debug: bool) -> None:
    h = logging.StreamHandler(stream=sys.stderr)
    h.setFormatter(
        logging.Formatter("[compute_worker:%(process)d] %(asctime)s %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(h)
    root.setLevel(logging.DEBUG if debug else logging.INFO)


# Module-level stop event. ``main()`` initialises it and ``router``
# (specifically the ``shutdown`` action) sets it. The main thread
# waits on it and exits when set.
_WORKER_STOP_EVENT: threading.Event = threading.Event()


# --------------------------------------------------------------------------- #
# rkai lazy-import + session builder                                          #
# --------------------------------------------------------------------------- #
_RKAI = None


def _load_rkai() -> dict:
    global _RKAI
    if _RKAI is not None:
        return _RKAI
    try:
        from rkai import (
            Attachment,
            LLMSession,
            SystemLanguageModel,
        )
    except ImportError as e:
        raise RuntimeError(f"rkai import failed: {e}")
    _RKAI = {
        "Attachment": Attachment,
        "LLMSession": LLMSession,
        "SystemLanguageModel": SystemLanguageModel,
    }
    return _RKAI


def _build_session(model_id: str, npu_index: int):
    """Construct an LLMSession. May raise (or call terminate())."""
    mod = _load_rkai()
    if model_id == "qwen3-vl-4b":
        spec = mod["SystemLanguageModel"].qwen3_vl_4b()
    elif model_id == "qwen3-tts":
        spec = mod["SystemLanguageModel"].qwen3_tts()
    elif model_id == "qwen3-8b":
        from dataclasses import replace as _dc_replace
        spec = mod["SystemLanguageModel"].qwen3_8b()
        spec = _dc_replace(spec, max_context_len=32768).with_options(
            max_new_tokens=2048, keep_history=1, enable_thinking=True,
        )
    else:
        raise ValueError(f"unknown model_id: {model_id!r}")
    LOG.info("building LLMSession(model=%s, npu=%d) ...", model_id, npu_index)
    t0 = time.time()
    sess = mod["LLMSession"](model=spec, npu=npu_index)
    LOG.info("LLMSession ready (%.1fs)", time.time() - t0)
    return sess


# --------------------------------------------------------------------------- #
# Per-call handlers                                                          #
# --------------------------------------------------------------------------- #
def info_handler(state: dict, model_id: str,
                 startup_npu_index: int | None,
                 model_dir: str | None) -> dict:
    npu_index = state.get("npu_index")
    return {
        "status":             state.get("status", "not_loaded"),
        "model":              model_id,
        "model_dir":          model_dir,
        "npu_index":          npu_index,
        "npu_index_startup":  startup_npu_index,
        "load_time_sec":      state.get("load_time_sec"),
        "init_error":         state.get("init_error", ""),
        "rkai_available":     state.get("rkai_available", False),
    }


def load_handler(state: dict, model_id: str,
                 payload_npu_index: Any) -> dict:
    npu_index = payload_npu_index
    if npu_index is None:
        npu_index = state.get("npu_index")
    if not isinstance(npu_index, int) or npu_index < 0:
        return {
            "status": "error",
            "error": "npu_index 缺失：load action 必须传 npu_index",
        }
    # If already running on the same NPU, idempotent.
    if (state.get("status") == "running"
            and state.get("npu_index") == npu_index
            and state.get("session") is not None):
        return {
            "status": "running",
            "npu_index": npu_index,
            "load_time_sec": state.get("load_time_sec"),
            "loaded_at": state.get("loaded_at"),
        }
    # If previously bound to a different NPU, drop the old session first.
    if state.get("session") is not None:
        try:
            state["session"].close()
        except Exception:  # noqa: BLE001
            pass
        state["session"] = None
    state["status"] = "loading"
    state["npu_index"] = npu_index
    state["init_error"] = ""
    try:
        t0 = time.time()
        sess = _build_session(model_id, npu_index)
        load_sec = round(time.time() - t0, 2)
    except Exception as e:  # noqa: BLE001
        state["status"] = "error"
        state["init_error"] = f"{type(e).__name__}: {e}"
        return {"status": "error",
                "npu_index": npu_index,
                "init_error": state["init_error"]}
    state["session"] = sess
    state["status"] = "running"
    state["load_time_sec"] = load_sec
    state["loaded_at"] = time.time()
    return {
        "status": "running",
        "npu_index": npu_index,
        "load_time_sec": load_sec,
        "loaded_at": state["loaded_at"],
    }


def unload_handler(state: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "noop", "npu_index": state.get("npu_index")}
    try:
        sess.close()
    except Exception as e:  # noqa: BLE001
        LOG.warning("session.close raised: %s", e)
    state["session"] = None
    state["status"] = "not_loaded"
    state["npu_index"] = None
    state["load_time_sec"] = None
    state["loaded_at"] = None
    return {"status": "unloaded"}


def reset_handler(state: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "noop (not loaded)"}
    try:
        sess.reset()
        return {"status": "reset"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"reset failed: {e}"}


def infer_handler(state: dict, payload: dict) -> dict:
    sess = state.get("session")
    if sess is None:
        return {"status": "error", "error": "session not loaded; send action='load' first"}

    try:
        mod = _load_rkai()
        Attachment = mod["Attachment"]
        prompt = payload.get("prompt")
        atts_meta = payload.get("attachment", [])
        attachments = []
        for item in atts_meta:
            if isinstance(item, dict) and "data_b64" in item:
                import base64 as _b64
                import numpy as _np
                raw = _b64.b64decode(item["data_b64"])
                arr = _np.frombuffer(raw, dtype=_np.uint8)
                import cv2 as _cv2
                img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
                if img is not None:
                    attachments.append(Attachment(img))
                continue
            if isinstance(item, str):
                try:
                    import base64 as _b64
                    import numpy as _np
                    raw = _b64.b64decode(item)
                    arr = _np.frombuffer(raw, dtype=_np.uint8)
                    import cv2 as _cv2
                    img = _cv2.imdecode(arr, _cv2.IMREAD_COLOR)
                    if img is not None:
                        attachments.append(Attachment(img))
                        continue
                except Exception:
                    pass
                attachments.append(Attachment(item, kind="text"))
                continue
            if hasattr(item, "shape"):
                attachments.append(Attachment(item))

        import asyncio
        generating_cls = None
        gen_name = payload.get("generating")
        if gen_name == "VLMDoc":
            try:
                from app.services.detector import _get_vlm_doc_cls
                generating_cls = _get_vlm_doc_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("VLMDoc schema load failed in worker: %s", e)
        elif gen_name == "TargetDoc":
            try:
                from app.services.detector import _get_target_doc_cls
                generating_cls = _get_target_doc_cls()
            except Exception as e:  # noqa: BLE001
                LOG.warning("TargetDoc schema load failed in worker: %s", e)

        async def _call():
            try:
                sess.reset()
            except Exception:
                pass
            kwargs = {}
            if generating_cls is not None:
                kwargs["generating"] = generating_cls
            return await sess.respond(
                prompt=prompt,
                attachment=attachments if attachments else None,
                **kwargs,
            )

        result = asyncio.run(_call())
        text = getattr(result, "text", None) or (
            result if isinstance(result, str) else None)
        return {
            "text": text,
            "level": getattr(result, "level", None),
            "tool_call": getattr(result, "tool_call", None),
            "bbox": getattr(result, "bbox", None),
            "crop_b64": getattr(result, "crop_b64", None),
        }
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# Router                                                                    #
# --------------------------------------------------------------------------- #
def router(service_name: str, payload: Any, state: dict,
           model_id: str, startup_npu_index: int | None,
           model_dir: str | None) -> dict:
    # The cshim v0.5 replier (``Replier._on_request_c``) already
    # ``json.loads`` the inbound payload before dispatching it here, so
    # ``payload`` is a dict. The cshim then json.dumps the dict this
    # function returns and embeds it as a raw JSON object under the
    # wire ``data`` field — consumers access handler results directly
    # via ``r["data"]`` without an extra unwrap.
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"payload must be dict, got {type(payload).__name__}"}

    # The cshim v0.5 client stuffs the entire original JSON into the
    # typed ``prompt`` field (because the IDL only carries action /
    # every_n / request_id + a prompt string). The cshim worker side
    # re-quotes that prompt field as a JSON string value, so we see
    # ``payload["prompt"]`` as a JSON-encoded string here. Re-parse
    # it so downstream handlers see the original fields
    # (attachment / generating / tools / options / npu_index / ...).
    prompt_field = payload.get("prompt", "")
    if isinstance(prompt_field, str) and prompt_field.startswith("{"):
        try:
            inner = json.loads(prompt_field)
            if isinstance(inner, dict):
                payload = inner
        except Exception:
            pass

    action = payload.get("action", "info")
    if action == "info":
        return info_handler(state, model_id, startup_npu_index, model_dir)
    if action == "load":
        return load_handler(state, model_id, payload.get("npu_index"))
    if action == "infer":
        return infer_handler(state, payload)
    if action == "reset":
        return reset_handler(state)
    if action == "unload":
        return unload_handler(state)
    if action == "shutdown":
        # Trigger graceful exit of the worker main loop. The caller
        # (ComputeRegistry.stop_instance_local) waits up to 3 s, then
        # escalates to SIGKILL.
        LOG.info("shutdown action received — exiting main loop")
        _WORKER_STOP_EVENT.set()
        return {"status": "shutting_down"}
    return {"ok": False, "error": f"unknown action: {action!r}"}


# --------------------------------------------------------------------------- #
# Main — wdndds cshim Hub as RPC server                                      #
# --------------------------------------------------------------------------- #
def _config_path() -> str:
    """Resolve config.json path from PYRKAIWEB_CONFIG env var, defaulting
    to the repo root config.json."""
    import os
    return os.environ.get('PYRKAIWEB_CONFIG') or '/userdata/pyrkaiweb/config.json'


def main() -> int:
    ap = argparse.ArgumentParser(prog="compute_worker")
    ap.add_argument("--model", required=True,
                    help="model id (key in MODEL_PRESETS)")
    ap.add_argument("--npu", type=int, required=False, default=None,
                    help="DEPRECATED: npu_index 现在通过 RPC load action 传入。"
                         "保留只为兼容旧 systemd / 调试用。worker 启动后只注册 "
                         "DDS endpoint 不建 LLMSession；npu_index 由用户在算力"
                         "配置页选择后通过 RPC ``action=load npu_index=N`` 传"
                         "入。如果这里也传，worker 启动后会自动 build LLMSession"
                         "（仅用于 e2e 测试方便）。")
    ap.add_argument("--debug", action="store_true",
                    default=os.environ.get("WDNDDS_DEBUG") == "1")
    args = ap.parse_args()

    _setup_logging(args.debug)

    model_id = args.model
    npu_index = args.npu

    # Resolve model_dir for the info reply (don't fail if missing).
    model_dir = None
    try:
        base_dir = os.environ.get("RKAI_MODELS_DIR", "/userdata/models/")
        from app.services.compute_registry import MODEL_PRESETS
        preset = MODEL_PRESETS.get(model_id)
        if preset:
            model_dir = os.path.join(base_dir, preset["model_dir_name"])
    except Exception:
        pass

    LOG.info("compute_worker start: model=%s service=wdndds/rpc/%s domain=0 "
             "(npu_index=%s, deferred-to-RPC=%s)",
             model_id, model_id, npu_index, npu_index is None)

    # Bring up wdndds Hub with the single service. The cshim Hub has a
    # background pump thread that calls our ``router`` for each
    # inbound request.
    from wdndds.dds._rpc import Hub
    # Discovery: point at the LAN's wdndds_daemon (one per LAN). If
    # config.json doesn't list any discovery_servers, the worker
    # falls back to SIMPLE discovery (single-host, multicast).
    discovery_servers = []
    try:
        import json as _json
        with open(_config_path()) as f:
            cfg = _json.load(f).get("compute", {})
        servers = cfg.get("discovery_servers") or []
        if isinstance(servers, str):
            servers = [s.strip() for s in servers.split(",") if s.strip()]
        discovery_servers = [s for s in servers if s]
    except (OSError, ValueError):
        pass

    LOG.info("compute_worker discovery_servers=%r (config=%s)",
             discovery_servers, _config_path())

    # State dict shared with the router callback. Initial status is
    # ``not_loaded`` — worker is alive and reachable, but has not
    # built an LLMSession yet (npu_index deferred to RPC load).
    _worker_state: dict = {
        "status": "not_loaded",
        "started_at": time.time(),
        "npu_index": None,
        "load_time_sec": None,
        "loaded_at": None,
        "init_error": "",
        "session": None,
        "rkai_available": False,
    }

    # Reset module-level stop event in case main() is re-entered under
    # the same interpreter (unusual but defensive).
    global _WORKER_STOP_EVENT
    _WORKER_STOP_EVENT = threading.Event()
    stop_event = _WORKER_STOP_EVENT

    hub = Hub(
        participant_name=f"compute_worker-{model_id}",
        domain_id=0,
        services=[f"wdndds/rpc/{model_id}"],
        callback=lambda svc, pl: router(
            svc, pl, _worker_state, model_id, npu_index, model_dir),
        discovery_servers=discovery_servers,
    )
    hub.start()

    # Legacy compat: if --npu was passed on the command line, build the
    # session eagerly so old systemd / debug workflows keep working.
    # Production paths omit --npu and rely on the RPC load action.
    if npu_index is not None:
        def _eager_load():
            try:
                LOG.info("eager load starting (--npu=%d) for %s",
                         npu_index, model_id)
                load_handler(_worker_state, model_id, npu_index)
            except Exception as e:  # noqa: BLE001
                LOG.warning("eager load raised: %s", e)
        threading.Thread(target=_eager_load, name="eager-load",
                         daemon=True).start()

    # Graceful shutdown on SIGTERM / SIGINT — same effect as RPC shutdown.
    def _signal_handler(signum, frame):
        LOG.info("received signal %d — shutting down", signum)
        stop_event.set()
        hub.stop()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Print ready marker for the parent to detect.
    print(json.dumps({
        "type": "ready",
        "service": f"wdndds/rpc/{model_id}",
        "model": model_id,
        "npu_index": npu_index,
        "process_id": os.getpid(),
    }), file=sys.stderr, flush=True)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)
    finally:
        LOG.info("compute_worker exit")
        try:
            hub.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())