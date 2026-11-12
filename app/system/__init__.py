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
        apps,
        compute,
        config,
        coprocessor,
        fs_browser,
        im_bridge,
        model_download,
        power,
        system_logs,
        system_monitor,
        translate,
        versions,
    )
    app.include_router(system_monitor.router, prefix="/api")
    app.include_router(system_logs.router,    prefix="/api")
    app.include_router(compute.router,        prefix="/api")
    app.include_router(model_download.router, prefix="/api")
    app.include_router(config.router,         prefix="/api")
    app.include_router(power.router,          prefix="/api")
    app.include_router(coprocessor.router,    prefix="/api")
    app.include_router(versions.router,       prefix="/api")
    # 界面翻译代理（右上角语言切换）。前端不直连任何翻译站点 —— 浏览器在
    # 局域网里访问不了 Google 系域名，翻译请求统一转发到板子，由板子去够
    # 真正干活的 provider（默认 MyMemory，见 app/core/translate/）。
    app.include_router(translate.router,      prefix="/api")
    # 应用包安装（首页应用网格末尾那个"添加应用"磁位拖 wheel 到这里）。
    # ⚠️ 装包 = 在本机执行任意代码，本端点无鉴权、信任局域网。
    app.include_router(apps.router,           prefix="/api")
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
    #
    # 走 ``app.core.tools._builtins.register_builtin_tools()`` —— 与
    # compute_worker 子进程共用同一份"真源",避免 worker 端
    # ``chat_agent._tool_classes(['VideoSearch'])`` 查不到类 → prompt
    # 缺工具 schema → 模型 hallucinate "假装在调用" 的破事
    # (issue: 应用对接 / im-bridge 视频检索在 c219e71 后输出正常 →
    # v0.4 重构后仅返回 "我将模拟调用工具" 的假答案)。
    try:
        from app.core.tools.registry import get_tool_registry
        from app.core.tools._builtins import register_builtin_tools
        register_builtin_tools(get_tool_registry())
    except Exception as e:
        import logging
        logging.getLogger("app.system").warning(
            "im_bridge: video_search registration failed: %s", e
        )

    # 微信图片检索缓存：服务启动即清空（不跨重启保留）。这里显式调一次，
    # 而不是依赖 video_manager 单例的 lazy 初始化 —— 否则用户没进过
    # im-bridge 页面时，上一轮运行留下的图片会一直躺在磁盘上。
    try:
        from app.system.im_bridge.video_manager import clear_image_cache
        clear_image_cache()
    except Exception as e:
        import logging
        logging.getLogger("app.system").warning(
            "im_bridge: clear image cache failed: %s", e
        )
