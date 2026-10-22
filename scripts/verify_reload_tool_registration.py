"""Smoke test: uvicorn reload 后工具自动注册(修复 "（模型未输出有效内容）")。

背景
====
用户报告: 应用对接(im-bridge)对视频提问时返"（模型未输出有效内容）",
确认是当前 channel 隔离改动引起的。

根因(实测)
==========
* uvicorn reload 时重新 import 改动过的模块 (`app/core/tools/_builtins.py`,
  `app/core/tools/registry.py`),module-level 全局变量被重置:
  - ``_REGISTERED = False`` (重置)
  - ``_registry = None`` (重置)
* reload **不会** 自动重新调 ``register_all()`` (它只在 startup 调一次)
* 旧代码: ``get_tool_registry()`` 创建新空 ToolRegistry 后返回,
  后续 ``resolve_available_tool_names`` 看到空 registry → 返 ``[]``
* chat_run 带 ``tool_names=[]`` → 模型没有 tool schema → 返空 answer
* ``app/system/im_bridge/llm.py:328`` 兜底返 "（模型未输出有效内容）"

修复
====
两层防御:
1. ``register_builtin_tools`` 幂等检查改为:既看 ``_REGISTERED`` 全局 flag,
   也看该 registry 是否有任一 builtin key(uvicorn reload 后 _REGISTERED
   重置为 False 但 registry 仍是新空 → 走第二条路重新注册)。
2. ``get_tool_registry`` 在创建新 registry 时立即调一次
   ``register_builtin_tools`` —— 这是 reload 修复的关键。任何调用方
   (chat-agent / worker / compute/registry.py 算力配置页 / detector / 等)
   都自动受益于内置工具的存在,无需依赖 ``register_all`` 被重新调过。

为什么这是 ROOT CAUSE 修复(不是兜底)
=====================================
旧代码: reload 后空 registry → 模型无工具 → 空 answer → fallback
新代码: reload 后空 registry → ``get_tool_registry`` 自动触发 register
        → registry 有工具 → resolver 返正确 list → 模型有 schema → 正常 wrap-up

测试策略
========
1. 模拟 reload 后状态 (``_REGISTERED = False``, ``_registry = None``)
2. 验证 ``get_tool_registry()`` 第一次返的 registry **自带 builtins**
   (这是修复的契约 —— 此之前返空)
3. 验证 ``resolve_available_tool_names(channel=...)`` 在 reload 后也能返
   正确的工具名 (这是用户可见的行为)
4. 验证 chat-agent 路径走完能拿到 tool schema (chat-agent._lookup_spec
   能找到 VideoSearch/TtsAlert 类)
5. 验证幂等: 多次 ``get_tool_registry()`` 不重复 register
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# 单元: reload 后 get_tool_registry 自带 builtins
# ---------------------------------------------------------------------------


def test_post_reload_registry_auto_populates_builtins():
    """模拟 uvicorn reload 后状态:``_REGISTERED = False`` + ``_registry``
    是新空对象。``get_tool_registry()`` 必须自动调用
    ``register_builtin_tools``,否则 chat 链路拿到空工具列表。"""
    import importlib
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod

    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    assert b_mod._REGISTERED is False, (
        "reload 后 _REGISTERED 应被重置为 False"
    )
    assert reg_mod._registry is None, (
        "reload 后 _registry 应被重置为 None"
    )

    reg = reg_mod.get_tool_registry()

    specs = reg.list_specs()
    assert len(specs) >= 2, (
        f"FAIL: reload 后 get_tool_registry() 返的 registry 应自带 builtins, "
        f"但只有 {len(specs)} 个 spec: {[s.key for s in specs]}"
    )
    keys = {s.key for s in specs}
    assert "video_search" in keys
    assert "tts_alert" in keys

    by_key = {s.key: s for s in specs}
    assert by_key["video_search"].channels == ("chat_wechat",)
    assert by_key["tts_alert"].channels == ("chat_web", "chat_wechat")


def test_post_reload_resolver_returns_tools_for_chat_wechat():
    """reload 后 im-bridge 路径:``resolve_available_tool_names(
    channel="chat_wechat")`` 必须返 ``['VideoSearch', 'TtsAlert']``(否则
    模型拿不到 schema → 空 answer → fallback)。"""
    import importlib
    import unittest.mock as _mock
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod

    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    class _MockReg:
        def get_session(self, cap): return object()
    import app.core.compute.registry as comp_mod
    with _mock.patch.object(comp_mod, "get_registry", lambda: _MockReg()):
        result = reg_mod.resolve_available_tool_names(channel="chat_wechat")
    assert "VideoSearch" in result, (
        f"FAIL: chat_wechat 应包含 VideoSearch, got {result}"
    )
    assert "TtsAlert" in result, (
        f"FAIL: chat_wechat 应包含 TtsAlert, got {result}"
    )


def test_post_reload_resolver_returns_only_tts_for_chat_web():
    """reload 后 web 文本对话路径:VideoSearch 不该出现(空 user_id 必败),
    只返 TtsAlert。"""
    import importlib
    import unittest.mock as _mock
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod

    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    class _MockReg:
        def get_session(self, cap): return object()
    import app.core.compute.registry as comp_mod
    with _mock.patch.object(comp_mod, "get_registry", lambda: _MockReg()):
        result = reg_mod.resolve_available_tool_names(channel="chat_web")
    assert "VideoSearch" not in result, (
        f"FAIL: chat_web 不应包含 VideoSearch, got {result}"
    )
    assert "TtsAlert" in result, (
        f"FAIL: chat_web 应包含 TtsAlert, got {result}"
    )


def test_post_reload_chat_agent_can_lookup_video_search_class():
    """reload 后 chat-agent 实际场景:用 class.name 查 registry 能找到
    VideoSearch(否则 ``_build_instance`` 抛 ToolExecutionError)。"""
    import importlib
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod
    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    name = "VideoSearch"
    found = None
    for spec in reg_mod.get_tool_registry().list_specs():
        if spec.tool_class.__name__ == name:
            found = spec
            break
    assert found is not None
    assert found.requires_capability == "swcap_vision_embedding"


def test_reload_does_not_double_register():
    """幂等: 多次 ``get_tool_registry()`` 不会重复 register 同一个 tool。"""
    import importlib
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod
    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    reg1 = reg_mod.get_tool_registry()
    n1 = len(reg1.list_specs())

    reg2 = reg_mod.get_tool_registry()
    n2 = len(reg2.list_specs())

    assert reg1 is reg2, "singleton 应返同一对象"
    assert n1 == n2


def test_register_builtin_tools_idempotent_with_existing_registry():
    """幂等: ``register_builtin_tools`` 对已含工具的 registry 应 no-op。"""
    import importlib
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod
    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    reg = reg_mod.get_tool_registry()
    n_before = len(reg.list_specs())

    b_mod.register_builtin_tools(reg)
    n_after = len(reg.list_specs())

    assert n_before == n_after


def test_register_builtin_tools_repopulates_empty_registry():
    """核心修复:``register_builtin_tools`` 对**空** registry 应重新注册
    builtin(即使 ``_REGISTERED = True``,只要 registry 是新的空对象
    就重新走一遍)—— 这是 reload bug 的兜底层。"""
    import importlib
    import app.core.tools._builtins as b_mod
    import app.core.tools.registry as reg_mod
    importlib.reload(b_mod)
    importlib.reload(reg_mod)

    reg = reg_mod.get_tool_registry()
    with reg._lock:
        reg._tools.clear()
    b_mod._REGISTERED = True
    assert not reg.list_specs()

    b_mod.register_builtin_tools(reg)

    assert reg.list_specs(), (
        "FAIL: 空 registry + _REGISTERED=True 时应重新注册"
    )


def test_get_tool_registry_auto_registers_builtins():
    """AST 契约:``get_tool_registry()`` 函数体内必须存在
    ``register_builtin_tools`` 调用 —— 防止以后 refactor 时误删。"""
    import ast
    import app.core.tools.registry as reg_mod
    src = open(reg_mod.__file__).read()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "get_tool_registry"),
        None,
    )
    assert fn is not None

    has_register_call = False
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "register_builtin_tools"):
            has_register_call = True
            break
    assert has_register_call, (
        "FAIL: get_tool_registry() 必须调用 register_builtin_tools "
        "(reload bug 修复的核心契约)"
    )


def test_register_builtin_tools_checks_registry_state_not_just_flag():
    """AST 契约:``register_builtin_tools`` 不能只看 ``_REGISTERED`` 全局
    flag,必须额外检查 registry 是否已有 builtin。"""
    import ast
    import app.core.tools._builtins as b_mod
    src = open(b_mod.__file__).read()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "register_builtin_tools"),
        None,
    )
    assert fn is not None

    has_list_specs = False
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "list_specs"):
            has_list_specs = True
            break
    assert has_list_specs, (
        "FAIL: register_builtin_tools 必须调 registry.list_specs() "
        "检查 registry 是否已含 builtin key"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


TESTS = [
    test_post_reload_registry_auto_populates_builtins,
    test_post_reload_resolver_returns_tools_for_chat_wechat,
    test_post_reload_resolver_returns_only_tts_for_chat_web,
    test_post_reload_chat_agent_can_lookup_video_search_class,
    test_reload_does_not_double_register,
    test_register_builtin_tools_idempotent_with_existing_registry,
    test_register_builtin_tools_repopulates_empty_registry,
    test_get_tool_registry_auto_registers_builtins,
    test_register_builtin_tools_checks_registry_state_not_just_flag,
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
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")

    print()
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()