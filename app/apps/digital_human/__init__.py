"""数字人卡片（``digital-human``）。

内置卡（``app/apps/<id>/``）—— v0.26 起宿主仓的内置卡目录清空了，
text_chat 是最后一张内置卡；**下一张内置卡直接放这里**（见
``scripts/card_dirs.py`` 的说明）。

这张卡做的事
============

把一段语音变成**会说话的形象**：

#. 把音频（裸 PCM）交给**算力页上的「数字人」算力实例**；
#. 那个实例（compute worker 进程）用板载 RK3588 NPU 跑完整条管线：
   fbank 频谱 → Paraformer encoder → 嘴型参数 → 逐帧解码 → 贴回背景；
#. 卡片把回来的帧序列写盘，前端按帧播放。

⚠️ **模型生命周期归算力层** —— 卡片不再自己拉引擎进程、不再自己加载
模型。要用它，先到「算力」页启用「[AV]数字人 LiteAvatar 渲染」。

⚠️ **零 torch** —— 参考帧编码器与 AdaIN 调制投影都是 numpy / OpenCV
等价实现（见 ``wdnai.avatar``），decoder 在板载 NPU 上。

目录结构::

    cards/digital_human/
    ├── manifest.json      # 卡片声明（id / 标题 / 展示顺序 / 静态资源）
    ├── api.py             # FastAPI 路由（挂在 /api/digital-human）
    ├── service.py         # 业务逻辑（作业编排 + 算力实例消费）
    ├── avatars.py         # 形象库（素材校验 / 按需下载 / 预览图）
    ├── regen_ref_frames.py# 维护工具：从背景视频重裁参考帧
    ├── static/            # template.html + app.js + app.css
    └── __init__.py        # register_api / register_shutdown
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.apps.digital_human.api import router

LOG = logging.getLogger("apps.digital_human")


def register_api(app: FastAPI) -> None:
    """把卡片路由挂在 ``/api/digital-human``。"""
    app.include_router(router, prefix="/api/digital-human")
    LOG.info("digital_human: API mounted at /api/digital-human")
    # ⚠️ **不再预热** —— 模型在算力层的 worker 进程里，卡片这边没有东西
    # 可热（worker 首次 render 时自己懒加载 fbank / encoder / 嘴型 / 渲染器）。
    # 用户要的是「到算力页启用那张卡」，不是「卡片偷偷把模型拉起来」。


def register_shutdown() -> None:
    """释放卡片持有的资源（引擎进程 + NPU 上下文）。"""
    try:
        from app.apps.digital_human import service as _service_mod
    except Exception:  # noqa: BLE001
        return
    for attr in ("close", "stop", "shutdown", "close_all"):
        fn = getattr(_service_mod, attr, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                LOG.warning("digital_human: %s.%s 失败", _service_mod.__name__, attr)
            break
