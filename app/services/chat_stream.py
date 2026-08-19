from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict

LOG = logging.getLogger("chat_stream")

_OBSERVERS: Dict[int, Callable[[str], None]] = {}
_OBSERVER_LOCK = threading.Lock()
_PATCH_DONE = False

_STATE_NORMAL = 0
_STATE_FINISH_STATES = (2, 3, 4)


def install_patch() -> None:
    global _PATCH_DONE
    if _PATCH_DONE:
        return
    import wdnai.backends._common as _common
    _original_factory = _common.make_result_callback

    def _patched(sink):
        inner_cb = _original_factory(sink)
        sink_id = id(sink)
        last_text_ref = [""]

        def _cb_with_observer(userdata, result_ptr, state):
            ret = inner_cb(userdata, result_ptr, state)
            if state == _STATE_NORMAL or state in _STATE_FINISH_STATES:
                with _OBSERVER_LOCK:
                    obs = _OBSERVERS.get(sink_id)
                if obs is not None:
                    new_text = sink.text
                    old_text = last_text_ref[0]
                    if new_text != old_text:
                        delta = new_text[len(old_text):]
                        last_text_ref[0] = new_text
                        try:
                            obs(delta)
                        except Exception as e:  # noqa: BLE001
                            LOG.warning("observer raised: %s", e)
            return ret

        from rknn3lite.api import LLMResultCallback
        return LLMResultCallback(_cb_with_observer)

    _common.make_result_callback = _patched
    _PATCH_DONE = True
    LOG.info("monkey-patched wdnai.backends._common.make_result_callback")


def register_observer(sink: Any, callback: Callable[[str], None]) -> None:
    with _OBSERVER_LOCK:
        _OBSERVERS[id(sink)] = callback


def unregister_observer(sink: Any) -> None:
    with _OBSERVER_LOCK:
        _OBSERVERS.pop(id(sink), None)