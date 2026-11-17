"""日志护栏：阻止单条日志把整个进程的内存/终端打爆。

2026-11-17 真机实测：监控反查的 `/play` 在客户端消费不过来时，``asyncio``
的默认异常处理器会把 ``<Queue ... _queue=[b'\\xff\\xd8...']>`` 整段 repr
写进日志 —— 一个 16 槽的 JPEG 队列约 5MB，25fps 下几十秒就能刷出 665MB。
根因已经在 ``video_retrieval/api.py::vr_play_ws`` 修掉（入队回调自己吞
``QueueFull``），这里再兜一层：任何来源的单条日志超过 16KB 都截断，
并对每个 (文件, 行号) 只提示一次来源。
"""
from __future__ import annotations

import logging

MAX_LOG_CHARS = 16 * 1024


class TruncateLongRecords(logging.Filter):
    """把超长日志记录截断到 ``MAX_LOG_CHARS``。

    只改 ``record.msg`` / ``record.args``，不改变级别与 logger 名；
    同一来源只打一条 ``log_guard`` 提示（带栈），方便定位是谁在打大日志。
    """

    def __init__(self, max_chars: int = MAX_LOG_CHARS) -> None:
        super().__init__()
        self._max_chars = max(1024, int(max_chars))
        self._seen: set[tuple[str, int]] = set()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if len(msg) <= self._max_chars:
            return True
        original_len = len(msg)
        record.msg = (
            msg[: self._max_chars]
            + f"... [truncated {original_len - self._max_chars} chars]"
        )
        record.args = ()
        key = (record.pathname or "", int(record.lineno or 0))
        if key not in self._seen:
            self._seen.add(key)
            logging.getLogger("log_guard").warning(
                "oversized log record from %s:%s (%d chars) — truncated",
                record.pathname, record.lineno, original_len,
                stack_info=True,
            )
        return True


def install_log_guard() -> TruncateLongRecords:
    """把护栏挂到 root handler 与常见库 logger 上（幂等）。"""
    guard = TruncateLongRecords()
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, TruncateLongRecords) for f in handler.filters):
            handler.addFilter(guard)
    for name in ("asyncio", "uvicorn.error", "uvicorn.access", "websockets"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, TruncateLongRecords) for f in logger.filters):
            logger.addFilter(guard)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    return guard
