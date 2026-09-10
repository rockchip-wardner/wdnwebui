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


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------

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
