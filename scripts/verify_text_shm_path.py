"""Smoke test: text SHM round-trip (v0.14 VFA SceneFrame Path 5 fix).

Simulates the bug scenario from issue report:
  - Model output JSON exceeds 255 bytes after JSON-encoding in
    ComputeReply.text IDL field.
  - With the fix: worker routes reply.text via /dev/shm path-list,
    proxy reads it back, VFA Path 2 sees the full text.

Run from repo root:
    source .venv/bin/activate
    python scripts/verify_text_shm_path.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Import the worker helpers (no NPU required — just helpers)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "services"))

from compute_worker import (  # noqa: E402
    _write_reply_payload_to_shm,
    _REPLY_SHM_DIR,
    _REPLY_SHM_PREFIX,
)

# ----------------------------------------------------------------------------
# 1. 阈值判定 (compute_worker.infer_handler: text_inline / text_path 逻辑)
# ----------------------------------------------------------------------------

THRESHOLD_BYTES = 200  # 与 result_data / structured 阈值对齐


def should_route_to_shm(text: str) -> tuple[bool, int]:
    """镜像 infer_handler 中的判定逻辑。"""
    if not isinstance(text, str) or not text:
        return False, 0
    encoded_bytes = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
    return encoded_bytes > THRESHOLD_BYTES, encoded_bytes


def _route_text_for_reply(text, *, is_remote: bool) -> tuple[Optional[str], Optional[str]]:
    """Mirror infer_handler v0.15 text routing logic.
    Returns (text_inline, text_path): large text on is_remote drops
    instead of using SHM; otherwise SHM as before.
    """
    if not isinstance(text, str) or not text:
        return None, None
    try:
        encoded_bytes = len(json.dumps(text, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return text, None
    if encoded_bytes <= THRESHOLD_BYTES:
        return text, None
    if is_remote:
        return None, None
    path = _write_reply_payload_to_shm(text.encode("utf-8"), "txt")
    return (None, path) if path else (None, None)


# ----------------------------------------------------------------------------
# 2. SHM round-trip (worker write → proxy read)
# ----------------------------------------------------------------------------


def shm_round_trip(payload: bytes, suffix: str) -> bytes:
    """模拟 worker 写 SHM → proxy 读 SHM → proxy unlink 的完整流程。"""
    path = _write_reply_payload_to_shm(payload, suffix)
    assert path is not None, "SHM write should succeed"
    assert path.startswith(_REPLY_SHM_DIR)
    assert path.startswith(os.path.join(_REPLY_SHM_DIR, _REPLY_SHM_PREFIX))
    try:
        with open(path, "rb") as f:
            read_back = f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return read_back


# ----------------------------------------------------------------------------
# 3. Bug reproduction — 模拟用户报告的 SceneFrame 截断场景
# ----------------------------------------------------------------------------

# 取用户报错中的 SceneFrame JSON (前面 ~200 字节,模拟 FastCDR 截断)
TRUNCATED_FROM_BUG = (
    '{\n  "scene": "厨房",\n  "actors": [],\n  '
    '"action": "火焰在炉灶上燃烧",\n  '
    '"objects": ["炉灶", "火焰"],\n  '
    '"text": "厨房内炉灶上的火焰正熊熊燃烧，映照出温暖的橙红'
    # 真实场景下 model 输出更长 (后续被 max_new_tokens=250 / FastCDR 255 字节切)
)
# 完整 JSON 应该是 ~330 字节 (UTF-8 中文 3 字节/字), 截断版本就是用户看到的
# 那个 "malformed reply" 字符串。
FULL_SCENE_FRAME = json.dumps(
    {
        "scene": "厨房",
        "actors": [],
        "action": "火焰在炉灶上燃烧",
        "objects": ["炉灶", "火焰"],
        "text": "厨房内灶上火焰正熊熊燃烧，映照出温暖的橙红光。"
                "灶具为家用燃气灶，火焰呈蓝色基底配橙红边缘。",
    },
    ensure_ascii=False,
    indent=2,
)


def test_threshold_short_text_kept_inline():
    """< 200 字节的 text 不走 SHM。"""
    text = "厨房内灶上火焰燃烧"  # 8 Chinese chars ≈ 27 bytes
    should_route, encoded = should_route_to_shm(text)
    assert should_route is False, f"短文本不应走 SHM ({encoded} bytes)"
    assert encoded < THRESHOLD_BYTES


def test_threshold_long_text_routed_to_shm():
    """> 200 字节的 text 走 SHM。"""
    should_route, encoded = should_route_to_shm(FULL_SCENE_FRAME)
    assert should_route is True, f"长 SceneFrame JSON 应走 SHM ({encoded} bytes)"
    assert encoded > THRESHOLD_BYTES


def test_bug_scenario_full_text_round_trips_through_shm():
    """完整 SceneFrame JSON (截断前的版本) 能 SHM round-trip, 内容字节级一致。"""
    payload = FULL_SCENE_FRAME.encode("utf-8")
    read_back = shm_round_trip(payload, "txt")
    assert read_back == payload, "SHM round-trip should be byte-exact"
    # 验证读回内容能 JSON-parse (这正是 VFA Path 2 regex 之后想要的)
    parsed = json.loads(read_back.decode("utf-8"))
    assert parsed["scene"] == "厨房"
    assert parsed["text"].startswith("厨房内")


def test_shm_filename_pattern():
    """SHM 文件路径应该用 wdnwebui_cr_<uuid>.txt 后缀, 与其他 reply SHM 一致。"""
    path = _write_reply_payload_to_shm(b"hello", "txt")
    assert path is not None
    basename = os.path.basename(path)
    assert basename.startswith(_REPLY_SHM_PREFIX)
    assert basename.endswith(".txt")
    try:
        os.unlink(path)
    except OSError:
        pass


def test_shm_write_failure_drop_text():
    """OSError 路径下 SHM write 返 None → worker drop text_inline。"""
    import unittest.mock as _mock
    with _mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
        result = _write_reply_payload_to_shm(b"hello world", "txt")
    assert result is None, "OSError 路径下应返 None"


def test_text_path_decoded_back_to_original_in_data_dict():
    """模拟 proxy 端 _decode_reply_shm: 读 SHM 内容填回 data["text"]。

    这是 worker 端 set text_inline=None + text_path=path 后的镜像测试。
    """
    original_text = FULL_SCENE_FRAME
    payload = original_text.encode("utf-8")

    # worker 端: 写 SHM, 设 data["text"]=None + data["text_path"]=path
    path = _write_reply_payload_to_shm(payload, "txt")
    assert path is not None
    data = {"text": None, "text_path": path, "level": "Normal"}

    # proxy 端: 读 SHM, 填回 data["text"], pop text_path, unlink
    with open(data["text_path"], "rb") as f:
        raw = f.read()
    data["text"] = raw.decode("utf-8")
    os.unlink(data["text_path"])
    data.pop("text_path", None)

    # 验证: VFA Path 2 拿到的 data["text"] 是完整原始文本
    assert data["text"] == original_text
    assert "text_path" not in data
    # VFA Path 2 regex 抽 JSON 应该能成功
    import re
    m = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", data["text"])
    assert m is not None, "完整文本下 regex 应能抽 JSON"
    parsed = json.loads(m.group(0))
    assert parsed["scene"] == "厨房"


# ----------------------------------------------------------------------------
# 4. v0.15 LAN peer 降级 (is_remote=True 跳过 SHM 路由)
# ----------------------------------------------------------------------------


def test_is_remote_true_drops_large_text():
    """is_remote=True + text > 200 字节 → drop (None, None), 不写 SHM。

    模拟跨机 LAN peer: worker 端 /dev/shm 不可达, 强行写无意义且
    主进程读不到。正确行为是 drop 整段 text, 主进程拿 None →
    VFA Path 1 garbage 标"本帧分析失败"占位事件, 不崩。
    """
    text_inline, text_path = _route_text_for_reply(
        FULL_SCENE_FRAME, is_remote=True)
    assert text_inline is None, "LAN peer 不应 inline (会被 FastCDR 截断)"
    assert text_path is None, "LAN peer 不应写 SHM (跨机不可达)"


def test_is_remote_false_routes_to_shm():
    """is_remote=False (默认同机) + text > 200 字节 → 走 SHM (None, path)。"""
    text_inline, text_path = _route_text_for_reply(
        FULL_SCENE_FRAME, is_remote=False)
    assert text_inline is None  # proxy 端从 SHM 读回
    assert text_path is not None
    assert text_path.startswith(_REPLY_SHM_DIR)
    try:
        os.unlink(text_path)
    except OSError:
        pass


def test_is_remote_short_text_kept_inline():
    """short text (< 200 字节) 不论 is_remote 都 inline, 不走 SHM。"""
    short = "短文本 inline 测试"
    for is_remote in (True, False):
        text_inline, text_path = _route_text_for_reply(
            short, is_remote=is_remote)
        assert text_inline == short
        assert text_path is None


def test_proxy_payload_contains_is_remote_field():
    """rpc_session_proxy.py::_sync_call 构造的 payload 必须含 is_remote key。

    这是 static 检查 (通过读源码 + parse AST), 因为构造完整 _RpcSessionProxy
    实例需要 stub 大量依赖。直接 grep 源码确保契约不被破坏。
    """
    import ast as _ast
    with open("app/services/rpc_session_proxy.py") as f:
        tree = _ast.parse(f.read())
    found_payload_build = False
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.FunctionDef):
            continue
        if node.name != "_sync_call":
            continue
        # 找 function body 里第一个 Dict literal, 确认含 "is_remote" key
        for child in _ast.walk(node):
            if isinstance(child, _ast.Dict):
                for key in child.keys:
                    if (isinstance(key, _ast.Constant)
                            and key.value == "is_remote"):
                        found_payload_build = True
                        break
        break
    assert found_payload_build, (
        "rpc_session_proxy._sync_call must build payload with "
        "'is_remote' key (LAN peer protocol contract)")


def test_worker_infer_handler_reads_is_remote_payload():
    """compute_worker.infer_handler 必须从 payload 读 is_remote。

    同样 static 检查: infer_handler 函数体里要出现
    ``payload.get(\"is_remote\"`` 或 ``payload[\"is_remote\"`` 的引用。
    """
    import ast as _ast
    with open("app/services/compute_worker.py") as f:
        tree = _ast.parse(f.read())
    found_read = False
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.FunctionDef) or node.name != "infer_handler":
            continue
        for child in _ast.walk(node):
            if (isinstance(child, _ast.Call)
                    and isinstance(child.func, _ast.Attribute)
                    and child.func.attr == "get"
                    and len(child.args) >= 1
                    and isinstance(child.args[0], _ast.Constant)
                    and child.args[0].value == "is_remote"):
                found_read = True
                break
        break
    assert found_read, (
        "compute_worker.infer_handler must call payload.get('is_remote') "
        "(LAN peer protocol contract)")



def test_scene_frame_docstring_has_total_char_constraint():
    """_build_scene_frame_schema 生成的 SceneFrame class docstring 必须含
    总字数约束 (wire-safe 防护, 配套方案A text_path guard)。

    SDK 把 @Generable class docstring 当 schema hint 注入 prompt 末尾。
    无此约束时 model 自由发挥会输出 100+ 字 scene description → 5 字段
    JSON-encoded 超 200B → 触发 /dev/shm path-list (方案A 兜底) 或直接
    FastCDR 截断 (修复前)。约束 docstring 强制 model 自约束输出 ≤ 200B,
    减少 SHM 兜底频率。
    """
    import ast as _ast
    import re as _re
    tree = _ast.parse(open("app/services/video_file_analysis.py").read())

    build_fn = next(
        (n for n in _ast.walk(tree)
         if isinstance(n, _ast.FunctionDef) and n.name == "_build_scene_frame_schema"),
        None,
    )
    assert build_fn is not None, "_build_scene_frame_schema 函数没找到"

    scene_cls = next(
        (n for n in _ast.walk(build_fn)
         if isinstance(n, _ast.ClassDef) and n.name == "SceneFrame"),
        None,
    )
    assert scene_cls is not None, "SceneFrame class 没找到"

    docstring = _ast.get_docstring(scene_cls)
    assert docstring is not None, "SceneFrame class docstring 为空"
    assert "总字数" in docstring, (
        "FAIL: SceneFrame docstring 没提总字数约束 (model 无 hint 自约束, "
        "5 字段 JSON-encoded 易超 200B 触发 wire 截断 / SHM 兜底)"
    )
    m = _re.search(r"总字数.{0,20}?(\d+)\s*字", docstring)
    assert m is not None, (
        "FAIL: SceneFrame docstring 含 总字数 但没数字约束 (例: '≤ 150 字')"
    )
    budget = int(m.group(1))
    assert budget <= 200, (
        f"FAIL: 总字数约束 {budget} 字过大, JSON-encoded 后可能超 "
        f"FastCDR 255B (按 5 字段 × max_length 估算 + 30% JSON overhead)"
    )



def test_looks_like_schema_echo_unit():
    """_looks_like_schema_echo 直接单测: 用户报告的样本必须 flag,
    合法自然语言描述必须 pass。

    配套 fix Qwen3-VL-4B 在 RKNN3 上的 schema-echo 退化 (用户 VFA
    prompt 是字段名拼接, 模型 echo 回来)。_looks_garbled 漏判
    (Latin 各 1 次 + 空格分隔 CJK), 这个 detector 补位。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from video_file_analysis import _looks_like_schema_echo

    # 用户报告样本: 必 flag
    assert _looks_like_schema_echo("scene 小区入口 actors action objects text") is True, (
        "FAIL: 用户报告的 schema-echo 样本未被检测"
    )
    # 略变体: 仍要 flag
    assert _looks_like_schema_echo("text scene objects action actors") is True
    assert _looks_like_schema_echo("note scene objects action actors text") is True

    # 合法自然语言: 必 pass (即使含 1-2 个字段名作为普通词)
    assert _looks_like_schema_echo(
        "画面里几个人在厨房做饭, 切菜煮汤。暖色调灯光。"
    ) is False, "合法描述不应误判"
    assert _looks_like_schema_echo(
        "户外街道, 行人走过。远处有车辆。注意观察。"
    ) is False
    # 单字段名作为词: pass (阈值 ≥3)
    assert _looks_like_schema_echo("scene here is a nice view") is False
    assert _looks_like_schema_echo("动作: 跑步; 对象: 球; 场景: 球场") is False


def test_split_scene_frame_result_drops_schema_echo():
    """_split_scene_frame_result Path 1: SceneFrame 实例的 text 命中
    schema-echo → 返回 (None, "") 走"本帧分析失败"占位 (与 garbled 同路径)。

    集成测试: 构造 SceneFrame(text=schema-echo 样本, 其它字段=default),
    调 _split_scene_frame_result, 断言 structured=None 且 text="",
    模拟前端 event 流程不会渲染 echo 内容。
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from video_file_analysis import (
        _get_scene_frame_cls,
        _split_scene_frame_result,
    )
    SceneFrame = _get_scene_frame_cls()

    # 用户报告样本 (整段当 text, 其它 default)
    echoed = SceneFrame(
        text="scene 小区入口 actors action objects text",
        scene="",
        actors=[],
        action="",
        objects=[],
        note=None,
    )
    structured, text = _split_scene_frame_result(echoed)
    assert structured is None, (
        f"FAIL: schema-echo 应被静默丢弃 (structured=None), "
        f"got structured={structured}"
    )
    assert text == "", f"FAIL: schema-echo 应返回空 text, got {text!r}"


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------


def test_schema_parse_failed_branch_skips_raw_text_when_text_path_set():
    """worker 端 schema_parse_failed 分支里的 raw_text inline 必须有
    text_path 守卫,否则长文本 (VFA SceneFrame 解析失败时典型 300+B)
    与 text_path 重复落 wire → FastCDR ComputeReply.text 255B 静默截断
    → 主进程 json.loads 失败报 "malformed reply: ...{...scene 小区入口...}"。

    Static AST check: 找 reply["raw_text"] = text 赋值,确认它的
    父 if 条件里出现 "text_path" not in reply Compare 节点。
    """
    import ast as _ast
    tree = _ast.parse(open("app/services/compute_worker.py").read())

    raw_text_assignment = None
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], _ast.Subscript)
                and isinstance(node.targets[0].value, _ast.Name)
                and node.targets[0].value.id == "reply"
                and isinstance(node.targets[0].slice, _ast.Constant)
                and node.targets[0].slice.value == "raw_text"):
            raw_text_assignment = node
            break
    assert raw_text_assignment is not None, (
        "FAIL: compute_worker.py 里没找到 reply['raw_text'] = ... 赋值, "
        "可能已重构,需手动确认回归测试是否仍 relevant"
    )

    def _parent(tree, target):
        for p in _ast.walk(tree):
            for c in _ast.iter_child_nodes(p):
                if c is target:
                    return p
        return None

    def _has_text_path_guard(expr):
        for sub in _ast.walk(expr):
            if not isinstance(sub, _ast.Compare):
                continue
            for op, comp in zip(sub.ops, sub.comparators):
                if (isinstance(op, _ast.NotIn)
                        and isinstance(comp, _ast.Name) and comp.id == "reply"
                        and isinstance(sub.left, _ast.Constant)
                        and sub.left.value == "text_path"):
                    return True
        return False

    cur = raw_text_assignment
    guarded = False
    for _ in range(10):
        p = _parent(tree, cur)
        if p is None:
            break
        if isinstance(p, _ast.If) and _has_text_path_guard(p.test):
            guarded = True
            break
        cur = p
    assert guarded, (
        """FAIL: reply["raw_text"] = text 没有 text_path 守卫!
VFA SceneFrame 解析失败时 reply 会同时含 text_path + raw_text
(同一份中文文本塞两次), wire 超过 FastCDR 255B 静默截断,
主进程 json.loads 失败报 'malformed reply: ...{...scene 小区入口...}'。
修法: 在 raw_text inline 的 if 里加 `"text_path" not in reply`。"""
    )



TESTS = [
    test_threshold_short_text_kept_inline,
    test_threshold_long_text_routed_to_shm,
    test_bug_scenario_full_text_round_trips_through_shm,
    test_shm_filename_pattern,
    test_shm_write_failure_drop_text,
    test_text_path_decoded_back_to_original_in_data_dict,
    test_is_remote_true_drops_large_text,
    test_is_remote_false_routes_to_shm,
    test_is_remote_short_text_kept_inline,
    test_proxy_payload_contains_is_remote_field,
    test_worker_infer_handler_reads_is_remote_payload,
    test_schema_parse_failed_branch_skips_raw_text_when_text_path_set,
    test_scene_frame_docstring_has_total_char_constraint,
    test_looks_like_schema_echo_unit,
    test_split_scene_frame_result_drops_schema_echo,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")

    print()
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
