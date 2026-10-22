"""回归防护：grounding（视觉定位）链路的 prompt 里**不能出现 JSON Schema**。

背景（2026-10 实测，RK3588 真机）：

    vtl 卡片把「定位到 41 个目标、标签里混进
    ``description`` / ``ROI 左上角 x，0–1000 归一化。``」报给开发。

    根因不是模型输出质量，而是**输入侧拼接**：grounding 链路给模型
    发的 prompt 长达 2035 字符 —— 用户查询后面整段跟着
    ``LocatorDoc`` 的 JSON Schema。LocateAnything 是 grounding 模型，
    它会把 prompt 里的**每个名词短语都当成要定位的目标**，于是
    schema 的字段名 / 字段描述被逐个 ground 出来：

        <ref>description</ref><box>...</box>
        <ref>ROI 右下角 y，0–1000 归一化。</ref><box>...</box>

    真机复现（vtl 卡片本身，非模拟）：12 条事件里 11 条是 1 个目标、
    第 11 条炸成 **40 个**假框 + 1 个真框 = 用户报的「41 个目标」。
    同一批帧的 A/B 对照（广州地铁人流素材 + 查询「穿黄色衣服的人」）：

        修复前（schema 进 prompt）：每帧目标数 [1, 0, 144, 5, 2, 1]，最慢一帧 13.7 s
        修复后（干净 prompt）      ：每帧目标数 [1, 0,   1, 1, 1, 1]，稳定 ~0.5 s

    修复点有两层，**缺一不可**：

    1. wdnai：``LLMSession._suppress_generating_in_prompt`` 对
       ``kind == "grounding"`` 返回 True（``kind == "tts"`` 同理）；
    2. wdnwebui：主进程给 ``LLMRPCSession`` 传**真正的 ModelSpec**，
       而不是 worker manifest 的 preset dict —— preset dict 里没有
       ``kind``，于是第 1 层的判定拿到 ``None``，抑制**静默失效**。

本脚本**不占 NPU**：用一个假的 ``_dispatch`` 截获真正发给模型的
prompt，断言

  1. 每个 worker manifest 都能解析出带 ``kind`` 的 ModelSpec；
  2. grounding 的 prompt 里没有 schema hint，且就是用户查询本身；
  3. vlm 链路**仍然带** schema hint（防止修复过头）。

用法::

    python -m scripts.verify_grounding_prompt
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any, List

sys.path.insert(0, ".")

from pydantic import BaseModel  # noqa: E402

from wdnai import Field, Generable  # noqa: E402
from wdnai.model import SystemLanguageModel  # noqa: E402
from wdnai.session import LLMSession  # noqa: E402

from app.core.compute.workers import build_model_spec, discover  # noqa: E402

SCHEMA_MARKERS = (
    "Please strictly follow the JSON Schema", "Output JSON only",
    "```json", "$defs", "ROI 左上角 x", "ROI 右下角 y", "归一化",
    "description", "BoundingBox", "LocatorDoc",
)


@Generable
class _DummyDoc(BaseModel):
    """一个带中文描述的 schema —— 描述文本就是污染源本身。"""

    boxes: List[str] = Field(
        description="定位结果列表,每个元素是 0–1000 归一化坐标(x1/y1 左上角,x2/y2 右下角)。",
    )
    text: str = Field(description="模型回显的原始 grounding 文本。")


class _CaptureSession(LLMSession):
    """只截获 prompt，不真的推理。"""

    def __init__(self, model: Any) -> None:
        super().__init__(model)
        self.captured: List[str] = []

    async def _dispatch(self, prompt, attachments, *, on_delta=None, **overrides):
        self.captured.append(prompt)
        return "ok", []

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass

    def embed_text(self, text):
        raise NotImplementedError

    def embed_video(self, frames, prompt=""):
        raise NotImplementedError


async def _capture(spec, query: str) -> str:
    sess = _CaptureSession(spec)
    await sess.respond(prompt=query, attachment=None, generating=_DummyDoc)
    return sess.captured[-1]


def main() -> int:
    query = "穿黄色衣服的人"
    fails: List[str] = []

    # ---- 1) 每个 manifest 都能解析出带 kind 的 ModelSpec -------------------
    print("manifest → ModelSpec:")
    for m in discover():
        if m.status != "ok":
            print(f"  {m.id:24s} status={m.status} (跳过)")
            continue
        try:
            spec = build_model_spec(m.id)
        except Exception as e:  # noqa: BLE001
            fails.append(f"{m.id}: 解析 ModelSpec 失败 —— {e}")
            print(f"  {m.id:24s} ❌ {e}")
            continue
        kind = getattr(spec, "kind", None)
        if not kind:
            fails.append(f"{m.id}: ModelSpec 没有 kind —— "
                         f"SDK 无法判断模型族，JSON Schema 抑制会静默失效")
        print(f"  {m.id:24s} kind={kind!r}")

    # ---- 2) grounding：prompt 必须只有用户查询 -----------------------------
    grounding = build_model_spec("LocateAnything-3b")
    if getattr(grounding, "kind", None) != "grounding":
        fails.append(
            f"LocateAnything-3b 的 kind 是 "
            f"{getattr(grounding, 'kind', None)!r}，不是 'grounding'")
    g_prompt = asyncio.run(_capture(grounding, query))

    if g_prompt != query:
        fails.append(
            f"grounding prompt 不是用户查询本身:\n"
            f"  期望 {query!r}\n  实际 {g_prompt[:200]!r} (len={len(g_prompt)})")
    for bad in SCHEMA_MARKERS:
        if bad in g_prompt:
            fails.append(f"grounding prompt 里出现了 schema 片段 {bad!r}")
    print(f"\ngrounding prompt  len={len(g_prompt)}  {g_prompt!r}")

    # ---- 3) vlm：schema hint 必须还在 --------------------------------------
    vlm = build_model_spec("qwen3-vl-4b")
    v_prompt = asyncio.run(_capture(vlm, query))
    if "Please strictly follow the JSON Schema" not in v_prompt:
        fails.append(
            "vlm prompt 里没有 schema hint —— 抑制过头了，"
            "Qwen3-VL 的结构化输出会被关掉")
    print(f"vlm prompt        len={len(v_prompt)}  "
          f"(含 schema hint: "
          f"{'Please strictly follow the JSON Schema' in v_prompt})")

    # ---- 4) preset dict 不能当 model 传（老坑）----------------------------
    from app.core.compute.registry import MODEL_PRESETS
    preset = MODEL_PRESETS.get("LocateAnything-3b") or {}
    if "kind" in preset:
        print("note: preset dict 现在带 kind（兼容性更好，但主进程仍应传 ModelSpec）")
    else:
        print("note: preset dict 不带 kind —— 主进程必须传 build_model_spec() 的结果")

    if fails:
        print("\n❌ 失败：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\n✅ grounding prompt 不含 JSON Schema；vlm 的 schema hint 未受影响")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
