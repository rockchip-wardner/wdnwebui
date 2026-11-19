#!/usr/bin/env python3
"""Intern-Decision 家族（4B / 0.8B）的模型接入自检。

背景
----
「图文内容审查」卡同时支持两个 Intern-Decision 型号：4B 与 0.8B。两者
**同 tokenizer、同控制 token id、同 prompt 协议** ⇒ 调用方代码一个都不用改，
换模型只是换一份权重。

这条链路上有三个**不报错但会静默出错**的坑，本脚本逐个钉住：

1. **manifest 必须能被发现**：模型目录里没有带 ``spec_factory`` 的 json ⇒
   ``discover()`` 认不到 ⇒ 算力页上根本不出现这张卡、registry 不认、
   卡片拿不到 session。文件名即 model_id（``<model_id>.json``）。
2. **两个型号的 worker 进程名不能撞**：registry 的
   ``_find_workers_by_name()`` 按 ``/proc/<pid>/comm`` **精确匹配**
   ``worker:<alias>``，且**截断到 15 字节**。4B 的 ``worker:intern-dec``
   被截成 ``worker:intern-d`` —— 0.8B 若沿用同一个 ``worker_name``，启停
   0.8B 时会把 4B 的 worker 当成自己的**孤儿杀掉**。
3. **0.8B 的元数据不能照抄 4B**：词表 / 控制 token id 必须**从 0.8B 自己的
   GGUF 里读出来**核对（embedding 宽度不同 ⇒ 校准温度不同）。

用法::

    .venv/bin/python scripts/verify_decision_models.py
"""
from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

_PASS = 0
_FAIL = 0
_SKIP = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name}   {extra}")


def skip(name: str, why: str = "") -> None:
    global _SKIP
    _SKIP += 1
    print(f"  SKIP {name}" + (f"   {why}" if why else ""))


# --------------------------------------------------------------------------- #
# GGUF 元数据读取（只取要的那几项）
# --------------------------------------------------------------------------- #
#: GGUF 的类型枚举（v3）。8 = STRING（**带长度前缀**）—— 别当成 f64 读。
_GGUF_STR = 8
_GGUF_ARRAY = 9


def gguf_meta(path: Path) -> dict:
    """读 GGUF 的 KV 区（tokenizer 就在这儿）。"""
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError("不是 GGUF 文件")
        f.read(4)                      # version
        f.read(8)                      # tensor count
        n_kv, = struct.unpack("<Q", f.read(8))

        def rstr() -> str:
            n, = struct.unpack("<Q", f.read(8))
            return f.read(n).decode("utf-8", errors="replace")

        def rval(t: int):
            if t == 0:
                return struct.unpack("<b", f.read(1))[0]
            if t == 1:
                return struct.unpack("<B", f.read(1))[0]
            if t == 2:
                return struct.unpack("<h", f.read(2))[0]
            if t == 3:
                return struct.unpack("<H", f.read(2))[0]
            if t == 4:
                return struct.unpack("<i", f.read(4))[0]
            if t == 5:
                return struct.unpack("<I", f.read(4))[0]
            if t == 6:
                return struct.unpack("<f", f.read(4))[0]
            if t == 7:
                return bool(struct.unpack("<?", f.read(1))[0])
            if t == _GGUF_STR:
                return rstr()
            if t == _GGUF_ARRAY:
                et, = struct.unpack("<I", f.read(4))
                n, = struct.unpack("<Q", f.read(8))
                return [rval(et) for _ in range(n)]
            if t == 10:
                return struct.unpack("<Q", f.read(8))[0]
            if t == 11:
                return struct.unpack("<q", f.read(8))[0]
            if t == 12:
                return struct.unpack("<d", f.read(8))[0]
            raise ValueError("未知 GGUF 类型 %d" % t)

        kv = {}
        for _ in range(n_kv):
            key = rstr()
            t, = struct.unpack("<I", f.read(4))
            kv[key] = rval(t)
        return kv


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 72)
    print("Intern-Decision 家族（4B / 0.8B）接入自检")
    print("=" * 72)

    from app.core.compute.workers import build_model_spec, discover, load_one

    M4B = "intern-decision-4b"
    M08B = "intern-decision-0.8b"

    # ---------------------------------------------------------------- [1] --
    print("\n[1] manifest 能被 worker 发现机制认到")
    found = {m.id: m for m in discover()}
    for mid in (M4B, M08B):
        m = found.get(mid)
        check("%s 被 discover() 发现" % mid, m is not None,
              "已发现：%s" % sorted(found))
        if m is None:
            continue
        check("%s 的 manifest 解析无错" % mid,
              getattr(m, "status", "ok") == "ok",
              "%s / %s" % (getattr(m, "status", ""), getattr(m, "errors", "")))
        check("%s 声明了 swcap_decision 能力" % mid,
              "swcap_decision" in (m.capability_types or []),
              str(m.capability_types))
        check("%s 的 spec_factory 指向 wdnai 上的方法" % mid,
              m.spec_factory in ("intern_decision_4b", "intern_decision_0_8b"),
              m.spec_factory)
        check("%s 的 model_dir 指向自己的目录" % mid,
              bool(m.model_dir) and m.model_dir.endswith(m.model_dir_name),
              "%s vs %s" % (m.model_dir, m.model_dir_name))

    # ---------------------------------------------------------------- [2] --
    print("\n[2] worker 进程名不冲突（撞名 = 启停时误杀对方的 worker）")
    from app.core.compute.registry import _worker_comm_name

    comms = {}
    for mid in sorted(found):
        comms[mid] = _worker_comm_name(mid)
    check("4B 的 comm 名已算出", bool(comms.get(M4B)), str(comms))
    check("0.8B 的 comm 名已算出", bool(comms.get(M08B)), str(comms))
    if comms.get(M4B) and comms.get(M08B):
        check("两个型号的 comm 名**不相同**（worker:intern-dec 会被截到 15 字节）",
              comms[M4B] != comms[M08B],
              "4B=%r 0.8B=%r" % (comms[M4B], comms[M08B]))
    # 与本板其它已装模型也不能撞
    dup = {}
    for mid, c in comms.items():
        dup.setdefault(c, []).append(mid)
    clashes = {c: mids for c, mids in dup.items() if len(mids) > 1}
    check("本板所有已装模型的 comm 名互不相同", not clashes, str(clashes))

    # ---------------------------------------------------------------- [3] --
    print("\n[3] 两个型号的 spec 都能造出来，且路径各自落在自己的目录里")
    specs = {}
    for mid in (M4B, M08B):
        try:
            spec = build_model_spec(mid)
            specs[mid] = spec
            check("%s 的 spec 构造成功" % mid, True, spec.name)
        except Exception as exc:  # noqa: BLE001
            check("%s 的 spec 构造成功" % mid, False, repr(exc))
    if len(specs) == 2:
        s4, s08 = specs[M4B], specs[M08B]
        check("模型名不同（4B / 0.8B）",
              s4.name != s08.name, "%s vs %s" % (s4.name, s08.name))
        check("词表大小一致（248320）",
              s4.vocab_size == s08.vocab_size == 248320,
              "%s vs %s" % (s4.vocab_size, s08.vocab_size))
        check("eos id 一致（248046）",
              s4.special_eos_id == s08.special_eos_id == 248046,
              "%s vs %s" % (s4.special_eos_id, s08.special_eos_id))
        check("视觉输入尺寸一致（384）",
              s4.image_size == s08.image_size == 384,
              "%s vs %s" % (s4.image_size, s08.image_size))
        check("推理 options 一致（同一套 prompt 协议）",
              s4.options == s08.options,
              "%s vs %s" % (s4.options, s08.options))
        check("每个型号的路径都在**自己**的模型目录下",
              Path(s08.rknn_llm_path).parent == Path(s08.tokenizer_path).parent
              and Path(s08.rknn_llm_path).parent != Path(s4.rknn_llm_path).parent,
              "%s vs %s" % (s08.rknn_llm_path, s4.rknn_llm_path))
        check("0.8B 的 tokenizer 在 0.8B 目录里",
              "0.8B" in Path(s08.tokenizer_path).name
              and Path(s08.tokenizer_path).is_file(),
              s08.tokenizer_path)

    # ---------------------------------------------------------------- [4] --
    print("\n[4] 0.8B 的 GGUF 实测：控制 token id 与 4B 一致（不能照抄）")
    if M08B not in specs:
        skip("0.8B 的 GGUF 元数据", "spec 没造出来")
    else:
        tok_path = Path(specs[M08B].tokenizer_path)
        if not tok_path.is_file():
            skip("0.8B 的 GGUF 元数据", "tokenizer 不在 %s" % tok_path)
        else:
            try:
                kv = gguf_meta(tok_path)
                toks = kv.get("tokenizer.ggml.tokens") or []
                check("0.8B 词表大小 = 248320", len(toks) == 248320, str(len(toks)))
                check("0.8B 的 eos = 248046",
                      kv.get("tokenizer.ggml.eos_token_id") == 248046,
                      str(kv.get("tokenizer.ggml.eos_token_id")))
                check("0.8B 的预分词器 = qwen35",
                      kv.get("tokenizer.ggml.pre") == "qwen35",
                      str(kv.get("tokenizer.ggml.pre")))
                for name, want in (("<decision>", 248077),
                                   ("<think>", 248068),
                                   ("</think>", 248069)):
                    idx = [i for i, t in enumerate(toks) if t == name]
                    check("0.8B 的 %s 是**单 token**且 id = %d" % (name, want),
                          idx == [want], str(idx))
            except Exception as exc:  # noqa: BLE001
                check("0.8B 的 GGUF 元数据可读", False, repr(exc))

    # ---------------------------------------------------------------- [5] --
    print("\n[5] 概率校准温度：按 embedding 宽度查表，两个宽度都在表里")
    from wdnai.decision import CHECKPOINT_TEMPERATURES

    check("温度表里有 0.8B 的 embedding 宽度 1024",
          1024 in CHECKPOINT_TEMPERATURES, str(sorted(CHECKPOINT_TEMPERATURES)))
    check("温度表里有 4B 的 embedding 宽度 2560",
          2560 in CHECKPOINT_TEMPERATURES, str(sorted(CHECKPOINT_TEMPERATURES)))
    check("两个宽度的温度不同（0.8B 更平）",
          CHECKPOINT_TEMPERATURES.get(1024) != CHECKPOINT_TEMPERATURES.get(2560),
          "%s vs %s" % (CHECKPOINT_TEMPERATURES.get(1024),
                        CHECKPOINT_TEMPERATURES.get(2560)))

    # ---------------------------------------------------------------- [6] --
    print("\n[6] 卡片侧：两个型号都在候选里")
    try:
        from wdnapps_audio_chat.cards.content_review.service import (
            DEFAULT_MODEL,
            MODEL_IDS,
        )
        check("卡片支持 4B 与 0.8B",
              MODEL_IDS == (M4B, M08B), str(MODEL_IDS))
        check("默认仍是 4B", DEFAULT_MODEL == M4B, DEFAULT_MODEL)
    except Exception as exc:  # noqa: BLE001
        check("卡片支持 4B 与 0.8B", False, repr(exc))

    from card_dirs import card_dir as _card_dir
    wiz = json.loads(
        (Path(_card_dir("content-review")) / "static" / "wizard.json")
        .read_text(encoding="utf-8"))
    pin = (wiz.get("swcap_to_model") or {}).get("swcap_decision") or {}
    check("向导首选 4B", pin.get("model_id") == M4B, str(pin.get("model_id")))
    check("向导候选里也有 0.8B（首选没装时能退过去）",
          M08B in (pin.get("models") or []), str(pin.get("models")))

    print("\n" + "=" * 72)
    print("通过 %d 项,失败 %d 项,跳过 %d 项" % (_PASS, _FAIL, _SKIP))
    print("=" * 72)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
