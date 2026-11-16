#!/usr/bin/env python
"""HY-MT 翻译实例布局实测：几个实例、怎么分卡，能快多少？

    .venv/bin/python scripts/bench_npu_dual.py --layout "0,0,1,1"

``--layout`` 是一串卡号，一个 worker 对应一个：
    "0"        1 个实例，卡 0
    "0,0"      2 个实例，都在卡 0（用户问的「一张卡两个实例」）
    "0,1"      2 个实例，卡 0 / 卡 1
    "0,0,1,1"  4 个实例，每卡 2 个

每个 worker 是独立的 compute_worker 进程（自己的 rknn3 context、自己的
DDS per-PID service），主进程侧各建一个 session。

⚠️ **两条必须记住的架构事实**（本脚本测出来的）：

1. **一个 session 同时只能有一个推理在飞** —— ``wdndds/ai_session.py`` 的
   ``LLMRPCSession._executor`` 是 ``ThreadPoolExecutor(max_workers=1)``。
   ⇒ 客户端线程开再多也没用（实测 1 实例 8 线程 = 1.03×）。
   **要并发必须有多个 session（多个 worker 进程）。**
2. **items 必须按条轮转分给 session**。``ThreadPoolExecutor.map`` 按顺序
   取任务，若 items 是「前一半给 A、后一半给 B」的成块排列，最先跑起来的
   N 个任务会全落在 A 上 —— 并发根本没发生（本脚本第一版就栽在这，
   测出「双实例 1.00×」的假象）。

它自己起 worker、自己建 session、自己测，**不碰**正在跑的 Web UI 进程。
"""
from __future__ import annotations

import argparse
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MODEL_ID = "hy-mt-1-5"
CAPABILITY = "swcap_translate"

# 界面里真实会出现的文案（十几~三十字的中文）
SEGMENTS = [
    "正在翻译，请稍候。", "翻译失败，请检查网络。", "界面语言", "简体中文", "英语",
    "模型加载中，请稍候。", "该卡位已被其它模型占用。", "算力配置", "系统运行正常",
    "启用翻译服务", "禁用翻译服务", "翻译服务已启用。", "本板 NPU 翻译", "外网翻译站",
    "实时语音识别", "视频目标监控", "图片生成", "文本对话", "网页翻译",
    "计算卡", "协处理器", "已安装", "未启用", "正在启动模型", "已停止",
    "保存成功", "删除失败", "请选择文件", "上传中", "已完成",
]

# 跟 NpuTranslateProvider 的首选措辞一致（实测成功率最高的那条）
PROMPT_TMPL = "Translate the following text from {csrc} to {ctgt}:\n{text}"


# --------------------------------------------------------------------------- #
# worker / session
# --------------------------------------------------------------------------- #
def ensure_env() -> None:
    """把 ``WDNAI_MODELS_DIR`` 对齐到本板配置里的模型根。

    ⚠️ 必须显式设：``default_models_root()`` 会先找 ``/userdata/models``
    / ``/mnt/oem/models``，而本板的模型在 ``base.model_path``
    （``/home/rockchip/models``）—— 不设的话 worker 会去找错目录，
    表现为「manifest not found」。
    """
    from app.core.config import get_config

    root = get_config().get_section("base", {}).get("model_path")
    if root:
        os.environ["WDNAI_MODELS_DIR"] = str(root)


def card_device_id(card: int) -> str:
    """卡号 → PCI BDF（worker 用它按身份选卡，与枚举顺序无关）。

    ⚠️ **不要走 ``get_registry()``** —— 它的开机路径会把所有已装模型的
    worker 都拉起来（探测 / adopt / spawn），在这个基准里既慢又脏
    （会把别人的实例也搅进来）。这里直接探一次 RKNN 设备列表就够。
    """
    from app.core.compute.cards import local_ep_device_ids
    from wdnai._librknn import RKNN3Lite

    probe = RKNN3Lite()
    ids = local_ep_device_ids(list(probe.get_devices_id() or []))
    if card < 0 or card >= len(ids):
        raise RuntimeError("卡号 %d 不存在（本机 %d 张 EP 协卡：%s）"
                           % (card, len(ids), ids))
    return ids[card]


def spawn_worker(card: int, tag: str):
    """起一个 hy-mt worker，eager load 到指定卡。

    每个 worker 一个**独立**日志文件 —— 两个 worker 写同一个文件的话，
    就分不清「ready」那行是哪个进程打的。
    """
    from app.core.compute.workers import load_one as load_worker_manifest
    from app.core.config import get_config

    manifest = load_worker_manifest(MODEL_ID)
    entry = manifest.entry_path if manifest is not None else str(
        REPO / "app/core/compute/workers/compute_worker.py")
    dev_id = card_device_id(card)

    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO))
    env.setdefault("WDNAI_MODELS_DIR",
                   get_config().get_section("base", {}).get(
                       "model_path", f"{os.path.expanduser('~')}/models/"))
    cmd = [sys.executable, "-u", entry, "--model", MODEL_ID,
           "--local-only", "--npu", str(card), "--device-id", dev_id]
    log_dir = Path("/tmp/wdnwebui-worker")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"bench-{MODEL_ID}-{tag}-c{card}.log"
    fp = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(cmd, env=env, stdout=fp, stderr=fp,
                            start_new_session=True)
    service_name = "wdndds/rpc/%s/worker-%d" % (MODEL_ID, proc.pid)
    return proc, service_name, log_path


def wait_ready(proc: subprocess.Popen, log_path: Path,
               timeout: float = 240.0) -> bool:
    """等 worker 把模型加载完。

    判据用 worker 自己打的 ready 标记（``LLMSession ready (19.9s) for
    hy-mt-1-5 @ npu=0``）—— 比「进程还活着」准得多：进程活着只说明
    DDS 起来了，模型还没加载。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            txt = log_path.read_text(errors="replace")
        except OSError:
            txt = ""
        if "LLMSession ready" in txt:
            return True
        if "load failed" in txt or "load raised" in txt:
            return False
        time.sleep(1.0)
    return False


def build_session(service_name: str, hub, side_hub):
    """照 registry 的做法给一个 worker 建主进程侧的 session。"""
    from app.core.compute.proxy import make_rpc_session
    from app.core.compute.workers import build_model_spec

    spec = build_model_spec(MODEL_ID)
    svc = hub.create_service(service_name)
    requester = svc.create_requester(timeout_ms=5_000)
    return make_rpc_session(model=spec, model_id=MODEL_ID,
                            service_name=service_name, hub=hub,
                            requester=requester, side_hub=side_hub,
                            _svc=svc, is_remote=False)


def run_one(session, text: str, csrc: str = "zh", ctgt: str = "en") -> str:
    """跑一次推理（同步）。跟 NpuTranslateProvider._infer 同一条路。"""
    from app.core.translate.npu_provider import _run_coro

    prompt = PROMPT_TMPL.format(csrc=csrc, ctgt=ctgt, text=text)
    out = _run_coro(session.respond(prompt=prompt, max_new_tokens=512))
    if isinstance(out, str):
        return out
    t = getattr(out, "text", None)
    return t if isinstance(t, str) else ("" if out is None else str(out))


# --------------------------------------------------------------------------- #
# 基准
# --------------------------------------------------------------------------- #
def bench(sessions, texts, threads: int, dump: bool = False):
    """跑一批文案。返回 (耗时, 成功数, 失败数)。

    ⚠️ **items 按条轮转分给 session**：``ThreadPoolExecutor.map`` 是按顺序
    取任务的，若 items 成块排列（前一半 A、后一半 B），最先跑起来的 N 个
    任务会全落在 A 上 —— 而一个 session 的 RPC 执行器 max_workers=1，
    于是两个 worker 抢着给同一个 session 排队，并发根本没发生。
    """
    ok = 0
    bad = 0
    lock = threading.Lock()
    samples = []

    def one(item):
        nonlocal ok, bad
        sess, text = item
        try:
            out = run_one(sess, text)
        except Exception as exc:  # noqa: BLE001
            out = ""
            err = str(exc)
        else:
            err = ""
        good = bool(out.strip()) and not _is_echo(out, text)
        with lock:
            if good:
                ok += 1
            else:
                bad += 1
            if dump and len(samples) < 8:
                samples.append((text, out, err))
        return good

    items = []
    for j, t in enumerate(texts):
        items.append((sessions[j % len(sessions)], t))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=threads) as ex:
        list(ex.map(one, items))
    dt = time.perf_counter() - t0
    if dump:
        for text, out, err in samples:
            print("      %-28s → %-46s %s"
                  % (text[:26], (out or "（空）")[:44], err[:40]))
    return dt, ok, bad


def _is_echo(out: str, src: str) -> bool:
    """模型有没有只是把原文复读回来（没翻）。"""
    from app.core.translate.npu_provider import _script_set

    if not out.strip():
        return True
    if out.strip() == src.strip():
        return True
    s = _script_set(out)
    return "han" in s and "latin" not in s


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", type=int, default=24)
    ap.add_argument("--layout", default="0",
                    help="每个 worker 放哪张卡，逗号分隔，如 0,0,1,1")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--dump", action="store_true", help="打印前 8 条译文")
    args = ap.parse_args()

    ensure_env()
    layout = [int(x) for x in args.layout.split(",") if x.strip() != ""]
    if not layout:
        print("--layout 不能为空")
        return 1

    texts = SEGMENTS[: max(1, args.segments)]

    # 每轮换文案：直接打 session，**不经过 TranslateService 的缓存**，
    # 所以本来就不用绕缓存 —— 但仍然换一批，避免模型侧的隐式前缀缓存
    # 带来「后几轮特别快」的假象。
    # ⚠️ 别加 ``#N`` 后缀：实测它会让模型把短文案复读回来（成功率从
    # 87% 掉到 ~50%），污染「质量」这一维的读数。
    def batch(i):
        return ["%s %d" % (t, i) for t in texts]

    from wdndds.dds._rpc import Hub as RpcHub

    hub = RpcHub(participant_name="wdnwebui-bench-%d" % os.getpid(),
                 domain_id=0, discovery_servers=[])
    hub.start()
    side_hub = RpcHub(participant_name="wdnwebui-bench-side-%d" % os.getpid(),
                      domain_id=0, discovery_servers=[])
    side_hub.start()

    procs = []
    sessions = []
    try:
        for i, card in enumerate(layout):
            print("起 worker #%d（卡 %d）…" % (i + 1, card), flush=True)
            p, svc, log = spawn_worker(card, "w%d" % (i + 1))
            procs.append(p)
            if not wait_ready(p, log):
                print("worker #%d 没能在 240s 内就绪 —— %s" % (i + 1, log))
                return 2
            sessions.append(build_session(svc, hub, side_hub))
            print("worker #%d 就绪 (pid=%d, 卡 %d)" % (i + 1, p.pid, card),
                  flush=True)

        # ---- 基线：只用第一个实例，串行 ----
        base = []
        for i in range(args.repeat):
            dt, ok, bad = bench([sessions[0]], batch(i), 1, dump=(args.dump and i == 0))
            base.append(dt)
            print("  基线 1实例串行   第%d次 %6.2fs  成功%d 失败%d"
                  % (i + 1, dt, ok, bad), flush=True)

        # ---- 全实例并发 ----
        full = []
        for i in range(args.repeat):
            dt, ok, bad = bench(sessions, batch(i), len(sessions))
            full.append(dt)
            print("  全实例并发(%d)  第%d次 %6.2fs  成功%d 失败%d"
                  % (len(sessions), i + 1, dt, ok, bad), flush=True)

        # ---- 线程数加倍（每个 session 排 2 个）----
        over = []
        for i in range(args.repeat):
            dt, ok, bad = bench(sessions, batch(i), len(sessions) * 2)
            over.append(dt)
            print("  全实例×2线程   第%d次 %6.2fs  成功%d 失败%d"
                  % (i + 1, dt, ok, bad), flush=True)

        b = statistics.median(base)
        f = statistics.median(full)
        o = statistics.median(over)
        print("\n===== 布局 %s（%d 个实例），条数=%d ====="
              % (args.layout, len(sessions), len(texts)))
        print("  1 实例串行      %6.2fs  %6.2f 条/秒  1.00×" % (b, len(texts) / b))
        print("  %d 实例并发     %6.2fs  %6.2f 条/秒  %.2fx"
              % (len(sessions), f, len(texts) / f, b / f))
        print("  %d 实例×2线程   %6.2fs  %6.2f 条/秒  %.2fx"
              % (len(sessions), o, len(texts) / o, b / o))
        return 0
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        try:
            hub.stop()
            side_hub.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
