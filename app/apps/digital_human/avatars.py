"""数字人形象库。

一个「形象」= 一整套素材：

* ``net_encode.npz`` / ``bs_proj.npz`` —— 参考帧编码器 + AdaIN 调制投影的
  **numpy** 权重（每个形象自带一份，替代原来的 torch ``.pt``）；
* ``net_decode_<尺寸>.rknn`` —— decoder，**跑在板载 NPU 上**（只有这一条路）；
* ``ref_frames_bg/ref_*.jpg`` —— 150 帧**同源**参考图（从 ``bg_video.mp4``
  按 ``face_box.txt`` 裁的）；
* ``bg_video.mp4`` —— 背景视频；
* ``face_box.txt`` —— 嘴部在背景帧里的框；
* ``neutral_pose.npy`` —— 中性表情的 32 维嘴型参数。

⚠️ 官方素材包里带的是 **torch 权重**（``net_encode.pt`` / ``net_decode.pt``）。
板端**运行时一个字节都不读**它们（零 torch）—— 运行时读的是 119 上导出的
``*.npz`` 与转好的 ``*.rknn``。所以刚下载下来的形象**还不能渲**，要在 119
上跑一遍准备（见 :meth:`AvatarRegistry.resolve` 的报错文案）。

关键点
======

**嘴型模型是形象无关的。** 同一个 RKNN 模型（``input_sp`` 恒为 2）能驱动
任意形象 —— 形象差异完全来自上面这套素材。上游也是这么用的：
``audio2mouth_cpu.py`` 里 ``self.sp`` 写死为 2，从来不随形象变化，官方
形象库也只提供素材 zip、不带任何 per-avatar 配置。

所以「换形象」= **换一套素材**，不需要重新转模型、也不需要重新跑 NPU 推理
（嘴型参数跟形象无关，只有最后的渲染步骤用到素材）。

素材来源
========

官方形象库（ModelScope）：

    https://modelscope.cn/models/HumanAIGC-Engineering/LiteAvatarGallery

仓库里有两个集合：

* ``20250408`` —— 101 个形象；
* ``20250612`` —— 50+ 个着职业装的形象。

每个形象有两个文件：``.png``（立绘预览图）和 ``.zip``（素材包，12~27 MB）。
本模块负责列出远端清单、按需下载、解压到本地，并维护一个本地清单。

目录布局
========

::

    <模型目录>/avatars/
        index.json            # 本地清单（缓存，可重建）
        _gallery.json         # 远端清单缓存
        _cache/               # 远端预览图缓存
        <形象 id>/
            meta.json         # 名字、来源、下载时间
            preview.png       # 立绘预览图
            net_encode.pt ...
            ref_frames_bg/ ...   # 同源参考帧（引擎自动重裁）
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("apps.digital_human.avatars")

#: 形象 id 必须匹配（只用安全字符，避免路径穿越）
AVATAR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

#: 官方形象库
GALLERY_REPO = "HumanAIGC-Engineering/LiteAvatarGallery"
GALLERY_API = "https://modelscope.cn/api/v1/models/" + GALLERY_REPO
GALLERY_FILES_API = GALLERY_API + "/repo/files?Revision=master&Recursive=true"
GALLERY_FILE_API = GALLERY_API + "/repo?Revision=master&FilePath={path}"

#: 远端清单缓存有效期（秒）
GALLERY_TTL = 24 * 3600
#: 下载超时
HTTP_TIMEOUT = 60
#: 素材包大小上限（防御）
MAX_ZIP_BYTES = 200 * 1024 * 1024

#: 素材包里必须有的文件
#:
#: ⚠️ 这是**素材包（官方画廊 zip）里**必须有的文件 —— 官方包带的是 torch
#: 权重（``.pt``）。板端**运行时**不读它们：参考帧编码器用
#: ``net_encode.npz``、AdaIN 调制用 ``bs_proj.npz``（都是 numpy 权重，在
#: 119 上从 ``.pt`` 导出），decoder 用 ``net_decode_<尺寸>.rknn``（NPU）。
#:
#: ⇒ 新下载的形象跟以前一样**不能直接用**：要在 119 上跑一遍准备
#: （导出 npz + 转 decoder RKNN），见 :meth:`AvatarRegistry.resolve` 的报错。
#:
#: ⚠️ 这份清单只用于**解压时校验素材包完整性**（官方包带什么就查什么）。
#: 它**不是**「板端已就绪」的判据 —— 后者用 :data:`RUNTIME_FILES` +
#: decoder RKNN，见 :func:`is_avatar_dir` / :meth:`_missing_runtime_files`。
#:
#: ``.pt`` 在这里的角色是**传输载体**：新下载的形象要带着它送到 119 上
#: 导出 npz / 转 RKNN，准备完之后板端就不再需要了（可以删）。
REQUIRED_FILES = ("net_encode.pt", "net_decode.pt", "face_box.txt",
                  "neutral_pose.npy")

#: 板端运行时**真正要读**的文件（相对形象目录）。
#:
#: * ``net_encode.npz`` —— 参考帧编码器的 numpy 权重（替代 net_encode.pt）
#: * ``bs_proj.npz`` —— AdaIN 调制投影的 numpy 权重（取自 net_decode.pt）
#: * ``net_decode_<size>.rknn`` —— decoder，**只有 NPU 这一条路**
RUNTIME_FILES = ("net_encode.npz", "bs_proj.npz", "neutral_pose.npy")

#: 渲染分辨率（决定要哪个 decoder RKNN）—— 与 ``wdnai.avatar`` 的默认档
#: 一致（384 是模型的原生分辨率）。
RENDER_SIZE = int(os.environ.get("WDNAI_AVATAR_RENDER_SIZE") or 384)


def decoder_rknn_name(size: int = RENDER_SIZE) -> str:
    """该渲染档位对应的 decoder 模型文件名。"""
    return ("net_decode.rknn" if int(size) == 256
            else "net_decode_{}.rknn".format(int(size)))


#: 判定「这个目录**是**一个形象」的标志文件。
#:
#: 选 ``face_box.txt`` 而不是 torch 权重（``.pt``）有两个理由：
#:
#: * 它**官方素材包必有**（见 :data:`REQUIRED_FILES`）；
#: * 它**运行时真读** —— 渲染器靠它拿 face_box 坐标裁脸
#:   （``wdnai.avatar.renderer``）。
#:
#: ⇒ 用它当标志，既认得出「已下载的形象」，又不会因为板端**零 torch**
#: （``.pt`` 可以删）就把一个准备好的形象误判成不存在。
#:
#: ⚠️ 2026-11-26 之前这里用的是 ``net_decode.pt``。那让「板端运行时
#: 一个字节都不读」的 torch 权重成了**存在性哨兵** —— 一份准备好的
#: 形象（numpy 权重 + NPU decoder 齐全）只要删掉 ``.pt`` 就会从列表里
#: 消失、``resolve()`` 直接报「素材不存在」。``.pt`` 的真实用途是
#: **传输载体**：新下载的形象要送 119 上导出 npz / 转 RKNN。
AVATAR_MARKER = "face_box.txt"


def is_avatar_dir(path: Path) -> bool:
    """这个目录**像**一个已下载的形象（标志文件在位）。"""
    path = Path(path)
    return path.is_dir() and (path / AVATAR_MARKER).is_file()

#: 解压时**丢弃**的文件 —— 素材包里有、但板端用不上。
#:
#: ``net.pth`` = 原始 PyTorch 权重。它只是当初在 119 上转 RKNN 的**源**
#: （119 有自己的一份），板端 decoder 跑在 NPU 上（``net_decode.rknn``），
#: 这个文件零引用。留着只是让模型目录里堆 1 MB 用不上的东西。
DROP_ON_EXTRACT = frozenset({"net.pth"})
#: 解压时丢弃的**目录**（相对素材包根）。
#:
#: ⚠️ ``ref_frames/`` **不再丢弃**（2026-11-27 改）。
#:
#: 以前这里丢的是官方 ``ref_frames/``，改用从 ``bg_video.mp4`` 同帧重裁的
#: ``ref_frames_bg/``。实测那样**嘴张不开**：decoder 是 AdaIN 调制，隐特征
#: 里带的「嘴是闭着」的先验会压制嘴型参数（实测口腔暗像素 2402~3209）；
#: 官方 ``ref_frames/`` 的参考帧嘴本来就是张开的，同样的参数能到 4249~4985。
#:
#: 官方参考帧与 ``bg_video.mp4`` 是**同一段视频的邻近帧**（脸型/位置一致，
#: 只是表情不同），所以「脸缘接缝」本来就不大；再加上贴图现在走**嘴部蒙版**
#: （只贴动的嘴部、保留背景清晰的脸），接缝问题不用靠换参考帧来躲。
DROP_DIRS_ON_EXTRACT = frozenset()


class AvatarError(RuntimeError):
    """形象相关的错误。"""


# --------------------------------------------------------------------------- #
# HTTP 小工具（只用标准库，不给 webui 的生产 venv 加依赖）
# --------------------------------------------------------------------------- #
def _open(url: str, timeout: int = HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": "wd-webui/digital-human/1.0",
        "Accept": "*/*",
    })
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310


def _http_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict:
    with _open(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_download(url: str, dest: Path, timeout: int = 600,
                   max_bytes: int = MAX_ZIP_BYTES) -> int:
    """下载到 ``dest``，返回字节数。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with _open(url, timeout=timeout) as resp, tmp.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AvatarError("文件太大（> {} MB）".format(max_bytes // 1024 // 1024))
                fh.write(chunk)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return total


def _safe_id(value: str) -> str:
    """校验形象 id —— 必须是安全字符，防止路径穿越。"""
    value = (value or "").strip()
    if not AVATAR_ID_RE.match(value):
        raise AvatarError("非法形象 id: {}".format(value[:80]))
    return value


def _runtime_cache_root() -> Path:
    """**运行时缓存**的根目录 —— 不放模型目录里。

    模型目录是**素材**（官方发布内容，带 SHA256 清单、可能被重新下载
    覆盖）。远端预览图、画廊清单这些运行时产物写进去，会让「哪些是
    素材、哪些是产物」分不清。

    按 XDG 惯例放 ``~/.cache/lite-avatar/``（``XDG_CACHE_HOME`` 优先），
    可用 ``WDNAI_AVATAR_CACHE_DIR`` 覆盖。

    ⚠️ 渲染器的**中性基准缓存**在 ``wdnai.avatar.renderer`` 里也有一份
    同名约定（那是真正写缓存的地方：``<id>_<hash>/neutral_384_np1/``）。
    两边都认 ``WDNAI_AVATAR_CACHE_DIR`` / ``XDG_CACHE_HOME``，改的时候
    要一起改。
    """
    override = (os.environ.get("WDNAI_AVATAR_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    xdg = (os.environ.get("XDG_CACHE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "lite-avatar"


# --------------------------------------------------------------------------- #
# 形象库
# --------------------------------------------------------------------------- #
class AvatarRegistry:
    """本地形象库 + 官方形象库（远端）的统一入口。"""

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = Path(model_dir)
        self._lock = threading.Lock()
        #: 下载任务状态：{id: {"state": ..., "error": ..., "progress": ...}}
        self._downloads: dict[str, dict] = {}
        #: 形象的帧尺寸缓存：{形象目录: (宽, 高)}
        self._frame_sizes: dict[str, tuple] = {}

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """本地形象库根目录。"""
        return self.model_dir / "avatars"

    @property
    def cache_dir(self) -> Path:
        """远端预览图缓存。

        ⚠️ **不放模型目录里** —— 模型目录是「素材」（带 SHA256 清单、
        可能被重新下载覆盖），运行时产物（预览图、清单缓存）写进去会
        让两者混在一起。按 XDG 惯例放 ``~/.cache/lite-avatar/gallery/``。
        """
        return _runtime_cache_root() / "gallery"

    @property
    def gallery_cache(self) -> Path:
        """远端清单缓存（同上，不在模型目录里）。"""
        return _runtime_cache_root() / "gallery.json"

    def avatar_dir(self, avatar_id: str) -> Path:
        return self.root / _safe_id(avatar_id)

    # ------------------------------------------------------------------ #
    # 本地形象
    # ------------------------------------------------------------------ #
    def list_avatars(self) -> list[dict]:
        """列出本地已就绪的形象。"""
        root = self.root
        if not root.is_dir():
            return []
        out: list[dict] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            if not is_avatar_dir(child):
                continue
            meta = self._read_meta(child)
            width, height = self._frame_size(child)
            out.append({
                "id": child.name,
                "name": meta.get("name") or child.name,
                "source": meta.get("source", "local"),
                "collection": meta.get("collection", ""),
                "preview": "/api/digital-human/avatars/{}/preview".format(child.name),
                "has_preview": (child / "preview.png").is_file(),
                "downloaded_at": meta.get("downloaded_at", 0),
                "size_bytes": self._dir_size(child),
                # 渲染出来的帧尺寸 = 背景视频的分辨率。前端拿它把舞台调成
                # 形象的**真实比例**（各形象不一样：多数 1024×1408，
                # sample 是 890×1920）—— 舞台跟形象一样大，两边不再有黑边。
                "width": width,
                "height": height,
                # 板端**能不能渲**（numpy 权重 + NPU decoder 都在位）。
                # 没有就**渲染不了** —— 没有 CPU 兜底，也没有 torch 兜底。
                "npu_ready": self._npu_ready(child.name),
                # 缺什么（给前端显示「需要先在 119 上准备」）
                "missing": self._missing_runtime_files(child),
            })
        return out

    def _frame_size(self, avatar_dir: Path) -> tuple:
        """形象素材的帧尺寸 ``(宽, 高)`` —— 背景视频的分辨率。

        渲染出来的每一帧就是背景视频那么大，所以这个尺寸同时也是
        「舞台该给多大」的答案。

        结果**按目录缓存**（注册表是单例，尺寸对已装好的形象不会变）——
        每次列形象都去开 18 个 mp4 太浪费。
        """
        key = str(avatar_dir)
        with self._lock:
            cached = self._frame_sizes.get(key)
        if cached is not None:
            return cached
        size = (0, 0)
        try:
            import cv2  # noqa: PLC0415 —— 只在真的要读视频时才 import

            cap = cv2.VideoCapture(str(avatar_dir / "bg_video.mp4"))
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            if width > 0 and height > 0:
                size = (width, height)
        except Exception as exc:  # noqa: BLE001 —— 读不到就按未知处理
            LOG.debug("digital_human: 读 %s 的尺寸失败: %s", avatar_dir, exc)
        with self._lock:
            self._frame_sizes[key] = size
        return size

    def frame_preview_path(self, avatar_id: str) -> Optional[Path]:
        """形象素材的**等比首帧**（舞台占位图）—— 从 ``bg_video.mp4`` 抽第一帧。

        为什么不用 ``preview.png``：那是官方图库的 **3:4 立绘**，跟形象的真实
        比例不一致（多数形象 1024×1408 = 0.73，``sample`` 是 890×1920 = 0.46）。
        塞进按真实比例做的舞台会被裁切 —— 窄形象（0.46）直接被裁成一张大特写，
        观感完全不像「这个形象」。
        用背景视频首帧 ⇒ 占位图和渲染出来的第一帧**一模一样**，比例天然对齐。

        产物是**运行时缓存**（``~/.cache/lite-avatar/frames/``），不进模型目录
        —— 模型目录是「素材」，运行时产物写进去会跟素材混在一起。
        """
        avatar_id = _safe_id(avatar_id)
        avatar_dir = self.avatar_dir(avatar_id)
        video = avatar_dir / "bg_video.mp4"
        if not video.is_file():
            return None

        cache_dir = _runtime_cache_root() / "frames"
        dest = cache_dir / "{}.jpg".format(avatar_id)
        try:
            # 背景视频换了就重新抽（缓存比素材旧 ⇒ 过期）
            if dest.is_file() and dest.stat().st_mtime >= video.stat().st_mtime:
                return dest
        except OSError:
            pass

        try:
            import cv2  # noqa: PLC0415 —— 只在真的要读视频时才 import

            cap = cv2.VideoCapture(str(video))
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            # 占位图不用原始分辨率（1024 宽就够），省磁盘也省带宽
            height, width = frame.shape[:2]
            if width > 720:
                scale = 720.0 / float(width)
                frame = cv2.resize(frame, (720, max(1, int(height * scale))))
            cache_dir.mkdir(parents=True, exist_ok=True)
            # ⚠️ cv2.imwrite **靠扩展名**判断格式 —— 临时名必须保留 .jpg，
            # 否则它会直接返回 False（"Unsupported file extension"）。
            tmp = dest.with_name(dest.stem + ".tmp.jpg")
            if not cv2.imwrite(str(tmp), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88]):
                tmp.unlink(missing_ok=True)
                return None
            tmp.replace(dest)
            return dest
        except Exception as exc:  # noqa: BLE001 —— 抽不出来就退回 preview.png
            LOG.debug("digital_human: 抽 %s 首帧失败: %s", avatar_id, exc)
            return None

    def resolve(self, avatar_id: Optional[str]) -> Path:
        """形象 id → 素材目录；``None``/空 → 默认形象。

        ⚠️ 素材目录里必须有**运行时真正要读**的那几件：

        * ``net_encode.npz`` / ``bs_proj.npz`` —— numpy 权重（替代 torch
          的 ``.pt``；板端零 torch）；
        * ``net_decode_<尺寸>.rknn`` —— decoder，**只有 NPU 一条路**。

        缺了就直接拒掉，不"先凑合用着" —— 官方素材包直接下载下来是**不能
        渲**的：要在 119 上跑一遍准备（导出 npz + 转 decoder RKNN）。
        """
        if not avatar_id:
            avatar_id = self.default_id()
        else:
            avatar_id = _safe_id(avatar_id)
        path = self.root / avatar_id
        if not path.is_dir():
            raise AvatarError("形象素材不存在: {}".format(avatar_id))
        # ⚠️ 存在性判据用标志文件（face_box.txt），**不是** torch 权重 ——
        # 板端零 torch，``.pt`` 只是新下载形象的传输载体。
        if not (path / AVATAR_MARKER).is_file():
            raise AvatarError("形象素材不完整（缺少 {}）: {}".format(
                AVATAR_MARKER, avatar_id))
        missing = self._missing_runtime_files(path)
        if missing:
            raise AvatarError(
                "形象 {} 缺少 {} —— 板端渲染器只读 numpy 权重（net_encode.npz / "
                "bs_proj.npz）+ NPU decoder（{}），没有 CPU / torch 兜底。"
                "需要在 119 上跑一遍准备（export_avatar_weights.py 导出 npz + "
                "build_decoders_rknn.py 转 decoder RKNN）。".format(
                    avatar_id, "、".join(missing), decoder_rknn_name()))
        return path

    def default_id(self) -> str:
        """默认形象（``index.json`` 里标记的，或第一个**已转好 NPU 模型**的形象）。

        没转 NPU 模型的形象不能当默认 —— 否则一开口就报错。
        """
        index = self._read_index()
        marked = str(index.get("default") or "")
        if marked and self._npu_ready(marked):
            return marked
        for avatar in self.list_avatars():
            if avatar.get("npu_ready"):
                return avatar["id"]
        return marked

    def _missing_runtime_files(self, avatar_dir: Path) -> list[str]:
        """这个形象还缺哪些**运行时**文件（空 = 就绪）。"""
        avatar_dir = Path(avatar_dir)
        missing = [f for f in RUNTIME_FILES if not (avatar_dir / f).is_file()]
        if not (avatar_dir / decoder_rknn_name()).is_file():
            missing.append(decoder_rknn_name())
        return missing

    def _npu_ready(self, avatar_id: str) -> bool:
        """板端能不能渲这个形象（numpy 权重 + NPU decoder 齐了才算）。"""
        return not self._missing_runtime_files(self.root / avatar_id)

    def _read_meta(self, path: Path) -> dict:
        meta_file = path / "meta.json"
        if not meta_file.is_file():
            return {}
        try:
            return json.loads(meta_file.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _read_index(self) -> dict:
        index_file = self.root / "index.json"
        if not index_file.is_file():
            return {}
        try:
            return json.loads(index_file.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write_index(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "index.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    @staticmethod
    def _dir_size(path: Path) -> int:
        total = 0
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    # ------------------------------------------------------------------ #
    # 官方形象库（远端）
    # ------------------------------------------------------------------ #
    def gallery_list(self, refresh: bool = False) -> list[dict]:
        """远端形象清单（带缓存）。

        返回 ``[{"id", "name", "collection", "zip_size", "preview_size", "local"}]``。
        """
        cache = self.gallery_cache
        if not refresh and cache.is_file():
            try:
                data = json.loads(cache.read_text("utf-8"))
                if time.time() - float(data.get("fetched_at", 0)) < GALLERY_TTL:
                    return data.get("items", [])
            except Exception:  # noqa: BLE001
                LOG.debug("digital_human: 形象库缓存读失败", exc_info=True)

        items = self._fetch_gallery()
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(
                {"fetched_at": time.time(), "items": items}, ensure_ascii=False), "utf-8")
        except Exception:  # noqa: BLE001
            LOG.debug("digital_human: 形象库缓存写失败", exc_info=True)
        return items

    def _fetch_gallery(self) -> list[dict]:
        try:
            data = _http_json(GALLERY_FILES_API, timeout=30)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("digital_human: 拉取形象库清单失败: %s", exc)
            # 拉不到就用旧缓存
            if self.gallery_cache.is_file():
                try:
                    return json.loads(self.gallery_cache.read_text("utf-8")).get("items", [])
                except Exception:  # noqa: BLE001
                    return []
            return []
        files = (data.get("Data") or {}).get("Files") or []
        by_key: dict[tuple[str, str], dict] = {}
        for f in files:
            path = f.get("Path") or ""
            if "/" not in path:
                continue
            collection, _, filename = path.partition("/")
            if collection == "lite_avatar_weights" or not filename.endswith(".png"):
                continue
            if f.get("IsLFS"):
                continue
            avatar_id = filename[:-4]
            by_key[(collection, avatar_id)] = {
                "id": avatar_id,
                "name": avatar_id,
                "collection": collection,
                "preview_path": path,
                "zip_path": "{}/{}.zip".format(collection, avatar_id),
                "preview_size": int(f.get("Size") or 0),
            }
        # 补上 zip 大小
        for f in files:
            path = f.get("Path") or ""
            if not path.endswith(".zip"):
                continue
            collection, _, filename = path.partition("/")
            avatar_id = filename[:-4]
            entry = by_key.get((collection, avatar_id))
            if entry is not None:
                entry["zip_size"] = int(f.get("Size") or 0)
        items = sorted(by_key.values(), key=lambda x: (x["collection"], x["id"]))
        local = {a["id"] for a in self.list_avatars()}
        for item in items:
            item["local"] = item["id"] in local
        return items

    def gallery_entry(self, avatar_id: str, collection: str = "") -> Optional[dict]:
        """按 id 在远端清单里找一条。"""
        avatar_id = _safe_id(avatar_id)
        for item in self.gallery_list():
            if item["id"] == avatar_id and (not collection or item["collection"] == collection):
                return item
        return None

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #
    def downloads(self) -> dict:
        with self._lock:
            return dict(self._downloads)

    def pull(self, avatar_id: str, collection: str = "") -> dict:
        """后台下载一个官方形象（素材包 + 预览图）。

        已经在本地就直接返回。
        """
        avatar_id = _safe_id(avatar_id)
        target = self.avatar_dir(avatar_id)
        if is_avatar_dir(target):
            return {"ok": True, "id": avatar_id, "state": "done", "local": True}
        with self._lock:
            cur = self._downloads.get(avatar_id)
            if cur and cur.get("state") in ("running", "done"):
                return {"ok": True, "id": avatar_id, "state": cur["state"]}

        entry = self.gallery_entry(avatar_id, collection)
        if entry is None:
            raise AvatarError("形象库里没有这个形象: {}".format(avatar_id))

        with self._lock:
            self._downloads[avatar_id] = {"state": "running", "error": "",
                                         "progress": 0, "started_at": time.time()}
        thread = threading.Thread(target=self._pull_worker, args=(entry,),
                                  daemon=True, name="avatar-pull-{}".format(avatar_id))
        thread.start()
        return {"ok": True, "id": avatar_id, "state": "running"}

    def _pull_worker(self, entry: dict) -> None:
        avatar_id = entry["id"]
        target = self.avatar_dir(avatar_id)
        try:
            LOG.info("digital_human: 开始下载形象 %s", avatar_id)
            tmp_dir = Path(tempfile.mkdtemp(prefix="avatar-", dir=str(self.root)))
            try:
                zip_path = tmp_dir / "asset.zip"
                size = _http_download(
                    GALLERY_FILE_API.format(path=urllib.parse.quote(entry["zip_path"])),
                    zip_path)
                with self._lock:
                    self._downloads[avatar_id]["progress"] = 0.6

                # 解压 —— 素材包自带一层 <id>/ 目录，剥掉它
                extract_dir = tmp_dir / "x"
                extract_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_path) as zf:
                    for member in zf.namelist():
                        parts = [p for p in Path(member).parts if p not in ("", ".")]
                        if not parts:
                            continue
                        if member.endswith("/"):
                            continue
                        rel = Path(*parts[1:]) if len(parts) > 1 else Path(parts[0])
                        if ".." in rel.parts:
                            continue
                        # ``net.pth`` —— 原始 PyTorch 权重，**板端用不到**：
                        # decoder 已经在 NPU 上跑（net_decode.rknn），这个文件
                        # 只是当初在 119 上转 RKNN 的源权重（119 有自己的一份）。
                        # 不落盘，免得模型目录里堆用不上的东西。
                        if rel.name in DROP_ON_EXTRACT:
                            continue
                        # ``ref_frames/`` —— 官方参考帧，**另一段视频**裁的，
                        # 不同源（跟 bg_video.mp4 同号帧 20% 像素对不上），
                        # 贴回去脸缘会有接缝。同源那份引擎自己重裁。
                        if len(rel.parts) > 1 and rel.parts[0] in DROP_DIRS_ON_EXTRACT:
                            continue
                        dest = extract_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, dest.open("wb") as dst:
                            shutil.copyfileobj(src, dst)

                missing = [f for f in REQUIRED_FILES if not (extract_dir / f).is_file()]
                if missing:
                    raise AvatarError("素材包不完整，缺少: {}".format(", ".join(missing)))
                # 官方 ref_frames/ 是**另一段视频**裁的（不同源），引擎用不上 ——
                # 解压时已丢弃（见 DROP_ON_EXTRACT）。同源参考帧由引擎在首次
                # 加载时从 bg_video.mp4 重裁，所以这里要校验的是背景视频。
                if not (extract_dir / "bg_video.mp4").is_file():
                    raise AvatarError("素材包缺少 bg_video.mp4")

                # 预览图 —— 放进解压根目录，跟着素材一起搬过去
                # （目标目录此时还不存在，直接写 target/preview.png 会失败）
                try:
                    _http_download(
                        GALLERY_FILE_API.format(path=urllib.parse.quote(entry["preview_path"])),
                        extract_dir / "preview.png", timeout=120,
                        max_bytes=32 * 1024 * 1024)
                except Exception as exc:  # noqa: BLE001 —— 预览图不是必须
                    LOG.warning("digital_human: 预览图下载失败 (%s): %s", avatar_id, exc)

                # 换入正式目录
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(extract_dir), str(target))

                (target / "meta.json").write_text(json.dumps({
                    "id": avatar_id,
                    "name": avatar_id,
                    "source": "LiteAvatarGallery",
                    "collection": entry.get("collection", ""),
                    "gallery_path": entry.get("zip_path", ""),
                    "downloaded_at": time.time(),
                }, ensure_ascii=False, indent=2), "utf-8")

                with self._lock:
                    self._downloads[avatar_id] = {
                        "state": "done", "error": "", "progress": 1.0,
                        "started_at": time.time(), "bytes": size,
                    }
                LOG.info("digital_human: 形象 %s 下载完成（%.1f MB）", avatar_id, size / 1e6)
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("digital_human: 形象 %s 下载失败: %s", avatar_id, exc)
            with self._lock:
                self._downloads[avatar_id] = {"state": "error", "error": str(exc),
                                             "progress": 0, "started_at": time.time()}

    # ------------------------------------------------------------------ #
    # 预览图
    # ------------------------------------------------------------------ #
    def preview_path(self, avatar_id: str) -> Optional[Path]:
        """本地预览图；没有就从远端拉一个缓存下来。"""
        avatar_id = _safe_id(avatar_id)
        local = self.avatar_dir(avatar_id) / "preview.png"
        if local.is_file():
            return local
        cached = self.cache_dir / "{}.png".format(avatar_id)
        if cached.is_file():
            return cached
        entry = self.gallery_entry(avatar_id)
        if entry is None:
            return None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            _http_download(
                GALLERY_FILE_API.format(path=urllib.parse.quote(entry["preview_path"])),
                cached, timeout=60, max_bytes=32 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("digital_human: 预览图拉取失败 (%s): %s", avatar_id, exc)
            return None
        return cached if cached.is_file() else None


_registry: Optional[AvatarRegistry] = None
_registry_lock = threading.Lock()


def get_registry(model_dir: Path) -> AvatarRegistry:
    """进程内单例。"""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = AvatarRegistry(model_dir)
        return _registry


def reset_registry() -> None:
    """测试用：丢掉单例。"""
    global _registry
    with _registry_lock:
        _registry = None
