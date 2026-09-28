"""微信视频分析结果 LanceDB 缓存 —— md5 → 切片 embedding 索引。

目的：同一视频（字节级相同，本地 md5 相同）重复发送时跳过昂贵的切片
embedding 分析（每片 1-3s NPU 推理，10 分钟视频 ≈ 数分钟），直接从
缓存恢复分析结果。

设计要点：

* **key = 下载文件本地 md5**（流式分块计算，200MB ≈ <1s）。所有消息
  形态（video / file）统一走"下载 → 本地算 md5 → 查缓存"，file 形态
  消息自带的服务端 md5 不使用 —— 服务端 md5 与实际下发文件存在极小
  概率不一致（CDN 转码），本地复核是唯一真源。
* **value = 分析结果（一行一片）**：LanceDB 表 ``wechat_video_index``，
  每行一个 6s 切片的 embedding 向量（2048 维 float32）+ 时间窗 +
  冗余 meta（filename/duration/切片参数/模型标识），检索与恢复都按
  ``md5`` where 过滤拉全行，不做向量近邻查询。
* **embedding_model 隔离**：每行记录产生向量的模型 id；embedding 模型
  换代后旧缓存自动失配（lookup 查不到）—— 不会拿旧向量硬算余弦。
* **仅全成功的分析入库**（调用方保证）：部分成功的索引只驻留内存，
  不写缓存、不删本地视频文件，用户重发可重试失败段。
* **LRU 淘汰**：按视频条数（distinct md5）计，超
  ``config.compute.wechat_video.cache_max_entries``（默认 100，热读）
  时删除 ``analyzed_at`` 最旧的条目。空间增长 ≈ 1MB / 10min 视频。
* **故障兜底**：lancedb 不可用 / 表损坏 / schema 不匹配（向量维度
  变了）时 lookup 返回 None、save 静默跳过（仅 LOG warning）——
  缓存层任何异常都不阻塞分析管线，行为退回无缓存现状。
  ``cache_enabled``（默认 true，热读）为 false 时完全旁路。

数据目录：``app/services/uploads/lancedb/``（uploads/ 已 gitignore）。
LanceDB 连接无需显式关闭（本地嵌入式，无网络资源），因此没有
shutdown hook。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

_LOG = logging.getLogger("core.video.cache")

_DB_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "lancedb"
_TABLE_NAME = "video_index"

_DEFAULT_CACHE_ENABLED = True
_DEFAULT_MAX_ENTRIES = 100
# 单视频切片行数上限（10 分钟 / 5s 步进 ≈ 120 行），lookup 拉取 + LRU
# 扫描的 limit 都以此为基数留余量。
_MAX_ROWS_PER_VIDEO = 256
_LRU_SCAN_ROWS = 100_000


def _vec_dim() -> int:
    """向量维度（与 wdnai embedding 输出一致；真机实测 2048）。"""
    return 2048


def _build_schema() -> "pa.Schema":
    import pyarrow as pa
    return pa.schema([
        ("md5", pa.string()),
        ("chunk_idx", pa.int32()),
        ("start_time", pa.float32()),
        ("end_time", pa.float32()),
        ("fps", pa.float32()),
        ("vec", pa.list_(pa.float32(), _vec_dim())),
        ("filename", pa.string()),
        ("source_path", pa.string()),
        ("duration_s", pa.float32()),
        ("slices_total", pa.int32()),
        ("slices_embedded", pa.int32()),
        ("chunk_seconds", pa.int32()),
        ("chunk_overlap_seconds", pa.int32()),
        ("embedding_model", pa.string()),
        ("analyzed_at", pa.float64()),
    ])


def _migrate_add_source_path(table) -> None:
    """v0.12.2 schema migration：旧表补 ``source_path`` 列（空串默认）。

    lancedb 0.34 的 ``add_columns`` 接受 ``{col: sql_default}`` 形状
    （实测 ``{"source_path": "''"}``），**持久化**且幂等；失败兜底为
    drop + 新 schema 重建（缓存是可再生资源，旧条目 source_path 为
    空 → UI 勾选检索时该条目 frame 置灰，功能不受影响）。
    """
    if "source_path" in table.schema.names:
        return
    try:
        table.add_columns({"source_path": "''"})
        _LOG.info("wechat video cache: added source_path column via "
                  "add_columns")
        return
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("wechat video cache: add_columns failed (%s) — "
                     "rebuilding table", exc)
    # 兜底：读全行 → drop → 按新 schema 重建 → 原行写回（source_path=""）
    try:
        import pyarrow as pa
        old_tbl = table.search().limit(_LRU_SCAN_ROWS).to_arrow()
        db = None
        try:
            db_path = str(_DB_DIR)
            import lancedb
            db = lancedb.connect(db_path)
        except Exception:  # noqa: BLE001
            db = None
        if db is not None:
            db.drop_table(_TABLE_NAME)
            new = db.create_table(
                _TABLE_NAME, data=None, schema=_build_schema(), mode="create")
            if old_tbl.num_rows:
                keep = [c for c in old_tbl.column_names
                        if c != "source_path"]
                rows = {c: old_tbl.column(c).to_pylist() for c in keep}
                n = old_tbl.num_rows
                rows["source_path"] = [""] * n
                new.add(pa.Table.from_pydict(rows, schema=_build_schema()))
            _LOG.info("wechat video cache: rebuilt table with "
                      "source_path (%d legacy rows preserved)",
                      old_tbl.num_rows)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("wechat video cache: schema migration failed "
                     "(clear cache and re-analyze to rebuild): %s", exc)


def file_md5(path: str, chunk_size: int = 1 << 20) -> str:
    """流式计算文件 md5（1MB 分块，200MB 文件 < 1s，内存占用恒定）。"""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _resolve_cache_config() -> "tuple[bool, int]":
    """热读 ``config.compute.wechat_video`` 的 cache_enabled /
    cache_max_entries；缺失 / 非法回退默认（true / 100）。"""
    enabled, max_entries = _DEFAULT_CACHE_ENABLED, _DEFAULT_MAX_ENTRIES
    try:
        from app.core.config import get_config
        cfg = get_config().get_section("compute", {})
        wv = cfg.get("wechat_video") if isinstance(cfg, dict) else None
        if isinstance(wv, dict):
            v = wv.get("cache_enabled")
            if isinstance(v, bool):
                enabled = v
            n = int(wv.get("cache_max_entries", max_entries))
            if n > 0:
                max_entries = n
    except Exception:  # noqa: BLE001
        pass
    return enabled, max_entries


class WechatVideoCache:
    """LanceDB 持久缓存。lazy 单例（:func:`get_video_cache`）。

    线程模型：manager 的视频管线 worker 是唯一写方；llm 桥接线程 /
    UI 线程可能并发 lookup。lancedb 连接与表对象内部由 Rust 实现，
    多线程并发读写安全；LRU 淘汰等 read-modify-write 序列用实例锁
    串行化。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._db = None
        self._table = None
        self._init_failed = False

    # ------------------------------------------------------------ 连接
    def _ensure_table(self):
        """惰性连接 + 建表。失败置 _init_failed，本次进程内不再重试
        （避免每条请求都吃一遍连接异常栈）。"""
        if self._table is not None:
            return self._table
        if self._init_failed:
            return None
        with self._lock:
            if self._table is not None:
                return self._table
            if self._init_failed:
                return None
            try:
                import lancedb
                _DB_DIR.mkdir(parents=True, exist_ok=True)
                self._db = lancedb.connect(str(_DB_DIR))
                try:
                    self._table = self._db.open_table(_TABLE_NAME)
                except Exception:
                    # lancedb 0.34 的 list_tables() 返回自定义分页对象，
                    # ``in`` 成员判定恒 False —— 不能用它判断表存在；
                    # 直接 try open_table，失败再建。
                    self._table = self._db.create_table(
                        _TABLE_NAME,
                        data=None,
                        schema=_build_schema(),
                        mode="create",
                    )
                _migrate_add_source_path(self._table)
                return self._table
            except Exception as exc:  # noqa: BLE001
                self._init_failed = True
                _LOG.warning("wechat video cache: lancedb init failed "
                             "(cache disabled this run): %s", exc)
                return None

    # ------------------------------------------------------------ 查询
    def lookup(self, md5: str, embedding_model: str,
               chunk_seconds: int = 0, chunk_overlap_seconds: int = 0,
               ) -> Optional[dict]:
        """按 md5 + 模型 + 切片参数取分析结果；未命中返回 None。

        **切片参数必须进缓存键**：同一视频不同 ``chunk_seconds`` /
        ``overlap`` 的索引时间窗与帧数完全不同，混用会产生时间戳
        错位（微信管线固定 6s/1s；UI 检索页用户可选 3-6s / 1-2s）。
        历史行为：也可不传参数向后兼容（部分影片条目若有请避免），
        建议调用方永不过滤为同一参数子集和提示 —— VCS 端
        （UI 默认 4s/1s，`meta.blocked_list` 有列）仅为 warning。
        返回::

            {"index": [ {start_time, end_time, chunk_idx, fps, vec}, ... ],
             "filename", "fps", "duration_s", "slices_total",
             "slices_embedded", "chunk_seconds", "chunk_overlap_seconds"}

        返回值未标记 ``video_path``（VCS 还原后自行按当前用户选择
        source 的 basename 重绑 —— 帧接口与 ``GET .../frame`` 走当前
        source，与 md5 无关）。
        """
        if not md5 or not embedding_model:
            return None
        table = self._ensure_table()
        if table is None:
            return None
        where = (f"md5 = '{md5}' AND "
                 f"embedding_model = '{embedding_model}'")
        # 切片参数过滤：>0 时入条件（保持老调用方兼容 —— 旧参数一律
        # 查不过滤将混淆不同档位的索引）。
        if chunk_seconds and chunk_overlap_seconds:
            where += (f" AND chunk_seconds = {int(chunk_seconds)} AND "
                      f"chunk_overlap_seconds = {int(chunk_overlap_seconds)}")
        try:
            tbl = (table.search()
                   .where(where)
                   .limit(_MAX_ROWS_PER_VIDEO)
                   .to_arrow())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: lookup failed for %s: %s",
                         md5[:12], exc)
            return None
        if tbl.num_rows == 0:
            return None

        chunk_idx = tbl.column("chunk_idx").to_pylist()
        order = sorted(range(len(chunk_idx)), key=lambda i: chunk_idx[i])
        index: List[dict] = []
        for i in order:
            vec = np.asarray(tbl.column("vec")[i].as_py(), dtype=np.float32)
            if vec.shape != (_vec_dim(),):
                _LOG.warning("wechat video cache: vec dim mismatch "
                             "(%s), treating as miss", vec.shape)
                return None
            index.append({
                "start_time": float(tbl.column("start_time")[i].as_py()),
                "end_time": float(tbl.column("end_time")[i].as_py()),
                "chunk_idx": int(chunk_idx[i]),
                "fps": float(tbl.column("fps")[i].as_py()),
                "vec": vec,
            })

        first = lambda col: tbl.column(col)[0].as_py()  # noqa: E731
        return {
            "index": index,
            "filename": str(first("filename") or ""),
            "source_path": str(first("source_path") or ""),
            "fps": float(first("fps") or 0.0),
            "duration_s": float(first("duration_s") or 0.0),
            "slices_total": int(first("slices_total") or 0),
            "slices_embedded": int(first("slices_embedded") or 0),
            "chunk_seconds": int(first("chunk_seconds") or 0),
            "chunk_overlap_seconds": int(first("chunk_overlap_seconds") or 0),
        }

    # ------------------------------------------------------------ 写入
    def save(self, md5: str, embedding_model: str, index: List[dict],
             meta: Dict[str, Any]) -> bool:
        """全成功的分析结果入库（delete + add 幂等覆盖）→ LRU 淘汰。

        任何异常只 LOG warning 返回 False —— 缓存失败不影响业务。
        """
        if not md5 or not index:
            return False
        table = self._ensure_table()
        if table is None:
            return False
        with self._lock:
            try:
                import pyarrow as pa
                n = len(index)
                now = time.time()
                rows = {
                    "md5": [md5] * n,
                    "chunk_idx": [int(c["chunk_idx"]) for c in index],
                    "start_time": [float(c["start_time"]) for c in index],
                    "end_time": [float(c["end_time"]) for c in index],
                    "fps": [float(meta.get("fps") or c.get("fps") or 0.0)
                            for c in index],
                    "vec": [np.asarray(c["vec"], dtype=np.float32)
                            .tolist() for c in index],
                    "filename": [str(meta.get("filename") or "")] * n,
                    "duration_s": [float(meta.get("duration_s") or 0.0)] * n,
                    "slices_total": [int(meta.get("slices_total") or n)] * n,
                    "slices_embedded": [int(meta.get("slices_embedded")
                                            or n)] * n,
                    "chunk_seconds": [int(meta.get("chunk_seconds") or 6)] * n,
                    "chunk_overlap_seconds": [
                        int(meta.get("chunk_overlap_seconds") or 1)] * n,
                    "embedding_model": [embedding_model] * n,
                    "source_path": [str(meta.get("source_path") or "")] * n,
                    "analyzed_at": [now] * n,
                }
                arrow = pa.Table.from_pydict(rows, schema=_build_schema())
                table.delete(f"md5 = '{md5}'")
                table.add(arrow)
                self._evict_if_needed(table)
                _LOG.info("wechat video cache: saved md5=%s (%d chunks, "
                          "model=%s)", md5[:12], n, embedding_model)
                return True
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("wechat video cache: save failed for %s "
                             "(skipped): %s", md5[:12], exc)
                return False

    def _evict_if_needed(self, table) -> None:
        """按视频条数（distinct md5）LRU：超上限删 analyzed_at 最旧的。"""
        _, max_entries = _resolve_cache_config()
        try:
            sel = (table.search()
                   .select(["md5", "analyzed_at"])
                   .limit(_LRU_SCAN_ROWS)
                   .to_arrow())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: evict scan failed: %s", exc)
            return
        seen: Dict[str, float] = {}
        for md5, at in zip(sel.column("md5").to_pylist(),
                           sel.column("analyzed_at").to_pylist()):
            prev = seen.get(md5)
            if prev is None or (at or 0) > prev:
                seen[md5] = at or 0.0
        if len(seen) <= max_entries:
            return
        stale = sorted(seen.items(), key=lambda kv: kv[1])[
            :len(seen) - max_entries]
        for md5, _at in stale:
            try:
                table.delete(f"md5 = '{md5}'")
                _LOG.info("wechat video cache: evicted md5=%s "
                          "(analyzed_at=%.0f, entries>=%d)",
                          md5[:12], _at, max_entries)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("wechat video cache: evict %s failed: %s",
                             md5[:12], exc)

    # ------------------------------------------------------------ 管理
    def stats(self) -> dict:
        """调试 / UI"清空缓存"提示用：条目数 / 行数 / 配置快照。"""
        enabled, max_entries = _resolve_cache_config()
        table = self._ensure_table() if enabled else None
        if table is None:
            return {"enabled": enabled, "max_entries": max_entries,
                    "videos": 0, "rows": 0}
        try:
            with self._lock:
                rows = table.count_rows()
                sel = (table.search()
                       .select(["md5"])
                       .limit(_LRU_SCAN_ROWS).to_arrow())
                return {"enabled": True, "max_entries": max_entries,
                        "videos": len(set(sel.column("md5").to_pylist())),
                        "rows": rows}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: stats failed: %s", exc)
            return {"enabled": enabled, "max_entries": max_entries,
                    "videos": 0, "rows": 0}

    def list_videos(self, channel: Optional[str] = None) -> List[dict]:
        """UI"已缓存视频"窗口数据源：按 (md5, chunk 参数) 聚合的条目列表。

        每条 = 一个可检索的独立索引单元（同视频不同切片参数是不同
        行）；``source_path`` 为空（微信管线分析完即删文件）→ UI 侧
        frame 功能置灰。

        **渠道过滤**：``source_path`` 同时承担渠道判别 —— 微信管线
        固定存 ``""`` 占位、web（VCS）恒存真实本地路径（入库约定，
        不随文件存亡漂移），因此：

        * ``channel="web"`` → 只返回 ``source_path != ''`` 的条目
          （web 窗口/勾选/行删除的数据源；微信条目不进 web 界面）；
        * ``channel=None``（默认）→ 全量（微信工具链路如需枚举时的
          兼容入口）。

        ``lookup`` / 命中短路 / LRU 淘汰不受影响 —— 跨入口命中复用
        保持（表共享、展示隔离）。
        """
        table = self._ensure_table()
        if table is None:
            return []
        where = None
        if channel == "web":
            where = "source_path != ''"
        try:
            with self._lock:
                builder = table.search().select(
                    ["md5", "filename", "source_path",
                     "chunk_seconds", "chunk_overlap_seconds",
                     "duration_s", "analyzed_at"])
                if where:
                    builder = builder.where(where)
                tbl = (builder
                       .limit(_LRU_SCAN_ROWS).to_arrow())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: list failed: %s", exc)
            return []
        if tbl.num_rows == 0:
            return []
        grouped: Dict[tuple, dict] = {}
        for i in range(tbl.num_rows):
            md5 = str(tbl.column("md5")[i].as_py() or "")
            chunk_sec = int(tbl.column("chunk_seconds")[i].as_py() or 0)
            overlap = int(tbl.column("chunk_overlap_seconds")[i].as_py() or 0)
            fname = str(tbl.column("filename")[i].as_py() or "")
            spath = str(tbl.column("source_path")[i].as_py() or "")
            dur = float(tbl.column("duration_s")[i].as_py() or 0.0)
            at = float(tbl.column("analyzed_at")[i].as_py() or 0.0)
            key = (md5, chunk_sec, overlap)
            g = grouped.setdefault(key, {
                "md5": md5, "filename": fname, "source_path": spath,
                "chunk_seconds": chunk_sec,
                "chunk_overlap_seconds": overlap,
                "duration_s": dur, "slices": 0, "analyzed_at": at,
            })
            g["slices"] += 1
            if (at or 0.0) > g["analyzed_at"]:
                g["analyzed_at"] = at
            if spath and not g["source_path"]:
                g["source_path"] = spath
        return sorted(grouped.values(), key=lambda g: -g["analyzed_at"])

    def delete_video(self, md5: str, chunk_seconds: int,
                     chunk_overlap_seconds: int) -> int:
        """按行（md5 × 切片参数三元键）删除条目，返回删除行数。

        同 md5 其它切片参数档的索引保留 —— UI 删除按钮按行删除。
        """
        if not md5:
            return 0
        table = self._ensure_table()
        if table is None:
            return 0
        where = f"md5 = '{md5}'"
        if chunk_seconds:
            where += f" AND chunk_seconds = {int(chunk_seconds)}"
        if chunk_overlap_seconds:
            where += (f" AND chunk_overlap_seconds = "
                      f"{int(chunk_overlap_seconds)}")
        try:
            with self._lock:
                before = table.count_rows()
                table.delete(where)
                after = table.count_rows()
                deleted = before - after
                if deleted:
                    _LOG.info("wechat video cache: deleted %d rows "
                              "(md5=%s chunk=%s/overlap=%s)",
                              deleted, md5[:12], chunk_seconds,
                              chunk_overlap_seconds)
                return deleted
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: delete failed (%s): %s",
                         md5[:12], exc)
            return 0

    def clear(self) -> int:
        """清空缓存（UI"清空缓存"按钮 / 管理用途），返回删除的视频条数。"""
        table = self._ensure_table()
        if table is None:
            return 0
        with self._lock:
            try:
                sel = (table.search()
                       .select(["md5"])
                       .limit(_LRU_SCAN_ROWS).to_arrow())
                videos = len(set(sel.column("md5").to_pylist()))
                if videos == 0:
                    return 0
                table.delete("md5 != ''")
                _LOG.info("wechat video cache: cleared %d entries", videos)
                return videos
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("wechat video cache: clear failed: %s", exc)
                return 0


def _resolve_enabled() -> bool:
    return _resolve_cache_config()[0]


_cache: Optional[WechatVideoCache] = None
_cache_lock = threading.Lock()


def get_video_cache() -> Optional[WechatVideoCache]:
    """lazy 单例；``cache_enabled=false`` 时返回 None（调用方旁路）。

    lancedb 初始化失败不返回 None —— 返回实例，由 lookup/save 内部
    各自失败静默降级，避免配置半开时行为不一致。
    """
    global _cache
    if not _resolve_enabled():
        return None
    with _cache_lock:
        if _cache is None:
            _cache = WechatVideoCache()
        return _cache
