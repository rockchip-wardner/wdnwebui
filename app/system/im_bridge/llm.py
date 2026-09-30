"""微信对接文本对话模型桥接。

开启开关后，微信用户发来的文字消息自动进入一个串行 worker 队列，桥接
调用 ``swcap_text_chat``（qwen3-4b）的 ``chat_run`` 推理，把回答通过
``Wechat.send_text`` 发回给该微信用户。

设计要点
========

* **开关内存态**：``_enabled`` 只存内存，不落盘（重启即关）；开关可开性
  由 ``available()`` 决定 —— qwen3-4b 未加载时无法开启（后端校验兜底）。
* **per-user 上下文隔离**：``self.histories: dict[user_id, deque]`` 各自
  独立，与 web 文本对话的全局历史完全无关。上下文裁剪复用
  ``chat_runner.trim_history``。
* **串行保证**：桥接 worker 线程串行消费队列；``chat_run`` 用
  ``chat_serial.get_chat_run_lock()`` 与 text_chat worker 互斥。
* **只回 answer 不回 thinking**：最终轮输出已是面向用户的自然语言
  （正常回答 / wrap-up 确认语 / 失败回喂后的解释），直接发微信。
* **发送复用 wechat_bot**：回复走 ``Wechat.send_text``，自动出现在"调试
  收发"消息列表（out 方向），与手动发送共用同一发送通道、线程安全。
"""
from __future__ import annotations

import logging
import os
import queue as _queue
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from app.core.tools.registry import resolve_available_tool_names

_LOG = logging.getLogger("wechat_llm")

_HISTORY_MAX = 200

# 视频格式白名单（与视频监控页目录扫描清单同源）：微信"文件"形态发送
# 的 mp4/mov 等同样分流到视频分析管线，只有真正的文档类文件才拒绝。
_VIDEO_FILE_EXTS = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".ts", ".m4v", ".3gp",
    ".wmv", ".flv", ".mpg", ".mpeg", ".rmvb", ".h264", ".h265", ".hevc",
    ".m2ts", ".mts",
}


def _llm_log(msg: str) -> None:
    print(f"[wechat-llm] {msg}", flush=True)


class _LlmBridge:
    """微信文字消息 → qwen3-4b → 自动回复 的串行桥接。"""

    MAX_NEW_TOKENS = 2048

    def __init__(self) -> None:
        self._enabled = False
        self._lock = threading.RLock()
        self.histories: Dict[str, Deque[dict]] = {}
        self._queue: "_queue.Queue[Optional[tuple]]" = _queue.Queue()
        self._wake = threading.Event()
        self._busy = False
        self._active_sessions = 0
        self._worker = threading.Thread(
            target=self._worker_loop, name="wechat-llm-bridge", daemon=True,
        )
        self._worker.start()

    # ------------------------------------------------------------- 状态
    def available(self) -> bool:
        """qwen3-4b 是否已加载、可对话（用于前端置灰 + 后端开启校验）。"""
        try:
            from app.core.compute.registry import get_registry
            get_registry().get_session("swcap_text_chat")
            return True
        except RuntimeError:
            return False

    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def active_sessions(self) -> int:
        with self._lock:
            return self._active_sessions

    def snapshot(self) -> dict:
        return {
            "enabled": self.enabled(),
            "available": self.available(),
            "busy": self.busy(),
            "active_sessions": self.active_sessions(),
        }

    # ------------------------------------------------------------- 开关
    def set_enabled(self, on: bool) -> dict:
        if on and not self.available():
            raise RuntimeError(
                "文本对话模型未启用（请在算力配置页启用 [LLM]文字对话v3 "
                "(qwen3-4b)）"
            )
        with self._lock:
            self._enabled = bool(on)
            if not self._enabled:
                self.histories.clear()
        _llm_log(f"llm bridge enabled={self._enabled}")
        return self.snapshot()

    # ------------------------------------------------------------- 入口
    def handle_incoming(self, msg: Any) -> None:
        """由 wechat 的消息处理器调用；开关开启时处理文字消息。"""
        if not self.enabled():
            return
        kind = getattr(msg, "type", None)
        text = (getattr(msg, "text", "") or "").strip()
        user_id = getattr(msg, "user_id", "") or ""
        if not user_id:
            return
        if kind == "text" and text:
            self._queue.put((user_id, text, time.time()))
            self._wake.set()
        elif kind == "video":
            # 视频消息 → 微信视频管理器确定性分流：落盘 + 自动后台提取。
            # 不走 LLM 工具链路（提取是确定性事件无需智能决策，且不能
            # 让 CDN 下载阻塞消息回调线程 —— enqueue 纯内存入队毫秒级）。
            self._dispatch_to_video_manager(user_id, msg)
        elif kind == "file" and msg.files:
            # 以"文件"形式发送的视频（mp4/mov/...）同样进分析管线 ——
            # SDK download() 对 file 类型完全支持（返回原始 file_name）。
            fname = getattr(msg.files[0], "file_name", "") or ""
            ext = os.path.splitext(fname)[1].lower()
            if ext in _VIDEO_FILE_EXTS:
                self._dispatch_to_video_manager(user_id, msg)
            else:
                self._reply_no_context(
                    user_id,
                    f"暂不支持「{fname}」这类文件；请发送文字、视频消息，"
                    f"或视频格式的文件（如 mp4 / mov / mkv）。")
        else:
            self._reply_no_context(user_id, "当前仅支持文字与视频消息（图片 / 语音暂不支持）。")

    def _dispatch_to_video_manager(self, user_id: str, msg: Any) -> None:
        """video 消息与 file 形态的共用分流：capability 预检 → 入队提取
        → 注入系统通知。

        预检失败（emb-2b 未加载）时整体不可用 —— 不入队（加载后需重发），
        回固定文案并注入系统通知，让 LLM 对话中能准确解释原因而非含糊
        其辞。"""
        try:
            from app.core.compute.registry import get_registry
            get_registry().get_session("swcap_video_retrieval")
        except Exception as exc:  # noqa: BLE001
            _LOG.info("wechat video skipped for %s (model disabled): %s",
                      user_id, exc)
            self._reply_no_context(
                user_id,
                "视频检索模型功能未被启用，需在算力配置页中加载启用后重新发送")
            try:
                self._push_model_disabled_notice(user_id)
            except Exception as exc2:  # noqa: BLE001
                _LOG.exception(
                    "wechat model-disabled notice failed for %s: %r",
                    user_id, exc2)
            return
        from app.system.im_bridge.video_manager import (
            get_wechat_video_manager,
        )
        try:
            info = get_wechat_video_manager().enqueue_video(user_id, msg)
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("wechat video enqueue failed for %s", user_id)
            self._reply_no_context(user_id, f"视频接收失败：{exc}")
            return
        try:
            self._push_video_notice(user_id, info.get("filename") or "")
        except Exception as exc:  # noqa: BLE001
            _LOG.exception(
                "wechat video notice injection failed for %s", user_id)

    def _push_model_disabled_notice(self, user_id: str) -> None:
        """检索模型未启用时的对话历史通知。

        此场景下 VideoSearch 不在工具 schema 中（capability 过滤），
        模型没有可调用的工具 —— 若不注入本条目，它只能对用户的视频
        相关提问给出含糊的通用回答。注入后 LLM 能准确说明"功能当前
        不可用、如何启用"。语言随该用户最近输入自适应中英。
        """
        from app.core.chat.agent import _prefer_english
        with self._lock:
            hist = self.histories.setdefault(
                user_id, deque(maxlen=_HISTORY_MAX))
            last_user = ""
            for m in reversed(hist):
                if m.get("role") == "user" and not m.get("_system_note"):
                    last_user = m.get("content") or ""
                    break
            if _prefer_english(last_user):
                notice = (
                    "[System] The user just tried to send a video for "
                    "content search, but the retrieval model is currently "
                    "NOT enabled on this device, so the video cannot be "
                    "analyzed. If the user asks about video content, tell "
                    "them the feature is unavailable and they should have "
                    "an administrator load it on the compute-config page, "
                    "then re-send the video.")
            else:
                notice = (
                    "[系统] 用户刚刚尝试发送一个视频进行内容检索，但视频检索"
                    "模型（Qwen3-VL-Embedding）当前未被启用，视频无法被分析。"
                    "之后用户询问与视频内容相关的问题时，请说明该功能当前"
                    "不可用，建议用户联系管理员在设备的算力配置页中加载启用"
                    "后再重新发送视频。")
            hist.append({"role": "user", "content": notice,
                         "ts": time.time(), "_system_note": True})
        _LOG.info("wechat-llm: injected model-disabled notice for %s",
                  user_id)

    def _push_video_notice(self, user_id: str, filename: str) -> None:
        """新视频到达 → 注入系统通知条目，宣告旧视频/旧检索结果失效。

        若不注入，模型看到 history 里上一轮的检索结果会跳过工具调用
        直接复用旧答案（真机实测复现：换新视频后回答仍基于旧素材）。
        语言按该用户最近的输入自适应中英，与 chat_agent 的占位/回喂
        策略一致；条目随对话滚动被 trim 淘汰属可接受折中。
        """
        from app.core.chat.agent import _prefer_english
        with self._lock:
            hist = self.histories.setdefault(
                user_id, deque(maxlen=_HISTORY_MAX))
            last_user = ""
            for m in reversed(hist):
                if m.get("role") == "user" and not m.get("_system_note"):
                    last_user = m.get("content") or ""
                    break
            if _prefer_english(last_user):
                notice = (
                    f"[System] The user just sent a NEW video \"{filename}\"; "
                    f"it will be analyzed automatically in the background. "
                    f"The previous video and its retrieval results are now "
                    f"INVALID — for any video-related question, you MUST "
                    f"call the VideoSearch tool again instead of reusing "
                    f"old results from this conversation.")
            else:
                notice = (
                    f"[系统] 用户刚刚发送了一个新视频「{filename}」，系统将"
                    f"自动后台分析。此前的视频及其检索结果均已失效 —— 之后"
                    f"所有与视频内容相关的问题，必须重新调用 VideoSearch "
                    f"工具获取最新结果，不要沿用历史中的旧检索结果。")
            hist.append({"role": "user", "content": notice,
                         "ts": time.time(), "_system_note": True})
        _LOG.info("wechat-llm: injected new-video notice for %s (%s)",
                  user_id, filename)

    def _reply_no_context(self, user_id: str, text: str) -> None:
        try:
            from app.system.im_bridge.service_wechat import get_wechat
            get_wechat().send_text(user_id, text)
        except Exception as exc:  # noqa: BLE001
            _llm_log(f"reply error to {user_id}: {exc!r}")

    # ------------------------------------------------------------- worker
    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            user_id, text, ts = item
            with self._lock:
                self._busy = True
                self._active_sessions += 1
            try:
                self._process(user_id, text)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("wechat-llm _process failed: %s", exc)
                self._reply_no_context(user_id, f"处理失败：{exc}")
            finally:
                with self._lock:
                    self._busy = False
                    self._active_sessions -= 1

    def _process(self, user_id: str, text: str) -> None:
        from app.core.chat.agent import run_chat_with_tools
        from app.core.chat.serial import get_chat_run_lock
        from app.core.compute.registry import get_registry

        try:
            session = get_registry().get_session("swcap_text_chat")
        except RuntimeError as e:
            self._reply_no_context(user_id, f"功能未启用: swcap_text_chat（{e}）")
            return

        # 读取该用户历史（惰性建 deque）
        with self._lock:
            history_snapshot = list(
                self.histories.setdefault(user_id, deque(maxlen=_HISTORY_MAX)))

        context_limit = self._resolve_context_limit()

        # 裁剪 / ChatML 拼接由 worker 端 _chat_run_loop 完成（真实 tokenizer）；
        # 工具执行与 wrap-up / 回喂 / 失败告知循环由主进程侧 chat_agent 完成。
        try:
            with get_chat_run_lock():
                agent = run_chat_with_tools(
                    session,
                    history_snapshot,
                    text,
                    context_limit=context_limit,
                    max_new_tokens=self.MAX_NEW_TOKENS,
                    tool_names=self._resolve_tool_names(),
                    user_id=user_id,
                    channel="wechat",
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("chat_run failed for %s", user_id)
            self._reply_no_context(user_id, f"推理失败：{exc}")
            return

        if agent.get("interrupted"):
            return

        # 最终轮输出：正常回答 / wrap-up 确认语 / 失败回喂后的自然语言
        # 解释 —— 三种路径都已是面向用户的文本，直接发送。
        answer = (agent.get("answer") or "").strip()
        answer = answer or "（模型未输出有效内容）"

        steps = agent.get("steps") or []
        if steps:
            executed = "；".join(
                f"{s.get('tool_call')}({'成功' if not s.get('error') else '失败'})"
                for s in steps)
            _llm_log(f"agent-loop for {user_id}: {executed}")

        with self._lock:
            hist = self.histories.setdefault(user_id, deque(maxlen=_HISTORY_MAX))
            hist.append({"role": "user", "content": text, "ts": time.time()})
            appended = agent.get("history_append") or []
            for item in appended:
                hist.append(dict(item))
            if not appended and answer != "（模型未输出有效内容）":
                hist.append({
                    "role": "assistant", "content": answer,
                    "thinking": agent.get("thinking") or "",
                    "ts": time.time(),
                })

        self._reply_no_context(user_id, answer)

    def _resolve_context_limit(self) -> int:
        try:
            from app.core.config import get_config
            cfg = get_config().get_section("compute", {})
            if isinstance(cfg, dict):
                tc = cfg.get("text_chat")
                if isinstance(tc, dict):
                    v = int(tc.get("context_limit") or 16384)
                    if v >= 1:
                        return v
        except Exception:  # noqa: BLE001
            pass
        return 16384

    def _resolve_tool_names(self) -> list:
        """列出**当前可用**的 ``@Callable`` class 名（capability + 渠道过滤）。

        委托 ``resolve_available_tool_names(channel="chat_wechat")``：
        qwen3-tts 未加载时 tts_alert 直接从 schema 排除，模型看不到就
        调不到，避免必败调用；同时 ``channels`` 限定仅 ``chat_wechat``
        渠道生效的 ``VideoSearch`` 才会被注入 —— VideoSearch 依赖
        ``ctx.user_id`` 拉对应用户上传的视频索引,本渠道每个 ``user_id``
        都对应 ``WechatVideoManager`` 里的真实视频条目,功能闭环。
        web 文本对话（``channel="chat_web"``）拿不到 ``user_id``,所以
        那边只暴露 ``TtsAlert`` 不暴露 ``VideoSearch``,避免模型误调
        浪费一轮推理。
        """
        try:
            from app.core.tools.registry import resolve_available_tool_names
            return resolve_available_tool_names(channel="chat_wechat")
        except Exception as exc:  # noqa: BLE001
            _llm_log(f"resolve_tool_names failed: {exc!r}")
            return []

    def close(self) -> None:
        self._queue.put(None)
        with self._lock:
            self._enabled = False
            self.histories.clear()


_bridge: Optional[_LlmBridge] = None
_bridge_lock = threading.Lock()


def get_llm_bridge() -> _LlmBridge:
    """lazy singleton。"""
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = _LlmBridge()
        return _bridge
