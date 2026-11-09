"""FastAPI application factory — plugin-driven (v0.4)."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import parse_qs

# uvicorn's default LOGGING_CONFIG does not configure the root logger, so
# every ``LOG.info(...)`` from app.core.* / app.apps.* / app.system.* is
# silently swallowed without this. Keep this near the top of the module so
# it runs for both ``run.py`` and direct ``uvicorn app.main:app``.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware

from app import __version__
from app.core.config import get_config
from app.core.forwarded import XForwardedProtoMiddleware
from app.core.html_assembly import assemble_index_html
from app.core.manifest import discover as discover_cards
from app.core.compute.registry import get_registry

LOG = logging.getLogger("app.main")

STATIC_DIR = Path(__file__).parent / "static"


def _raise_nofile_limit() -> None:
    """把 soft ``RLIMIT_NOFILE`` 提到 hard 上限。

    放在 app 模块而非 ``run.py``：服务有三种入口（``python run.py`` /
    ``uvicorn app.main:app`` / systemd unit），挂在 run.py 上就漏掉后两种，
    而 ``sudo`` 还会把 soft 重置回 1024。1024 装不下多路视频 —— MPP 每路
    VideoCapture 约 17 个 fd，28 路就撞 ``ioctl alloc failed for Too many
    open files``。
    """
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < hard:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            LOG.info("RLIMIT_NOFILE soft %d -> %d", soft, hard)
        elif soft < 4096:
            # hard 也抬不动（少数容器 / 老 systemd 配置），点明症状来源，
            # 否则 28 路崩溃时很难联想到 fd。
            LOG.warning(
                "RLIMIT_NOFILE soft=hard=%d，多路视频（约 17 fd/路）"
                "在 30 路左右就会 Too many open files；"
                "请用 LimitNOFILE=524288 的 systemd unit 或 "
                "`ulimit -n 524288` 启动", soft,
            )
    except Exception as exc:  # noqa: BLE001
        LOG.warning("提升 RLIMIT_NOFILE 失败（忽略）: %s", exc)


_raise_nofile_limit()


def _prewarm_preview_posters() -> None:
    """后台预热卡片预览海报（失败只记日志，不影响启动）。"""
    try:
        from app.core.preview_poster import prewarm
        prewarm()
    except Exception as exc:           # noqa: BLE001
        LOG.warning("预热预览海报失败（忽略）: %s", exc)


class _NoCacheStaticFiles(StaticFiles):
    """StaticFiles with a **stamp-aware** ``Cache-Control`` policy.

    两种策略，按 URL 是否带版本戳（``?v=<mtime>``）区分：

    * **带 ``?v=``** → ``public, max-age=31536000, immutable``
      —— URL 本身已经包含了文件内容的"版本"，内容变 → mtime 变 → URL 变，
      所以可以放心让浏览器**长期缓存**，再也不回校验。
    * **不带 ``?v=``** → ``no-cache``
      —— 浏览器每次都带 ETag 回校验：未变 304、变了立即更新。
      卡片资源早先没有版本串，只能走这条。

    为什么值得分成两种
    ------------------
    早先这里一律 ``no-cache``，于是**每次刷新都要重下 42 个文件**（HTML +
    core JS/CSS + 每张卡的 app.js/app.css）。在 RTT 高 / 走隧道的客户端上，
    这一项就是"网页打不开"的放大器 —— 实测某个客户端加载同一页花 76 秒，
    每个资源批次之间隔 16~22 秒。
    带上版本戳之后，第二次打开只剩 1 个请求（HTML）+ 若干 API。

    ``no-cache`` **不是** ``no-store`` —— 浏览器仍会缓存，只是每次请求都
    回校验，所以既保性能又不陈旧。
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = _cache_control(_scope_of(args, kwargs))
        return resp


def _scope_of(args: tuple, kwargs: dict):
    """从 ``file_response`` 的实参里把 ASGI ``scope`` 挖出来。

    Starlette 调的是 ``self.file_response(full_path, stat_result, scope)``
    —— 位置传参，所以不能只看 ``kwargs``；两个位置都兜住，免得 Starlette
    哪天改成关键字传参就静默退化成"永远 no-cache"。
    """
    scope = kwargs.get("scope")
    if scope is None and len(args) >= 3:
        scope = args[2]
    return scope


def _cache_control(scope) -> str:
    """按 URL 有没有版本戳决定 ``Cache-Control``。"""
    if not scope:
        return "no-cache"
    qs = scope.get("query_string") or b""
    try:
        params = parse_qs(qs.decode("latin-1"))
    except Exception:               # noqa: BLE001
        return "no-cache"
    if params.get("v"):
        return "public, max-age=31536000, immutable"
    return "no-cache"


# --------------------------------------------------------------------------- #
# FastAPI factory                                                             #
# --------------------------------------------------------------------------- #
def create_app() -> FastAPI:
    cfg = get_config()

    # Propagate WDNAI_MODELS_DIR from config so downstream SDKs pick up
    # the configured model root before any LLMSession is built.
    base = cfg.get_section("base", {})
    model_path = (base or {}).get("model_path")
    if isinstance(model_path, str) and model_path.strip():
        os.environ["WDNAI_MODELS_DIR"] = model_path

    # Propagate the user-voice directory to the compute worker. The
    # worker spawn does ``env = os.environ.copy()`` (see
    # ``app/core/compute/registry.py``), so setting it here makes the
    # worker see the same directory — it restores the user-uploaded
    # voices from disk at startup instead of waiting for the app to
    # push them back.
    try:
        from app.core.uploads import get_tts_user_voices_root
        os.environ["WDNAI_TTS_USER_VOICES_DIR"] = str(get_tts_user_voices_root())
    except Exception as exc:               # noqa: BLE001
        print(f"[main] WDNAI_TTS_USER_VOICES_DIR not set: {exc}", flush=True)

    # Best-effort: 板载 ES8388 codec Speaker PA 默认 mute,TTS 启动时
    # aplay 写出 PCM 但听不到声音。boot 时 un-mute ES8388 的 Speaker
    # control(失败不影响其它路径)。
    try:
        for cmd in (
            ["amixer", "-c", "1", "sset", "Speaker", "on"],
            ["amixer", "sset", "Speaker", "on"],
        ):
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2.0, check=False,
            )
            break
    except Exception:                   # noqa: BLE001
        pass

    app = FastAPI(
        title="wdnwebui",
        version=__version__,
        description="wdnwebui service (plugin-driven frontend)",
    )

    # 0. TLS 转发（scripts/tls_relay.py）在前面终结 TLS 时会把原始 scheme
    #    用 X-Forwarded-Proto 送过来 —— 这一层把它落到 scope["scheme"]，
    #    后端生成的绝对 URL（307 的 Location 等）才是对的。
    #    详见 app/core/forwarded.py 的说明。
    app.add_middleware(XForwardedProtoMiddleware)

    # 0b. gzip 压缩 —— 首屏字节数的主要杠杆。
    #
    #     压缩前：index.html 116 KB、framework.css 140 KB、framework.js
    #     64 KB …… 首屏要传几百 KB 明文。压缩后同样的内容约 1/5~1/8。
    #     在 RTT 高 / 走隧道的客户端上，这是"网页一直转圈"最直接的解药。
    #
    #     为什么用 Starlette 自带的而不是自己写：
    #     * 它**显式跳过 206 Partial Content**（``partial_response`` 判断）
    #       —— 视频 preview.mp4 的 Range 请求绝不能被压坏；
    #     * 默认排除 ``video/*`` / ``audio/*`` / ``image/*`` /
    #       ``text/event-stream``；
    #     * ``scope["type"] != "http"`` 时原样放行 —— **WebSocket 不受影响**。
    #     compresslevel 用 6 而不是默认的 9：RK3588 上 level 9 编 116 KB
    #     HTML 的 CPU 代价明显更大，而 6 的体积差距只有百分之几。
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

    # 1. Discover cards (manifest scan + validation)
    manifests = discover_cards()
    LOG.info(
        "discovered %d card(s): %s",
        len(manifests),
        ", ".join(m.id for m in manifests),
    )
    for m in manifests:
        if m.errors:
            for e in m.errors:
                LOG.error("[%s] %s", m.id, e)

    # 2. Mount each card's API + register shutdown
    for m in manifests:
        try:
            from app.core.manifest import call_register_api
            call_register_api(m, app)
        except Exception as e:  # noqa: BLE001
            LOG.exception("[%s] register_api failed: %s", m.id, e)
            m.errors.append(f"register_api failed: {e}")
            m.status = "fatal"

    # 3. Mount system pages (manual; not plugin-driven)
    from app.system import register_all as system_register_all
    system_register_all(app)

    # 3b. 帧判据共享 API —— vd / vtd / vtl 共用 VideoSession 单例，
    #     所以判据配置也只有一份。三张卡各自暴露端点会走样（改一处漏
    #     两处），故挂在 core 层由三张卡共用。video-retrieval（监控反查）
    #     只保留检索，没有判据，不在此列。
    from app.core.video.api import register_judge_api
    register_judge_api(app)

    # 4. boot_compute_services — daemon thread (kept from v0.4 architecture)
    threading.Thread(
        target=get_registry().boot_compute_services,
        name="compute-boot",
        daemon=True,
    ).start()

    # 4b. 预热卡片预览海报（后台线程）。
    #
    #     首屏改成只显示 ~16 KB 的静态首帧（海报），不再整段下载
    #     preview.mp4（实测 9 张卡 3.2 MB）。预热是为了让第一次进网格的
    #     用户不用等现解码（每张 ~80 ms）。
    threading.Thread(
        target=_prewarm_preview_posters,
        name="preview-prewarm",
        daemon=True,
    ).start()

    # 5. Mount per-card static dirs at /static/apps/<id>
    #    卡片资源（app.js / app.css / template.html）不带 ?v= 版本串，
    #    StaticFiles 默认也不发 Cache-Control → 浏览器按 Last-Modified
    #    启发式缓存，改了卡片 JS 后仍用旧副本（症状：点击无反应 / 行为
    #    与源码不符，只能硬刷新）。挂载时显式发 no-cache：浏览器每次
    #    带 ETag 回来校验，未变则 304、变了立即拉新，既保性能又不陈旧。
    for m in manifests:
        if not m.static_dir or not m.static_dir.is_dir():
            continue
        try:
            app.mount(
                f"/static/apps/{m.id}",
                _NoCacheStaticFiles(directory=str(m.static_dir)),
                name=f"static-apps-{m.id}",
            )
        except Exception as e:  # noqa: BLE001
            LOG.warning("[%s] failed to mount static dir: %s", m.id, e)

    # 6. Mount framework-level static at /static/core + /static/system + /static
    # /static/core 同样走 _NoCacheStaticFiles：template 里虽然带了 ?v=<mtime>
    # 版本串，但浏览器在地址栏里手动导航或被 hashchange 切到同一页时，**仍**
    # 拿旧 index.html 跑（hash 变化不重新拉 index），于是 ?v= 不更新、JS 也就
    # 不变。no-cache 让浏览器每次都带 ETag 校验：URL 变了一定拉新，URL 没变
    # 也只是多一次 304。详见 _NoCacheStaticFiles 注释。
    core_static = STATIC_DIR / "core"
    if core_static.is_dir():
        app.mount(
            "/static/core",
            _NoCacheStaticFiles(directory=str(core_static)),
            name="static-core",
        )
    # System pages have their own static dir (not plugin-driven)
    system_static = Path(__file__).parent / "system" / "static"
    if system_static.is_dir():
        app.mount(
            "/static/system",
            StaticFiles(directory=str(system_static)),
            name="static-system",
        )
    app.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static-app",
    )

    @app.get("/")
    async def index() -> Response:
        # Re-assemble on every request to pick up manifest changes during
        # hot-reload. Cheap enough for the ~30 cards case.
        return Response(
            content=assemble_index_html(discover_cards()),
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        """Serve the favicon at the root path.

        Browsers request ``/favicon.ico`` **by default** when the page
        declares no icon — and some (Safari / iOS, crawlers, bookmarks)
        request it even when it does. Without this route every visit
        leaves a 404 in the console and in the access log.

        The page itself points at ``/static/core/favicon.svg`` (plus the
        ``.ico`` fallback), so modern browsers never hit this route; it
        exists to keep the root path from 404-ing.

        Note: routes are registered at boot → this one only exists after
        a restart (the ``<link rel=icon>`` in the page takes effect
        immediately, because ``/`` re-assembles the HTML per request).
        """
        ico = STATIC_DIR / "core" / "favicon.ico"
        if not ico.is_file():
            return Response(status_code=404)
        return FileResponse(ico, media_type="image/x-icon",
                            headers={"Cache-Control": "no-cache"})

    @app.api_route("/preview/{card_id}.jpg", methods=["GET", "HEAD"],
                   include_in_schema=False)
    async def preview_poster(card_id: str) -> Response:
        """卡片预览视频的**首帧海报**（JPEG）。

        为什么要有这个端点：应用网格原本给每张卡挂 ``<video autoplay>``，
        浏览器会**整段下载**每个预览视频 —— 实测 9 张卡 3.2 MB，占首屏字节
        的 67%，在慢链路客户端上就是「网页一直转圈」。

        首屏改用这张 ~16 KB 的静态海报占位，鼠标悬停才去加载视频。

        ``?v=<preview.mp4 的 mtime>`` 让海报可以吃 immutable 长期缓存：
        视频换了内容 → mtime 变 → URL 变 → 浏览器必然重取。
        """
        from app.core.preview_poster import poster_bytes

        data = await run_in_threadpool(poster_bytes, card_id)
        if not data:
            return Response(status_code=404)
        return Response(
            content=data,
            media_type="image/jpeg",
            # URL 里带 ``?v=<preview.mp4 的 mtime>`` → 内容变了 URL 就变，
            # 所以可以跟其它带戳的静态资源一样吃 immutable 长期缓存。
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "version": __version__,
            "cards": [m.id for m in manifests],
            "version_status": {m.id: m.status for m in manifests},
        }

    @app.on_event("shutdown")
    async def _on_shutdown():
        # Card shutdown handlers
        for m in manifests:
            if not m.backend_register_shutdown:
                continue
            try:
                from app.core.manifest import shutdown_callbacks
                for manifest, fn in shutdown_callbacks():
                    if manifest.id == m.id:
                        try:
                            fn()
                        except Exception as e:  # noqa: BLE001
                            LOG.warning("[%s] shutdown: %s", m.id, e)
            except Exception as e:  # noqa: BLE001
                LOG.warning("[%s] shutdown import: %s", m.id, e)
        # Compute registry cleanup
        try:
            get_registry().close_all()
        except Exception:
            pass

    return app


app = create_app()
