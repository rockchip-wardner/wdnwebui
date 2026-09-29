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

数据目录：``app/uploads/lancedb/``（uploads/ 已 gitignore）。
LanceDB 连接无需显式关闭（本地嵌入式，无网络资源），因此没有
shutdown hook。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

_LOG = logging.getLogger("wechat_video_cache")

_DB_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "lancedb"
_TABLE_NAME = "wechat_video_index"

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
        ("kind", pa.string()),
        ("every_n", pa.int32()),
        ("prompt", pa.string()),
        ("prompt_hash", pa.string()),
        ("events_json", pa.string()),
        ("scene_boundaries_json", pa.string()),
        ("run_id", pa.string()),
        ("frame_count", pa.int32()),
        ("dirty_count", pa.int32()),
        ("state", pa.string()),
        ("summary", pa.string()),
        ("meta_json", pa.string()),
    ])


_KIND_RETRIEVAL = "retrieval"
_KIND_ANALYSIS = "analysis"

# 各新列的 SQL 默认值（迁移用；retrieval 行 kind='retrieval' + 其余空/零）。
_NEW_COLUMN_DEFAULTS = {
    "kind": "'retrieval'",
    "every_n": "0",
    "prompt": "''",
    "prompt_hash": "''",
    "events_json": "''",
    "scene_boundaries_json": "''",
    "run_id": "''",
    "frame_count": "0",
    "dirty_count": "0",
    "state": "''",
    "summary": "''",
    "meta_json": "''",
}


def _migrate_schema(table) -> None:
    """schema migration：补齐 ``source_path`` + 多 kind 列。

    历史：
    * 补 ``source_path`` 列（空串默认，见渠道隔离）。
    * 补 ``kind`` / ``every_n`` / ``prompt`` / ``prompt_hash`` /
      ``events_json`` / ``scene_boundaries_json`` / ``run_id`` /
      ``frame_count`` / ``dirty_count`` / ``state`` / ``summary``；
      旧行一律 ``kind='retrieval'``（迁移前表里只有检索数据）。

    lancedb 0.34 的 ``add_columns`` 接受 ``{col: sql_default}`` 形状
    （实测），**持久化**且幂等；失败的列兜底为 drop + 新 schema 重建
    （缓存是可再生资源，旧条目 source_path 为空 → UI 勾选检索时该条目
    frame 置灰，功能不受影响；分析缓存同样可再生）。
    """
    names = set(table.schema.names)
    missing = [(c, d) for c, d in
               ({"source_path": "''", **_NEW_COLUMN_DEFAULTS}).items()
               if c not in names]
    if not missing:
        return
    try:
        table.add_columns(dict(missing))
        _LOG.info("wechat video cache: added columns %s via add_columns",
                  [c for c, _ in missing])
        return
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("wechat video cache: add_columns failed (%s) — "
                     "rebuilding table", exc)
    # 兜底：读全行 → drop → 按新 schema 重建 → 原行写回（缺列填默认）
    try:
        import pyarrow as pa
        old_tbl = table.search().limit(_LRU_SCAN_ROWS).to_arrow()
        db = None
        try:
            db_path = str(_DB_DIR)
            import lancedb as _lancedb
            db = _lancedb.connect(db_path)
        except Exception:  # noqa: BLE001
            db = None
        if db is not None:
            db.drop_table(_TABLE_NAME)
            new = db.create_table(
                _TABLE_NAME, data=None, schema=_build_schema(), mode="create")
            if old_tbl.num_rows:
                n = old_tbl.num_rows
                keep = list(old_tbl.column_names)
                rows = {c: old_tbl.column(c).to_pylist() for c in keep}
                # 旧的 retrieval 行补 kind；source_path 缺失补空串；
                # 其余新列一律用默认值填充。
                py_defaults = {
                    "kind": _KIND_RETRIEVAL,
                    "source_path": "",
                    "every_n": 0, "prompt": "", "prompt_hash": "",
                    "events_json": "", "scene_boundaries_json": "",
                    "run_id": "", "frame_count": 0, "dirty_count": 0,
                    "state": "done", "summary": "", "meta_json": "",
                }
                for c, dv in py_defaults.items():
                    if c not in rows:
                        rows[c] = [dv] * n
                rows["kind"] = [_KIND_RETRIEVAL] * n
                rows = _complete_rows(rows)
                new.add(pa.Table.from_pydict(
                    {c: rows[c] for c in _build_schema().names},
                    schema=_build_schema()))
            _LOG.info("wechat video cache: rebuilt table with new schema "
                      "(%d legacy rows preserved)", old_tbl.num_rows)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("wechat video cache: schema migration failed "
                     "(clear cache and re-analyze to rebuild): %s", exc)


def _complete_rows(rows: Dict[str, list]) -> Dict[str, list]:
    """按 ``_build_schema()`` 补齐 ``rows`` 中缺失的列（默认值兜底）。

    背景：``meta_json`` 事故 —— 给 schema 新增列时只更新了
    ``save_analysis`` 而漏了 ``save``，``pa.Table.from_pydict(rows,
    schema=...)`` 直接抛 ``The passed mapping doesn't contain the
    following field(s) of the schema: meta_json``，异常被写入点的
    ``except`` 吞成 LOG warning → **静默丢数据**（VCS 提取的 29 片索引
    全部没入库，前端历史列表看不到）。

    所有写入点（``save`` / ``save_analysis``）在 ``from_pydict`` 前调用
    本函数 —— 以后再加 schema 列无需同步改每个写入点。
    """
    import pyarrow as pa
    if not rows:
        return rows
    n = len(next(iter(rows.values())))
    for f in _build_schema():
        if f.name in rows:
            continue
        if pa.types.is_integer(f.type):
            default: Any = 0
        elif pa.types.is_floating(f.type):
            default = 0.0
        elif (pa.types.is_list(f.type)
              or pa.types.is_fixed_size_list(f.type)):
            default = None  # vec：分析行为 null（不参与向量检索）
        else:
            default = ""
        rows[f.name] = [default] * n
    return rows


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
                _migrate_schema(self._table)
                return self._table
            except Exception as exc:  # noqa: BLE001
                self._init_failed = True
                _LOG.warning("wechat video cache: lancedb init failed "
                             "(cache disabled this run): %s", exc)
                return None

    # ------------------------------------------------------------ 查询
    def checkout_latest(self) -> None:
        """把 table 视图刷新到最新 manifest（外部进程写入后可见）。

        LanceDB 是嵌入式存储：``open_table`` 后 table 对象持有当时
        manifest 的视图。同进程内的写入（VCS / 微信管线的 ``save``）
        立即可见；**其它进程**（脚本 / 手工修库 / 未来多写者）的写入
        需要显式 ``checkout_latest()``。读取侧端点（``/history`` /
        ``/cache/list`` / ``/cache/stats``）调用本方法兜底。
        """
        table = self._ensure_table()
        if table is None:
            return
        try:
            table.checkout_latest()
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("wechat video cache: checkout_latest failed: %s", exc)

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
                 f"embedding_model = '{embedding_model}' AND "
                 f"kind = '{_KIND_RETRIEVAL}'")
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
                    "kind": [_KIND_RETRIEVAL] * n,
                    "every_n": [0] * n,
                    "prompt": [""] * n,
                    "prompt_hash": [""] * n,
                    "events_json": [""] * n,
                    "scene_boundaries_json": [""] * n,
                    "run_id": [""] * n,
                    "frame_count": [0] * n,
                    "dirty_count": [0] * n,
                    "state": ["done"] * n,
                    "summary": [""] * n,
                }
                arrow = pa.Table.from_pydict(
                    _complete_rows(rows), schema=_build_schema())
                # 只覆盖同 md5 的检索行 —— 不能误删同 md5 的分析缓存行。
                table.delete(f"md5 = '{md5}' AND kind = '{_KIND_RETRIEVAL}'")
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
        """按视频条数（distinct md5）**分 kind** LRU：超上限删最旧。

        检索 / 分析两类缓存各自独立计数与淘汰（互不影响）—— 分析
        条目被淘汰不会动检索索引，反之亦然。
        """
        _, max_entries = _resolve_cache_config()
        for kind in (_KIND_RETRIEVAL, _KIND_ANALYSIS):
            try:
                sel = (table.search()
                       .where(f"kind = '{kind}'")
                       .select(["md5", "analyzed_at"])
                       .limit(_LRU_SCAN_ROWS)
                       .to_arrow())
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("wechat video cache: evict scan (%s) failed: %s",
                             kind, exc)
                continue
            seen: Dict[str, float] = {}
            for md5, at in zip(sel.column("md5").to_pylist(),
                               sel.column("analyzed_at").to_pylist()):
                prev = seen.get(md5)
                if prev is None or (at or 0) > prev:
                    seen[md5] = at or 0.0
            if len(seen) <= max_entries:
                continue
            stale = sorted(seen.items(), key=lambda kv: kv[1])[
                :len(seen) - max_entries]
            for md5, _at in stale:
                try:
                    table.delete(f"md5 = '{md5}' AND kind = '{kind}'")
                    _LOG.info("wechat video cache: evicted %s md5=%s "
                              "(analyzed_at=%.0f, entries>=%d)",
                              kind, md5[:12], _at, max_entries)
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("wechat video cache: evict %s %s failed: %s",
                                 kind, md5[:12], exc)

    def save_analysis(self, md5: str, every_n: int, prompt: str,
                      prompt_hash: str, run_id: str, events: List[dict],
                      scene_boundaries: List, summary: str,
                      meta: Dict[str, Any],
                      payload: Optional[Dict[str, Any]] = None) -> bool:
        """逐帧分析结果入库（**一行一视频**，delete + add 幂等覆盖）。

        与检索缓存不同：分析缓存的主体是 ``events_json``（逐帧结构化
        结果序列化）+ ``scene_boundaries_json``（综述分组边界，必须随
        缓存保存，否则命中后综述退化为固定 15 帧切块）；``vec`` 为
        ``null``（分析缓存不参与向量检索）。

        缓存键 = ``(md5, every_n, prompt_hash)`` —— 逐帧结果由"分析
        要求"（prompt）问出，换 prompt 必须重新逐帧（正确性）。同一
        视频不同 every_n / prompt 是**独立行**，各自占 LRU 槽。

        仅调用方确认全成功（done）时入库。
        """
        if not md5 or not run_id:
            return False
        table = self._ensure_table()
        if table is None:
            return False
        with self._lock:
            try:
                import pyarrow as pa
                now = time.time()
                rows = {
                    "md5": [md5],
                    "chunk_idx": [-1],
                    "start_time": [0.0],
                    "end_time": [0.0],
                    "fps": [float(meta.get("fps") or 0.0)],
                    "vec": [None],
                    "filename": [str(meta.get("filename") or "")],
                    "source_path": [str(meta.get("source_path") or "")],
                    "duration_s": [float(meta.get("duration_s") or 0.0)],
                    "slices_total": [0],
                    "slices_embedded": [0],
                    "chunk_seconds": [0],
                    "chunk_overlap_seconds": [0],
                    "embedding_model": [""],
                    "analyzed_at": [now],
                    "kind": [_KIND_ANALYSIS],
                    "every_n": [int(every_n)],
                    "prompt": [str(prompt or "")],
                    "prompt_hash": [str(prompt_hash or "")],
                    "events_json": [json.dumps(events or [],
                                               ensure_ascii=False)],
                    "scene_boundaries_json": [json.dumps(
                        scene_boundaries or [], ensure_ascii=False)],
                    "run_id": [str(run_id)],
                    "frame_count": [int(len(events or []))],
                    "dirty_count": [int(meta.get("dirty_count") or 0)],
                    "state": ["done"],
                    "summary": [str(summary or "")],
                    "meta_json": [json.dumps(payload or {},
                                             ensure_ascii=False)],
                }
                arrow = pa.Table.from_pydict(
                    _complete_rows(rows), schema=_build_schema())
                # 同 (md5, every_n, prompt_hash) 幂等覆盖；不碰检索行。
                table.delete(
                    f"md5 = '{md5}' AND kind = '{_KIND_ANALYSIS}' AND "
                    f"every_n = {int(every_n)} AND "
                    f"prompt_hash = '{prompt_hash}'")
                table.add(arrow)
                self._evict_if_needed(table)
                _LOG.info("wechat video cache: saved analysis md5=%s "
                          "(every_n=%d, %d frames, run=%s)",
                          md5[:12], int(every_n), len(events or []),
                          str(run_id)[:8])
                return True
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("wechat video cache: save_analysis failed for "
                             "%s (skipped): %s", md5[:12], exc)
                return False

    def lookup_analysis(self, md5: str, every_n: int,
                        prompt_hash: str) -> Optional[dict]:
        """按 ``(md5, every_n, prompt_hash)`` 取逐帧分析缓存；未命中 None。

        返回 ``{events, scene_boundaries, summary, run_id, every_n,
        prompt, filename, source_path, duration_s, fps, frame_count,
        dirty_count, analyzed_at}``（``events`` / ``scene_boundaries``
        已从 JSON 反序列化）。
        """
        if not md5:
            return None
        table = self._ensure_table()
        if table is None:
            return None
        where = (f"md5 = '{md5}' AND kind = '{_KIND_ANALYSIS}' AND "
                 f"every_n = {int(every_n)} AND "
                 f"prompt_hash = '{prompt_hash}'")
        try:
            tbl = (table.search().where(where).limit(1).to_arrow())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: lookup_analysis failed for "
                         "%s: %s", md5[:12], exc)
            return None
        if tbl.num_rows == 0:
            return None
        return self._analysis_row_to_dict(tbl, 0)

    def get_analysis_by_run_id(self, run_id: str) -> Optional[dict]:
        """按 run_id 取分析条目（VFA 历史加载 / meta 用）。"""
        if not run_id:
            return None
        table = self._ensure_table()
        if table is None:
            return None
        where = f"run_id = '{run_id}' AND kind = '{_KIND_ANALYSIS}'"
        try:
            tbl = (table.search().where(where).limit(1).to_arrow())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: get_analysis_by_run_id "
                         "failed (%s): %s", str(run_id)[:8], exc)
            return None
        if tbl.num_rows == 0:
            return None
        return self._analysis_row_to_dict(tbl, 0)

    @staticmethod
    def _analysis_row_to_dict(tbl, i: int) -> dict:
        col = lambda c: tbl.column(c)[i].as_py()  # noqa: E731
        try:
            events = json.loads(col("events_json") or "[]")
        except Exception:  # noqa: BLE001
            events = []
        try:
            bounds = json.loads(col("scene_boundaries_json") or "[]")
        except Exception:  # noqa: BLE001
            bounds = []
        try:
            payload = json.loads(col("meta_json") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return {
            "md5": str(col("md5") or ""),
            "run_id": str(col("run_id") or ""),
            "every_n": int(col("every_n") or 0),
            "prompt": str(col("prompt") or ""),
            "prompt_hash": str(col("prompt_hash") or ""),
            "events": events if isinstance(events, list) else [],
            "scene_boundaries": bounds if isinstance(bounds, list) else [],
            "summary": str(col("summary") or ""),
            "filename": str(col("filename") or ""),
            "source_path": str(col("source_path") or ""),
            "duration_s": float(col("duration_s") or 0.0),
            "fps": float(col("fps") or 0.0),
            "frame_count": int(col("frame_count") or 0),
            "dirty_count": int(col("dirty_count") or 0),
            "state": str(col("state") or ""),
            "analyzed_at": float(col("analyzed_at") or 0.0),
            "payload": payload if isinstance(payload, dict) else {},
        }

    def list_analysis(self, channel: Optional[str] = None) -> List[dict]:
        """VFA 历史列表（不含 events_json，仅元数据），按 analyzed_at 倒序。

        ``channel="web"`` → 只返回 ``source_path != ''``（与检索侧渠道
        隔离一致）。
        """
        table = self._ensure_table()
        if table is None:
            return []
        where = f"kind = '{_KIND_ANALYSIS}'"
        if channel == "web":
            where += " AND source_path != ''"
        try:
            with self._lock:
                tbl = (table.search()
                       .where(where)
                       .select(["md5", "run_id", "every_n", "prompt",
                                "prompt_hash", "frame_count", "dirty_count",
                                "state", "filename", "source_path",
                                "duration_s", "fps", "analyzed_at",
                                "summary", "meta_json"])
                       .limit(_LRU_SCAN_ROWS).to_arrow())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: list_analysis failed: %s", exc)
            return []
        out: List[dict] = []
        for i in range(tbl.num_rows):
            col = lambda c: tbl.column(c)[i].as_py()  # noqa: E731
            try:
                payload = json.loads(col("meta_json") or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
            out.append({
                "md5": str(col("md5") or ""),
                "run_id": str(col("run_id") or ""),
                "every_n": int(col("every_n") or 0),
                "prompt": str(col("prompt") or ""),
                "prompt_hash": str(col("prompt_hash") or ""),
                "frame_count": int(col("frame_count") or 0),
                "dirty_count": int(col("dirty_count") or 0),
                "state": str(col("state") or ""),
                "filename": str(col("filename") or ""),
                "source_path": str(col("source_path") or ""),
                "duration_s": float(col("duration_s") or 0.0),
                "fps": float(col("fps") or 0.0),
                "analyzed_at": float(col("analyzed_at") or 0.0),
                "summary": str(col("summary") or ""),
                "payload": payload if isinstance(payload, dict) else {},
            })
        return sorted(out, key=lambda r: -r["analyzed_at"])

    def delete_analysis_by_run_id(self, run_id: str) -> int:
        """按 run_id 删除分析条目，返回删除行数。"""
        if not run_id:
            return 0
        table = self._ensure_table()
        if table is None:
            return 0
        where = f"run_id = '{run_id}' AND kind = '{_KIND_ANALYSIS}'"
        return self._delete_rows(table, where, f"run_id={str(run_id)[:8]}")

    def delete_analysis(self, md5: str, every_n: int,
                        prompt_hash: str) -> int:
        """按 (md5, every_n, prompt_hash) 删除分析条目。"""
        if not md5:
            return 0
        table = self._ensure_table()
        if table is None:
            return 0
        where = (f"md5 = '{md5}' AND kind = '{_KIND_ANALYSIS}' AND "
                 f"every_n = {int(every_n)} AND "
                 f"prompt_hash = '{prompt_hash}'")
        return self._delete_rows(table, where, f"md5={md5[:12]}")

    def _delete_rows(self, table, where: str, label: str) -> int:
        try:
            with self._lock:
                before = table.count_rows()
                table.delete(where)
                after = table.count_rows()
                deleted = before - after
                if deleted:
                    _LOG.info("wechat video cache: deleted %d rows (%s)",
                              deleted, label)
                return deleted
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: delete failed (%s): %s",
                         label, exc)
            return 0

    def list_history_grouped(self, channel: str = "web") -> List[dict]:
        """按 md5 聚合的"历史记录"数据源（前端一个视频一行 + 展开子行）。

        每项::

            {md5, filename, source_path, duration_s, fps, updated_at,
             retrieval: [{chunk_seconds, chunk_overlap_seconds, slices,
                          analyzed_at}],
             analysis:  [{every_n, run_id, frame_count, dirty_count,
                          state, summary, analyzed_at}]}

        排序按 ``updated_at`` 倒序。``frame_enabled`` 由 API 层按
        ``source_path`` 是否仍存在判定（本方法不碰文件系统）。
        ``fps`` 供前端从历史记录选中时推算视频总帧数（反查 modal 的
        时间范围 hint 用）。
        """
        items: Dict[str, dict] = {}

        def _get(md5, filename, source_path, duration_s, at, fps):
            it = items.get(md5)
            if it is None:
                it = {"md5": md5, "filename": filename,
                      "source_path": source_path, "duration_s": duration_s,
                      "fps": float(fps or 0.0),
                      "updated_at": at, "retrieval": [], "analysis": []}
                items[md5] = it
            else:
                if filename and not it["filename"]:
                    it["filename"] = filename
                if source_path and not it["source_path"]:
                    it["source_path"] = source_path
                if duration_s and not it["duration_s"]:
                    it["duration_s"] = duration_s
                if fps and not it["fps"]:
                    it["fps"] = float(fps)
                if at > it["updated_at"]:
                    it["updated_at"] = at
            return it

        for v in self.list_videos(channel=channel):
            it = _get(v["md5"], v["filename"], v["source_path"],
                      v["duration_s"], v["analyzed_at"], v.get("fps") or 0.0)
            it["retrieval"].append({
                "chunk_seconds": v["chunk_seconds"],
                "chunk_overlap_seconds": v["chunk_overlap_seconds"],
                "slices": v["slices"],
                "analyzed_at": v["analyzed_at"],
            })

        for a in self.list_analysis(channel=channel):
            it = _get(a["md5"], a["filename"], a["source_path"],
                      a["duration_s"], a["analyzed_at"], a.get("fps") or 0.0)
            it["analysis"].append({
                "every_n": a["every_n"],
                "run_id": a["run_id"],
                "frame_count": a["frame_count"],
                "dirty_count": a["dirty_count"],
                "state": a["state"],
                "summary": a["summary"],
                "analyzed_at": a["analyzed_at"],
                # 提示词（用户"分析要求"；含追加的 schema hint）—— 前端
                # 子行展示摘要用，同 every_n 不同提示词的记录可区分。
                "prompt": a.get("prompt", ""),
            })

        for it in items.values():
            it["retrieval"].sort(key=lambda r: -r["analyzed_at"])
            it["analysis"].sort(key=lambda r: -r["analyzed_at"])
        return sorted(items.values(), key=lambda x: -x["updated_at"])

    # ------------------------------------------------------------ 管理
    def stats(self) -> dict:
        """调试 / UI"清空缓存"提示用：条目数 / 行数 / 配置快照。

        ``videos`` / ``rows`` 只统计**检索**侧（UI"清空缓存"按钮的
        语义 = 清检索缓存）；分析侧单列 ``analysis_videos`` /
        ``analysis_rows`` 供展示。
        """
        enabled, max_entries = _resolve_cache_config()
        table = self._ensure_table() if enabled else None
        empty = {"enabled": enabled, "max_entries": max_entries,
                 "videos": 0, "rows": 0,
                 "analysis_videos": 0, "analysis_rows": 0}
        if table is None:
            return empty
        try:
            with self._lock:
                def _count(kind: str):
                    sel = (table.search()
                           .where(f"kind = '{kind}'")
                           .select(["md5"])
                           .limit(_LRU_SCAN_ROWS).to_arrow())
                    return len(set(sel.column("md5").to_pylist())), sel.num_rows
                vids, rows = _count(_KIND_RETRIEVAL)
                avids, arows = _count(_KIND_ANALYSIS)
                return {"enabled": True, "max_entries": max_entries,
                        "videos": vids, "rows": rows,
                        "analysis_videos": avids, "analysis_rows": arows}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("wechat video cache: stats failed: %s", exc)
            return empty

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
        where = f"kind = '{_KIND_RETRIEVAL}'"
        if channel == "web":
            where += " AND source_path != ''"
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
        where = f"md5 = '{md5}' AND kind = '{_KIND_RETRIEVAL}'"
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
        """清空**全部**缓存（检索 + 分析），返回删除的视频数（distinct md5）。

        全清两种 kind。注意分析记录同时是
        「视频检索分析」页历史列表里分析子行的数据源，清空后这些子行
        消失（历史列表只剩检索档，或整条消失）。
        """
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
                table.delete(
                    f"kind = '{_KIND_RETRIEVAL}' OR kind = '{_KIND_ANALYSIS}'")
                _LOG.info(
                    "wechat video cache: cleared %d videos "
                    "(retrieval + analysis)", videos)
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


def close_wechat_video_cache() -> None:
    """shutdown hook —— LanceDB 无需显式关闭，仅重置单例。"""
    global _cache
    with _cache_lock:
        _cache = None
