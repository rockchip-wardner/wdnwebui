"""System pages (not plugin-driven).

System pages (system_monitor / base_config / compute_config / discovery /
im_bridge / system_logs / power / coprocessor / versions) are wired by
hand in :func:`register_all`. They do NOT participate in the manifest
plugin mechanism — they're stable, board-level UI that doesn't follow the
"drop a card folder to add a card" workflow.

Cards (apps/<id>/) are off-limits to system code by the zero-coupling rule
(likewise the reverse: cards cannot import from app.system).
"""
from __future__ import annotations

from fastapi import FastAPI


def register_all(app: FastAPI) -> None:
    """Mount every system router under ``/api``.

    Equivalent to the old ``app.api.__init__`` import-list. Each system
    module exposes a ``router`` object that gets included here.
    """
    from app.system.api import (
        compute,
        config,
        coprocessor,
        fs_browser,
        im_bridge,
        power,
        system_logs,
        system_monitor,
        versions,
    )
    app.include_router(system_monitor.router, prefix="/api")
    app.include_router(system_logs.router,    prefix="/api")
    app.include_router(compute.router,        prefix="/api")
    app.include_router(config.router,         prefix="/api")
    app.include_router(power.router,          prefix="/api")
    app.include_router(coprocessor.router,    prefix="/api")
    app.include_router(versions.router,       prefix="/api")
    # 设备端文件浏览器(v0.4 后从 video_detection 抽出,所有需要"浏览设备文件"
    # 的卡片共用):前端走 /static/core/fs-modal.js 渲染模态 + /api/fs 取数据。
    app.include_router(fs_browser.router,     prefix="/api")
    # v0.4 重构后整理:im-bridge(应用对接 / 微信机器人)是单一页面,
    # 不再按"应用卡片"加载,改为系统配置页,与 system-monitor /
    # base-config / compute-config 同级。前端通过侧栏 "应用对接" 进入,
    # 不再出现在首页应用网格中。
    # 路由前缀沿用 ``/api/wechat`` —— 前端 app.js 里的 fetch URL
    # (如 ``API_PREFIX + "/wechat/users"``)与历史一致,避免改静态资源。
    app.include_router(im_bridge.router,      prefix="/api/wechat")
    # video_search @Callable 工具归 im-bridge 所有(零耦合),随系统模块
    # 一同注册到 tool registry。原本在 ``apps/im_bridge/__init__.py``
    # 的 register_api 里,卡→系统迁移时一起搬过来。
    try:
        from app.core.tools.registry import get_tool_registry
        from app.system.im_bridge.video_search import VideoSearch
        get_tool_registry().register(
            key="video_search",
            name="视频内容检索",
            description=(
                "在用户最新发送的视频中按文字描述检索相关片段,"
                "返回命中时间段与相似度;依赖视频已完成后台分析"
                "(收到视频后自动进行)。"
            ),
            requires_capability="swcap_video_retrieval",
            tool_class=VideoSearch,
            icon="🎬",
        )
    except Exception as e:
        import logging
        logging.getLogger("app.system").warning(
            "im_bridge: video_search registration failed: %s", e
        )
