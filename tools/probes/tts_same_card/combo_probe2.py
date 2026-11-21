#!/usr/bin/env python3
"""裸组合 + LLM session + 输出张量 —— 逼近真实 TTS 实例的构造顺序。

真实实例（ensure_loaded 顺序）：
    text_projection (普通 0x01)
    speech_decoder  (普通 0xFF)
    code_predictor  (llm 0xFF, init_runtime 带 llm_args ⇒ 建 session)
    talker          (llm 0xFF, init_runtime 无 args ⇒ 不建 session
                     → 再 init_llm_session 建 session
                     → 再 create_output_tensors([0,1]))

逐段加，看哪一步让「第二个 talker」挂掉。
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


def cp_args():
    from wdnai.backends.tts import (
        CODEC_MAX_CONTEXT_LEN, CODEC_VOCAB_SIZE, CODEC_EOS_TOKEN_ID,
        CODEC_MAX_NEW_TOKENS, CODE_PREDICTOR_SAMPLING,
    )
    return [{
        "max_context_len": CODEC_MAX_CONTEXT_LEN, "vocab_size": CODEC_VOCAB_SIZE,
        "special_eos_id": CODEC_EOS_TOKEN_ID, "max_new_tokens": CODEC_MAX_NEW_TOKENS,
        "keep_history": 0, "logits_name": "logits", **CODE_PREDICTOR_SAMPLING,
    }]


def talker_args():
    from wdnai.backends.tts import (
        QWEN3_TTS_MAX_CONTEXT_LEN, TALKER_VOCAB_SIZE, TALKER_SAMPLING,
    )
    return [{
        "max_context_len": QWEN3_TTS_MAX_CONTEXT_LEN, "vocab_size": TALKER_VOCAB_SIZE,
        "special_eos_id": 0, "max_new_tokens": QWEN3_TTS_MAX_CONTEXT_LEN,
        "keep_history": 0, "logits_name": "logits", **TALKER_SAMPLING,
    }]


def new_rt(role, llm_mode):
    from wdnai._librknn import RKNN3Lite
    t = RKNN3Lite(llm_mode=llm_mode, verbose=False)
    assert t.load_rknn(f"{MD}/{role}.1828.rknn", f"{MD}/{role}.weight") == 0
    return t


def step(tag, fn):
    try:
        fn()
    except Exception as e:
        print(">>> %s FAILED: %s   [%s]  <== 撞墙" % (tag, e, mem()))
        sys.stdout.flush()
        return False
    print("    %s OK  [%s]" % (tag, mem()))
    sys.stdout.flush()
    return True


def main():
    # ---- 实例 A ----
    a_tp = new_rt("text_projection", False)
    assert step("A.text_projection", lambda: a_tp.init_runtime(
        target="rk1820", core_mask=0x01, device_id=BDF)) is True
    KEEP.append(a_tp)

    a_sdec = new_rt("speech_decoder", False)
    assert step("A.speech_decoder", lambda: a_sdec.init_runtime(
        target="rk1820", core_mask=0xFF, device_id=BDF)) is True
    KEEP.append(a_sdec)

    a_cp = new_rt("code_predictor", True)
    assert step("A.code_predictor(runtime+session)", lambda: a_cp.init_runtime(
        target="rk1820", core_mask=0xFF, device_id=BDF,
        llm_args=cp_args(), llm_callback=make_cb())) is True
    KEEP.append(a_cp)

    a_talker = new_rt("talker", True)
    assert step("A.talker(runtime)", lambda: a_talker.init_runtime(
        target="rk1820", core_mask=0xFF, device_id=BDF)) is True
    KEEP.append(a_talker)
    assert step("A.talker(output_tensors)", lambda: a_talker.create_output_tensors([0, 1])) is True
    assert step("A.talker(session)", lambda: a_talker.init_llm_session(
        llm_args=talker_args(), llm_callback=make_cb(), session_index=0)) is True

    # ---- 实例 B ----
    b_tp = new_rt("text_projection", False)
    assert step("B.text_projection", lambda: b_tp.init_runtime(
        target="rk1820", core_mask=0x01, device_id=BDF)) is True
    KEEP.append(b_tp)

    b_sdec = new_rt("speech_decoder", False)
    assert step("B.speech_decoder", lambda: b_sdec.init_runtime(
        target="rk1820", core_mask=0xFF, device_id=BDF)) is True
    KEEP.append(b_sdec)

    b_cp = new_rt("code_predictor", True)
    assert step("B.code_predictor(runtime+session)", lambda: b_cp.init_runtime(
        target="rk1820", core_mask=0xFF, device_id=BDF,
        llm_args=cp_args(), llm_callback=make_cb())) is True
    KEEP.append(b_cp)

    b_talker = new_rt("talker", True)
    ok = step("B.talker(runtime)", lambda: b_talker.init_runtime(
        target="rk1820", core_mask=0xFF, device_id=BDF))
    if not ok:
        print("### 真实双实例的阻塞点复现：第二个 talker 的 model setup ###")
        return
    KEEP.append(b_talker)
    step("B.talker(output_tensors)", lambda: b_talker.create_output_tensors([0, 1]))
    step("B.talker(session)", lambda: b_talker.init_llm_session(
        llm_args=talker_args(), llm_callback=make_cb(), session_index=0))
    print("### 两个完整实例的 runtime + session 全部共存 ###")


if __name__ == "__main__":
    main()
