"""应用层的「思考模式」总闸（Qwen3.5）。

为什么要有这个模块
==================

Qwen3.5 —— **4B 和 9B 都一样** —— 开思考有概率陷入**死循环**：贪心解码
一路复读、不吐 EOS，把 worker 的推理预算烧光。更糟的是 C 侧的 thinking
是「开了就锁住」的：一旦某轮开过，之后的 ``enable_thinking=False`` 会被
忽略，必须 ``reset()`` + 重设 chat template 才回得来。

一次这样的跑飞会把 NPU 占住 —— worker 的推理超时只是**弃用**推理线程，
并不打断它 —— 后面每个请求都排队等死，用户看到的现场就是「电脑控制老是
推理错误」。

所以应用层不指望每个调用点都记得传对标志，而是**直接把闸拉下来**。

一个开关，四个执行点
====================

:data:`THINKING_ENABLED` 是唯一的真源，它在四处被强制执行：

1. ``app/core/compute/proxy.py`` —— 主进程侧所有推理请求的咽喉
   （文本对话 / 电脑控制 / 音视频描述 / 微信桥接都从这里过）。它同时
   压住 **C 侧模式**（``set_thinking_mode``）和 **per-call 覆盖值**
   （``enable_thinking``）。
2. ``app/core/compute/workers/_base.py`` —— worker 侧兜底。直接对
   ``wdndds/rpc/<model>`` 发包的局域网 peer 同样开不了思考。
3. ``wdnapps_audio_chat.cards.text_chat.service`` —— 客户端切分。9B 的
   ``split_9b_thinking(…, enabled=True)`` 在模型**没有**产出 ``\\n\\n\\n``
   分界时会把**整段回答吞进 thinking**、让 answer 变空；客户端标志必须
   和实际模式对齐。

怎么把思考打开
==============

把下面的 :data:`THINKING_ENABLED` 改成 ``True`` 即可，per-call 开关会
端到端恢复，其它文件都不用动。
"""
from __future__ import annotations

#: 总闸。``False`` = 应用层永远不请求思考模式。
THINKING_ENABLED: bool = False


def effective_thinking(requested: bool = False) -> bool:
    """返回**实际生效**的思考模式，而不是调用方想要的那个。

    ``requested`` 是调用方要的（比如页面上的复选框），返回值是应用真正
    做的。总闸关着的时候，返回值恒为 ``False`` —— 调用方传 ``True`` 也
    拿不到思考模式。
    """
    return bool(THINKING_ENABLED) and bool(requested)
