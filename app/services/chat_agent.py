"""chat agent-loop —— 文本对话链路的工具执行与多轮回喂。

``compute_worker`` 在 worker 进程内完成工具 schema 注入与 ``<tool_call>``
解析（阶段 1），但工具副作用依赖主进程资源（TtsSession / registry /
未来的设备控制等），worker 子进程无法访问。本模块在主进程侧补齐
agent-loop：

    用户输入 ─▶ chat_run(tools) ──无 tool_call──▶ 正常出口
                    │
                    └─ 有 tool_call ─▶ 主进程实例化并执行 @Callable
                                          │
                       consume_iteration ├─ True  ─▶ wrap-up 轮(tools=[]) 生成
                       （side-effect 型）│             自然语言确认 → 出口
                                         └─ False ─▶ 结果转 transcript 作为
                       （结果型）                       新一轮输入继续 loop

**工具输出契约**（新增工具只需遵守，无需改动本模块）：

* side-effect 型：``__call__`` 返回 dict 带 ``consume_iteration: True``
  （如 ``tts_alert``）——执行本身即目的，成功后由 wrap-up 轮生成确认语。
* 结果型：返回业务数据 dict、不带 ``consume_iteration`` ——结果会被
  序列化为 transcript 回喂模型，模型基于结果继续回答（可再次发起
  tool_call 形成链式调用）。
* 抛异常：视为该次调用失败，立即以失败文案出口（不再回喂）。

并发约束：调用方必须持有 ``chat_serial.get_chat_run_lock()``（与现有
text_chat / wechat_llm 相同），保证同一时刻只有一个 chat_run 在飞。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable as TypingCallable, Dict, List, Optional

_LOG = logging.getLogger("chat_agent")

_ABSOLUTE_MAX_ITERS = 200
_DEFAULT_MAX_ITERS = 3


class ToolExecutionError(RuntimeError):
    """@Callable 执行抛错时的包装，携带工具名。"""

    def __init__(self, tool_name: str, exc: BaseException) -> None:
        super().__init__(f"{tool_name}: {exc}")
        self.tool_name = tool_name


def _resolve_max_iters() -> int:
    """读 config.compute.tool_call_max_iters；0 = 无限（仍受硬上限），
    缺失 / 非法回退默认值。与 WdnaiDetector 同一套配置键。"""
    try:
        from app.services.config import get_config
        cfg = get_config().get_section("compute", {})
        if isinstance(cfg, dict):
            v = int(cfg.get("tool_call_max_iters", _DEFAULT_MAX_ITERS))
            if v == 0:
                return _ABSOLUTE_MAX_ITERS
            if v > 0:
                return min(v, _ABSOLUTE_MAX_ITERS)
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_MAX_ITERS


def _lookup_tool_class(name: str):
    from app.services.tools import get_tool_registry
    for spec in get_tool_registry().list_specs():
        if spec.tool_class.__name__ == name:
            return spec.tool_class
    return None


def _build_instance(name: str, args: Optional[dict]):
    """按 class 名从 registry 重建 @Callable 实例并校验参数。

    worker 端已经 dispatch 校验过一次；这里重建是因为实例无法跨进程
    传输——主进程拿到的是 (name, args dict)。
    """
    cls = _lookup_tool_class(name)
    if cls is None:
        raise ToolExecutionError(name, RuntimeError("未注册的工具"))
    try:
        return cls.model_validate(args or {})
    except Exception as exc:  # noqa: BLE001
        raise ToolExecutionError(name, exc) from exc


def _execute_tool(inst: Any, prompt: str) -> dict:
    """统一执行入口。返回归一化后的 dict outcome。"""
    from app.services.tools.registry import ToolContext
    ctx = ToolContext(width=0, height=0, prompt=prompt)
    out = inst(None, ctx)
    if isinstance(out, dict):
        return out
    return {"value": "" if out is None else str(out)}


def _transcript_for_result(name: str, outcome: dict) -> str:
    """结果型工具 → 回喂 transcript（prose 前缀，避免模型复读 JSON）。"""
    payload = json.dumps(outcome, ensure_ascii=False, default=str)
    return (
        f"[系统] 工具 {name} 已执行，返回结果：{payload}。"
        f"请基于该结果用自然语言回答用户的问题；如信息不足可直接说明。"
    )


def _wrapup_instruction(name: str, outcome: dict, user_text: str) -> str:
    detail = outcome.get("text") or outcome.get("detail") or ""
    base = (
        f"[系统] 工具 {name} 已成功执行"
        + (f"（{detail}）" if detail else "")
        + "。请用一句简短自然的中文向用户确认操作已完成，不要提及工具名、"
          "JSON 或任何技术细节。"
    )
    if user_text.strip():
        base += f"用户最初的请求是：「{user_text.strip()}」"
    return base


def run_chat_with_tools(
    session,
    history: List[dict],
    text: str,
    *,
    context_limit: int,
    max_new_tokens: int = 2048,
    tool_names: List[str],
    on_delta: Optional[TypingCallable[[str], None]] = None,
    stop_event=None,
    max_iters: Optional[int] = None,
) -> dict:
    """带工具循环的一轮对话。

    参数与 ``RpcSessionProxy.chat_run`` 对齐，额外承担工具执行与
    多轮回喂。返回 dict::

        {
            "answer", "thinking",       # 最终轮输出（给用户看的）
            "steps": [...],             # 每次工具执行记录（调试 / 前端徽章）
            "history_append": [...],    # 除首条 user 外的全部新增历史条目，
                                        # 调用方 extend 即可
            "tool_call", "tool_call_args",   # 最后一次工具调用（兼容旧帧）
            "elapsed_sec", "ts", "prompt_tokens",
            "interrupted": bool,        # stop_event 触发中断
            "error": "",                # 非 fatal 出口为空；工具异常时有值
        }
    """
    t0 = time.perf_counter()
    iters = max_iters if max_iters and max_iters > 0 else _resolve_max_iters()
    iters = min(iters, _ABSOLUTE_MAX_ITERS)

    work_history = list(history)
    current_text = text
    first_user_text = text
    steps: List[dict] = []
    history_append: List[dict] = []
    last_tool_name: Optional[str] = None
    last_tool_args: Optional[dict] = None
    final_thinking = ""
    final_answer = ""
    prompt_tokens = 0
    interrupted = False
    error_msg = ""

    for i in range(1, iters + 1):
        if stop_event is not None and stop_event.is_set():
            interrupted = True
            break
        try:
            result = session.chat_run(
                history=work_history,
                text=current_text,
                context_limit=context_limit,
                max_new_tokens=max_new_tokens,
                on_delta=on_delta,
                stop_event=stop_event,
                tool_names=tool_names if i == 1 else [],
            )
        except Exception as exc:  # noqa: BLE001
            if stop_event is not None and stop_event.is_set():
                interrupted = True
                break
            error_msg = str(exc)
            _LOG.warning("chat_run failed at iter %d: %s", i, exc)
            break

        prompt_tokens = int(result.get("prompt_tokens") or 0)
        final_thinking = result.get("thinking") or ""
        final_answer = (result.get("answer") or "").strip()

        tool_name = result.get("tool_call")
        if not tool_name:
            break  # 正常出口：模型直接给出回答

        # ---- 工具轮 ---------------------------------------------------
        last_tool_name = tool_name
        last_tool_args = result.get("tool_call_args")
        step: Dict[str, Any] = {
            "iter": i, "tool_call": tool_name, "args": last_tool_args,
        }
        try:
            inst = _build_instance(tool_name, last_tool_args)
            outcome = _execute_tool(inst, current_text)
        except Exception as exc:  # noqa: BLE001
            step["error"] = str(exc)
            steps.append(step)
            error_msg = f"工具 {tool_name} 执行失败：{exc}"
            _LOG.warning("tool %s failed: %s", tool_name, exc)
            break

        consume = bool(outcome.get("consume_iteration"))
        step["outcome"] = {k: v for k, v in outcome.items()
                          if k != "consume_iteration"}
        steps.append(step)
        _LOG.info("agent iter %d executed %s consume=%s", i, tool_name, consume)

        # assistant 占位：语义化 content，让下一轮 prompt 里模型能看到
        # 自己"已调用过什么、结果如何"，避免空回合诱导复读（修 B3）。
        placeholder_content = (
            f"[已调用工具 {tool_name}，参数 "
            f"{json.dumps(last_tool_args or {}, ensure_ascii=False)}]"
        )
        history_append.append({
            "role": "assistant",
            "content": placeholder_content,
            "thinking": final_thinking,
            "ts": time.time(),
            "tool_call": tool_name,
            "_placeholder": True,
        })
        work_history.extend(history_append[-1:])

        # 系统指令作为下一轮 current_text（ChatML 尾部 user 段）；
        # 同时记入 history_append 供调用方持久化——但不进 work_history，
        # 否则同一文本会在 prompt 里出现两次。
        next_input = (
            _wrapup_instruction(tool_name, outcome, first_user_text)
            if consume
            else _transcript_for_result(tool_name, outcome)
        )
        history_append.append({
            "role": "user", "content": next_input, "ts": time.time(),
            "_system_note": True,
        })
        current_text = next_input

        if consume:
            # wrap-up：tools=[] 跑最后一轮生成自然语言确认后退出
            try:
                wres = session.chat_run(
                    history=work_history, text=current_text,
                    context_limit=context_limit,
                    max_new_tokens=max_new_tokens,
                    on_delta=on_delta, stop_event=stop_event,
                    tool_names=[],
                )
                prompt_tokens = int(wres.get("prompt_tokens") or 0)
                final_thinking = wres.get("thinking") or ""
                final_answer = (wres.get("answer") or "").strip()
                history_append.append({
                    "role": "assistant", "content": final_answer,
                    "thinking": final_thinking, "ts": time.time(),
                })
            except Exception as exc:  # noqa: BLE001
                if stop_event is not None and stop_event.is_set():
                    interrupted = True
                    break
                _LOG.warning("wrap-up run failed: %s", exc)
                final_answer = f"好的，已完成：{_summarize_outcome(outcome)}"
                history_append.append({
                    "role": "assistant", "content": final_answer,
                    "thinking": "", "ts": time.time(),
                })
            break
        # 结果型：下一轮 loop 继续（current_text 已置空，输入走 history 尾部）

    else:
        # iters 用尽仍未收敛
        error_msg = error_msg or (
            f"连续 {iters} 次工具调用未能完成请求，已停止。")
        final_answer = final_answer or "抱歉，我暂时无法完成这个请求。"

    # 工具轮发生过时，确保 history_append 以最终 assistant 回答收尾：
    # wrap-up 路径已在循环内 append；结果型路径正常出口后此处补齐，
    # 否则下轮 prompt 会丢失本轮回答。
    if (history_append and not interrupted and final_answer):
        tail = history_append[-1]
        if not (tail.get("role") == "assistant"
                and not tail.get("_placeholder")):
            history_append.append({
                "role": "assistant", "content": final_answer,
                "thinking": final_thinking, "ts": time.time(),
            })

    if interrupted and not final_answer:
        final_answer = ""
    if error_msg and not final_answer:
        final_answer = f"抱歉，{error_msg}"

    return {
        "answer": final_answer,
        "thinking": final_thinking,
        "steps": steps,
        "history_append": history_append,
        "tool_call": last_tool_name,
        "tool_call_args": last_tool_args,
        "elapsed_sec": time.perf_counter() - t0,
        "ts": time.time(),
        "prompt_tokens": prompt_tokens,
        "interrupted": interrupted,
        "error": error_msg,
    }


def _summarize_outcome(outcome: dict) -> str:
    for key in ("text", "detail", "message", "value"):
        v = outcome.get(key)
        if v:
            return str(v)
    return "操作已完成"
