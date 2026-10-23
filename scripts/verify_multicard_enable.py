#!/usr/bin/env python3
"""回归：同一个模型在多张卡上「一键开启」——两张卡都要起来，且只出现一张卡。

覆盖 v0.27 修的真机 bug（用户在「视频目标定位」向导点「一键开启所有模型」
后，LocateAnything-3b 只加载到 NPU0，NPU1 一直不加载，算力页还多出一张
重复的 [GD] 卡片）：

  1. **实例 key 撞车**：`_spawn_compute_worker` 给每个 spawn 都用占位 key
     ``(-1, model_id, "spawned")``，而 `bind_instance` 直到**异步** load
     跑完（实测 ~35s）才把 key 换成 ``(npu, model_id, source)``。同一个
     模型开第二张卡时，第二张卡的 spawn 用同一个占位 key 注册 → dict 覆盖
     → 第一张卡的实例被挤出 `_instances`。
  2. **adopt 找不到它 → 另建一条 adopted_local**：peer listener 触发
     `_maybe_adopt_from_peer` → probe 命中本机 worker → 因为 1. 把它挤出
     了，`_find_inst_by_worker_pid` 找不到 → 新建 adopted_local → 算力页
     出现两张同模型卡。
  3. **adopt 把第二张卡的 worker 当孤儿杀掉**：「清理多余孤儿」把 `/proc`
     里所有**不是**被 adopt 的同 model worker SIGTERM 掉 → 第二张卡的
     worker 死了 → 那张卡永远加载不上。

跑法：``.venv/bin/python scripts/verify_multicard_enable.py``
（纯逻辑，**不碰真机**：Popen / DDS / /proc 扫描全被桩掉，不启停任何模型）
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import socket
import sys
import threading
import tempfile

os.environ["WDNWEBUI_COMPUTE_MODE"] = "inprocess"   # 不起 DDS Hub

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.core.compute.registry as R  # noqa: E402

MODEL = "LocateAnything-3b"

#: worker 的 stdout/stderr 落盘目录（``_spawn_compute_worker`` 里硬编码）。
#: 真机上那个目录是 root 建的，普通用户跑测试会 Permission denied —— 换到
#: 临时目录，别去动系统目录的权限。
_TMP_WORKER_LOG = tempfile.mkdtemp(prefix="wdnwebui-test-worker-")

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"  ({detail})" if detail else ""))


# --------------------------------------------------------------------------- #
# 桩件                                                                        #
# --------------------------------------------------------------------------- #
class FakeProc:
    """假的 subprocess.Popen —— 不真的起进程，但记得住被 terminate 过。"""

    _next_pid = [41000]

    def __init__(self, argv):
        self.pid = FakeProc._next_pid[0]
        FakeProc._next_pid[0] += 1
        self.args = list(argv)
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeRpc:
    """假的 RpcClient。

    **load 必须"卡住"** —— 真机上模型加载要 ~35s，而 bug 就发生在这个
    窗口里：第一张卡的 load 还在跑，第二张卡就 spawn 了。桩件要是秒回，
    异步的 key 迁移会在第二张卡 spawn 之前跑完，race 就被抹掉了，测试
    变成"永远绿"的假回归。

    ``gate`` 未 set 时 load 一直挂着（模拟"还在加载"），set 之后才放行。
    """

    def __init__(self, gate):
        self.gate = gate
        self.calls = []

    def call(self, payload, timeout_ms=None):
        self.calls.append(payload)
        self.gate.wait(timeout=30)
        return {"ok": True, "data": {"status": "loading"}}


class Harness:
    """一个不碰硬件的 ComputeRegistry。"""

    def __init__(self):
        self.reg = R.ComputeRegistry()
        self.procs = []
        self.killed = []
        #: 未 set = 所有 load RPC 都挂着（模拟"模型还在加载"）
        self.gate = threading.Event()
        self._install()

    def _install(self):
        reg = self.reg
        self._orig_popen = R.subprocess.Popen
        self._orig_find = R._find_workers_by_name
        self._orig_kill = R._terminate_orphan_pids
        self._orig_path = R.Path

        procs = self.procs

        def fake_popen(cmd, *a, **kw):
            # 只接管 worker 自己的 spawn；别的调用（wdnai 探测 rknn 库时会
            # 跑 `file -b`）原样放行,别把它们的进程算成 worker。
            argv = cmd if isinstance(cmd, (list, tuple)) else [str(cmd)]
            if any("compute_worker.py" in str(x) for x in argv):
                p = FakeProc(argv)
                procs.append(p)
                return p
            return self._orig_popen(cmd, *a, **kw)

        def patched_path(*a, **kw):
            p = pathlib.Path(*a, **kw)
            if str(p) == "/tmp/wdnwebui-worker":
                return pathlib.Path(_TMP_WORKER_LOG)
            return p

        R.subprocess.Popen = fake_popen
        R.Path = patched_path
        # /proc 扫描：测试里由用例自己指定，别把真机上活着的 worker 扫进来
        R._find_workers_by_name = lambda model_id: list(self._proc_pids)
        R._terminate_orphan_pids = lambda pids, grace_sec=5: (
            self.killed.extend(pids) or len(pids))

        # 不起 DDS client / probe 线程
        gate = self.gate
        reg._setup_rpc_client = lambda inst: setattr(
            inst, "rpc_client", FakeRpc(gate))
        reg._probe_instance = lambda inst, *a, **kw: None
        # **卡位也要 fake**：本脚本测的是「一个模型同时跑两张卡」的语义，
        # 与真机有几张卡无关。不 fake 的话，v0.29 新增的卡位校验
        # （registry.npu_card_error）会真去探硬件 —— 单卡板上 npu=1 被判
        # 越界 → HTTP 400 →【7】直接崩。PCI BDF 形态 = 能跑 rknn 的卡。
        reg._cards = [
            {"npu_index": 0, "device_id": "0000:01:00.0"},
            {"npu_index": 1, "device_id": "0000:02:00.0"},
        ]
        reg._cards_failed = ""

    _proc_pids = []

    def set_proc_pids(self, pids):
        self._proc_pids = list(pids)

    def restore(self):
        R.subprocess.Popen = self._orig_popen
        R._find_workers_by_name = self._orig_find
        R._terminate_orphan_pids = self._orig_kill
        R.Path = self._orig_path


def local_ip() -> str:
    try:
        return R._get_local_ip() or ""
    except Exception:
        return ""


def enable_on_cards(reg, cards):
    """等价于算力页 / 向导的 ``POST /api/compute/instances``。"""
    out = []
    for n in cards:
        inst = reg.ensure_worker_for_model(MODEL, allow_adopt=False,
                                           npu_index=n, eager_load=False)
        reg.bind_instance(inst, n)
        out.append(inst)
    return out


def keys(reg):
    return sorted(reg._instances.keys())


def alive(procs):
    return [p for p in procs if not p.terminated]


# --------------------------------------------------------------------------- #
print("\n【1】同一个模型开两张卡 —— 两个实例都要留在 _instances 里")
h = Harness()
try:
    h.set_proc_pids([])
    insts = enable_on_cards(h.reg, [0, 1])
    ks = keys(h.reg)
    check("两张卡各有一个实例（不是一张）", len(h.reg._instances) == 2,
          f"instances={len(h.reg._instances)} keys={ks}")
    check("key 带上卡号，互不覆盖",
          ks == [(0, MODEL, "spawned"), (1, MODEL, "spawned")], f"keys={ks}")
    check("两个 worker 是不同进程",
          insts[0].proc.pid != insts[1].proc.pid,
          f"pids={insts[0].proc.pid},{insts[1].proc.pid}")
    check("第一张卡的实例没被挤掉", insts[0] in h.reg._instances.values())
    check("第二张卡的实例在", insts[1] in h.reg._instances.values())
    check("两个 worker 都没被杀", len(alive(h.procs)) == 2)

    print("\n【2】peer listener 触发 adopt —— 不能多出一张卡、不能杀 worker")
    # probe 命中本机 worker（peer_ip / hostname 都是本机 → adopted_local）
    h.reg._probe_service = lambda model_id: {
        "status": "running", "pid": insts[0].proc.pid,
        "hostname": socket.gethostname(), "peer_ip": local_ip(),
        "npu_index": 0, "device_name": "本机",
    }
    adopted = h.reg._adopt_existing_worker(MODEL)
    ks2 = keys(h.reg)
    check("adopt 后仍然是两个实例（没有多出重复卡）",
          len(h.reg._instances) == 2, f"instances={len(h.reg._instances)} keys={ks2}")
    check("没有多出 adopted_local 卡",
          not any(k[2] == "adopted_local" for k in ks2), f"keys={ks2}")
    check("adopt 复用了已跟踪的实例", adopted is insts[0],
          f"adopted={getattr(adopted, 'key', None)}")
    check("两个 worker 都没被杀", len(alive(h.procs)) == 2,
          f"terminated={[p.pid for p in h.procs if p.terminated]}")
    check("没有触发孤儿清理", h.killed == [], f"killed={h.killed}")
finally:
    h.gate.set()
    h.restore()

print("\n【3】adopt 真的遇到孤儿时 —— 只杀没被 registry 跟踪的")
h = Harness()
try:
    h.set_proc_pids([])
    insts = enable_on_cards(h.reg, [0, 1])
    tracked = {p.pid for p in h.procs}
    # /proc 里：两个本板 worker + 一个 reload 残留的孤儿
    orphan = 99999
    h.set_proc_pids(sorted(tracked) + [orphan])
    h.reg._probe_service = lambda model_id: {
        "status": "running", "pid": orphan,
        "hostname": socket.gethostname(), "peer_ip": local_ip(),
        "npu_index": -1, "device_name": "本机",
    }
    h.reg._adopt_existing_worker(MODEL)
    check("被 registry 跟踪的两个 worker 都没被杀",
          h.killed == [], f"killed={h.killed} tracked={sorted(tracked)}")
    check("两个 worker 都还活着", len(alive(h.procs)) == 2)
finally:
    h.gate.set()
    h.restore()

print("\n【4】孤儿清理的老语义不能丢 —— 没被跟踪的 extras 仍要清掉")
h = Harness()
try:
    h.set_proc_pids([])
    h.reg._probe_service = lambda model_id: {
        "status": "running", "pid": 11111,
        "hostname": socket.gethostname(), "peer_ip": local_ip(),
        "npu_index": -1, "device_name": "本机",
    }
    h.set_proc_pids([11111, 22222])
    h.reg._adopt_existing_worker(MODEL)
    check("多出来的真孤儿被清理", h.killed == [22222], f"killed={h.killed}")
finally:
    h.gate.set()
    h.restore()

print("\n【5】单卡启用不能回归")
h = Harness()
try:
    h.set_proc_pids([])
    insts = enable_on_cards(h.reg, [1])
    check("单卡只有一个实例", len(h.reg._instances) == 1,
          f"keys={keys(h.reg)}")
    check("key 带上卡号", keys(h.reg) == [(1, MODEL, "spawned")],
          f"keys={keys(h.reg)}")
    check("worker 活着", len(alive(h.procs)) == 1)
finally:
    h.gate.set()
    h.restore()

print("\n【6】boot(autostart) 路径：同模型两条不同卡 —— 不走 bind_instance")
h = Harness()
try:
    h.set_proc_pids([])
    # autostart 路径：worker 自己带 --npu 加载，主进程**不**补 bind
    for n in (0, 1):
        h.reg.ensure_worker_for_model(MODEL, allow_adopt=False, npu_index=n)
    ks = keys(h.reg)
    check("两张卡各一个实例", len(h.reg._instances) == 2,
          f"instances={len(h.reg._instances)} keys={ks}")
    check("key 带上卡号", ks == [(0, MODEL, "spawned"), (1, MODEL, "spawned")],
          f"keys={ks}")
    check("两个 worker 都活着", len(alive(h.procs)) == 2)
    # 再来一次 adopt（peer listener 触发）—— 不能杀，也不能多出卡
    h.reg._probe_service = lambda model_id: {
        "status": "running", "pid": h.procs[0].pid,
        "hostname": socket.gethostname(), "peer_ip": local_ip(),
        "npu_index": 0, "device_name": "本机",
    }
    h.reg._adopt_existing_worker(MODEL)
    check("adopt 后还是两个实例", len(h.reg._instances) == 2,
          f"keys={keys(h.reg)}")
    check("两个 worker 都没被杀", len(alive(h.procs)) == 2,
          f"terminated={[p.pid for p in h.procs if p.terminated]}")
finally:
    h.gate.set()
    h.restore()

print("\n【7】HTTP 层：POST /api/compute/instances {npu_index:[0,1]}")
try:
    import app.system.api.compute as capi

    h = Harness()
    try:
        h.set_proc_pids([])
        orig_get = capi.get_registry
        capi.get_registry = lambda: h.reg
        try:
            req = capi.InstanceRequest(model=MODEL, npu_index=[0, 1])
            res = asyncio.run(capi.start_instance(req))
        finally:
            capi.get_registry = orig_get
        check("返回两个新建实例", len(res["instances"]) == 2,
              f"n={len(res['instances'])} errors={res['errors']}")
        check("没有报错", res["errors"] == [], f"errors={res['errors']}")
        check("两个实例卡号不同",
              {i["npu_index"] for i in res["instances"]} == {0, 1},
              f"npus={[i['npu_index'] for i in res['instances']]}")
        check("算力页只会有两张卡（每卡一条）",
              len(h.reg._instances) == 2, f"keys={keys(h.reg)}")
        check("两个 worker 都活着", len(alive(h.procs)) == 2)
    finally:
        h.gate.set()
        h.restore()
except ImportError as e:  # pragma: no cover
    check("HTTP 层可导入", False, str(e))

print("\n【8】set_active_app —— 同一个模型跑两张卡，两张都要记进 autostart")
h = Harness()
try:
    h.set_proc_pids([])
    insts = enable_on_cards(h.reg, [0, 1])
    # set_active_app 只认 running 的实例（probe 线程被桩掉了，这里直接置位）
    h.gate.set()
    for i in insts:
        i.status = "running"

    class FakeConfig:
        def __init__(self):
            self.sections = {"compute": {"autostart": []}}

        def get_section(self, name, default=None):
            return dict(self.sections.get(name, {}))

        def set_section(self, name, data):
            self.sections[name] = data

    fake_cfg = FakeConfig()
    orig_cfg = R.get_config
    R.get_config = lambda: fake_cfg
    try:
        res = h.reg.set_active_app("video-target-localization",
                                   ["swcap_vision_grounding"])
    finally:
        R.get_config = orig_cfg
    autostart = res["autostart"]
    cards = sorted(e["npu_index"] for e in autostart)
    check("autostart 记了两张卡", cards == [0, 1], f"autostart={autostart}")
    check("两条都是同一个模型",
          {e["model"] for e in autostart} == {MODEL}, f"autostart={autostart}")
finally:
    h.gate.set()
    h.restore()

print(f"\n{'=' * 60}")
print(f"通过 {PASS} / 失败 {FAIL}")
print("=" * 60)
sys.exit(1 if FAIL else 0)
