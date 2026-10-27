"""微信对接文本对话模型桥接。

开启开关后，微信用户发来的文字消息自动进入一个串行 worker 队列，桥接
调用 ``swcap_text_chat``（qwen3.5-vl-4b）的 ``respond`` 推理，把回答通过
``Wechat.send_text`` 发回给该微信用户。

设计要点
========

* **开关内存态**：``_enabled`` 只存内存，不落盘（重启即关）；开关可开性
  由 ``available()`` 决定 —— 对话模型未加载时无法开启（后端校验兜底）。
* **per-user 上下文隔离**：``self.histories: dict[user_id, deque]`` 各自
  独立，与 web 文本对话的全局历史完全无关。上下文裁剪由
  ``chat_agent`` 统一处理。
* **串行保证**：桥接 worker 线程串行消费队列；``respond`` 用
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
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from wdnai import SoftwareCapability

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
    """微信文字消息 → swcap_text_chat → 自动回复 的串行桥接。"""

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
        # 图片检索独立 worker：下载 / 质量检查 / embedding 可能耗时，
        # 不能阻塞 SDK 消息回调，也不该和文字对话抢同一条队列。
        self._image_queue: "_queue.Queue[Optional[tuple]]" = _queue.Queue()
        self._image_worker = threading.Thread(
            target=self._image_worker_loop, name="wechat-llm-image",
            daemon=True,
        )
        self._image_worker.start()

    # ------------------------------------------------------------- 状态
    def available(self) -> bool:
        """对话模型是否已加载、可对话（用于前端置灰 + 后端开启校验）。"""
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
                "文本对话模型未启用（请在算力配置页启用 "
                "[VLLM]视觉文字多模态v3.5）"
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
            # 引用消息（微信协议只给被引用消息的 msg_id，不给内容/媒体）
            # 无法区分引用的是图片还是文字，因此一律按普通文字处理；
            # 是否“基于最近一张图片追问”由模型调用
            # ``VideoSearch(use_last_image=true)`` 自行判断。
            self._queue.put((user_id, text, time.time()))
            self._wake.set()
        elif kind == "image":
            # 图片消息：下载/质量检查/embedding 全部在 image worker 做。
            # 微信客户端不支持图片 + 文字同发，caption 基本恒为空；
            # 后续文字由模型决定是否 use_last_image（不做 TTL 自动绑定）。
            caption = self._image_caption(msg)
            self._image_queue.put((user_id, msg, caption))
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
            self._reply_no_context(
                user_id,
                "当前仅支持文字、图片与视频消息（语音暂不支持）。")

    @staticmethod
    def _image_caption(msg: Any) -> str:
        """图片消息自带的文字（微信把 caption 作为同消息 text_item）。

        SDK 的 ``_extract_text`` 会把图片 URL 也拼进 ``msg.text``，这里
        剔除 URL / ``[image]`` 后剩下的才是真正的描述文字。
        """
        text = (getattr(msg, "text", "") or "").strip()
        for im in (getattr(msg, "images", None) or []):
            url = getattr(im, "url", None)
            if url:
                text = text.replace(url, "")
        return text.replace("[image]", "").strip()

    def _dispatch_to_video_manager(self, user_id: str, msg: Any) -> None:
        """video 消息与 file 形态的共用分流：capability 预检 → 入队提取
        → 注入系统通知。

        预检失败（emb-2b 未加载）时整体不可用 —— 不入队（加载后需重发），
        回固定文案并注入系统通知，让 LLM 对话中能准确解释原因而非含糊
        其辞。"""
        try:
            from app.core.compute.registry import get_registry
            get_registry().get_session(SoftwareCapability.VISION_EMBEDDING)
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

    def _process(self, user_id: str, text: str,
                 system_note: str = "", tool_names=None) -> None:
        from app.core.chat.agent import run_chat_with_tools
        from app.core.chat.serial import get_chat_run_lock
        from app.core.compute.registry import get_registry

        try:
            session = get_registry().get_session("swcap_text_chat")
        except RuntimeError as e:
            self._reply_no_context(user_id, f"功能未启用: swcap_text_chat（{e}）")
            return

        # 读取该用户历史（惰性建 deque）。图片检索结果先作为系统注记
        # 写入 history（供本轮回答 + 后续追问引用），再取快照。
        with self._lock:
            hist = self.histories.setdefault(user_id, deque(maxlen=_HISTORY_MAX))
            if system_note:
                hist.append({"role": "user", "content": system_note,
                             "ts": time.time(), "_system_note": True})
            history_snapshot = list(hist)

        context_limit = self._resolve_context_limit()

        # 上下文裁剪用 worker 的 tokenizer 精确计数（一次无推理的
        # count_tokens 控制调用），ChatML 由 wdnai 拼；工具执行与
        # wrap-up / 回喂 / 失败告知循环由 chat_agent 完成。
        try:
            with get_chat_run_lock():
                agent = run_chat_with_tools(
                    session,
                    history_snapshot,
                    text,
                    context_limit=context_limit,
                    max_new_tokens=self.MAX_NEW_TOKENS,
                    tool_names=(self._resolve_tool_names()
                                if tool_names is None else tool_names),
                    user_id=user_id,
                    channel="wechat",
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("chat turn failed for %s", user_id)
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

    # ------------------------------------------------------------- 图片检索
    def _image_worker_loop(self) -> None:
        """图片下载 / 质量检查 / 检索 / 组织回答（串行，独立于文字队列）。"""
        while True:
            item = self._image_queue.get()
            if item is None:
                break
            try:
                user_id, msg, text = item
                self._process_image(user_id, msg, text)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("wechat image worker failed")
                _llm_log(f"image worker error: {exc!r}")

    def _process_image(self, user_id: str, msg: Any, text: str) -> None:
        """下载图片 → 质量检查 → 检索 → 文本模型组织回答。"""
        from app.system.im_bridge.service_wechat import get_wechat
        from app.system.im_bridge.video_manager import get_wechat_video_manager

        caption = (text or "").strip()
        wc = get_wechat()
        mgr = get_wechat_video_manager()

        try:
            downloaded = wc.download_message(msg)
            if downloaded is None or not getattr(downloaded, "data", None):
                self._reply_no_context(
                    user_id,
                    "图片下载失败（未收到图片数据），请重新发送。")
                return
            data = downloaded.data
        except Exception as exc:  # noqa: BLE001
            _llm_log(f"image download failed for {user_id}: {exc!r}")
            self._reply_no_context(user_id, f"图片下载失败：{exc}")
            return

        path = ""
        try:
            path = self._save_image(mgr.image_dir, user_id, data)
            # 质量检查在 encode_query 内部（原图）完成；纯色/噪声抛 ValueError。
            result = mgr.search(user_id, caption, image_path=path)
        except ValueError as exc:
            # 无视频 / 图片质量不合格 —— 直接给用户原因，不进入 LLM。
            self._reply_no_context(user_id, str(exc))
            self._discard(path)
            return
        except Exception as exc:  # noqa: BLE001
            _llm_log(f"image search failed for {user_id}: {exc!r}")
            self._reply_no_context(user_id, f"图片检索失败：{exc}")
            self._discard(path)
            return

        # 检索成功 → 保留为「最近一张」（新图覆盖旧图）。
        mgr.set_last_image(user_id, path, caption)

        if not self.available():
            self._reply_no_context(
                user_id,
                "文本对话模型未启用，无法总结检索结果。原始命中：\n"
                + self._format_hits(result))
            return

        user_text = caption or "[用户发送了一张图片]"
        note = self._image_result_note(result, caption)
        with self._lock:
            self._busy = True
            self._active_sessions += 1
        try:
            self._process(user_id, user_text, system_note=note,
                          tool_names=[])
        except Exception as exc:  # noqa: BLE001
            _LOG.exception("wechat image answer failed for %s", user_id)
            self._reply_no_context(
                user_id,
                f"检索完成，但回答生成失败：{exc}\n" + self._format_hits(result))
        finally:
            with self._lock:
                self._busy = False
                self._active_sessions -= 1

    @staticmethod
    def _save_image(root: Path, user_id: str, data: bytes) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_id or "user")[:40]
        d = Path(root) / safe
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"image-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.jpg"
        p.write_bytes(data)
        return str(p)

    @staticmethod
    def _discard(path: str) -> None:
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _format_hits(result: dict) -> str:
        hits = result.get("hits") or []
        if not hits:
            return "（没有命中片段）"
        lines = [f"{h.get('time', '')}（相似度 {h.get('similarity')}）"
                 for h in hits]
        if result.get("all_below_threshold"):
            lines.insert(0, "所有片段相似度均低于阈值，以下是最接近的片段：")
        return "\n".join(lines)

    @staticmethod
    def _image_result_note(result: dict, caption: str) -> str:
        qtype = {"image": "图片", "image+text": "图片+文字"}.get(
            result.get("query_type") or "", "图片")
        what = "用户发送了一张图片"
        if caption:
            what += f"（附带文字：{caption}）"
        return (
            f"[系统] {what}，已用{qtype}检索其最新视频"
            f"「{result.get('video', '')}」。命中片段：\n"
            f"{_LlmBridge._format_hits(result)}\n"
            "请用自然语言告诉用户这些片段与图片/描述的关系；"
            "若全部低于阈值，请明确说明只是最接近的片段。"
            "本轮结果已给出，不要再调用 VideoSearch 工具。\n"
            "注意：用户之后可能继续用文字追问这张图片。若下一条文字是"
            "在描述/追问这张图（例如颜色、车型、画面里还有谁），调用 "
            "VideoSearch 时把 use_last_image 设为 true；若是独立的新问题，"
            "按普通文字检索，不要强行关联这张图。")

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
        self._image_queue.put(None)
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
