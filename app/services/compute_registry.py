"""算力注册中心：算力卡 + 模型实例的统一管理。

设计目标：

* 所有 LLMSession 都从这里出，**任何业务模块（视频检测 / 语音生成 / 视频监控）
  都禁止自己初始化** —— 找不到对应 capability 就报错"功能未启用"。
* 启动阶段不阻塞 FastAPI：``apply_boot_config_async()`` 起一个 daemon thread
  串行从 config.json 加载实例，每条实例状态机走
  ``queued → loading → running``，附带时间戳供前端算倒计时。
* wdiot 注册发现这一轮**不做** —— capability 仅作内部 key 用，
  discovery 页面只读 registry 内存状态。

时间字段：

* ``queued_at``  —— 进队列时刻（start_one 返回那一刻）
* ``started_at`` —— status 转 loading 的时刻
* ``finished_at`` —— status 转 running/error 的时刻

前端倒计时（每秒重算）：

* 对 ``queued`` 实例： ``(同 npu 前面 queued+loading 数) × MODEL_PRESETS[model_id].estimated_init_seconds``
* 对 ``loading`` 实例： ``max(0, MODEL_PRESETS[model_id].estimated_init_seconds - (now - started_at))``
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from app.services.config import get_config


# --------------------------------------------------------------------------- #
# 硬件能力（HW）目录 —— 注册发现页面与系统配置页共用同一份 source-of-truth。
# 顺序决定 UI 渲染顺序；新增 HW 能力时在这里登记并同步 wdiot 子类。
# --------------------------------------------------------------------------- #
HARDWARE_CAPABILITIES: list[dict] = [
    {
        "capability_type": "hwcap_video_input",
        "class_name":      "HWVideoInput",
        "category":        "video",
        "label":           "视频输入（摄像头）",
        "description":     "采集视频帧，供其它设备订阅。",
    },
    {
        "capability_type": "hwcap_audio_input",
        "class_name":      "HWAudioInput",
        "category":        "audio",
        "label":           "音频输入（麦克风）",
        "description":     "采集音频数据，供其它设备订阅。",
    },
    {
        "capability_type": "hwcap_audio_output",
        "class_name":      "HWAudioOutput",
        "category":        "audio",
        "label":           "音频输出（扬声器）",
        "description":     "对外播放音频，配合 swcap_audio_tts 一起使用。",
    },
    {
        "capability_type": "hwcap_hdmi_display",
        "class_name":      "HWHDMIDisplay",
        "category":        "video",
        "label":           "HDMI 显示输出",
        "description":     "把视频 / 画面推送到 HDMI 显示设备。",
    },
]


def _hardware_capability_default_state() -> dict[str, bool]:
    """首次配置时所有 HW 能力默认启用。"""
    return {entry["capability_type"]: True for entry in HARDWARE_CAPABILITIES}


def _load_hardware_capability_state() -> dict[str, bool]:
    """从 ``config.json["compute"]["hardware_capabilities"]`` 读启用状态。

    缺失字段默认 ``True``（"全部启用"），但**只在内存中**补齐 —— 不主动写
    回磁盘；用户实际改 checkbox 并保存时才落盘，避免冷启动就写一次
    config.json。
    """
    cfg = get_config().get_section("compute", {})
    stored = cfg.get("hardware_capabilities") if isinstance(cfg, dict) else None
    state = _hardware_capability_default_state()
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k in state and isinstance(v, bool):
                state[k] = v
    return state


LOG = logging.getLogger("compute_registry")


# 默认倒计时兜底（per-model 没有 ``estimated_init_seconds`` 字段时使用）。
DEFAULT_ESTIMATED_INIT_SECONDS = 10


# --------------------------------------------------------------------------- #
# preset 映射表 —— 唯一来源；任何新增 model 必须在这里注册。
# --------------------------------------------------------------------------- #
def _qwen3_vl_4b_spec():
    from rkai import SystemLanguageModel  # 懒导入：保持 rkai native lib 懒加载
    return SystemLanguageModel.qwen3_vl_4b()


def _qwen3_tts_spec():
    from rkai import SystemLanguageModel
    return SystemLanguageModel.qwen3_tts()


def _qwen3_asr_spec():
    from rkai import SystemLanguageModel
    return SystemLanguageModel.qwen3_asr()


# 每个 model 的 ``estimated_init_seconds`` 是经验值，用来渲染"加载中"倒计时
# 的初始起点；loading 段倒计时按 (estimate - (now - started_at)) 实算，estimate
# 只影响倒计时的**起点**。真机冷启动实测（page cache dropped 之后）：
#   * Qwen3-VL-4B: 57s → +3s buffer → 60s
#   * Qwen3-TTS:   改 rkai 加上 ensure_loaded() 后变成 eager init：
#                  - load_resources:  ~4s
#                  - init_text_projection: ~0.4s
#                  - init_speech_decoder:  ~2s
#                  - init_code_predictor:  ~1.6s
#                  - init_talker (785MB weight + 24MB rknn, 8-core AR): dev box
#                    init_talker 直接 MODEL_SETUP fail，没法在 dev box 测真机
#                    时延；真机用户体感"首次等非常久"，按 VL-4B 同档拍 60s
#                    + buffer → 60s（无真机实测，等你测完再调）
#   * Qwen3-ASR:   未实测（模型文件不在板上），按编码器+LLM 推算 ~15s + 10s buffer → 25s
# 取"最冷"加载时间（即用户首次加载，不是文件页缓存命中后的二次加载）+ 小 buffer，
# 保证倒计时结束时实际加载大概率已就绪，避免 UI 卡很久才更新。
MODEL_PRESETS: dict[str, dict] = {
    "qwen3-vl-4b": {
        "label":            "Qwen3-VL-4B",
        "preset_factory":   _qwen3_vl_4b_spec,
        "model_dir_name":   "Qwen3-VL-4B",
        "memory_estimate_mb": 3500,
        "estimated_init_seconds": 60,
        "capability_types": ["swcap_video_describe"],
        "instance_name":    "llm-vl",
    },
    "qwen3-tts": {
        "label":            "Qwen3-TTS",
        "preset_factory":   _qwen3_tts_spec,
        "model_dir_name":   "Qwen3-TTS",
        "memory_estimate_mb": 1900,
        "estimated_init_seconds": 30,
        "capability_types": ["swcap_audio_tts"],
        "instance_name":    "llm-tts",
    },
    "qwen3-asr": {
        "label":            "Qwen3-ASR",
        "preset_factory":   _qwen3_asr_spec,
        "model_dir_name":   "Qwen3-ASR",
        "memory_estimate_mb": 2500,
        "estimated_init_seconds": 25,
        "capability_types": ["swcap_audio_query", "swcap_audio_analyse"],
        "instance_name":    "llm-asr",
    },
}


# --------------------------------------------------------------------------- #
# 老 capability_type 字符串 → 新字符串（hwcap_/swcap_ 前缀）
# 在 ComputeRegistry.__init__ 末尾一次性把 config.json["compute"]["instances"]
# 里的旧字段改写成新字符串并写回磁盘，避免老配置直接失配。
# --------------------------------------------------------------------------- #
_LEGACY_CAPABILITY_TYPE_RENAMES: dict[str, str] = {
    "video_input":    "hwcap_video_input",
    "video_query":    "swcap_video_query",
    "video_describe": "swcap_video_describe",
    "audio_input":    "hwcap_audio_input",
    "audio_output":   "hwcap_audio_output",
    "audio_query":    "swcap_audio_query",
    "audio_analyse":  "swcap_audio_analyse",
    "hdmi_display":   "hwcap_hdmi_display",
    "audio_tts":      "swcap_audio_tts",
    "video_analyse":  "swcap_video_analyse",
    "text_chat":      "swcap_text_chat",
}


# --------------------------------------------------------------------------- #
# ModelInstance
# --------------------------------------------------------------------------- #
@dataclass
class ModelInstance:
    key: Tuple[int, str]                       # (npu_index, model_id)
    npu_index: int
    model_id: str
    device_id: Optional[bytes] = None          # resolved from RKNN3Lite().get_devices_id()[npu_index]
    llm_session: Any = None                    # status="running" 时才有
    capability_types: List[str] = field(default_factory=list)
    instance_name: str = ""
    label: str = ""
    memory_estimate_mb: int = 0
    status: str = "queued"                     # queued | loading | running | error | stopped
    error: str = ""
    queued_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "npu_index":          self.npu_index,
            "model":              self.model_id,
            "label":              self.label,
            "instance_name":      self.instance_name,
            "status":             self.status,
            "error":              self.error,
            "capability_types":   list(self.capability_types),
            "memory_estimate_mb": self.memory_estimate_mb,
            "device_id":          self.device_id.decode("utf-8", errors="replace") if self.device_id else None,
            "queued_at":          self.queued_at,
            "started_at":         self.started_at,
            "finished_at":        self.finished_at,
        }


# --------------------------------------------------------------------------- #
# ComputeRegistry (singleton)
# --------------------------------------------------------------------------- #
class ComputeRegistry:
    """算力注册中心：管理 LLMSession 生命周期 + capability 路由。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._instances: dict[Tuple[int, str], ModelInstance] = {}
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_worker = threading.Event()
        self._wake_event = threading.Event()
        self._cards: list[dict] = []           # 缓存 list_cards() 结果
        self._cards_failed: str = ""           # list_cards 失败的错误信息
        # 一次性把 config.json["compute"]["instances"][].capability_types 里
        # 残留的老字符串（video_query / audio_output / …）改写成新前缀
        # （swcap_video_query / hwcap_audio_output / …）。幂等，老配置会
        # 被原位更新并写回磁盘；不存在旧字段时静默通过。
        self._migrate_legacy_capability_types()

    def _migrate_legacy_capability_types(self) -> None:
        cfg = get_config().get_section("compute", {})
        instances = cfg.get("instances") if isinstance(cfg, dict) else None
        if not isinstance(instances, list):
            return
        renamed_total = 0
        for inst in instances:
            if not isinstance(inst, dict):
                continue
            caps = inst.get("capability_types")
            if not isinstance(caps, list):
                continue
            new_caps = [_LEGACY_CAPABILITY_TYPE_RENAMES.get(c, c) for c in caps]
            if new_caps != caps:
                inst["capability_types"] = new_caps
                renamed_total += sum(
                    1 for old, new in zip(caps, new_caps) if old != new
                )
        if renamed_total:
            cfg["instances"] = instances
            try:
                get_config().set_section("compute", cfg)
            except Exception as e:  # noqa: BLE001
                LOG.warning("failed to persist migrated capability_types: %s", e)
            LOG.info(
                "migrated %d legacy capability_type entries to hwcap_/swcap_ prefix",
                renamed_total,
            )

    # ------------------------------------------------------------------ #
    # 元数据查询
    # ------------------------------------------------------------------ #
    def list_cards(self, force_refresh: bool = False) -> list[dict]:
        """枚举当前 RKNN3 device id 列表。失败（rknn3lite 不可用 / 非 RK 主机）返回 []。"""
        with self._lock:
            if self._cards and not force_refresh:
                return list(self._cards)
        try:
            from rknn3lite.api import RKNN3Lite  # 懒导入
            ids = RKNN3Lite().get_devices_id()
        except Exception as e:
            with self._lock:
                self._cards = []
                self._cards_failed = str(e)
            return []
        cards = []
        for i, x in enumerate(ids):
            raw = x if isinstance(x, (bytes, bytearray)) else str(x).encode("utf-8")
            cards.append({
                "npu_index": i,
                "device_id": raw.decode("utf-8", errors="replace"),
                "_raw_device_id": raw,
            })
        with self._lock:
            self._cards = cards
            self._cards_failed = ""
        return list(cards)

    def cards_failed_reason(self) -> str:
        """``list_cards`` 失败时的错误信息（UI 提示用）。"""
        with self._lock:
            return self._cards_failed

    def list_supported_models(self) -> list[dict]:
        """扫 base.model_path 子目录，结合 preset 白名单返回可用模型。"""
        base = get_config().get_section("base", {})
        model_path = base.get("model_path") or "/userdata/models/"
        root = Path(model_path)
        out = []
        for model_id, preset in MODEL_PRESETS.items():
            dir_name = preset["model_dir_name"]
            if dir_name:
                d = root / dir_name
                exists = d.is_dir()
            else:
                exists = None
            out.append({
                "id":                 model_id,
                "label":              preset["label"],
                "exists":             exists,
                "memory_estimate_mb": preset["memory_estimate_mb"],
                "capability_types":   list(preset["capability_types"]),
            })
        return out

    def snapshot(self) -> dict:
        # worker 存活状态：让 UI 能识别 "worker 已死，但 inst.status 还停在
        # loading" 这种场景（用户会看到"卡住"，需要提示）。
        worker_alive = bool(
            self._worker_thread is not None
            and self._worker_thread.is_alive()
            and not self._stop_worker.is_set()
        )
        with self._lock:
            instances = [inst.to_dict() for inst in self._instances.values()]
        # ``list_cards`` 内部用 ``_raw_device_id`` 缓存 RKNN3 raw bytes，
        # 但 snapshot 是对外接口，不应暴露内部字段。
        cards = [
            {"npu_index": c["npu_index"], "device_id": c["device_id"]}
            for c in self.list_cards()
        ]
        supported = self.list_supported_models()
        # 内存汇总
        per_card_mb: dict[int, int] = {}
        total_mb = 0
        for inst in instances:
            if inst["status"] != "running":
                continue
            per_card_mb[inst["npu_index"]] = per_card_mb.get(inst["npu_index"], 0) + inst["memory_estimate_mb"]
            total_mb += inst["memory_estimate_mb"]
        # 状态计数（instances 来自上面 to_dict()，已是 dict；要按 key 取）
        running_count = sum(1 for inst in instances if inst["status"] == "running")
        queued_count  = sum(1 for inst in instances if inst["status"] == "queued")
        loading_count = sum(1 for inst in instances if inst["status"] == "loading")
        error_count   = sum(1 for inst in instances if inst["status"] == "error")
        # 当前活跃 capability
        capabilities = self.list_active_capabilities()
        # 工具列表（懒导入，避免触发 rkai 装饰器在 compute 模块加载时执行）
        from app.services.tools import get_tool_registry
        tool_reg = get_tool_registry()
        tools = []
        for spec in tool_reg.list_specs():
            available = True
            reason = ""
            if spec.requires_capability:
                try:
                    self.get_session(spec.requires_capability)
                except RuntimeError as e:
                    available = False
                    reason = str(e)
            tools.append({
                "key":                 spec.key,
                "name":                spec.name,
                "description":         spec.description,
                "requires_capability": spec.requires_capability,
                "available":           available,
                "reason":              reason,
            })
        # per-model 启动预估（前端渲染倒计时查这个）
        estimated_per_model = {
            mid: preset.get("estimated_init_seconds", DEFAULT_ESTIMATED_INIT_SECONDS)
            for mid, preset in MODEL_PRESETS.items()
        }
        return {
            "cards":             cards,
            "cards_failed":      self.cards_failed_reason(),
            "supported_models":  supported,
            "instances":         instances,
            "total_memory_mb":   total_mb,
            "per_card_memory_mb": per_card_mb,
            "queue_length":      queued_count + loading_count,
            "running_count":     running_count,
            "queued_count":      queued_count,
            "loading_count":     loading_count,
            "error_count":       error_count,
            "estimated_init_seconds_per_model": estimated_per_model,
            "default_estimated_init_seconds":  DEFAULT_ESTIMATED_INIT_SECONDS,
            "worker_alive":      worker_alive,
            "last_updated":      time.time(),
"capabilities":      capabilities,
            "tools":             tools,
        }

    def list_active_capabilities(self) -> list[dict]:
        """当前 running 实例的 capability 列表（discovery 页面渲染源）。"""
        with self._lock:
            out = []
            for inst in self._instances.values():
                if inst.status != "running":
                    continue
                for cap_type in inst.capability_types:
                    out.append({
                        "type":              cap_type,
                        "name":              inst.instance_name,
                        "model":             inst.model_id,
                        "npu_index":         inst.npu_index,
                        "memory_estimate_mb": inst.memory_estimate_mb,
                    })
        return out

    def list_hardware_capabilities(self) -> list[dict]:
        """所有 HW 能力的静态定义 + 当前启用状态（来自 config.json）。

        discovery 页面与系统配置页都从这里读。enabled 字段反映
        ``config.json["compute"]["hardware_capabilities"]`` 的最新值。
        """
        state = _load_hardware_capability_state()
        out = []
        for entry in HARDWARE_CAPABILITIES:
            item = dict(entry)
            item["enabled"] = bool(state.get(entry["capability_type"], True))
            out.append(item)
        return out

    def set_hardware_capability_state(self, updates: dict[str, bool]) -> dict[str, bool]:
        """更新 HW 能力启用状态。未知 cap_type 静默忽略；非法值返回错误。

        写入 ``config.json["compute"]["hardware_capabilities"]`` 并返回新
        的完整状态（包含未知 key 在内的"全集"），便于 UI 刷新展示。
        """
        if not isinstance(updates, dict):
            raise ValueError("updates 必须是 dict[str, bool]")
        valid_keys = {entry["capability_type"] for entry in HARDWARE_CAPABILITIES}
        new_state = _load_hardware_capability_state()
        applied: list[str] = []
        rejected: list[str] = []
        for k, v in updates.items():
            if k not in valid_keys:
                rejected.append(k)
                continue
            if not isinstance(v, bool):
                raise ValueError(f"{k} 必须是 bool, got {type(v).__name__}")
            new_state[k] = v
            applied.append(k)
        if applied:
            with self._lock:
                cfg = get_config().get_section("compute", {})
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg["hardware_capabilities"] = new_state
                try:
                    get_config().set_section("compute", cfg)
                except Exception as e:  # noqa: BLE001
                    LOG.warning("failed to persist hardware_capabilities: %s", e)
            LOG.info("updated hardware_capabilities: %s", applied)
        return {
            "applied":  applied,
            "rejected": rejected,
            "state":    new_state,
        }

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def start_one(self, npu_index: int, model_id: str) -> ModelInstance:
        """同步建 ModelInstance(status="queued")，返回。后台 worker 拾取后
        转 loading → 调 rkai.LLMSession → running/error。

        Raises:
            ValueError: model_id 不在 preset 白名单 / npu_index 越界 / 已存在。
            RuntimeError: list_cards() 拿不到卡（无法 resolve device_id）。
        """
        if model_id not in MODEL_PRESETS:
            raise ValueError(f"未知 model: {model_id!r}")
        cards = self.list_cards()
        if not cards:
            raise RuntimeError(
                "未检测到任何 NPU 卡（RKNN3Lite.get_devices_id() 返回空）"
            )
        if not (0 <= npu_index < len(cards)):
            raise ValueError(f"npu_index 越界: {npu_index}（当前有 {len(cards)} 张卡）")
        key = (npu_index, model_id)
        with self._lock:
            if key in self._instances:
                raise ValueError(f"重复实例: npu={npu_index} model={model_id}")
        preset = MODEL_PRESETS[model_id]
        inst = ModelInstance(
            key=key,
            npu_index=npu_index,
            model_id=model_id,
            device_id=cards[npu_index].get("_raw_device_id"),
            capability_types=list(preset["capability_types"]),
            instance_name=preset["instance_name"],
            label=preset["label"],
            memory_estimate_mb=preset["memory_estimate_mb"],
            status="queued",
            queued_at=time.time(),
        )
        with self._lock:
            self._instances[key] = inst
        self._wake_worker()
        return inst

    def stop_one(self, npu_index: int, model_id: str) -> None:
        key = (npu_index, model_id)
        with self._lock:
            inst = self._instances.pop(key, None)
        if inst is None:
            return
        sess = inst.llm_session
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    def stop_all(self) -> None:
        with self._lock:
            keys = list(self._instances.keys())
        for k in keys:
            self.stop_one(*k)

    def close_all(self) -> None:
        """app shutdown：停所有实例 + 停后台 worker。"""
        self.stop_all()
        self._stop_worker.set()
        self._wake_worker()
        t = self._worker_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    # ------------------------------------------------------------------ #
    # 启动 / 配置持久化
    # ------------------------------------------------------------------ #
    def apply_boot_config_async(self) -> None:
        """读 config.json["compute"]["instances"]，逐条 start_one。立即返回。

        后台 daemon thread 串行执行真正加载；UI 通过 ``/api/compute`` 轮询状态。
        """
        self._ensure_worker()
        stored = get_config().get_section("compute", {})
        instances = stored.get("instances") if isinstance(stored, dict) else None
        if not isinstance(instances, list):
            return
        # 第一次访问 cards，让 start_one 能拿到 device_id
        self.list_cards()
        for entry in instances:
            if not isinstance(entry, dict):
                continue
            npu_index = entry.get("npu_index")
            model_id = entry.get("model")
            if not isinstance(npu_index, int) or not isinstance(model_id, str):
                continue
            if model_id not in MODEL_PRESETS:
                LOG.warning("config.json 跳过未知 model: %s", model_id)
                continue
            try:
                self.start_one(npu_index, model_id)
            except ValueError as e:
                # 重复实例 / npu_index 越界 —— 已经在跑或 config 错误，记录日志但不让启动挂
                LOG.warning("boot start_one 跳过 (%d, %s): %s", npu_index, model_id, e)

    def _persist(self) -> None:
        """把当前 instances 列表写回 config.json["compute"]。"""
        with self._lock:
            payload = {
                "instances": [
                    {"npu_index": inst.npu_index, "model": inst.model_id}
                    for inst in self._instances.values()
                ]
            }
        try:
            get_config().set_section("compute", payload)
        except Exception as e:  # pragma: no cover - disk failure
            LOG.warning("compute config persist failed: %s", e)

    # ------------------------------------------------------------------ #
    # 消费者 API
    # ------------------------------------------------------------------ #
    def get_session(self, capability_type: str) -> Any:
        """返回第一个 status='running' 且 capability_types 包含此 type 的 LLMSession。

        没有 → RuntimeError(f"功能未启用: {capability_type}")。
        """
        with self._lock:
            for inst in self._instances.values():
                if inst.status == "running" and capability_type in inst.capability_types:
                    return inst.llm_session
        raise RuntimeError(f"功能未启用: {capability_type}")

    # ------------------------------------------------------------------ #
    # 后台 worker —— 串行从队列里拾取 queued 实例，转 loading → 启动 → running/error
    # ------------------------------------------------------------------ #
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._stop_worker.clear()
            self._wake_event.clear()
            t = threading.Thread(
                target=self._worker_loop,
                name="compute-registry-worker",
                daemon=True,
            )
            self._worker_thread = t
            t.start()

    def _wake_worker(self) -> None:
        self._ensure_worker()
        self._wake_event.set()

    def _worker_loop(self) -> None:
        while not self._stop_worker.is_set():
            try:
                inst = self._take_next_queued()
                if inst is None:
                    # 没有任务，等 1s 或被 wake
                    self._wake_event.wait(timeout=1.0)
                    self._wake_event.clear()
                    continue
                self._run_one(inst)
            except BaseException as e:  # noqa: BLE001
                # 防御性兜底：任何从 _take_next_queued / _run_one 里逃逸出来
                # 的异常都不能让 worker thread 死掉。worker 一旦死掉，正在
                # loading 的实例永远停在 loading，后面 queued 的实例再也
                # 不会被拾取 —— 算力配置页就会"卡住"，用户看不到任何错误。
                # 这里记一条日志 + 继续循环，下次 wake 时再重试。
                LOG.exception(
                    "compute worker caught unexpected exception (loop continues): %s",
                    e,
                )

    def _take_next_queued(self) -> Optional[ModelInstance]:
        with self._lock:
            for inst in self._instances.values():
                if inst.status == "queued":
                    inst.status = "loading"
                    inst.started_at = time.time()
                    return inst
        return None

    def _run_one(self, inst: ModelInstance) -> None:
        """真正调 rkai.LLMSession()。失败 → status='error'。

        关键约束：**LLMSession(...)** 内部会触发 rknn3 native lib 的
        ``MODEL_SETUP``。在非 RK 开发板 / 算力配置错的板子上，该
        调用可能会直接走 ``std::terminate()``，把整 uvicorn 进程
        拖死 —— 之前亲眼见过 ``terminate called without an active
        exception`` 之后整个 web 服务整个挂掉、网页卡死。
        所以这里把构造放到 **子进程** 里跑：subprocess 挂了不影响
        父进程，父进程把实例标 ``status='error'``，算力配置页可
        以看到错误原因 + 重试（disable → enable）。

        失败兜底：整段包在 ``try/except BaseException`` 里，**任何**
        异常（包括 ``except Exception`` 抓不住的 ``SystemExit`` /
        ``KeyboardInterrupt`` 等 ``BaseException`` 子类）都会把实例
        标 ``status='error'`` 并写错误原因。**绝不**让实例停在
        ``loading`` 状态 —— 那意味着 worker 已经放弃了这个实例，但
        UI 永远看不到错误。
        """
        npu_index = inst.npu_index
        model_id = inst.model_id
        try:
            ok, err, probe = _init_session_subprocess(npu_index, model_id)
            if not ok:
                with self._lock:
                    inst.status = "error"
                    inst.error = err
                    inst.finished_at = time.time()
                LOG.warning(
                    "compute init subprocess failed (%d, %s): %s",
                    npu_index, model_id, err,
                )
                return

            # 子进程构造成功 —— 父进程也构造一份（consumers 要拿
            # ``llm_session`` 句柄用）。这一步是**双倍代价**，但只
            # 在 init 成功路径上出现，且确保 web 不会因为 native abort
            # 而挂。失败依旧标 error。
            try:
                preset = MODEL_PRESETS[model_id]
                spec = preset["preset_factory"]()
                from rkai import LLMSession  # 懒导入
                sess = LLMSession(model=spec, npu=npu_index)
            except BaseException as e:  # noqa: BLE001
                # 用 BaseException 而不是 Exception：rkai / rknn3 native
                # 代码可能在 LLMSession() 构造期间抛 SystemExit / 自定义
                # BaseException 子类，except Exception 抓不到 → 实例
                # 永远停在 loading → UI 卡住。
                with self._lock:
                    inst.status = "error"
                    inst.error = (
                        f"subprocess 探针 ok，但父进程 init 失败: "
                        f"{type(e).__name__}: {e}"
                    )
                    inst.finished_at = time.time()
                LOG.warning(
                    "compute parent init failed (%d, %s) after subprocess ok: %s",
                    npu_index, model_id, e,
                )
                return

            with self._lock:
                inst.llm_session = sess
                inst.status = "running"
                inst.error = ""
                inst.finished_at = time.time()
            LOG.info(
                "compute instance running (%d, %s, probe=%.1fs)", npu_index, model_id, probe,
            )
        except BaseException as e:  # noqa: BLE001
            # 兜底：_init_session_subprocess 自己抛（FileNotFoundError /
            # OSError / 别的意料外情况），或上面任何一个分支出错。**必须**
            # 把实例标 error，否则 status 会停在 loading 永远不变 —— 网页
            # 端只能看到 "初始化中..." 倒计时不动，用户没有任何线索。
            err_text = str(e) or "未知异常"
            with self._lock:
                inst.status = "error"
                inst.error = f"worker 内部异常: {type(e).__name__}: {err_text}"
                inst.finished_at = time.time()
            LOG.exception(
                "compute _run_one caught unexpected exception (%d, %s)", npu_index, model_id,
            )
            return

        # 持久化：实例集写回 config.json（live start 后）。
        # 故意放在 try/except 之外 —— LLMSession 已成功 init，_persist 失败
        # 不应该回滚 status='error'。持久化失败只记日志，进程不退。
        # 注意：这是后台 thread 调，_persist 会调 config.set_section，
        # 已经 thread-safe（ConfigStore 内部有 RLock）。
        try:
            self._persist()
        except Exception as pe:  # noqa: BLE001
            LOG.warning(
                "compute _persist failed after successful init (%d, %s): %s",
                npu_index, model_id, pe,
            )


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_registry: Optional[ComputeRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ComputeRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = ComputeRegistry()
        return _registry


# --------------------------------------------------------------------------- #
# 子进程隔离的 LLMSession 构造探针
# --------------------------------------------------------------------------- #
_INIT_SENTINEL = b"__PYRKAIWEB_INIT_DONE__"


def _init_session_subprocess(
    npu_index: int, model_id: str, *, timeout: float = 600.0,
) -> Tuple[bool, str, float]:
    """在子进程里尝试构造一次 LLMSession。

    **目的**：rkai 的 ``LLMSession(...)`` 在 rknn3 加载失败 / 模型文件
    缺失等情况下会触发 ``std::terminate()``（而不是抛 Python 异常），
    整 uvicorn 跟着挂。把构造放到子进程里：

    * 子进程 exit 0 → 子进程构造成功，父进程也照着构造一份（consumers
      要拿句柄），构造时再失败同样标 error。
    * 子进程 exit 非 0 / SIGABRT → 父进程捕获，把 stderr/stdout
      截一段写到 ``inst.error``，实例标 ``error``，网页不挂。

    Returns:
        (ok, error_str, probe_seconds)
        ok=True 时 error_str 为空；ok=False 时 error_str 含原因。
    """
    import subprocess
    import sys
    import json as _json

    env = os.environ.copy()
    # rkai 用 RKAI_MODELS_DIR 找模型；保留主进程里已经写过的值。
    repo_root = str(Path(__file__).resolve().parents[2])
    env.setdefault("PYTHONPATH", repo_root)
    if "PYTHONPATH" in env and repo_root not in env["PYTHONPATH"].split(":"):
        env["PYTHONPATH"] = repo_root + ":" + env["PYTHONPATH"]

    # 子进程脚本：import rkai, 构建 LLMSession, 把结果写到 stdout。
    # **故意**把 rkai 构造包在 try/except BaseException 里 —— 既捕
    # Python 异常，也避免 C++ 异常 unwind 把 Python 解释器拖死（虽然
    # std::terminate 我们抓不住，但至少 import 错误 / TypeError 等
    # 能被截到）。脚本**完全**用 ``.format()`` 风格字符串拼接，
    # 避开 ``%`` 模板 —— 下面整段作为 ``python -c`` 的代码不能
    # 含 ``%`` 字符。
    model_id_js = model_id.replace("'", "\\'")
    script = (
        "import sys, json, os\n"
        "sys.path.insert(0, " + repr(repo_root) + ")\n"
        "def _emit(ok, error=''):\n"
        "    sys.stdout.buffer.write(\n"
        "        b'__PYRKAIWEB_INIT_DONE__' +\n"
        "        json.dumps({'ok': bool(ok), 'error': str(error)}).encode('utf-8') +\n"
        "        b'\\n')\n"
        "    sys.stdout.flush()\n"
        "try:\n"
        "    from rkai import LLMSession, SystemLanguageModel\n"
        "except BaseException as _e:\n"
        "    _emit(False, 'import rkai failed: ' + repr(_e))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    model_id = '" + model_id_js + "'\n"
        "    npu_index = " + str(int(npu_index)) + "\n"
        "    if model_id == 'qwen3-vl-4b':\n"
        "        spec = SystemLanguageModel.qwen3_vl_4b()\n"
        "    elif model_id == 'qwen3-tts':\n"
        "        spec = SystemLanguageModel.qwen3_tts()\n"
        "    elif model_id == 'qwen3-asr':\n"
        "        spec = SystemLanguageModel.qwen3_asr()\n"
        "    else:\n"
        "        raise ValueError('unknown model: ' + model_id)\n"
        "    _ = LLMSession(model=spec, npu=npu_index)\n"
        "    _emit(True)\n"
        "except BaseException as _e:\n"
        "    _emit(False, 'LLMSession() raised: ' + type(_e).__name__ + ': ' + str(_e))\n"
        "finally:\n"
        "    sys.exit(0)\n"
    )

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"init 超时 (>={timeout}s)", time.time() - t0

    elapsed = time.time() - t0
    # 找 sentinel + JSON 行
    out = proc.stdout or b""
    idx = out.find(_INIT_SENTINEL)
    if idx >= 0:
        tail = out[idx + len(_INIT_SENTINEL):]
        line = tail.split(b"\n", 1)[0].strip()
        try:
            payload = _json.loads(line.decode("utf-8", errors="replace"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            ok = bool(payload.get("ok"))
            err = str(payload.get("error") or "")
            if not ok and not err:
                err = "subprocess returned error but empty message"
            return ok, err, elapsed
        return False, f"subprocess stdout 无法解析: {line!r}", elapsed

    # 没有 sentinel：subprocess 崩了 / 输出没到。常见是 rknn3
    # `std::terminate()` —— exit code 是 -6 (SIGABRT) 或 -11 (SIGSEGV)。
    rc = proc.returncode
    tail_err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    tail_out = out.decode("utf-8", errors="replace").strip()
    snippet = tail_err.splitlines()[-3:] if tail_err else []
    snippet = "\n".join(snippet) or (tail_out[-300:] if tail_out else "(无输出)")
    if rc < 0:
        sig = -rc
        try:
            sig_name = {6: "SIGABRT", 11: "SIGSEGV", 9: "SIGKILL"}.get(sig, f"signal {sig}")
        except Exception:
            sig_name = f"signal {sig}"
        return False, f"init 子进程崩溃（{sig_name}）：{snippet[:300]}", elapsed
    return False, f"init 失败（exit {rc}）：{snippet[:300]}", elapsed