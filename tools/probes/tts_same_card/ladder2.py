#!/usr/bin/env python3
"""组合阶梯：按顺序建一组 (role, mask, count)，看在哪一步撞墙。

用法：
    python ladder2.py "text_projection:0x01:40"
    python ladder2.py "speech_decoder:0xFF:4,text_projection:0x01:4"

每个 spec 形如 role:mask:count，逗号分隔，按顺序执行。
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, "/userdata/wdnwebui")

CARD = int(os.environ.get("PROBE_CARD", "1"))
BDF = {0: b"0001:11:00.0", 1: b"0003:31:00.0", 2: b"0004:41:00.0"}[CARD]
MD = "/userdata/models/Qwen3-TTS"

ROLE = {
    "text_projection": (f"{MD}/text_projection.1828.rknn", f"{MD}/text_projection.weight", False),
    "speech_decoder": (f"{MD}/speech_decoder.1828.rknn", f"{MD}/speech_decoder.weight", False),
    "talker": (f"{MD}/talker.1828.rknn", f"{MD}/talker.weight", True),
    "code_predictor": (f"{MD}/code_predictor.1828.rknn", f"{MD}/code_predictor.weight", True),
}

MEM_SNIPPET = (
    "import sys;sys.path.insert(0,'/userdata/wdnwebui');"
    "from wdnai._librknnsmi import RknnSmi;c=RknnSmi();"
    "mi=c.get_memory_info(device_id=%d, chip_id=0);"
    "print('used=%%.0fMB free=%%.0fMB'%%((mi['total_size']-mi['free_size'])/1048576.0,mi['free_size']/1048576.0))"
) % CARD

KEEP = []


def mem():
    try:
        out = subprocess.run(
            ["sudo", "-n", "/userdata/wdnwebui/.venv/bin/python", "-c", MEM_SNIPPET],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r"used=\d+MB free=\d+MB", out.stdout)
        return m.group(0) if m else "?"
    except Exception:
        return "?"


def main():
    spec = sys.argv[1]
    from wdnai._librknn import RKNN3Lite
    print("[start] %s" % mem())
    for part in spec.split(","):
        role, mask_s, cnt_s = part.split(":")
        mask = int(mask_s, 16)
        cnt = int(cnt_s)
        rknn, weight, llm_mode = ROLE[role]
        for i in range(cnt):
            t = RKNN3Lite(llm_mode=llm_mode, verbose=False)
            if t.load_rknn(rknn, weight) != 0:
                print("%s#%d load_rknn FAILED" % (role, i + 1))
                break
            ret = t.init_runtime(target="rk1820", core_mask=mask, device_id=BDF)
            if ret != 0:
                print(">>> %s#%d mask=0x%02X FAILED ret=%s   [%s]  <== 撞墙"
                      % (role, i + 1, mask, ret, mem()))
                sys.stdout.flush()
                break
            KEEP.append(t)
            print("    %s#%d mask=0x%02X OK   [%s]" % (role, i + 1, mask, mem()))
            sys.stdout.flush()
        else:
            print("    %s ×%d 全部成功" % (role, cnt))


if __name__ == "__main__":
    main()
