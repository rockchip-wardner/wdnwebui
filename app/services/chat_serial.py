"""进程级 LLM 推理串行锁。

``text_chat`` 的 worker 与微信桥接（``wechat_llm``）各自持有一个
worker 线程，二者都可能调用 ``RpcSessionProxy.chat_run(...)`` 往同一个
``compute_worker`` 子进程发 RPC 推理。worker 端虽然串行消费请求，但主进程侧
并发 ``chat_run`` 的轮询 / 停止逻辑需要跨调用者串行化，因此暴露一个共享的
``threading.RLock``，两边都用 ``with get_chat_run_lock():`` 包住调用。
"""
from __future__ import annotations

import threading

_call_lock = threading.RLock()
_call_lock_guard = threading.Lock()


def get_chat_run_lock() -> threading.RLock:
    """Return the process-wide RLock guarding ``chat_run`` calls.

    Reusable across multiple ``with`` blocks (never drains).
    """
    return _call_lock
