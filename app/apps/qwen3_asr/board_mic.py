"""板载麦克风采集进程的管理。

为什么要**独立进程**
==================

``snd_pcm_readi`` 是**阻塞 I/O** —— 读满一个 period 才返回。放进
webui 进程会把线程占死，整个 Web 服务卡住。声卡还是**独占**的：采集
进程占着的时候，webui 不该再碰它。

所以板载采集跑在**独立进程**里（``tools/asr_mic_agent.py``），它自己
开实时会话、自己推流、自己收尾 —— 与浏览器采集走**同一套 HTTP 接口**
（``/live/start`` → ``/live/chunk`` → ``/live/finish``），服务端不关心
音频从哪来。

这个模块只做一件事：**把这个子进程管起来**（起 / 停 / 看状态）。

生命周期
========

* :meth:`BoardMicController.start` —— spawn 子进程（``start_new_session``
  → 独立进程组，便于整组收）。
* :meth:`BoardMicController.stop` —— **按 PID** 发 SIGTERM（优雅收尾：
  agent 收到信号后会走一次 ``/live/finish``），超时再 SIGKILL。
  ⚠️ 绝不用 ``pkill -f`` —— pattern 会出现在自己的命令行里 → 自杀。
* :meth:`BoardMicController.shutdown` —— 服务退出时兜底收尾（atexit）。

自愈
====

* 服务挂了 → agent 的 HTTP 调用失败 → 它自己退出（不会变孤儿）；
* agent 挂了（ALSA 设备被占等）→ :meth:`status` 报 ``running=False``
  并带上退出码，UI 显示错误。
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.audio.alsa import (default_capture_device,
                                 list_capture_devices)

_LOG = logging.getLogger("qwen3_asr.board_mic")

#: 采集进程的日志目录（stdout / stderr 落这里，不走 PIPE ——
#: PIPE 在 parent 死掉时读端关闭 → agent 写 stderr 收到 SIGPIPE →
#: 直接死，错误信息也丢了）。
LOG_DIR = Path("/tmp/wdnwebui-board-mic")

#: 默认采集设备（ES8323 板载麦）。
DEFAULT_DEVICE = "hw:1,0"

#: 停止时先给 SIGTERM，等这么久还没死就 SIGKILL。
#:
#: agent 收到信号后要走一次 ``/live/finish`` —— 那次请求会触发**最后
#: 一次推理**（final=True），实测 0.5~3 s。5 s 太紧（会被 SIGKILL，
#: ``/live/finish`` 就丢了），所以给 8 s。
#:
#: ⚠️ 这段等待**不能**发生在事件循环线程里 —— 见
#: ``api.py::post_board_stop`` 的注释（死锁）。
_STOP_GRACE_SEC = 8.0


class BoardMicError(RuntimeError):
    """板载麦克风相关错误（HTTP 层转 400 / 409 / 502）。"""


def _find_agent() -> Optional[Path]:
    """找采集脚本 ``tools/asr_mic_agent.py``。

    从本文件所在目录**逐级向上**找（而不是写死层数）—— 目录结构一改
    （比如打成 wheel）也不会找不到。
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "tools" / "asr_mic_agent.py"
        if cand.is_file():
            return cand
    return None


class BoardMicController:
    """板载采集子进程的管理者（进程内单例）。"""

    def __init__(self, *, base_url: str = "http://127.0.0.1:8000",
                 agent_path: Optional[Path] = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._agent_path = agent_path or _find_agent()
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._device: str = ""
        self._started_at: float = 0.0
        self._log_path: str = ""
        self._last_exit: Optional[int] = None

    # ------------------------------------------------------------------ 设备
    def devices(self) -> List[Dict[str, Any]]:
        """本机可用的采集设备（供 UI 下拉）。"""
        out = []
        for d in list_capture_devices():
            out.append({
                "name": d.name,
                "description": d.description,
                "default": d.name == DEFAULT_DEVICE,
            })
        return out

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        """当前采集进程的状态。"""
        with self._lock:
            proc = self._proc
            if proc is None:
                # 没在跑：仍然报出**上次**用的设备与日志路径 —— UI 要能
                # 告诉用户「日志在哪儿」，而不是「一切归零」。
                return {
                    "running": False,
                    "device": self._device,
                    "pid": 0,
                    "log": self._log_path,
                    "last_exit": self._last_exit,
                    "default_device": DEFAULT_DEVICE,
                }
            rc = proc.poll()
            if rc is not None:
                # 进程已经死了 → 记下退出码，清掉句柄
                self._last_exit = rc
                self._proc = None
                return {
                    "running": False,
                    "device": self._device,
                    "pid": proc.pid,
                    "log": self._log_path,
                    "last_exit": rc,
                    "default_device": DEFAULT_DEVICE,
                }
            return {
                "running": True,
                "device": self._device,
                "pid": proc.pid,
                "log": self._log_path,
                "last_exit": None,
                "started_at": self._started_at,
                "default_device": DEFAULT_DEVICE,
            }

    # ------------------------------------------------------------------ 起停
    def start(self, device: str = "") -> Dict[str, Any]:
        """起一个板载采集进程。返回状态字典。"""
        device = (device or "").strip() or default_capture_device() \
            or DEFAULT_DEVICE
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                if self._device == device:
                    return self._status_locked()
                raise BoardMicError(
                    "板载麦克风已在运行（设备 {}）；先停止再换设备".format(
                        self._device))
            if not self._agent_path.is_file():
                raise BoardMicError(
                    "找不到采集脚本: {}".format(self._agent_path))

            log_dir = LOG_DIR
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                log_dir = Path("/tmp")
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self._log_path = str(log_dir / "board-mic-{}.log".format(stamp))
            try:
                log_fp = open(self._log_path, "ab", buffering=0)
            except OSError:
                log_fp = open(os.devnull, "ab")
            cmd = [
                sys.executable, str(self._agent_path),
                "--device", device,
                "--url", self._base_url,
            ]
            _LOG.info("spawn board mic: %s", " ".join(cmd))
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_fp,
                    stderr=log_fp,
                    stdin=subprocess.DEVNULL,
                    # 独立进程组 → 停止时可以整组收
                    start_new_session=True,
                )
            except Exception as e:  # noqa: BLE001
                raise BoardMicError(
                    "起板载采集进程失败: {}".format(e)) from e
            self._proc = proc
            self._device = device
            self._started_at = time.time()
            self._last_exit = None
            return self._status_locked()

    def stop(self) -> Dict[str, Any]:
        """停掉板载采集进程（优雅：先 SIGTERM，等它自己收尾）。

        ⚠️ **同步阻塞**（最多 ``_STOP_GRACE_SEC``）—— 只能在线程里调，
        别直接写在 ``async def`` 端点里（会堵死事件循环，见
        ``api.py::post_board_stop`` 的注释）。
        """
        with self._lock:
            proc = self._proc
            if proc is None:
                return {"ok": True, "running": False, "stopped": False}
            pid = proc.pid
            killed = False
            if proc.poll() is None:
                _LOG.info("stop board mic pid=%s", pid)
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError as e:
                    _LOG.warning("SIGTERM pid=%s failed: %s", pid, e)
                deadline = time.time() + _STOP_GRACE_SEC
                while time.time() < deadline and proc.poll() is None:
                    time.sleep(0.1)
                if proc.poll() is None:
                    killed = True
                    _LOG.warning("board mic pid=%s 不响应 SIGTERM，SIGKILL", pid)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:  # noqa: BLE001
                        pass
            rc = proc.poll()
            self._last_exit = rc
            self._proc = None
            # ⚠️ ``rc is None`` 说明连 ``wait`` 都没拿到退出码 —— 进程
            # 可能已经变成僵尸（父进程还没 wait）。如实报告，别装成成功。
            return {
                "ok": True,
                "running": False,
                "stopped": True,
                "pid": pid,
                "device": self._device,
                "log": self._log_path,
                "last_exit": rc,
                "killed": killed,
            }

    def shutdown(self) -> None:
        """服务退出时的兜底（atexit）。"""
        try:
            self.stop()
        except Exception as e:  # noqa: BLE001 — 退出路径不再抛
            _LOG.debug("board mic shutdown failed: %s", e)

    # ------------------------------------------------------------------ 内部
    def _status_locked(self) -> Dict[str, Any]:
        proc = self._proc
        return {
            "running": bool(proc is not None and proc.poll() is None),
            "device": self._device,
            "pid": proc.pid if proc is not None else 0,
            "log": self._log_path,
            "last_exit": self._last_exit,
            "started_at": self._started_at,
            "default_device": DEFAULT_DEVICE,
        }


_CONTROLLER: Optional[BoardMicController] = None
_CONTROLLER_LOCK = threading.Lock()


def get_board_mic() -> BoardMicController:
    """进程内单例。"""
    global _CONTROLLER
    with _CONTROLLER_LOCK:
        if _CONTROLLER is None:
            _CONTROLLER = BoardMicController()
            atexit.register(_CONTROLLER.shutdown)
        return _CONTROLLER
