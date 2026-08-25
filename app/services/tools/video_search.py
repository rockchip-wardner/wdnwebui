"""``VideoSearch`` —— 微信视频内容检索工具（结果型 @Callable）。

场景：用户通过微信发送视频后（``wechat_video_manager`` 自动下载并后台
切片 embedding），在对话中提问"视频里有消防员吗"→ 模型调用本工具 →
对该用户最新的已分析视频做语义检索 → 结果回喂，模型结合命中时间段
回答。

**四态返回**（统一结果型 dict，不带 ``consume_iteration`` —— 状态本身
是合法的业务查询结果而非错误，让模型自由组织回复）：

* ``ok``          — 命中片段列表（含相似度与阈值标记）
* ``extracting``  — 仍在提取中（附实时进度）
* ``downloading`` — 视频还在下载
* ``no_video``    — 该用户从未发送过视频

真正的执行失败（embedding 会话缺失、检索异常）以抛错结束 —— 走
chat-agent 的失败回喂路径。capability 门控
（``requires_capability="swcap_video_retrieval"``）由注册表声明 +
chat_agent 前置校验双层保证。
"""
from __future__ import annotations

from typing import Annotated

from wdnai import Callable as WdnaiCallable
from wdnai import Field as WdnaiField


@WdnaiCallable
class VideoSearch:
    """在用户最新发送的视频中按文字描述检索相关片段。"""

    query: Annotated[str, "要检索的内容描述，如'有人摔倒'、'海边日落'"] = WdnaiField(
        default="",
        description="要检索的内容描述",
    )
    top_k: Annotated[int, "最多返回的片段数（1-10）"] = WdnaiField(
        default=5,
        description="最多返回的片段数",
    )

    def __call__(self, frame_bgr, ctx):
        user_id = getattr(ctx, "user_id", "") or ""
        if not user_id:
            return {
                "status": "no_video",
                "hint": "当前对话渠道没有关联的用户视频；请该用户通过微信"
                        "发送视频后再试。",
            }

        from app.services.wechat_video_manager import get_wechat_video_manager
        mgr = get_wechat_video_manager()
        status = mgr.entry_status(user_id)
        if status is None:
            return {
                "status": "no_video",
                "hint": "该用户还没有发送过任何视频，无法进行内容检索。",
            }
        if status["state"] in ("downloading", "extracting"):
            done = status["slices_embedded"]
            total = status["slices_total"]
            return {
                "status": status["state"],
                "video": status["filename"],
                "progress": status["progress"],
                "segments_done": f"{done}/{total}" if total else "",
                "hint": f"视频 {status['filename']} 正在分析中"
                        f"（已完成 {int(status['progress'] * 100)}%），"
                        "请稍候再问。",
            }
        if status["state"] == "error":
            return {
                "status": "error",
                "video": status["filename"],
                "hint": f"视频 {status['filename']} 分析失败"
                        f"（{status['error']}），可请用户重新发送。",
            }
        # state == ready → 执行检索（内部 threshold 固定 + 兜底质量信号）
        result = mgr.search(user_id, self.query, top_k=self.top_k)
        result["query"] = (self.query or "").strip()
        return result
