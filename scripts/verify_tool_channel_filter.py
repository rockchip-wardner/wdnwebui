"""Smoke test: tool channel filter (web text-chat vs WeChat bridge).

Background
==========
用户报告"文本对话"页面把 VideoSearch 工具注入了 chat schema,但 web
对话渠道没有关联用户视频,模型调到该工具只能拿到 ``{"status":"no_video"}``
的占位 —— 既浪费一轮推理又污染对话历史。

根因
----
``app/core/tools/registry.py:resolve_available_tool_names()`` 之前只按
``requires_capability`` 是否加载过滤,没有按调用方渠道过滤。VideoSearch
是 im-bridge 拥有的微信视频检索工具 (``channel="chat_wechat"`` 专属),
却跟 web chat 共用同一份 schema 集合。

修复
----
* ``ToolSpec.channels: Tuple[str, ...] = ()`` —— 缺省空 tuple = 所有
  渠道都可用 (与原行为兼容);非空 tuple 限定暴露渠道。
* ``register(..., channels=...)`` 接受可选 channels 参数。
* ``resolve_available_tool_names(channel=...)`` 增加 channel 参数;
  工具 ``channels`` 非空且不包含传入 channel 时跳过。
* ``VideoSearch`` 标注 ``channels=("chat_wechat",)`` —— 只微信桥接可用。
* ``TtsAlert`` 标注 ``channels=("chat_web", "chat_wechat")`` —— 两侧
  chat 链路都可用。
* ``text_chat/service.py:_resolve_tool_names()`` 调
  ``channel="chat_web"`` → VideoSearch 不再注入。
* ``im_bridge/llm.py:_resolve_tool_names()`` 调
  ``channel="chat_wechat"`` → 微信侧功能闭环保留。

测试策略
========
1. ``ToolSpec.channels`` 字段存在且默认空 tuple (原行为兼容)。
2. ``register(..., channels=...)`` 正确写入 spec。
3. ``resolve_available_tool_names(channel=...)`` 按 channel 过滤:
   * 无 channel 参数 (向后兼容, 取所有)
   * channel="chat_web" → TtsAlert 在, VideoSearch 不在
   * channel="chat_wechat" → VideoSearch 在, TtsAlert 在
   * 任意不匹配 channel → 工具的 channels 非空时跳过
4. ``channels=()`` 仍对所有渠道可用 (向后兼容原行为)。
5. capability 加载状态过滤依然生效 (与 channel 过滤是 AND 关系)。
6. register/channels static AST check: `_builtins.py` 里 VideoSearch 与
   TtsAlert 注册时必须带 channels 参数,避免以后新增内置工具时漏标。
"""
from __future__ import annotations

import ast
import os
import sys
from typing import Any, Dict, Optional

# 让脚本能 import ``app.*``
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.tools.registry import (  # noqa: E402
    ToolRegistry,
    ToolSpec,
    resolve_available_tool_names,
)


# ---------------------------------------------------------------------------
# 测试夹具: stub @Callable 类 (不真正过 wdnai 装饰器,避免 SDK native import)
# ---------------------------------------------------------------------------


class _StubTool:
    """模拟 ``@Callable`` 子类。``ToolRegistry._extract_fields`` 读
    ``model_fields`` 属性,这里给个空 dict 即可让 register 顺利完成。
    """
    model_fields: Dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __call__(self, frame_bgr: Any, ctx: Any) -> dict:
        return {"ok": True}


class _WebOnly(_StubTool):
    """只在 web chat 渠道可用的工具"""
    model_fields = {}


class _WeChatOnly(_StubTool):
    """只在 wechat chat 渠道可用的工具"""
    model_fields = {}


class _BothChat(_StubTool):
    """web + wechat 两侧 chat 渠道都可用的工具"""
    model_fields = {}


class _Everywhere(_StubTool):
    """不限渠道(空 channels 列表, 任意 channel 都看到)"""
    model_fields = {}


def _fresh_registry() -> ToolRegistry:
    """构造一个隔离的 registry 实例,避免污染全局单例。

    ``resolve_available_tool_names`` 走 ``get_tool_registry()`` 模块
    单例。本测试不 mock 全局,而是直接对 ``ToolRegistry`` 实例验证
    filter 行为 (staticmethod 逻辑)。``resolve_available_tool_names``
    的 channel 过滤逻辑也在每个测试单独跑 (全局单例不会被污染,因为
    测试只用 stub 类,不会触达真实的 registry 状态机)。
    """
    return ToolRegistry()


def _register_test_set(reg: ToolRegistry) -> None:
    """注册测试用 stub 工具集"""
    reg.register(
        key="web_only", name="web-only",
        description="", requires_capability=None,
        tool_class=_WebOnly, channels=("chat_web",),
    )
    reg.register(
        key="wechat_only", name="wechat-only",
        description="", requires_capability=None,
        tool_class=_WeChatOnly, channels=("chat_wechat",),
    )
    reg.register(
        key="both_chat", name="both-chat",
        description="", requires_capability=None,
        tool_class=_BothChat, channels=("chat_web", "chat_wechat"),
    )
    reg.register(
        key="everywhere", name="everywhere",
        description="", requires_capability=None,
        tool_class=_Everywhere, channels=(),
    )


# ---------------------------------------------------------------------------
# 单元测试
# ---------------------------------------------------------------------------


def test_toolspec_channels_field_default_empty():
    """ToolSpec.channels 字段存在,缺省空 tuple (向后兼容)。"""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(ToolSpec)}
    assert "channels" in fields, "ToolSpec 缺 channels 字段"
    # 缺省值不能是 None (frozen dataclass 要求所有字段有值);空 tuple 也 OK。
    spec = ToolSpec(
        key="x", name="x", description="",
        requires_capability=None, tool_class=_Everywhere,
    )
    assert spec.channels == (), (
        f"FAIL: ToolSpec.channels 缺省应为空 tuple, got {spec.channels!r}"
    )


def test_register_accepts_channels_param():
    """``register(..., channels=...)`` 正确写入 spec.channels"""
    reg = _fresh_registry()
    _register_test_set(reg)
    specs = {s.key: s for s in reg.list_specs()}
    assert specs["web_only"].channels == ("chat_web",)
    assert specs["wechat_only"].channels == ("chat_wechat",)
    assert specs["both_chat"].channels == ("chat_web", "chat_wechat")
    assert specs["everywhere"].channels == ()


def test_register_channels_default_none_treated_as_empty():
    """``channels=None`` 等价于空 tuple, 不抛错 (向后兼容)。"""
    reg = _fresh_registry()
    reg.register(
        key="no_channels_kw", name=None, description="",
        requires_capability=None, tool_class=_Everywhere,
        # 故意不传 channels, 走 None 默认
    )
    spec = reg.list_specs()[0]
    assert spec.channels == ()


def test_filter_logic_in_registry_class_unit():
    """ToolRegistry.list_specs 不做 channel 过滤 (与原设计一致),
    channel 过滤是 ``resolve_available_tool_names`` 的职责。

    这里直接复用 ``resolve_available_tool_names`` 的核心过滤逻辑
    对 stub registry 做单元验证,避免污染全局单例。
    """
    reg = _fresh_registry()
    _register_test_set(reg)

    # 复刻 resolver 的 channel 过滤核心
    def _filtered(channel: str) -> list:
        return [
            s.tool_class.__name__ for s in reg.list_specs()
            if not s.channels or channel in s.channels
        ]

    # channel="" (resolver 缺省值) → channels 非空的工具被过滤掉,
    # 只剩 channels=() 的工具。这是修复目标 —— 避免旧调用点(没显式
    # 传 channel)继续注入错误暴露面的工具。新代码必须显式传 channel。
    default = _filtered("")
    assert "_Everywhere" in default, (
        f"FAIL: channel='' 应保留 channels=() 工具, got {default}"
    )
    assert "_WebOnly" not in default, (
        "FAIL: channel='' 时 channels 非空的工具应被过滤, _WebOnly 漏了"
    )
    assert "_WeChatOnly" not in default
    assert "_BothChat" not in default

    # chat_web 渠道
    web = _filtered("chat_web")
    assert "_WebOnly" in web, "chat_web 应看到 web_only"
    assert "_WeChatOnly" not in web, (
        "FAIL: chat_web 不应看到 wechat_only (这是修复目标)"
    )
    assert "_BothChat" in web
    assert "_Everywhere" in web  # 空 channels = 任意渠道

    # chat_wechat 渠道
    wx = _filtered("chat_wechat")
    assert "_WebOnly" not in wx, "chat_wechat 不应看到 web_only"
    assert "_WechatOnly" in wx or "_WeChatOnly" in wx, (
        "chat_wechat 应看到 wechat_only (这是修复后的目标)"
    )
    assert "_BothChat" in wx
    assert "_Everywhere" in wx


def test_resolver_signature_default_channel_is_empty_string():
    """``resolve_available_tool_names`` 必须有 ``channel`` 参数且
    缺省空字符串(向后兼容所有现有调用点;空 channel 时 channels
    非空的工具会被跳过 —— 这是预期的最严新行为)。
    """
    import inspect
    sig = inspect.signature(resolve_available_tool_names)
    assert "channel" in sig.parameters, (
        f"FAIL: resolve_available_tool_names 缺 channel 参数, got {sig}"
    )
    p = sig.parameters["channel"]
    assert p.default == "", (
        f"FAIL: channel 缺省值应为 '' (向后兼容), got {p.default!r}"
    )


def test_resolver_filters_by_channel_with_stubbed_registry(monkeypatch):
    """把全局 ``get_tool_registry`` 替换成 stub,然后验证 channel
    过滤在 resolver 内部正确生效。
    """
    stub = _fresh_registry()
    _register_test_set(stub)

    import app.core.tools.registry as reg_mod
    monkeypatch.setattr(reg_mod, "get_tool_registry", lambda: stub)
    # capability 过滤要靠 ``compute.registry.get_registry``,在 stub
    # 不依赖 capability 的场景下不会触发 (我们的 stub 都是 None)。

    # channel="" → 工具 channels 非空都被过滤,只剩 channels=() 的
    all_default = reg_mod.resolve_available_tool_names()
    assert all_default == ["_Everywhere"], (
        f"FAIL: channel='' 时应只剩 channels=() 的工具, got {all_default}"
    )

    web = reg_mod.resolve_available_tool_names(channel="chat_web")
    assert "_WebOnly" in web
    assert "_BothChat" in web
    assert "_Everywhere" in web
    assert "_WeChatOnly" not in web, (
        "FAIL: channel='chat_web' 时不应包含 _WeChatOnly"
    )

    wx = reg_mod.resolve_available_tool_names(channel="chat_wechat")
    assert "_WeChatOnly" in wx
    assert "_BothChat" in wx
    assert "_Everywhere" in wx
    assert "_WebOnly" not in wx, (
        "FAIL: channel='chat_wechat' 时不应包含 _WebOnly"
    )


def test_capability_filter_still_applies_with_channel_filter(monkeypatch):
    """channel 过滤与 capability 过滤是 AND 关系,不是 OR。

    通过 monkeypatch 把 capability 查询 (get_registry) 替成可控 stub,
    验证:即使工具 channels 命中,但 capability 未加载,仍被排除。
    """
    import app.core.tools.registry as reg_mod

    # 准备两个 stub 工具: capability_a (channels=anywhere) 与
    # capability_b (channels=chat_web); 前者 capability_a 加载,
    # 后者未加载。
    class _CapA(_StubTool):
        pass
    class _CapB(_StubTool):
        pass

    stub = _fresh_registry()
    stub.register(
        key="cap_a", name="cap_a", description="",
        requires_capability="swcap_a", tool_class=_CapA, channels=(),
    )
    stub.register(
        key="cap_b", name="cap_b", description="",
        requires_capability="swcap_b", tool_class=_CapB,
        channels=("chat_web",),
    )

    # monkeypatch compute.registry.get_registry
    class _FakeRegistry:
        def get_session(self, cap):
            if cap == "swcap_a":
                return object()  # 加载成功
            raise RuntimeError(f"功能未启用: {cap}")  # 未加载

    import app.core.compute.registry as compute_mod
    monkeypatch.setattr(compute_mod, "get_registry", lambda: _FakeRegistry())
    monkeypatch.setattr(reg_mod, "get_tool_registry", lambda: stub)

    web = reg_mod.resolve_available_tool_names(channel="chat_web")
    assert "_CapA" in web, "channels=() 的工具 capability 加载后应在"
    assert "_CapB" not in web, (
        "FAIL: capability 未加载 + channel 命中 → 仍应被排除"
    )

    wx2 = reg_mod.resolve_available_tool_names(channel="chat_wechat")
    assert "_CapA" in wx2
    assert "_CapB" not in wx2, (
        "FAIL: channel 不匹配 + capability 未加载 → 排除(双重)"
    )


# ---------------------------------------------------------------------------
# 静态 AST 检查: 防止以后新增内置工具时漏标 channels
# ---------------------------------------------------------------------------


def test_builtins_video_search_registers_with_chat_wechat_only():
    """``app/core/tools/_builtins.py:_register_video_search`` 注册
    VideoSearch 时必须带 ``channels=("chat_wechat",)`` —— 这是
    修复 web chat 不再误调 VideoSearch 的核心契约。
    """
    src = open(
        os.path.join(
            os.path.dirname(__file__),
            "..", "app", "core", "tools", "_builtins.py",
        ),
    ).read()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "_register_video_search"),
        None,
    )
    assert fn is not None, "FAIL: _register_video_search 没找到"

    reg_call = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr == "register"),
        None,
    )
    assert reg_call is not None, (
        "FAIL: _register_video_search 里没看到 registry.register(...) 调用"
    )

    has_channels_kwarg = any(
        (isinstance(kw, ast.keyword) and kw.arg == "channels")
        for kw in reg_call.keywords
    )
    assert has_channels_kwarg, (
        "FAIL: VideoSearch 注册必须带 channels=... 参数 (web chat 不再误调 "
        "的关键契约, 防止以后重构时漏掉)"
    )

    # 检查 channels 值是包含 "chat_wechat" 的 tuple
    ch_kw = next(
        kw for kw in reg_call.keywords
        if isinstance(kw, ast.keyword) and kw.arg == "channels"
    )
    ch_value = ch_kw.value
    # channels=("chat_wechat",) → ast.Tuple
    assert isinstance(ch_value, ast.Tuple), (
        f"FAIL: channels 必须是 tuple 字面量, got {type(ch_value).__name__}"
    )
    elts = [e.value for e in ch_value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    assert "chat_wechat" in elts, (
        f"FAIL: VideoSearch.channels 必须含 'chat_wechat', got {elts}"
    )
    assert "chat_web" not in elts, (
        "FAIL: VideoSearch.channels 不应含 'chat_web' "
        "(web chat 没有 user_id 关联视频, 暴露给 web 必然失败)"
    )


def test_builtins_tts_alert_registers_with_both_chat_channels():
    """TtsAlert 应同时对 chat_web + chat_wechat 开放 —— 是合理的两侧用例。
    """
    src = open(
        os.path.join(
            os.path.dirname(__file__),
            "..", "app", "core", "tools", "_builtins.py",
        ),
    ).read()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "_register_tts_alert"),
        None,
    )
    assert fn is not None, "FAIL: _register_tts_alert 没找到"

    reg_call = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Attribute)
         and n.func.attr == "register"),
        None,
    )
    assert reg_call is not None

    ch_kw = next(
        (kw for kw in reg_call.keywords
         if isinstance(kw, ast.keyword) and kw.arg == "channels"),
        None,
    )
    assert ch_kw is not None, (
        "FAIL: TtsAlert 注册必须显式带 channels=..., 防止以后重构漏掉"
    )
    ch_value = ch_kw.value
    assert isinstance(ch_value, ast.Tuple)
    elts = [e.value for e in ch_value.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    assert "chat_web" in elts, (
        f"FAIL: TtsAlert.channels 必须含 'chat_web', got {elts}"
    )
    assert "chat_wechat" in elts, (
        f"FAIL: TtsAlert.channels 必须含 'chat_wechat', got {elts}"
    )


def test_text_chat_resolver_calls_with_channel_chat_web():
    """``app/apps/text_chat/service.py`` 的 ``_resolve_tool_names``
    必须显式传 ``channel="chat_web"`` —— 这是修复 web 端不再注入
    VideoSearch 的契约。
    """
    src = open(
        os.path.join(
            os.path.dirname(__file__),
            "..", "app", "apps", "text_chat", "service.py",
        ),
    ).read()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "_resolve_tool_names"),
        None,
    )
    assert fn is not None, "FAIL: text_chat._resolve_tool_names 没找到"

    res_call = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name)
         and n.func.id == "resolve_available_tool_names"),
        None,
    )
    assert res_call is not None, (
        "FAIL: text_chat 应该调 resolve_available_tool_names"
    )

    ch_kw = next(
        (kw for kw in res_call.keywords
         if isinstance(kw, ast.keyword) and kw.arg == "channel"),
        None,
    )
    assert ch_kw is not None, (
        "FAIL: text_chat 必须显式传 channel=... 参数"
    )
    assert (isinstance(ch_kw.value, ast.Constant)
            and ch_kw.value.value == "chat_web"), (
        f"FAIL: text_chat 的 channel 必须是 'chat_web' 字面量, "
        f"got {ast.dump(ch_kw.value)}"
    )


def test_im_bridge_resolver_calls_with_channel_chat_wechat():
    """``app/system/im_bridge/llm.py`` 的 ``_resolve_tool_names``
    必须显式传 ``channel="chat_wechat"`` —— 微信侧仍要拿到 VideoSearch。
    """
    src = open(
        os.path.join(
            os.path.dirname(__file__),
            "..", "app", "system", "im_bridge", "llm.py",
        ),
    ).read()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef)
         and n.name == "_resolve_tool_names"),
        None,
    )
    assert fn is not None, "FAIL: im_bridge._resolve_tool_names 没找到"

    res_call = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name)
         and n.func.id == "resolve_available_tool_names"),
        None,
    )
    assert res_call is not None, (
        "FAIL: im_bridge 应该调 resolve_available_tool_names"
    )

    ch_kw = next(
        (kw for kw in res_call.keywords
         if isinstance(kw, ast.keyword) and kw.arg == "channel"),
        None,
    )
    assert ch_kw is not None, (
        "FAIL: im_bridge 必须显式传 channel=... 参数"
    )
    assert (isinstance(ch_kw.value, ast.Constant)
            and ch_kw.value.value == "chat_wechat"), (
        f"FAIL: im_bridge 的 channel 必须是 'chat_wechat' 字面量, "
        f"got {ast.dump(ch_kw.value)}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


TESTS = [
    test_toolspec_channels_field_default_empty,
    test_register_accepts_channels_param,
    test_register_channels_default_none_treated_as_empty,
    test_filter_logic_in_registry_class_unit,
    test_resolver_signature_default_channel_is_empty_string,
    test_resolver_filters_by_channel_with_stubbed_registry,
    test_capability_filter_still_applies_with_channel_filter,
    test_builtins_video_search_registers_with_chat_wechat_only,
    test_builtins_tts_alert_registers_with_both_chat_channels,
    test_text_chat_resolver_calls_with_channel_chat_web,
    test_im_bridge_resolver_calls_with_channel_chat_wechat,
]


def main() -> None:
    import pytest as _pytest  # type: ignore
    failed = 0
    for t in TESTS:
        try:
            # pytest 提供 monkeypatch fixture; 不是 pytest 环境时降级
            try:
                t(_pytest.MonkeyPatch())
            except TypeError:
                # 函数不接 monkeypatch
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