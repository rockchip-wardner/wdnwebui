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
