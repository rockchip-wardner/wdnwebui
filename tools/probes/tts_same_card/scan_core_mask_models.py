#!/usr/bin/env python3
"""用 **SDK 原路径**（RKNN3Lite.init_runtime）逐模型试 core_mask。

目的：厘清「普通 runtime 拒绝 0xFF」到底是
  (a) 驱动对普通 runtime 的通用规则（所有模型都不行）
  (b) 只对 text_projection 这个小模型成立（模型编译时的核数元信息）
  (c) 探针脚本本身有问题

模型：text_projection（普通，SDK 用 0x01）/ speech_decoder（普通，SDK 用 0xFF）
      talker（llm_mode，SDK 用 0xFF）/ code_predictor（llm_mode，SDK 用 0xFF）
"""
import sys

sys.path.insert(0, "/userdata/wdnwebui")

BDF = {
    0: b"0001:11:00.0",
    1: b"0003:31:00.0",
    2: b"0004:41:00.0",
}

MD = "/userdata/models/Qwen3-TTS"

CASES = [
    # (label, rknn, weight, llm_mode, masks)
    ("text_projection", f"{MD}/text_projection.1828.rknn", f"{MD}/text_projection.weight", False, [0xFF, 0x01, 0x02]),
    ("speech_decoder", f"{MD}/speech_decoder.1828.rknn", f"{MD}/speech_decoder.weight", False, [0xFF, 0x01, 0x0F]),
    ("talker", f"{MD}/talker.1828.rknn", f"{MD}/talker.weight", True, [0xFF, 0x01, 0x0F]),
    ("code_predictor", f"{MD}/code_predictor.1828.rknn", f"{MD}/code_predictor.weight", True, [0xFF, 0x01]),
]


def try_one(rknn, weight, llm_mode, mask, bdf):
    from wdnai._librknn import RKNN3Lite
    t = RKNN3Lite(llm_mode=llm_mode, verbose=False)
    ret = t.load_rknn(rknn, weight)
    if ret != 0:
        return "load_rknn ret=%s" % ret
    ret = t.init_runtime(target="rk1820", core_mask=mask, device_id=bdf)
    if ret != 0:
        return "init_runtime ret=%s" % ret
    try:
        if t.rknn_runtime is not None:
            t.rknn_runtime.release()
    except Exception:
        pass
    return "OK"


def main():
    card_no = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    bdf = BDF[card_no]
    for label, rknn, weight, llm_mode, masks in CASES:
        for m in masks:
            res = try_one(rknn, weight, llm_mode, m, bdf)
            print("card%d %-16s llm_mode=%-5s core_mask=0x%02X -> %s"
                  % (card_no, label, llm_mode, m, res))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
