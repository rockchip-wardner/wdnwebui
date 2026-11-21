#!/usr/bin/env python3
"""单模型探针：找它的「编译核数」。

用法：python scan_one.py <rknn> <weight> [llm]
输出：MASK=0x?? CORE=??
"""
import faulthandler
import os
import sys

sys.path.insert(0, "/userdata/wdnwebui")
faulthandler.enable()

CARD = int(os.environ.get("PROBE_CARD", "1"))
BDF = {0: b"0001:11:00.0", 1: b"0003:31:00.0", 2: b"0004:41:00.0"}[CARD]
MASKS = [0x01, 0x03, 0x07, 0x0F, 0x1F, 0x3F, 0x7F, 0xFF]


def main():
    rknn, weight = sys.argv[1], sys.argv[2]
    llm_mode = len(sys.argv) > 3 and sys.argv[3] == "llm"
    from wdnai._librknn import RKNN3Lite, _QueryCmd
    for mask in MASKS:
        t = RKNN3Lite(llm_mode=llm_mode, verbose=False)
        if t.load_rknn(rknn, weight) != 0:
            print("LOADFAIL", flush=True)
            return
        ret = t.init_runtime(target="rk1820", core_mask=mask, device_id=BDF)
        if ret != 0:
            continue
        try:
            cores = t.rknn_runtime.rknn3_query(int(_QueryCmd.CORE_NUMBER))
        except Exception as e:
            cores = "query-failed:%s" % e
        print("MASK=0x%02X CORE=%s" % (mask, cores), flush=True)
        try:
            t.rknn_runtime.release()
        except Exception:
            pass
        return
    print("NOMASK", flush=True)


if __name__ == "__main__":
    main()
