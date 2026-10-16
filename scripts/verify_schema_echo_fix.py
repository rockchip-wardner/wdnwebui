"""verify_schema_echo_fix.py — Regression test for VAS schema-echo detection in all paths.

User-reported bug (2026-09):
  在"监控分析检索"页面对 /userdata/video_test/rk-0.mp4 等部分视频进行分析时,
  逐帧分析列表概率性显示
    ``"scene 户外街道 actors action objects text"``
  —— 实质上是 Qwen3-VL-4B 在 RKNN3 上的 schema-echo 退化输出:模型把 user
  prompt 中 JSON 示例的字段名原样 echo 到 ``text`` 字段,其他字段(scene /
  action)却能填真值。

Root cause:
  ``_split_scene_frame_result()`` 已有 ``_looks_like_schema_echo`` 检测,但只
  在 Path 1 (SceneFrame 实例) 调用。Path 2 (dict 路径,worker 通过
  ``model_dump()`` 还原的 dict) 和 Path 3 (str 路径,SDK Path 5
  兜底返回 raw text) 都漏检。结果:
    * Path 2: ``_text_from_dict`` 取出 echo text → 前端当描述渲染
    * Path 3: 同 Path 2,且 raw 是 str 整体兜底直接显示

Fix (v0.15+):
  1. 把 ``_looks_like_schema_echo`` 调用扩展到 Path 2 / Path 3。
  2. 加结构性标记排除 (``:`` / ``：`` / ``{`` / ``[``) 避免误杀合法
     YAML / JSON 输入。
  3. Path 2 检测到 echo 时:丢弃 bad text 但**保留** structured 字段
     (scene='户外街道' 等真值仍可被反查 / 取帧 modal 看到),并从
     structured 字段合成 fallback text。

This test verifies:
  1. ``_looks_like_schema_echo`` 对纯 echo 返回 True,对结构化输入返回 False
  2. Path 1 (SceneFrame):echo 时静默丢弃 (text='' + structured=None)
  3. Path 2 (dict):echo 时丢弃 bad text,保留 structured 字段
  4. Path 3 (str):echo 时静默丢弃;合法 YAML 完整通过
  5. 正常 valid 输出在三条路径都不被误杀 (regression guard)
  6. ``_normalize_structured`` 不再把 dict 顶层 ``text`` 字段污染进 note

Run from repo root:
    source .venv/bin/activate
    python scripts/verify_schema_echo_fix.py
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)


# --------------------------------------------------------------------------- #
# Tests: _looks_like_schema_echo detection logic                              #
# --------------------------------------------------------------------------- #

def test_echo_pattern_detected():
    """User-reported bug pattern (5 field names, space-separated)."""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    text = "scene 户外街道 actors action objects text"
    assert _looks_like_schema_echo(text) is True, \
        f"BUG: should detect echo pattern, got False for {text!r}"


def test_echo_pattern_variants_detected():
    """Other variants with ≥3 field names + CJK in prose form."""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    variants = [
        "scene 小区入口 actors action objects text",  # variant scene value
        "scene actors action",                       # exactly 3 names (threshold)
        "scene actors action objects text note",      # all 6 names
    ]
    for text in variants:
        assert _looks_like_schema_echo(text) is True, \
            f"BUG: should detect {text!r}"


def test_yaml_input_not_flagged():
    """YAML key-value (合法输入) 不会被误判为 echo —— 有 ASCII ``:`` 即跳过。"""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    text = "scene: 户外\nactors: 行人\naction: 走路\nobjects: 车"
    assert _looks_like_schema_echo(text) is False, \
        f"REGRESSION: YAML input should not be flagged, got True for {text!r}"


def test_yaml_full_width_colon_not_flagged():
    """YAML with 全角冒号 ``：`` 同样跳过。"""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    text = "scene：户外\nactors：行人\naction：走路\nobjects：车"
    assert _looks_like_schema_echo(text) is False, \
        f"REGRESSION: YAML with full-width colon should not be flagged"


def test_json_input_not_flagged():
    """JSON 含 ``{`` 跳过 echo 检测。"""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    text = '{"scene": "户外街道", "actors": [], "action": "走路", "objects": []}'
    assert _looks_like_schema_echo(text) is False, \
        f"REGRESSION: JSON input should not be flagged"


def test_normal_prose_not_flagged():
    """普通描述不含字段名 → False (不会误杀)。"""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    assert _looks_like_schema_echo("A person walks on the street.") is False
    assert _looks_like_schema_echo("厨师在厨房切菜。") is False
    assert _looks_like_schema_echo("SceneFrame mentioned once.") is False


def test_below_threshold_not_flagged():
    """<3 字段名时不触发 (避免单行偶然含字段名被误杀)。"""
    from app.apps.video_analysis_search.service import _looks_like_schema_echo
    assert _looks_like_schema_echo("scene actors") is False  # only 2
    assert _looks_like_schema_echo("scene") is False         # only 1
    assert _looks_like_schema_echo("") is False              # empty


# --------------------------------------------------------------------------- #
# Tests: _split_scene_frame_result paths                                      #
# --------------------------------------------------------------------------- #

def test_path1_scene_frame_echo_drops_all():
    """Path 1: SceneFrame 实例路径 echo 时静默丢弃 (text='' + structured=None)。"""
    from app.apps.video_analysis_search.service import (
        _split_scene_frame_result, _get_scene_frame_cls,
    )
    SF = _get_scene_frame_cls()
    sf = SF(scene="户外街道", actors=[], action="", objects=[],
            text="scene 户外街道 actors action objects text")
    structured, text = _split_scene_frame_result(sf)
    assert text == "", f"Path 1 echo: should drop text, got {text!r}"
    assert structured is None, f"Path 1 echo: should drop structured, got {structured}"


def test_path2_dict_echo_keeps_scene_drops_echo_text():
    """Path 2: dict 路径 echo 时保留 scene 真值,丢弃 echo text,从 structured 合成 fallback。

    这是 user-reported bug 的核心场景 —— SceneFrame 解析后变 dict,text 字段
    是 echo 但 scene='户外街道' 是真值。修后行为:text 不再含 schema 字段名,
    fronted 看到 "户外街道" 而不是 "scene 户外街道 actors action objects text"。
    """
    from app.apps.video_analysis_search.service import _split_scene_frame_result
    d = {
        "scene":   "户外街道",
        "actors":  [],
        "action":  "",
        "objects": [],
        "text":    "scene 户外街道 actors action objects text",
        "note":    None,
    }
    structured, text = _split_scene_frame_result(d)
    # echo 字段名不应出现在 text 里
    field_names = ["scene", "actors", "action", "objects", "text", "note"]
    assert not any(name in (text or "").split() for name in field_names), \
        f"Path 2 echo: text should NOT contain schema field names, got {text!r}"
    # structured.scene 真值要保留
    assert structured is not None, "Path 2 echo: structured should not be None"
    assert structured.get("scene") == "户外街道", \
        f"Path 2 echo: scene='户外街道' should be preserved, got {structured}"
    # text 应该从 scene 合成 fallback
    assert "户外街道" in text, \
        f"Path 2 echo: text should contain scene value as fallback, got {text!r}"


def test_path3_str_echo_drops_all():
    """Path 3: str 路径 echo 时静默丢弃 (与 Path 1 对齐)。"""
    from app.apps.video_analysis_search.service import _split_scene_frame_result
    structured, text = _split_scene_frame_result(
        "scene 户外街道 actors action objects text"
    )
    assert text == "", f"Path 3 echo: should drop, got {text!r}"
    assert structured is None, f"Path 3 echo: structured should be None"


def test_path3_str_yaml_passes_through():
    """Path 3: 合法 YAML 输入完整通过 (regression guard —— 修复前可能误杀)。"""
    from app.apps.video_analysis_search.service import _split_scene_frame_result
    text = "scene: 户外\nactors: 行人\naction: 走路\nobjects: 车"
    structured, returned_text = _split_scene_frame_result(text)
    assert structured is not None, \
        f"Path 3 YAML: should NOT drop, got {structured}"
    assert structured.get("scene") == "户外", \
        f"Path 3 YAML: scene should be parsed, got {structured}"
    assert structured.get("actors") == ["行人"], \
        f"Path 3 YAML: actors should be parsed, got {structured}"
    assert returned_text == text, "Path 3 YAML: raw text should be preserved"


# --------------------------------------------------------------------------- #
# Regression: valid outputs not broken                                        #
# --------------------------------------------------------------------------- #

def test_path1_valid_passes_through():
    """Path 1: 合法 SceneFrame 完整通过 (修后不能误伤)。"""
    from app.apps.video_analysis_search.service import (
        _split_scene_frame_result, _get_scene_frame_cls,
    )
    SF = _get_scene_frame_cls()
    sf = SF(scene="厨房", actors=["厨师"], action="切菜", objects=["刀"],
            text="厨师在厨房切菜。")
    structured, text = _split_scene_frame_result(sf)
    assert text == "厨师在厨房切菜。", f"Path 1 valid: text broken, got {text!r}"
    assert structured["scene"] == "厨房", f"Path 1 valid: scene broken"
    assert structured["actors"] == ["厨师"], f"Path 1 valid: actors broken"


def test_path2_valid_passes_through():
    """Path 2: 合法 dict 完整通过 (修后不能误伤)。"""
    from app.apps.video_analysis_search.service import _split_scene_frame_result
    d = {
        "scene": "厨房", "actors": ["厨师"], "action": "切菜", "objects": ["刀"],
        "text": "厨师在厨房切菜。", "note": None,
    }
    structured, text = _split_scene_frame_result(d)
    assert text == "厨师在厨房切菜。", f"Path 2 valid: text broken, got {text!r}"
    assert structured["scene"] == "厨房"
    assert structured["actors"] == ["厨师"]


# --------------------------------------------------------------------------- #
# Regression: _normalize_structured no longer pollutes note with "text"      #
# --------------------------------------------------------------------------- #

def test_normalize_structured_skips_text_field():
    """_normalize_structured 不再把 dict 顶层 ``text`` 字段塞进 note_extras。

    Bug 副作用:旧实现把 ``text`` 当未知字段 → note 变成 "text: scene 户外街道
    actors ..." 二次污染。修后 ``text`` 列入 _KNOWN_BUT_DROP 集合,顶层 text
    在 ``_text_from_dict`` 已取出,这里直接 skip。
    """
    from app.apps.video_analysis_search.service import _normalize_structured
    d = {
        "scene":  "户外街道",
        "actors": [],
        "action": "",
        "objects": [],
        "text":   "scene 户外街道 actors action objects text",  # echo
        "note":   None,
    }
    structured = _normalize_structured(d)
    # note 不应包含 echo 字段名
    assert structured["note"] is None, \
        f"note should be None (text is echo, but text no longer leaks to note), got {structured['note']!r}"
    assert "scene" not in (structured["note"] or ""), \
        f"note should not contain schema field names, got {structured['note']!r}"


def test_normalize_structured_preserves_legitimate_extras():
    """_normalize_structured 仍把合法未知字段 (如 analysis / error) 拼到 note。"""
    from app.apps.video_analysis_search.service import _normalize_structured
    d = {
        "scene":    "厨房",
        "actors":   ["厨师"],
        "action":   "切菜",
        "objects":  ["刀"],
        "text":     "厨师在厨房切菜。",
        "note":     None,
        "analysis": "画面分辨率 1920x1080",
    }
    structured = _normalize_structured(d)
    # analysis 字段应该进入 note
    assert structured["note"] is not None
    assert "analysis" in structured["note"]
    assert "1920x1080" in structured["note"]


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Tests: _sanitize_text echo handling (v0.15+ critical fix)                 #
# --------------------------------------------------------------------------- #

def test_sanitize_text_json_inner_echo_dropped():
    """JSON-wrapped echo: model 输出 JSON,内层 text 是 echo,整体会被
    _sanitize_text 抽取后命中 schema-echo,返回空串。这是 Path 2 的
    colon-check 短路掉的关键漏洞的最终兜底。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    raw = '{"scene":"户外街道","actors":[],"action":"","objects":[],"text":"scene 户外街道 actors action objects text"}'
    assert _sanitize_text(raw) == "",         f"_sanitize_text should drop JSON-wrapped echo, got {_sanitize_text(raw)!r}"


def test_sanitize_text_pure_echo_dropped():
    """纯 echo 文本 (无 JSON 包装) 命中 schema-echo → 返回空串。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    assert _sanitize_text("scene 户外街道 actors action objects text") == "",         "_sanitize_text should drop pure echo"


def test_sanitize_text_valid_text_passes_through():
    """正常描述文本不受 schema-echo 检查影响。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    out = _sanitize_text("在户外街道上,几位行人正在路上行走")
    assert "行人" in out, f"Valid text should pass through, got {out!r}"


def test_sanitize_text_valid_json_text_passes_through():
    """JSON 含正常 text 字段 (text 不命中 echo) → 抽取后通过。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    raw = '{"scene":"户外","actors":["行人"],"action":"走","objects":["车"],"text":"在户外街道上,行人正在路上行走"}'
    out = _sanitize_text(raw)
    assert "在户外街道上" in out, f"Valid JSON.text should pass, got {out!r}"

# --------------------------------------------------------------------------- #
# Runner                                                                     #
# --------------------------------------------------------------------------- #

def main():
    import inspect
    fns = [f for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failures = []
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except AssertionError as e:
            failures.append((fn.__name__, str(e)))
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")

    print()
    if failures:
        print(f"{len(failures)}/{len(fns)} tests failed:")
        for n, msg in failures:
            print(f"  - {n}: {msg}")
        sys.exit(1)
    print(f"ALL {len(fns)} TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------- #
# Tests: _sanitize_text echo handling (v0.15+ critical fix)                 #
# --------------------------------------------------------------------------- #

def test_sanitize_text_json_inner_echo_dropped():
    """JSON-wrapped echo:model 输出 JSON,内层 ``text`` 是 echo,整体会被
    ``_sanitize_text`` 抽取后命中 schema-echo,返回空串。这是 Path 2 的
    colon-check 短路掉的关键漏洞的最终兜底。
    """
    from app.apps.video_analysis_search.service import _sanitize_text
    raw = '{"scene":"户外街道","actors":[],"action":"","objects":[],"text":"scene 户外街道 actors action objects text"}'
    assert _sanitize_text(raw) == "", \
        f"_sanitize_text should drop JSON-wrapped echo, got {_sanitize_text(raw)!r}"


def test_sanitize_text_pure_echo_dropped():
    """纯 echo 文本(无 JSON 包装)命中 schema-echo → 返回空串。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    assert _sanitize_text("scene 户外街道 actors action objects text") == "", \
        "_sanitize_text should drop pure echo"


def test_sanitize_text_valid_text_passes_through():
    """正常描述文本不受 schema-echo 检查影响。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    out = _sanitize_text("在户外街道上，几位行人正在路上行走，旁边停放着红色和白色的汽车。")
    assert "行人" in out, f"Valid text should pass through, got {out!r}"


def test_sanitize_text_valid_json_text_passes_through():
    """JSON 含正常 text 字段(text 不命中 echo)→ 抽取后通过。"""
    from app.apps.video_analysis_search.service import _sanitize_text
    raw = '{"scene":"户外","actors":["行人"],"action":"走","objects":["车"],"text":"在户外街道上,行人正在路上行走"}'
    out = _sanitize_text(raw)
    assert "在户外街道上" in out, f"Valid JSON.text should pass, got {out!r}"
