#!/usr/bin/env python3
"""同卡双 TTS 实例：真实 SDK 路径 + 驱动原始报错 + 卡上内存曲线。

在卡 1（板端规划给 TTS 的卡，当前空闲）上：
  1. 建第 1 个会话（talker/code_predictor/speech_decoder/text_projection）
  2. 建第 2 个会话 —— 抓**完整 traceback** 和驱动 stderr 原文
  3. 每步之间采样卡上内存（free/used）

驱动 stderr 不经 SDK 包装（SDK 把异常吞成 return -1），所以必须落文件看。
"""
import os
import subprocess
import sys
import traceback

sys.path.insert(0, "/userdata/wdnwebui")

CARD = int(os.environ.get("PROBE_CARD", "1"))
BDF = {0: "0001:11:00.0", 1: "0003:31:00.0", 2: "0004:41:00.0"}[CARD]

MEM_SNIPPET = r"""
import sys
sys.path.insert(0, "/userdata/wdnwebui")
from wdnai._librknnsmi import RknnSmi
c = RknnSmi()
mi = c.get_memory_info(device_id=%d, chip_id=0)
print("used=%%.0fMB free=%%.0fMB" %% ((mi['total_size']-mi['free_size'])/1048576.0, mi['free_size']/1048576.0))
"""


def mem(tag):
    try:
        out = subprocess.run(
            ["sudo", "-n", "/userdata/wdnwebui/.venv/bin/python", "-c",
             MEM_SNIPPET % CARD],
            capture_output=True, text=True, timeout=60,
        )
        print("[mem %s] %s" % (tag, out.stdout.strip() or out.stderr.strip()))
    except Exception as e:
        print("[mem %s] FAILED %s" % (tag, e))
    sys.stdout.flush()


def build(tag):
    from app.core.compute.workers._base import _build_session
    print("=== building %s on card %d (%s) ===" % (tag, CARD, BDF))
    sys.stdout.flush()
    try:
        s = _build_session("qwen3-tts", CARD, None)
        print("=== %s OK ===" % tag)
        sys.stdout.flush()
        return s
    except Exception:
        print("=== %s FAILED ===" % tag)
        traceback.print_exc()
        sys.stdout.flush()
        return None


def main():
    mem("before")
    s1 = build("instance1")
    mem("after-instance1")
    if s1 is None:
        return
    s2 = build("instance2")
    mem("after-instance2")
    if s2 is not None:
        print("### 同卡两个完整实例都起来了 ###")
    else:
        print("### 第二个实例失败 ###")


if __name__ == "__main__":
    main()
