#!/usr/bin/env python3
"""测「每卡能同时存在几个 LLM session」，以及「一个 runtime 能开几个 session」。

背景：裸 runtime 集合（2×tp + 2×sdec + 2×cp + 2×talker）能共存，
但真实双 TTS 实例在第 2 个实例的 talker 上 MODEL_SETUP fail
⇒ 怀疑阻塞项是 **LLM session**，不是上下文数。

用法：
    python session_quota.py contexts   # N 个 runtime，每个建 1 个 session
    python session_quota.py per_ctx    # 1 个 runtime，建 N 个 session
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, "/userdata/wdnwebui")

CARD = int(os.environ.get("PROBE_CARD", "1"))
BDF = {0: b"0001:11:00.0", 1: b"0003:31:00.0", 2: b"0004:41:00.0"}[CARD]
MD = "/userdata/models/Qwen3-TTS"

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


def _dummy(*a, **kw):
    return 0


def make_cb():
    from wdnai._librknn import build_llm_callback
    return build_llm_callback(result_cb=_dummy, embed_callback=_dummy)


def code_predictor_args():
    from wdnai.backends.tts import (
        CODEC_MAX_CONTEXT_LEN, CODEC_VOCAB_SIZE, CODEC_EOS_TOKEN_ID,
        CODEC_MAX_NEW_TOKENS, CODE_PREDICTOR_SAMPLING,
    )
    return [{
        "max_context_len": CODEC_MAX_CONTEXT_LEN,
        "vocab_size": CODEC_VOCAB_SIZE,
        "special_eos_id": CODEC_EOS_TOKEN_ID,
        "max_new_tokens": CODEC_MAX_NEW_TOKENS,
        "keep_history": 0,
        "logits_name": "logits",
        **CODE_PREDICTOR_SAMPLING,
    }]


def new_runtime():
    from wdnai._librknn import RKNN3Lite
    t = RKNN3Lite(llm_mode=True, verbose=False)
    assert t.load_rknn(f"{MD}/code_predictor.1828.rknn",
                       f"{MD}/code_predictor.weight") == 0
    return t


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "contexts"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    print("[start] %s" % mem())

    if mode == "contexts":
        # N 个 runtime × 1 session
        for i in range(n):
            t = new_runtime()
            ret = t.init_runtime(target="rk1820", core_mask=0xFF, device_id=BDF)
            if ret != 0:
                print(">>> ctx#%d init_runtime FAILED ret=%s [%s]"
                      % (i + 1, ret, mem()))
                sys.stdout.flush()
                break
            KEEP.append(t)
            ret = t.init_llm_session(llm_args=code_predictor_args(),
                                      llm_callback=make_cb(), session_index=0)
            if ret != 0:
                print(">>> ctx#%d session FAILED ret=%s [%s]"
                      % (i + 1, ret, mem()))
                sys.stdout.flush()
                break
            print("    ctx#%d runtime+session OK [%s]" % (i + 1, mem()))
            sys.stdout.flush()
    else:
        # 1 个 runtime × N 个 session
        t = new_runtime()
        ret = t.init_runtime(target="rk1820", core_mask=0xFF, device_id=BDF)
        print("runtime init ret=%s [%s]" % (ret, mem()))
        if ret != 0:
            return
        KEEP.append(t)
        for i in range(n):
            ret = t.init_llm_session(llm_args=code_predictor_args(),
                                      llm_callback=make_cb(), session_index=i)
            if ret != 0:
                print(">>> session#%d FAILED ret=%s [%s]" % (i + 1, ret, mem()))
                sys.stdout.flush()
                break
            print("    session#%d OK [%s]" % (i + 1, mem()))
            sys.stdout.flush()
        print("### 一个 runtime 上成功建了若干 session ###")


if __name__ == "__main__":
    main()
