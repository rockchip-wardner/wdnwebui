"""算力卡编号的唯一定义 —— **本机 EP 协卡的稳定序号**。

背景：驱动枚举列表里混着三类设备
--------------------------------
``RKNN3Lite.get_devices_id()`` 返回的是**驱动枚举列表**，里面混着三类东西：

* **本机 PCIe EP 协卡** —— device_id 是 PCI BDF（``0001:11:00.0``）；
* **mdns 发现的对端机器** —— device_id 是 ``IP:端口``（``172.16.9.139:11900``）；
* **本机 RGA / VPU 设备** —— device_id 是 USB 序列号形态（``180B6D…``）。

对端机器**随时会上下线**（实验室里另一台板子，IP 还会从 .139 变成 .159），
而它一旦在，就占住列表最前面的下标 —— 本机协卡的下标整体后移 1。真机实测：

* 对端离线：``[0001:11:00.0, 0003:31:00.0, 0004:41:00.0, …]`` → 卡号 0/1/2
* 对端在线：``[172.16.9.139:11900, 0001:11:00.0, 0003:31:00.0, …]`` → 驱动下标 1/2/3

于是「卡号」有两套含义，必须分清：

* **驱动下标**（``get_devices_id()`` 里的位置）—— 会漂移，**不能**当稳定句柄；
* **本机协卡序号**（本模块的 ``card_no``）—— 只数本机 EP 卡，稳定。

本模块把后者定为**唯一口径**：

    card_no = 本机 EP 协卡按 PCI BDF 升序排列后的序号（0 起）

三个使用者共用这一份实现，口径不可能漂移：

* **主进程**（``registry.list_cards``）—— 枚举一次、编号一次，卡号写进
  ``config.jsonc``、写进 ``snapshot.cards``、写进实例的 ``npu_index``；
* **worker**（``workers/_base._build_session``）—— 自己再枚举一次，用**同一套
  规则**把卡号解析成真实设备，于是「主进程说的卡号」与「worker 加载的卡」
  一定是同一张物理卡；
* **系统监控**（``system_monitor._wdnai_canonical_device_index``）—— 把 NSMI
  遥测行的 bus_id 对到同一个卡号，系统信息页与算力配置页的 ``#N`` 才能对齐。

为什么按 BDF 排序而不是按出现顺序
--------------------------------
驱动枚举顺序**不保证稳定**（对端插队、PCIe 重枚举都可能改变）。BDF 是物理
拓扑，排序后是确定的 —— 而且真机实测驱动本来就是按 BDF 递增给出的
（``0001`` < ``0003`` < ``0004``），所以换成 BDF 排序**不改变对端离线时
的既有卡号**，只是把「对端在线时的漂移」消掉了。
"""
from __future__ import annotations

import re
import time
from typing import Any, Iterable, Optional, Sequence

#: 本机 PCIe EP 协卡：device_id 必须是 PCI BDF（``0001:11:00.0``）。
PCI_BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d+$")

#: mdns 发现的对端机器：``IP:端口``（例如 ``172.16.9.139:11900``）。
REMOTE_ENDPOINT_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}$")


def device_id_text(raw: Any) -> str:
    """把 ``bytes`` / ``str`` 形态的 device_id 统一成文本。

    ``get_devices_id()`` 回的是 ``bytes``（尾部带 NUL），快照里存的是
    ``str`` —— 两边都要能喂进本模块。
    """
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).rstrip(b"\x00").decode("utf-8", errors="replace")
    return str(raw)


def is_ep_device_id(raw: Any) -> bool:
    """是不是**本机 PCIe EP 协卡**（PCI BDF 形态）。

    「能跑 rknn 模型的卡」的判据 —— RGA/VPU（USB 序列号形态）和对端机器
    （``IP:端口`` 形态）都不是。
    """
    return bool(PCI_BDF_RE.match(device_id_text(raw)))


def is_remote_endpoint(raw: Any) -> bool:
    """是不是 mdns 发现的对端机器（``IP:端口`` 形态）。"""
    return bool(REMOTE_ENDPOINT_RE.match(device_id_text(raw)))


def _id_of(entry: Any) -> str:
    """从一个枚举条目里取出 device_id 文本。

    接受两种形状：裸的 ``bytes`` / ``str``（``get_devices_id()`` 的输出），
    或带 ``device_id`` 键的 dict（``snapshot.cards`` 的元素）。
    """
    if isinstance(entry, dict):
        return device_id_text(entry.get("device_id"))
    return device_id_text(entry)


def local_ep_device_ids(devices: Iterable[Any]) -> list[str]:
    """枚举列表里**本机 EP 协卡**的 device_id，按 PCI BDF 升序。

    这就是卡号的定义域：``local_ep_device_ids(devices)[card_no]`` 即第
    ``card_no`` 张卡。RGA/VPU 与对端机器**不占卡号**。
    """
    out: list[str] = []
    for entry in devices or ():
        did = _id_of(entry)
        if is_ep_device_id(did):
            out.append(did)
    return sorted(set(out))


def card_no_to_device_id(devices: Iterable[Any], card_no: Any) -> Optional[str]:
    """卡号 → device_id（PCI BDF）。卡号不存在时返回 ``None``。

    ``devices`` 可以是裸 id 列表，也可以是带 ``device_id`` 键的 dict 列表。
    """
    if isinstance(card_no, bool) or not isinstance(card_no, int):
        return None
    ids = local_ep_device_ids(devices)
    if card_no < 0 or card_no >= len(ids):
        return None
    return ids[card_no]


def card_no_to_raw_index(devices: Sequence[Any], card_no: Any) -> Optional[int]:
    """卡号 → 驱动枚举列表里的**原始下标**。

    wdnai 的 ``npu=`` / ``npu_indices=`` 是**位置语义**（直接索引
    ``get_devices_id()``），所以需要这个换算。注意它只在「调用方自己
    枚举」的场景下有意义 —— 跨进程请用 :func:`card_no_to_device_id`。
    """
    if isinstance(card_no, bool) or not isinstance(card_no, int):
        return None
    want = card_no_to_device_id(devices, card_no)
    if want is None:
        return None
    for i, entry in enumerate(devices or ()):
        if _id_of(entry) == want:
            return i
    return None


def card_numbers(devices: Iterable[Any]) -> list[int]:
    """本机 EP 协卡的卡号列表（恒为 ``[0, 1, …, N-1]``）。

    空列表 = 本机没有 EP 协卡。
    """
    return list(range(len(local_ep_device_ids(devices))))


def describe_cards(devices: Iterable[Any]) -> str:
    """人类可读的卡位清单（给错误信息用）。

    例：``"0001:11:00.0(#0), 0003:31:00.0(#1)"``。
    """
    ids = local_ep_device_ids(devices)
    if not ids:
        return "（无）"
    return ", ".join(f"{did}(#{i})" for i, did in enumerate(ids))


# --------------------------------------------------------------------------- #
# 设备拓扑要求（EP 协卡 + **本机 SoC 的 NPU**）                                #
# --------------------------------------------------------------------------- #
#
# ⚠️ 为什么 ``requires_cards`` 不够
# -------------------------------
# ``requires_cards`` 数的是**本机 EP 协卡**（本模块的 ``card_no`` 口径）。
# 但有些模型还要占住**第三个 NPU 设备**，而那个设备**不是 EP 协卡**：
# Qwen-Image-2.1 的 TE/DiT 占 2 张 RK1828，**VAE 必须独占**第三个 NPU ——
# 要么第三张 1828 协卡，要么**本机 RK3588 自带的 NPU**（走 rknn2）。
#
# 本机 NPU **不在算力卡列表里**（那列表只列 EP 协卡）⇒ 只看
# ``requires_cards`` 会以为「2 张卡够了」，实际第三个设备没有 ⇒
# open 阶段才炸（VAE 没有可独占的 NPU 设备）。
#
# ⇒ worker manifest 可以声明 ``card_requirements``（见
#   ``workers/__init__.py::WorkerManifest.card_requirements``）：
#   一组**可选布局**，按顺序取第一个本机满足的；一个都不满足 ⇒
#   **不允许运行这个模型**。

#: rknn2 runtime 的 soname 候选（与 cshim ``rknn2_runner.cpp`` 的 dlopen
#: 顺序一致）—— 用来判断**本机 NPU 能不能跑 rknn2 模型**。
_RKNN2_RT_NAMES = (
    "librknnrt.so.2.5.0", "librknnrt.so.2", "librknnrt.so", "librknnrt.so.1",
)

#: rknn2 runtime 常见的安装位置（apt 装的是 ``/usr/lib/aarch64-linux-gnu``）。
_RKNN2_RT_PATHS = (
    "/usr/lib/librknnrt.so",
    "/usr/lib/aarch64-linux-gnu/librknnrt.so",
    "/usr/lib64/librknnrt.so",
    "/lib/aarch64-linux-gnu/librknnrt.so",
)

#: ``local_npu_ready()`` 的缓存：``(单调时刻, 结果)``。
_LOCAL_NPU_CACHE = (None, False)


def local_npu_ready() -> bool:
    """本机 SoC 自带的 NPU 能不能跑 rknn2 模型。

    判据 = **rknn2 runtime 在不在**（``librknnrt.so``）—— cshim 就是
    ``dlopen`` 它来跑本机 NPU 的，缺了就只能退回 rknn3 协卡路径。

    只做**文件存在性**检查（不 dlopen）⇒ 不在 webui 主进程里加载运行时。

    结果**缓存 10 秒** —— 算力页每秒拉一次 snapshot，每次都探测的话
    ``ctypes.util.find_library`` 会在热路径上反复跑 ``ldconfig``。
    """
    global _LOCAL_NPU_CACHE
    now = time.monotonic()
    if _LOCAL_NPU_CACHE[0] is not None and now - _LOCAL_NPU_CACHE[0] < 10.0:
        return bool(_LOCAL_NPU_CACHE[1])
    ready = _probe_local_npu()
    _LOCAL_NPU_CACHE = (now, ready)
    return ready


def _probe_local_npu() -> bool:
    """真正做一次「rknn2 runtime 在不在」的探测（不 dlopen）。"""
    import os
    for path in _RKNN2_RT_PATHS:
        try:
            if os.path.exists(path):
                return True
        except OSError:
            continue
    try:
        import ctypes.util
        return bool(ctypes.util.find_library("rknnrt"))
    except Exception:  # noqa: BLE001 — 探测失败只当“没有”
        return False


def allowed_ep_card_counts(req: Any) -> set:
    """manifest 声明的布局**允许的 EP 协卡数量**集合。

    没有声明 ``card_requirements`` ⇒ 空集合（调用方回退到静态
    ``requires_cards``）。

    为什么需要它：EP 协卡要绑几张**取决于选中的布局**——
    「2×1828 + 3588」绑 2 张（VAE 跑本机 NPU），「3×1828」绑 3 张
    （VAE 独占第三张协卡）。所以不能再用单一的静态 ``requires_cards``
    去卡 worker：主进程、worker、autostart 校验三处都要接受**集合**。
    """
    if not isinstance(req, dict):
        return set()
    layouts = req.get("layouts")
    if not isinstance(layouts, list):
        return set()
    out = set()
    for lay in layouts:
        if not isinstance(lay, dict):
            continue
        try:
            n = int(lay.get("ep_cards") or 0)
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.add(n)
    return out


def evaluate_card_requirements(
    req: Any,
    ep_card_count: int,
    model_dir: str = "",
) -> dict:
    """评估 worker manifest 声明的**设备拓扑要求**是否被本机满足。

    参数
    ----
    req:
        manifest 的 ``card_requirements`` 字段（可能为空 / 旧格式）。
    ep_card_count:
        本机可用的 **EP 协卡**数量（``registry.model_cards()`` 的长度）。
    model_dir:
        模型目录 —— 用来检查布局要求的模型文件是否齐（VAE 有 rknn2 版
        才有「本机 NPU」布局）。

    返回
    ----
    dict::

        {
          "total_devices": 3,          # 这个模型一共要占几个 NPU 设备
          "ok": True,                   # 本机是否满足（至少一个布局可用）
          "reason": "",                 # ok=False 时的一句话原因
          "selected": {...},            # 选中的布局（ok=True 时）
          "layouts": [ {...}, {...} ],  # 每个布局的判定
        }

    没有声明 ``card_requirements`` 的模型 ⇒ ``ok=True``、``total_devices``
    取 ``requires_cards``（**保持旧行为**：单卡模型不受影响）。
    """
    req = req if isinstance(req, dict) else {}
    layouts = req.get("layouts")
    layouts = layouts if isinstance(layouts, list) else []
    total = int(req.get("total_devices") or 0) or None

    if not layouts:
        # 旧格式 / 没声明 ⇒ 只看 EP 卡够不够（老行为）
        need = int(req.get("ep_cards") or 0)
        ok = (need <= ep_card_count) if need else True
        return {
            "total_devices": total or need or 1,
            "ok": ok,
            "reason": "" if ok else f"需要 {need} 张 EP 协卡，本机只有 {ep_card_count} 张",
            "selected": None,
            "layouts": [],
        }

    local_ok = local_npu_ready()
    results = []
    for lay in layouts:
        if not isinstance(lay, dict):
            continue
        ep_need = int(lay.get("ep_cards") or 0)
        local_need = int(lay.get("local_npu") or 0)
        label = str(lay.get("label") or lay.get("id") or "布局")
        reasons = []
        if ep_need > ep_card_count:
            reasons.append(f"需要 {ep_need} 张 EP 协卡，本机只有 {ep_card_count} 张")
        if local_need > 0 and not local_ok:
            reasons.append("本机 NPU 不可用（没装 rknn2-runtime）")
        files = lay.get("required_files")
        if isinstance(files, list) and files and model_dir:
            import os
            missing = [f for f in files
                       if not os.path.exists(os.path.join(str(model_dir), str(f)))]
            if missing:
                reasons.append("模型包缺少 " + ", ".join(str(m) for m in missing))
        results.append({
            "id": str(lay.get("id") or label),
            "label": label,
            "ep_cards": ep_need,
            "local_npu": local_need,
            # 本机 NPU 那部分的**人类可读说明**（谁跑在上面）—— 前端
            # 「运行在」要把第三个设备显示出来，得知道它叫什么。
            "local_label": str(lay.get("local_label") or ""),
            "ok": not reasons,
            "reason": "；".join(reasons),
            "total_devices": ep_need + local_need,
        })

    selected = next((r for r in results if r["ok"]), None)
    if selected is not None:
        total = total or selected["total_devices"]
        return {
            "total_devices": total,
            "ok": True,
            "reason": "",
            "selected": selected,
            "layouts": results,
        }
    total = total or max([r["total_devices"] for r in results] or [1])
    detail = "；".join(f"{r['label']}：{r['reason']}" for r in results if r["reason"])
    return {
        "total_devices": total,
        "ok": False,
        "reason": detail or f"本机硬件不满足该模型需要 {total} 个 NPU 设备",
        "selected": None,
        "layouts": results,
    }
