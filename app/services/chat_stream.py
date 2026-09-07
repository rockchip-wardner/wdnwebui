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
    """给 wdnai 的结果回调工厂挂 observer 采集（幂等 + fail-soft）。

    ``make_result_callback`` 改名为 ``_make_sink_result_cb``）：

    * 优先 ``_make_sink_result_cb``（当前 SDK）；
    * 兼容旧符号 ``make_result_callback``（若回滚 wdnai 版本）；
    * 都缺失 → **fail-soft**：LOG.error 后放弃 —— chat 退化为无流式
      （done 一次性输出全文），**不阻断模型加载**（避免 SDK 再改私有
      符号时把算力配置页的模型加载整体失败）。
    """
    global _PATCH_DONE
    if _PATCH_DONE:
        return
    # 无论成败只尝试一次（失败反复重试只会刷日志，不会自愈）。
    _PATCH_DONE = True
    import wdnai.backends._common as _common
    patch_name = None
    for name in ("_make_sink_result_cb", "make_result_callback"):
        if hasattr(_common, name):
            patch_name = name
            break
    if patch_name is None:
        LOG.error(
            "chat 流式 patch 未安装：wdnai.backends._common 缺少回调工厂"
            "符号（_make_sink_result_cb / make_result_callback 都不存在）"
            "—— chat 将退化为无流式（done 一次性输出），模型加载不受影响；"
            "如需恢复流式请适配 chat_stream.install_patch 的 patch 点")
        return
    _original_factory = getattr(_common, patch_name)

    def _patched(sink):
        inner_cb = _original_factory(sink)
        sink_id = id(sink)
        last_text_ref = [""]

        def _cb_with_observer(userdata, new_tokens, state):
            ret = inner_cb(userdata, new_tokens, state)
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

        # 新 shim（wdnai._librknn）以 3 实参调用 user_cb：
        # ``user_cb(py_obj, tokens, state)``，py_obj 是从 ``__self__``
        # 自动 pin 的 userdata（free closure 无 __self__ → pin closure
        # 本身），与 adapter/旧 rknn3lite 契约一致。
        return _cb_with_observer

    setattr(_common, patch_name, _patched)
    LOG.info("monkey-patched wdnai.backends._common.%s", patch_name)


def register_observer(sink: Any, callback: Callable[[str], None]) -> None:
    with _OBSERVER_LOCK:
        _OBSERVERS[id(sink)] = callback


def unregister_observer(sink: Any) -> None:
    with _OBSERVER_LOCK:
        _OBSERVERS.pop(id(sink), None)
