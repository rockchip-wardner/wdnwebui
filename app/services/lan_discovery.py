"""局域网设备发现 —— 同网段内的 pyrkaiweb 实例自动相互识别。

设计目标
--------

* Discovery 页加一个"在网注册"开关，打开后周期性扫描本机所在的所有 IPv4
  /24 子网，对每个候选 IP 探测 TCP 8000 + HTTP ``/api/lan-discovery/announce``。
* 探测成功的设备记到内存里（device_name / ip / port / 能力 / 最后一次被探测到
  的时间），前端每秒轮询 ``/api/lan-discovery`` 拉最新快照。
* 关闭开关后停止扫描线程，已发现的设备保留在内存里供用户回顾，再次打开会
  清空并立刻重新扫描。
* TTL 5 分钟：连续 5 分钟没再扫到的设备从列表里自动剔除，避免显示一堆
  已经下线的僵尸节点。

网络发现走纯 stdlib：

* 用 ``psutil.net_if_addrs()`` 列本机所有 IPv4 接口，过滤 loopback /
  link-local / 容器网桥，按 /24 切子网，剔除本机自身 IP；
* 用 ``urllib.request`` 做并发 HTTP 探测（``ThreadPoolExecutor`` 并行度
  32，单请求超时 1.5 s）；
* 无新依赖，复用 ``requirements.txt`` 里已有的 psutil。

配置 / 状态
-----------

* 配置（持久化到 ``config.json["compute"]["lan_discovery"]``）：enabled、
  port、scan_interval_seconds —— 通过 ``PUT /api/lan-discovery`` 修改。
* 运行时状态（仅内存）：devices[] / scanning / last_scan_started /
  last_scan_finished / scanned_subnets / local_ips —— 通过
  ``GET /api/lan-discovery`` 拉。
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from app.services.config import get_config


LOG = logging.getLogger("lan_discovery")


# --------------------------------------------------------------------------- #
# 默认值
# --------------------------------------------------------------------------- #
_DEFAULT_PORT = 8000
_DEFAULT_SCAN_INTERVAL_SECONDS = 30
_DEFAULT_PROBE_TIMEOUT_SECONDS = 0.7   # 单次 TCP/HTTP 超时：/24 扫描靠
                                       # 大量超时累积，单 host 调长会拖慢整轮。
_DEFAULT_TTL_SECONDS = 300             # 5 分钟没再出现就剔除
_DEFAULT_MAX_WORKERS = 64              # 并行度：/24 = 253 主机 ≈ 3 s 跑完


# --------------------------------------------------------------------------- #
# 持久化配置 (config.json["compute"]["lan_discovery"])
# --------------------------------------------------------------------------- #
def _default_config() -> dict:
    return {
        "enabled": False,
        "port": _DEFAULT_PORT,
        "scan_interval_seconds": _DEFAULT_SCAN_INTERVAL_SECONDS,
    }


def _load_config() -> dict:
    """从 config.json 读 lan_discovery 配置，缺字段补默认值。

    只在内存里补齐，**不主动写回磁盘**；用户改 toggle / port 并 PUT 时
    才落盘。
    """
    cfg = get_config().get_section("compute", {})
    stored = cfg.get("lan_discovery") if isinstance(cfg, dict) else None
    merged = _default_config()
    if isinstance(stored, dict):
        for k, v in stored.items():
            if k in merged and isinstance(v, bool):
                merged[k] = v
            elif k in merged and isinstance(v, int) and not isinstance(v, bool):
                merged[k] = v
    return merged


def _save_config(new_cfg: dict) -> None:
    """落盘到 config.json["compute"]["lan_discovery"]。"""
    cfg = get_config().get_section("compute", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["lan_discovery"] = dict(new_cfg)
    try:
        get_config().set_section("compute", cfg)
    except Exception as e:  # noqa: BLE001
        LOG.warning("failed to persist lan_discovery config: %s", e)


# --------------------------------------------------------------------------- #
# 本机网络枚举
# --------------------------------------------------------------------------- #
def _list_local_ipv4s() -> list[str]:
    """列出本机所有 IPv4 地址（去掉 loopback / link-local）。"""
    out: set[str] = set()
    try:
        import psutil  # 已有依赖
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                ip = a.address
                if not ip:
                    continue
                if ip.startswith("127."):
                    continue
                # link-local 169.254.x.x —— 跳过；多播 / 实验段也跳过
                try:
                    ip_obj = ipaddress.IPv4Address(ip)
                    if ip_obj.is_link_local:
                        continue
                    if ip_obj.is_multicast or ip_obj.is_unspecified:
                        continue
                    if ip_obj.is_private is False:
                        # 公网 IP 也不扫（不在 LAN 范畴）
                        continue
                except ValueError:
                    continue
                out.add(ip)
    except Exception as e:  # noqa: BLE001
        LOG.warning("psutil.net_if_addrs() failed: %s", e)

    # psutil 没拿到的时候兜底：用 socket 试探一次拿个默认接口 IP
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
            finally:
                s.close()
            if ip and not ip.startswith("127."):
                try:
                    ip_obj = ipaddress.IPv4Address(ip)
                    if ip_obj.is_private and not ip_obj.is_link_local:
                        out.add(ip)
                except ValueError:
                    pass
        except OSError:
            pass
    return sorted(out)


def _enumerate_subnets(local_ips: list[str]) -> list[tuple[str, list[str]]]:
    """按 /24 把本机每个私有 IP 切成 (subnet_cidr, hosts[]) 元组列表。

    同一 /24 出现多次时只保留一份；本机自身 IP 从候选列表剔除。
    """
    seen_subnets: dict[str, set[str]] = {}
    for ip in local_ips:
        try:
            iface = ipaddress.IPv4Interface(f"{ip}/24")
        except ValueError:
            continue
        net = iface.network
        if not net.is_private:
            continue
        cidr = str(net)
        hosts = seen_subnets.setdefault(cidr, set())
        for host in net.hosts():
            hosts.add(str(host))
    # 本机 IP 自身从候选里剔除
    for ip in local_ips:
        for cidr, hosts in seen_subnets.items():
            net = ipaddress.IPv4Network(cidr)
            if ipaddress.IPv4Address(ip) in net:
                hosts.discard(ip)
    return [
        (cidr, sorted(hosts, key=lambda s: tuple(int(x) for x in s.split("."))))
        for cidr, hosts in sorted(seen_subnets.items())
    ]


# --------------------------------------------------------------------------- #
# 设备记录
# --------------------------------------------------------------------------- #
@dataclass
class LanDevice:
    ip: str
    port: int
    device_name: str = ""
    sw_capabilities: list[dict] = field(default_factory=list)
    hw_capabilities: list[dict] = field(default_factory=list)
    last_seen: float = 0.0
    first_seen: float = 0.0
    last_rtt_ms: float = 0.0
    last_error: str = ""

    @property
    def key(self) -> tuple[str, int]:
        return (self.ip, self.port)

    def to_dict(self) -> dict:
        return {
            "ip":                self.ip,
            "port":              self.port,
            "device_name":       self.device_name,
            "sw_capabilities":   list(self.sw_capabilities),
            "hw_capabilities":   list(self.hw_capabilities),
            "last_seen":         self.last_seen,
            "first_seen":        self.first_seen,
            "last_rtt_ms":       round(self.last_rtt_ms, 1),
            "last_error":        self.last_error,
            "online":            (time.time() - self.last_seen) <= _DEFAULT_TTL_SECONDS,
        }


# --------------------------------------------------------------------------- #
# LanDiscoveryManager (singleton)
# --------------------------------------------------------------------------- #
class LanDiscoveryManager:
    """维护 enabled 开关 + 设备列表 + 后台扫描线程。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._devices: dict[tuple[str, int], LanDevice] = {}
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._scanning = False
        self._last_scan_started = 0.0
        self._last_scan_finished = 0.0

    # ------------------------------------------------------------------ #
    # 开关 / 配置
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        cfg = _load_config()
        with self._lock:
            now = time.time()
            devices = [d.to_dict() for d in self._devices.values()]
            # TTL 剔除 —— 顺手扫一遍，超时的从内存里删
            expired = [
                k for k, d in self._devices.items()
                if (now - d.last_seen) > _DEFAULT_TTL_SECONDS
            ]
            for k in expired:
                self._devices.pop(k, None)
            local_ips = _list_local_ipv4s()
            subnets = _enumerate_subnets(local_ips)
            return {
                "config": cfg,
                "enabled": bool(cfg.get("enabled", False)),
                "scanning": self._scanning,
                "last_scan_started":  self._last_scan_started,
                "last_scan_finished": self._last_scan_finished,
                "local_ips":  local_ips,
                "subnets":    [cidr for cidr, _ in subnets],
                "hosts_per_subnet": {
                    cidr: len(hosts) for cidr, hosts in subnets
                },
                "devices":    sorted(devices, key=lambda d: d["device_name"] or d["ip"]),
                "device_count": len(devices),
                "ttl_seconds": _DEFAULT_TTL_SECONDS,
                "probe_timeout_seconds": _DEFAULT_PROBE_TIMEOUT_SECONDS,
                "default_port": _DEFAULT_PORT,
            }

    def set_enabled(self, enabled: bool) -> None:
        cfg = _load_config()
        cfg["enabled"] = bool(enabled)
        _save_config(cfg)
        if cfg["enabled"]:
            # 重新打开时清空旧设备（避免显示已经下线的节点），并立刻扫描一次
            with self._lock:
                self._devices.clear()
            self._ensure_thread()
            self._wake_event.set()
        else:
            # 关闭：保留内存里的设备列表供用户回顾，但停止扫描
            pass

    def update_config(
        self,
        *,
        port: Optional[int] = None,
        scan_interval_seconds: Optional[int] = None,
    ) -> dict:
        cfg = _load_config()
        if port is not None:
            if not (1 <= int(port) <= 65535):
                raise ValueError(f"port 越界: {port}")
            cfg["port"] = int(port)
        if scan_interval_seconds is not None:
            si = int(scan_interval_seconds)
            if si < 5 or si > 600:
                raise ValueError(f"scan_interval_seconds 越界: {si}（5–600）")
            cfg["scan_interval_seconds"] = si
        _save_config(cfg)
        return cfg

    def clear_devices(self) -> None:
        with self._lock:
            self._devices.clear()

    def trigger_scan(self) -> dict:
        cfg = _load_config()
        # 即使用户没开 enabled，也允许手动跑一次 —— 跟"立刻扫描"按钮语义一致
        self._ensure_thread()
        self._wake_event.set()
        return {
            "ok": True,
            "message": "已触发扫描",
            "scanning": True,
            "enabled": bool(cfg.get("enabled", False)),
        }

    # ------------------------------------------------------------------ #
    # 公共 announce 接口 —— 其他 pyrkaiweb 实例调它来发现本机
    # ------------------------------------------------------------------ #
    def announce(self, remote_ip: str) -> dict:
        """返回本机的身份 + 能力快照，供对端探测时使用。

        本机始终允许被探测（不依赖 enabled 开关 —— 设备**在网注册**跟
        **主动扫描**是两件事：开关控制的是"我去找别人"，announce 控制的是
        "别人来找我时我能回什么"）。enabled 标志会附带在响应里供对端展示。
        """
        # device_name 走 config.json["base"]["device_name"]；缺时兜底"unknown"
        try:
            base = get_config().get_section("base", {})
            device_name = (base.get("device_name") or "").strip() or "unknown"
        except Exception:
            device_name = "unknown"

        # 能力从 compute registry 拿（不直接 import，避免循环依赖 + 启动期 rkai
        # 未就绪时也保持 announce 可用）
        sw_caps: list[dict] = []
        hw_caps: list[dict] = []
        try:
            from app.services.compute_registry import get_registry
            reg = get_registry()
            sw_caps = reg.list_active_capabilities()
            hw_caps = reg.list_hardware_capabilities()
        except Exception as e:  # noqa: BLE001
            LOG.debug("announce: registry 读取失败: %s", e)

        return {
            "device_name":     device_name,
            "ip":              remote_ip,
            "sw_capabilities": sw_caps,
            "hw_capabilities": hw_caps,
            "timestamp":       time.time(),
            "protocol_version": 1,
        }

    # ------------------------------------------------------------------ #
    # 后台扫描线程
    # ------------------------------------------------------------------ #
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            t = threading.Thread(
                target=self._worker_loop,
                name="lan-discovery-worker",
                daemon=True,
            )
            self._thread = t
            t.start()

    def _stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._lock:
            t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            cfg = _load_config()
            interval = float(cfg.get("scan_interval_seconds", _DEFAULT_SCAN_INTERVAL_SECONDS))
            enabled = bool(cfg.get("enabled", False))
            port = int(cfg.get("port", _DEFAULT_PORT))

            if enabled:
                self._run_one_scan(port)
            # 等下一次唤醒或 interval 超时
            self._wake_event.wait(timeout=interval)
            self._wake_event.clear()

    def _run_one_scan(self, port: int) -> None:
        """一轮扫描：枚举子网 → 并发探测 → 合并设备表。"""
        with self._lock:
            if self._scanning:
                # 防重入：上一轮还没跑完直接放弃
                return
            self._scanning = True
            self._last_scan_started = time.time()

        try:
            local_ips = _list_local_ipv4s()
            subnets = _enumerate_subnets(local_ips)
            # 合并所有候选主机（带 cidr 标记，方便调试 / 日志）
            candidates: list[tuple[str, str]] = []
            for cidr, hosts in subnets:
                for host in hosts:
                    candidates.append((host, cidr))
            if not candidates:
                LOG.debug("lan discovery scan: 没有可用子网")
                return

            results: list[tuple[str, int, dict | None, float, str]] = []
            with ThreadPoolExecutor(
                max_workers=_DEFAULT_MAX_WORKERS,
                thread_name_prefix="lan-probe",
            ) as pool:
                futures = {
                    pool.submit(self._probe_host, host, port): (host, cidr)
                    for host, cidr in candidates
                }
                for fut in as_completed(futures):
                    host, cidr = futures[fut]
                    try:
                        results.append(fut.result())
                    except Exception as e:  # noqa: BLE001
                        results.append((host, port, None, 0.0, str(e)))

            now = time.time()
            with self._lock:
                for host, p, payload, rtt, err in results:
                    key = (host, p)
                    if payload is None:
                        # 探测失败：不更新 last_seen，让其自然过期
                        existing = self._devices.get(key)
                        if existing is not None:
                            existing.last_error = err
                        continue
                    name = (payload.get("device_name") or "").strip() or host
                    sw = payload.get("sw_capabilities") or []
                    hw = payload.get("hw_capabilities") or []
                    if not isinstance(sw, list):
                        sw = []
                    if not isinstance(hw, list):
                        hw = []
                    existing = self._devices.get(key)
                    if existing is None:
                        self._devices[key] = LanDevice(
                            ip=host, port=p,
                            device_name=name,
                            sw_capabilities=sw,
                            hw_capabilities=hw,
                            first_seen=now,
                            last_seen=now,
                            last_rtt_ms=rtt,
                            last_error="",
                        )
                    else:
                        existing.device_name = name
                        existing.sw_capabilities = sw
                        existing.hw_capabilities = hw
                        existing.last_seen = now
                        existing.last_rtt_ms = rtt
                        existing.last_error = ""
        finally:
            with self._lock:
                self._scanning = False
                self._last_scan_finished = time.time()

    def _probe_host(
        self, host: str, port: int,
    ) -> tuple[str, int, dict | None, float, str]:
        """对单个 host 探测：先 TCP connect，再 HTTP GET announce。

        TCP connect 失败直接返回（host 不在线 / 端口未开），不浪费 HTTP 超时。
        """
        t0 = time.time()
        try:
            with socket.create_connection((host, port), timeout=_DEFAULT_PROBE_TIMEOUT_SECONDS):
                pass
        except (OSError, socket.timeout):
            return (host, port, None, 0.0, "tcp connect 失败")
        tcp_ms = (time.time() - t0) * 1000.0

        url = f"http://{host}:{port}/api/lan-discovery/announce"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(
                req, timeout=_DEFAULT_PROBE_TIMEOUT_SECONDS,
            ) as resp:
                raw = resp.read()
        except (urllib.error.URLError, socket.timeout, OSError, json.JSONDecodeError, ValueError) as e:
            return (host, port, None, 0.0, f"http 失败: {type(e).__name__}")

        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return (host, port, None, 0.0, "announce 响应非 JSON")

        if not isinstance(payload, dict):
            return (host, port, None, 0.0, "announce 响应非 object")

        rtt_ms = (time.time() - t0) * 1000.0
        return (host, port, payload, rtt_ms, "")


# --------------------------------------------------------------------------- #
# Singleton
# --------------------------------------------------------------------------- #
_manager: Optional[LanDiscoveryManager] = None
_manager_lock = threading.Lock()


def get_lan_discovery() -> LanDiscoveryManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LanDiscoveryManager()
        return _manager