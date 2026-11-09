"""模型下载 —— 远端清单 / 本地扫描 / 下载任务管理。

分工
----
* :mod:`app.core.compute.hf_transfer` —— **传输内核**（curl / Range 续传 /
  卡死看门狗 / sha256 校验）。不认识"模型"。
* **本模块** —— 模型语义：远端仓库清单、本地已装扫描、暂存目录、原子改名、
  进度语义、任务队列。

两个关键设计
------------
1. **暂存目录 + 原子改名**
   模型根目录每 ~2s 被重扫一次（``registry._model_rescan_loop``），而
   ``workers._model_dirs()`` 只过滤 ``.`` / ``_`` 开头的目录。所以下载落到
   ``<dest>/.dl-<name>/``（隐藏 → 扫描器看不见），**全部文件校验通过后**
   才 ``rename`` 成 ``<dest>/<name>/``。

   直接下到最终目录会有个致命中间态：worker manifest（小 json）先落地，
   权重还没下完 —— 模型已经被判定为「已安装」，用户点启用必然加载失败。

2. **串行队列**
   实测 4 条并发 Range 聚合 6.19 MB/s，与单流峰值 8.08 MB/s 同量级 ——
   **并发不放大带宽**，反而更快打爆镜像的 5 分钟配额。故仓与仓、文件与文件
   全部串行。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.compute import hf_transfer as xfer
from app.core.compute.workers import discover as _discover_workers
from app.core.compute.workers import models_root
from app.core.config import model_root_candidates

LOG = logging.getLogger(__name__)

#: 远端作者（固定）。清单来自 ``/api/models?author=<AUTHOR>``。
AUTHOR = "wardner"

#: 暂存目录前缀（隐藏 → 模型扫描器看不见）
STAGING_PREFIX = ".dl-"

#: 任务状态
STATE_QUEUED = "queued"
STATE_DOWNLOADING = "downloading"
STATE_VERIFYING = "verifying"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

TERMINAL_STATES = (STATE_DONE, STATE_FAILED, STATE_CANCELLED)


# --------------------------------------------------------------------------- #
# 路径与目录校验
# --------------------------------------------------------------------------- #
def default_dest() -> str:
    """默认下载目录 = 模型根目录（候选清单的第一项）。

    兜底**不写死路径字面量**：候选清单来自
    :func:`app.core.config.model_root_candidates`（中央真值源），第一条
    就是「当前配置的模型根」，取不到时退到 ``/userdata/models``。
    """
    try:
        return str(models_root())
    except Exception:  # noqa: BLE001
        # 中央真值源兜底（不写死路径字面量）。这里若再抛，就让它冒泡 ——
        # 静默返回一个假路径只会让调用方拿到「目录不在候选清单里」的
        # 误导性报错。
        return str(model_root_candidates()[0])


def _nearest_existing(path: Path) -> Path:
    """往上找第一个**已存在**的目录（最多 8 层）。

    ``/mnt/oem/models`` 在板上不存在 → 它的父 ``/mnt/oem`` 也不存在 →
    再上一级 ``/mnt`` 才是。算可用空间和「能不能建」都得看这个。
    """
    p = path
    for _ in range(8):
        if p.is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return p


def dest_candidates_paths() -> List[Path]:
    """下载目录候选（**都是模型根**）。

    顺序见 :func:`app.core.config.model_root_candidates`：当前配置的模型根
    在前，其后是 ``/userdata/models`` → ``/mnt/oem/models`` → ``<HOME>/models``。
    """
    try:
        root = models_root()
    except Exception:  # noqa: BLE001
        root = None
    return model_root_candidates(root)


def dest_candidates() -> List[Dict[str, Any]]:
    """下载目录候选（探测顺序）—— 前端下拉框用。

    每项给出：绝对路径、目录是否已存在、是否可写、**它是不是当前生效的
    模型根**。最后一条很关键 —— 模型根只有一个
    （``WDNAI_MODELS_DIR`` 在启动时从 ``base.model_path`` 写死），
    下载到别处不会自动被算力页识别。
    """
    try:
        root = str(models_root()).rstrip("/")
    except Exception:  # noqa: BLE001
        root = ""
    out: List[Dict[str, Any]] = []
    for p in dest_candidates_paths():
        sp = str(p)
        exists = p.is_dir()
        # 不存在时，看它最近的那个已存在祖先能不能建
        probe = p if exists else _nearest_existing(p.parent)
        try:
            writable = os.access(str(probe), os.W_OK)
        except OSError:
            writable = False
        out.append({
            "path": sp,
            "exists": exists,
            "writable": writable,
            "free_bytes": xfer.free_bytes(probe),
            "is_model_root": sp.rstrip("/") == root,
        })
    return out



# --------------------------------------------------------------------------- #
# 属主对齐
# --------------------------------------------------------------------------- #
def _reference_owner(dest: Path) -> Optional[tuple]:
    """找下载目标最近的一个「非 root 属主」祖先目录，返回 ``(uid, gid)``。

    生产服务（systemd ``wdnwebui.service``）以 **root** 运行 → curl 写出来的
    文件是 ``root:root``，而板上其它模型是 ``rockchip:rockchip``。功能上读
    不受影响（644/755 对 others 开放），但板子状态会不一致，rockchip 侧的
    运维脚本也写不动这些文件。

    参考对象取「最近的非 root 属主祖先」而**不是** dest 本身：dest 可能是
    服务刚创建出来的（root 属主），真正的参考是它上面的模型根
    （``/userdata/models`` → rockchip）。
    """
    try:
        if os.geteuid() != 0:
            return None  # 非 root 没资格改属主
    except AttributeError:  # pragma: no cover — 非 POSIX
        return None
    try:
        p = dest.resolve() if dest.exists() else dest.absolute()
    except OSError:
        return None
    for cand in [p] + list(p.parents):
        try:
            st = cand.stat()
        except OSError:
            continue
        if st.st_uid != 0:
            return (st.st_uid, st.st_gid)
    return None


def _staged_bytes(staging: Path) -> int:
    """暂存目录里已经占用的字节数（用来算续传时**真正还需要**多少空间）。

    失败 / 取消时 ``.dl-*`` 暂存目录**故意保留**（续传的落点），所以重试时
    里面可能躺着半个模型。这里量一下已经占了多少，重试的磁盘预算要把这部分
    扣掉 —— 否则会被自己的半成品卡成「可用空间不足」。

    量的是**当前磁盘占用**（``st_size``），不是逻辑大小：半截文件只占
    已下载的那部分，正好等于「还要再下多少」。
    """
    total = 0
    try:
        for root, _dirs, files in os.walk(staging):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _align_owner(staging: Path, dest: Path) -> None:
    """把暂存目录整棵树的属主对齐到参考属主（失败不影响下载结果）。"""
    ref = _reference_owner(dest)
    if ref is None:
        return
    uid, gid = ref
    for root, dirs, files in os.walk(staging):
        for name in list(dirs) + list(files):
            try:
                os.chown(os.path.join(root, name), uid, gid)
            except OSError:
                pass
    try:
        os.chown(staging, uid, gid)
    except OSError:
        pass


def resolve_dest(dest_dir: str) -> Path:
    """把用户选的目录解析成**合法的模型根**；不合法抛 ``ValueError``。

    ⚠️ **只接受候选清单里的那几个**（当前配置的模型根 / ``/userdata/models``
    / ``/mnt/oem/models`` / ``<HOME>/models``），**不接受它们的子目录**。

    理由：模型落地是 ``<dest>/<模型名>/``，而模型根扫描**只有一层**
    （``workers._model_dirs()`` 用 ``root.iterdir()``，不递归）——
    下载目录必须**就是模型根**。选子目录的话模型下好了也扫不到。
    """
    raw = (dest_dir or "").strip()
    if not raw:
        raise ValueError("下载目录不能为空")
    if not raw.startswith("/"):
        raise ValueError("下载目录必须是绝对路径")
    target = Path(raw)
    try:
        resolved = target.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"下载目录无法解析: {exc}") from exc

    cands = dest_candidates_paths()
    for cand in cands:
        try:
            if resolved == cand.resolve(strict=False):
                return resolved
        except OSError:
            continue
    raise ValueError(
        "下载目录必须是模型根目录之一（"
        + "、".join(str(c) for c in cands)
        + "）—— 模型扫描只有一层，子目录里的模型不会被识别"
    )


def short_name(repo_id: str) -> str:
    """``wardner/Qwen3.5-4B`` → ``Qwen3.5-4B``（= 本地模型目录名）。"""
    return repo_id.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# 远端清单
# --------------------------------------------------------------------------- #
#: 远端仓大小缓存 —— ``{repo_id: (取回时刻, 文件数, 字节数)}``。
#:
#: 为什么要缓存：镜像的 tree API **很慢且抖动**（实测同一接口 1.0s /
#: 1.3s / 11.4s 都出现过），8 个仓串行要 20s+ —— 弹框不能干等。
#: 而「仓里有多少文件、多大」对已发布的仓是**不变**的，缓存 6h 完全够。
_SIZE_CACHE: Dict[str, tuple] = {}
_SIZE_CACHE_TTL = 6 * 3600
_SIZE_LOCK = threading.Lock()

#: 仓库清单缓存（TTL 60s）—— 镜像是**共享的**，本板别的流量也会让它变慢
#: （实测同一个 list 接口 0.9s / 6.4s / 10.7s 都出现过）。弹框每次打开都
#: 拉一遍会让「加载中」飘到十几秒。
_LIST_CACHE: List[Dict[str, Any]] = []
_LIST_CACHE_TS = 0.0
_LIST_CACHE_TTL = 60


def _repo_size(repo_id: str) -> Dict[str, Any]:
    """单个仓的文件数与总字节（失败时带 error 字段）。"""
    try:
        tree = xfer.repo_tree(repo_id)
        return {"files": len(tree), "bytes": sum(tree.values()), "size_ok": True,
                "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"files": 0, "bytes": 0, "size_ok": False, "error": str(exc)}


def _refresh_sizes(repo_ids: List[str]) -> None:
    """后台把仓大小刷进缓存（并发 4）。请求路径不等它。"""
    def _worker() -> None:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            for repo_id, info in zip(repo_ids, pool.map(_repo_size, repo_ids)):
                if not info["size_ok"]:
                    continue
                with _SIZE_LOCK:
                    _SIZE_CACHE[repo_id] = (time.time(), info["files"], info["bytes"])
    threading.Thread(target=_worker, name="hf-size-refresh", daemon=True).start()


def list_remote_repos(author: str = AUTHOR) -> List[Dict[str, Any]]:
    """列出远端仓。

    **只等 1 个 API 调用**（仓库清单）—— 大小从缓存取；缓存里没有
    的仓**后台补**（tree API 慢且抖动，实测同一接口 1.0s / 11.4s 都出现过），
    首次打开时那些行显示「大小未知」，几秒后刷新就有了。

    清单本身也缓存 60s —— 镜像是共享资源，本板别的流量也会拖慢它
    （实测同一接口 0.9s / 10.7s）。
    """
    global _LIST_CACHE, _LIST_CACHE_TS
    now = time.time()
    with _SIZE_LOCK:
        if _LIST_CACHE and now - _LIST_CACHE_TS < _LIST_CACHE_TTL:
            return [dict(x) for x in _LIST_CACHE]

    url = f"{xfer.HF_ENDPOINT}/api/models?author={author}&limit=100"
    try:
        raw = xfer.http_get(url).decode()
        items = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if getattr(exc, "code", None) == xfer.HTTP_TOO_MANY_REQUESTS:
            raise RuntimeError("镜像配额已耗尽（429），请稍后重试") from exc
        raise RuntimeError(f"取远端仓库清单失败: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"取远端仓库清单失败: {exc}") from exc

    repo_ids = [str(i.get("id") or i.get("modelId") or "") for i in items]
    repo_ids = [r for r in repo_ids if r]
    with _SIZE_LOCK:
        stale = [
            r for r in repo_ids
            if r not in _SIZE_CACHE
            or now - _SIZE_CACHE[r][0] > _SIZE_CACHE_TTL
        ]
    if stale:
        _refresh_sizes(stale)

    out: List[Dict[str, Any]] = []
    for item in items:
        repo_id = str(item.get("id") or item.get("modelId") or "")
        if not repo_id:
            continue
        with _SIZE_LOCK:
            hit = _SIZE_CACHE.get(repo_id)
        files = hit[1] if hit else 0
        size = hit[2] if hit else 0
        out.append({
            "repo_id": repo_id,
            "name": short_name(repo_id),
            "files": files,
            "bytes": size,
            "size_ok": bool(hit),
            "error": "" if hit else "大小统计中…",
        })
    out.sort(key=lambda e: e["name"].lower())
    with _SIZE_LOCK:
        _LIST_CACHE = out
        _LIST_CACHE_TS = time.time()
    return [dict(x) for x in out]


# --------------------------------------------------------------------------- #
# 本地扫描
# --------------------------------------------------------------------------- #
def scan_local() -> Dict[str, Dict[str, Any]]:
    """扫描本板已识别的模型 → ``{模型名: {path, installed, bytes, manifest}}``。

    「已安装」的判据是**目录里有 worker manifest**（含 ``spec_factory``
    的 json），不是「目录存在」—— 半成品目录不能算已安装。
    """
    root = models_root()
    out: Dict[str, Dict[str, Any]] = {}
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    # 一次发现、多次查表 —— 每个目录都重新 discover 一遍会白扫 N 次全盘
    manifests_by_dir: Dict[str, List[Any]] = {}
    for m in _discover_workers():
        manifests_by_dir.setdefault(m.model_dir, []).append(m)
    for path in dirs:
        if path.name.startswith((".", "_")):
            continue
        manifests = manifests_by_dir.get(str(path), [])
        out[path.name] = {
            "path": str(path),
            "installed": bool(manifests),
            "manifest": manifests[0].path if manifests else "",
            "bytes": xfer.dir_size(path),
        }
    return out


def repo_inventory(author: str = AUTHOR) -> Dict[str, Any]:
    """远端 + 本地合并 —— 给 API 用。"""
    local = scan_local()
    remote = list_remote_repos(author)
    models: List[Dict[str, Any]] = []
    for entry in remote:
        name = entry["name"]
        loc = local.get(name)
        models.append({
            **entry,
            "installed": bool(loc and loc["installed"]),
            "local_path": loc["path"] if loc else "",
            "local_bytes": loc["bytes"] if loc else 0,
        })
    # 本地有、远端清单里没有的（用户自己 clone 的）也列出来
    known = {m["name"] for m in models}
    for name, loc in local.items():
        if name in known:
            continue
        models.append({
            "repo_id": "", "name": name, "files": 0, "bytes": 0,
            "size_ok": False, "error": "",
            "installed": bool(loc["installed"]),
            "local_path": loc["path"],
            "local_bytes": loc["bytes"],
            "local_only": True,
        })
    models.sort(key=lambda m: (not m["installed"], m["name"].lower()))
    try:
        free = xfer.free_bytes(models_root())
    except Exception:  # noqa: BLE001
        free = 0
    return {
        "author": author,
        "endpoint": xfer.HF_ENDPOINT,
        "models": models,
        "models_root": str(models_root()),
        "default_dest": default_dest(),
        "dest_candidates": dest_candidates(),
        "free_bytes": free,
    }


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
@dataclass
class DownloadJob:
    """一个模型仓的下载任务。"""

    id: str
    repo: str
    dest: str
    name: str = ""
    state: str = STATE_QUEUED
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current_file: str = ""
    speed_bps: float = 0.0
    eta_seconds: Optional[float] = None
    error: str = ""
    notice: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    staging: str = ""
    target: str = ""
    _samples: List[tuple] = field(default_factory=list, repr=False)
    _cancel: bool = field(default=False, repr=False)

    # ---- 内部 ----
    def cancelled(self) -> bool:
        return self._cancel

    def sample(self) -> None:
        """记一次进度采样（用于算速度）。"""
        now = time.time()
        self._samples.append((now, self.bytes_done))
        if len(self._samples) > 8:
            self._samples.pop(0)

    def recompute_speed(self) -> None:
        if len(self._samples) < 2:
            return
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        dt = t1 - t0
        if dt > 0.5:
            self.speed_bps = max(0.0, (b1 - b0) / dt)
        if self.speed_bps > 1024 and self.bytes_total > self.bytes_done:
            self.eta_seconds = (self.bytes_total - self.bytes_done) / self.speed_bps

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "repo": self.repo,
            "name": self.name or short_name(self.repo),
            "dest": self.dest,
            "state": self.state,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "current_file": self.current_file,
            "speed_bps": round(self.speed_bps, 1),
            "eta_seconds": (round(self.eta_seconds, 1)
                            if self.eta_seconds is not None else None),
            "error": self.error,
            "notice": self.notice,
            "target": self.target,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class DownloadManager:
    """下载任务管理器（进程内单例）。

    * **串行队列** —— 同时只跑一个仓。
    * 任务状态是普通 dict + 锁，前端 1Hz 轮询读。
    * 取消 = 置标志 → 下一次 curl 调用会杀掉子进程；``.dl-*`` 暂存目录
      保留，下次可以续传。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, DownloadJob] = {}
        self._queue: List[str] = []
        self._current: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._seq = 0

    # ---- 对外 ----
    def enqueue(self, repos: List[str], dest_dir: str) -> Dict[str, Any]:
        """把若干仓库排进队列。返回本次创建的任务 + 跳过原因。"""
        dest = resolve_dest(dest_dir)
        created: List[Dict[str, Any]] = []
        skipped: List[Dict[str, str]] = []
        with self._lock:
            busy = {j.repo for j in self._jobs.values()
                    if j.state not in TERMINAL_STATES}
            for repo in repos:
                repo = (repo or "").strip()
                if not repo:
                    continue
                if not repo.startswith(f"{AUTHOR}/"):
                    skipped.append({"repo": repo, "reason": f"只允许 {AUTHOR} 名下的仓库"})
                    continue
                if repo in busy:
                    skipped.append({"repo": repo, "reason": "已在下载队列中"})
                    continue
                name = short_name(repo)
                target = dest / name
                if target.exists() and any(target.iterdir()):
                    skipped.append({"repo": repo, "reason": f"目标已存在: {target}"})
                    continue
                self._seq += 1
                job = DownloadJob(
                    id=f"dl-{int(time.time())}-{self._seq}",
                    repo=repo,
                    dest=str(dest),
                    name=name,
                    state=STATE_QUEUED,
                    staging=str(dest / f"{STAGING_PREFIX}{name}"),
                    target=str(target),
                )
                self._jobs[job.id] = job
                self._queue.append(job.id)
                busy.add(repo)
                created.append(job.to_dict())
            self._ensure_worker()
        return {"created": created, "skipped": skipped}

    def status(self) -> Dict[str, Any]:
        """当前状态（前端 1Hz 拉）。"""
        with self._lock:
            self._refresh_progress()
            current = self._jobs.get(self._current) if self._current else None
            queued = [self._jobs[i].to_dict() for i in self._queue
                      if i in self._jobs]
            history = [j.to_dict() for j in self._jobs.values()
                       if j.state in TERMINAL_STATES]
            history.sort(key=lambda j: j["finished_at"], reverse=True)
            return {
                "active": current.to_dict() if current else None,
                "queued": queued,
                "history": history[:10],
                "running": bool(current),
            }

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"ok": False, "error": "找不到该任务"}
            if job.state in TERMINAL_STATES:
                return {"ok": False, "error": "任务已结束"}
            job._cancel = True
            if job.id in self._queue:
                self._queue.remove(job.id)
                job.state = STATE_CANCELLED
                job.finished_at = time.time()
            return {"ok": True, "job": job.to_dict()}

    # ---- 内部 ----
    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="model-download",
                                        daemon=True)
        self._thread.start()

    def _refresh_progress(self) -> None:
        """按暂存目录的实际字节数刷新进度（curl 正在写的文件也算）。"""
        for job in self._jobs.values():
            if job.state not in (STATE_DOWNLOADING, STATE_VERIFYING):
                continue
            staging = Path(job.staging)
            if not staging.is_dir():
                continue
            done = 0
            files_done = 0
            for path in staging.rglob("*"):
                if not path.is_file():
                    continue
                # ``.cache/`` 是下载器的自有目录（download.log 等），
                # 不算模型载荷 —— 不跳过的话进度会显示「15/14 文件」。
                rel = path.relative_to(staging)
                if rel.parts and rel.parts[0] == ".cache":
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                done += size
                if size > 0:
                    files_done += 1
            job.bytes_done = min(done, job.bytes_total) if job.bytes_total else done
            job.files_done = files_done
            job.sample()
            job.recompute_speed()

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._current = None
                    return
                job_id = self._queue.pop(0)
                job = self._jobs.get(job_id)
                self._current = job_id
            if job is None:
                continue
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001
                LOG.exception("下载任务 %s 异常", job.id)
                with self._lock:
                    job.state = STATE_FAILED
                    job.error = str(exc)
                    job.finished_at = time.time()

    def _run_job(self, job: DownloadJob) -> None:
        dest = Path(job.dest)
        staging = Path(job.staging)
        staging.mkdir(parents=True, exist_ok=True)
        # 下载日志放 ``.cache/`` 下 —— 不能留在模型目录里当普通文件
        # （发布侧的载荷比对只跳过 ``.cache`` 这一层）。
        log_path = staging / ".cache" / "download.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            job.state = STATE_DOWNLOADING
            job.started_at = time.time()

        def on_event(kind: str, **fields: Any) -> None:
            with self._lock:
                if kind == "file_start":
                    job.current_file = str(fields.get("path") or "")
                elif kind in ("file_done", "file_skip"):
                    if job.current_file == str(fields.get("path") or ""):
                        job.current_file = ""
                elif kind == "quota_wait":
                    job.notice = f"镜像配额暂时耗尽，等待 {fields.get('seconds', 60)}s 后重试"
                elif kind == "retry":
                    job.notice = str(fields.get("text") or "")
                elif kind == "error":
                    job.error = str(fields.get("text") or job.error)
                elif kind == "notice":
                    job.notice = str(fields.get("text") or "")

        # 远端文件清单（拿总大小 + 文件数）
        # 429（镜像配额耗尽）要退避重试 —— 配额窗口 5 分钟，等一等就好，
        # 不能因此判任务失败。
        def _on_quota_wait(seconds: int, attempt: int) -> None:
            with self._lock:
                job.notice = f"镜像配额暂时耗尽，等待 {seconds}s 后重试（第 {attempt} 次）"

        try:
            tree = xfer.repo_tree(job.repo, quota_retries=3,
                                  on_quota_wait=_on_quota_wait)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                job.state = STATE_FAILED
                job.error = f"取仓库清单失败: {exc}"
                job.finished_at = time.time()
            return
        with self._lock:
            job.files_total = len(tree)
            job.bytes_total = sum(tree.values())

        # 磁盘预算：不够就别开始
        #
        # ⚠️ 暂存目录里可能已经有上次失败 / 取消时留下的部分文件
        # （``.dl-*`` **故意保留** —— 那是续传的落点）。那些字节**不用重下**
        # （完整且 sha256 对得上 → 直接跳过；半截 → curl ``-C -`` 续传），
        # 所以真实需求 = 总量 − 已暂存量。不扣的话，续传场景会被**自己的
        # 半成品**卡成「可用空间不足」——空间明明够。
        need = job.bytes_total + (256 << 20)
        staged = _staged_bytes(staging)
        if staged:
            need = max(0, job.bytes_total - staged) + (256 << 20)
        avail = xfer.free_bytes(dest)
        if avail < need:
            with self._lock:
                job.state = STATE_FAILED
                job.error = (f"可用空间不足：需要 {need / 1024**3:.1f} GiB，"
                             f"实际 {avail / 1024**3:.1f} GiB")
                job.finished_at = time.time()
            return

        ok, detail = xfer.download_repo(
            job.repo, staging, log_path=log_path,
            on_event=on_event, should_cancel=job.cancelled,
        )

        with self._lock:
            if job._cancel:
                job.state = STATE_CANCELLED
                job.error = "已取消"
                job.finished_at = time.time()
                job.current_file = ""
                return
            if not ok:
                job.state = STATE_FAILED
                job.error = detail or "下载失败"
                job.finished_at = time.time()
                job.current_file = ""
                return
            job.state = STATE_VERIFYING
            job.current_file = ""

        # 校验 manifest 存在（没有它这个仓就装不成模型）
        manifests = [p for p in staging.glob("*.json")
                     if "spec_factory" in p.read_text(errors="replace")]
        if not manifests:
            with self._lock:
                job.state = STATE_FAILED
                job.error = "下载完成，但仓内没有 worker manifest（不会出现在算力配置页）"
                job.finished_at = time.time()
            return

        # 原子改名 → 模型目录
        target = Path(job.target)
        try:
            # 先对齐属主：服务以 root 跑，curl 写出来的文件是 root:root，
            # 跟板上其它模型（rockchip:rockchip）不一致。改名之后再改就晚了
            # —— 目录已经在模型根里，扫描器会把它当成已安装模型。
            _align_owner(staging, dest)
            if target.exists():
                shutil.rmtree(target)
            os.replace(str(staging), str(target))
        except OSError as exc:
            with self._lock:
                job.state = STATE_FAILED
                job.error = f"安装到 {target} 失败: {exc}"
                job.finished_at = time.time()
            return

        # 让算力配置页立刻看到新模型
        try:
            from app.core.compute.registry import refresh_model_presets
            refresh_model_presets()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("刷新模型预设失败: %s", exc)

        with self._lock:
            job.state = STATE_DONE
            job.bytes_done = job.bytes_total
            job.finished_at = time.time()
            job.notice = f"已安装到 {target}"


#: 进程内单例
_MANAGER: Optional[DownloadManager] = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> DownloadManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = DownloadManager()
        return _MANAGER
