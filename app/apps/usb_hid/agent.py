"""AI 电脑控制 — natural-language task → screenshot → VLM action → HID.

把「自然语言任务」那张占位卡变成真正能跑的东西。一轮循环是::

    截屏(HDMI-IN) → 拼 prompt(含已执行动作历史) → VLM 输出一个动作
                  → 0-1000 坐标换算成 PC 虚拟桌面像素 → 写 HID
                  → 等 PC 反应 → 下一轮

直到模型说 ``done``、用户停止、或到达步数上限。

为什么是这个形状而不是别的
==========================

**为什么不让 worker 解析 action。** AGENTS.md 设计原则 6：worker 是薄
RPC server，只做 ``{prompt, attachments, options}`` 的搬运，模型返回
的原始文本原样发回。这里严格遵守 —— ``AgentAction`` 的 schema、0-1000
→ 帧像素 → PC 像素的两次换算、"这是不是一个我能执行的键名"的判断、
以及所有 HID 副作用，全部发生在主进程本模块。好处不只是守规矩：

* worker 是 detach 进程，不能 reload。GUI 动作的坐标换算一旦要改，
  在这里改完刷新页面就生效，在 worker 里改得重启进程（LLMSession
  重启 50s+）。
* 同一份 RKNN 可以挂多个 capability，action 语义必须跟着**调用方**
  走，不能焊在模型里。

**为什么单独开一个 swcap 而不是复用 swcap_video_describe。**
``get_session`` 命中多实例时取「本机优先 + 卡号升序」的第一个
（``registry.py::get_session``），而 ``swcap_video_describe`` 同时被
qwen3-vl-4b 和 qwen3.5-vl-4b 声明 —— 电脑控制拿到哪个模型取决于卡号
排列，用户完全不可控。``swcap_computer_control`` 只挂在 qwen3.5-vl-4b
上，取到的永远是那一个（见该卡
``app/core/compute/workers/qwen3.5-vl-4b.json``）。

**为什么不调 ``sess.reset()``。** qwen3.5-vl-4b 这个实例同时挂着
``swcap_text_chat``（微信对话）和 ``swcap_computer_control``。每轮
prompt 都是自包含的（任务 + 已执行动作 + 当前截图），不依赖 worker
侧的会话历史，所以不需要 reset —— 而且不该调：那会把并发的微信
对话历史一起清掉。

坐标的三次空间
==============

模型看到的是 ``letterbox_resize_with_meta`` 产出的**方形画布**
（含灰边），输出 0–1000 归一化坐标。这跟 TargetDoc 的契约完全一致，
换算也是同一套：

1. ``unletterbox_point([x, y], meta)`` → 原始帧像素（去掉灰边）
2. `× host_screen_w / frame_w` → PC 虚拟桌面像素
3. ``DigitizerWriter`` 内部再 → HID 0..32767

第 2 步用真实帧宽高算，而不是写死 ``×1920/1000``：截图是
1920×1080，PC 假设也是 1920×1080 时两者恰好相等，但 PC 换分辨率后
写死的系数会把每一次点击都系统性偏移，而这里按实测帧尺寸走。

防跑飞
======

真实 PC 上一个 4B 模型会卡在同一个按钮上反复点。三道闸：

* 步数上限（默认 12，硬上限 60）；
* 连续 3 次**完全相同**的动作（action + 坐标 + 文本都一致）→ 判为
  卡死，停下来报错而不是继续烧 NPU；
* ``stop()`` / 页面离开时先 ``keyboard.release()`` + ``digitizer.up()``，
  保证宿主机不会看到一根卡住的键或一根停在半空的触点。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel
from wdnai import Field, Generable

LOG = logging.getLogger("apps.usb_hid.agent")

# --------------------------------------------------------------------------- #
# Capability / defaults
# --------------------------------------------------------------------------- #
#: 唯一声明了本能力的 worker manifest（qwen3.5-vl-4b）。找不到 running
#: 实例时 ``get_session`` 抛 ``RuntimeError("功能未启用: ...")``，由
#: HTTP 层按 AGENTS.md 设计原则 4 透传给前端。
CAPABILITY = "swcap_computer_control"

DEFAULT_MAX_STEPS = 12
HARD_MAX_STEPS = 60
#: 动作下发后给 PC 的重绘/响应时间。太小会在 PC 还没反应时就截下一张
#: 旧图，模型于是重复同一个动作；太大纯属浪费。1.2s 是「本地桌面应用
#: 已响应、重网络应用还在转」的折中。
DEFAULT_SETTLE_S = 1.2
#: 一次点按的按压时长（毫秒）。网页路径是"人按多久就是多久"（实测
#: 80~120 ms），这里取同量级，别比人快好几倍。
TAP_HOLD_MS = 60.0
#: HDMI-IN 抓帧等待上限。抓不到帧就不是「模型不行」，而是链路没通，
#: 必须报出来而不是把空图喂给模型。
SCREENSHOT_TIMEOUT_S = 6.0
#: 连续多少次**完全相同**的动作后判定卡死。
REPEAT_LIMIT = 3

#: PC 虚拟桌面尺寸。跟 ``api.py`` 的 ``_HOST_GRID_W/_HOST_GRID_H`` 必须
#: 一致 —— 手动透传和 AI 规划走的是同一个 HID 数字板坐标系。
HOST_SCREEN_W = 1920
HOST_SCREEN_H = 1080


# --------------------------------------------------------------------------- #
# Structured output schema
# --------------------------------------------------------------------------- #
# 两条来自 ``detector_base._build_target_doc_class`` 的血泪规矩，这里
# 原样遵守：
#
# 1. **继承 pydantic BaseModel，不继承 LMOutput。** LMOutput 带来
#    ``attachment: List[Attachment]`` 字段，而 Attachment 不是 pydantic
#    模型 —— ``model_json_schema()`` 会抛
#    ``PydanticInvalidForJsonSchema``，SDK 的 ``_safe_schema`` catch 后
#    返回空串。直接后果是模型在 prompt 里看不到任何字段说明，只能瞎猜。
# 2. **不能带 class docstring。** pydantic 会把 docstring 渲染进 schema
#    的顶层 ``description``，模型会把 ``description`` 当成数据字段，
#    把真正的字段整体包到 ``{"description": {...}}`` 里，校验失败后
#    走 raw text fallback 变成半截 JSON。
#
# 另外 `action` 用 ``Literal`` 而不是自由字符串：enum 会原样进 JSON
# schema，4B 模型在 enum 上的服从率远高于"自己挑一个动词"。
#
# **`not_found` 是必须的，不是可选的。** 实测踩过的坑：PC 上正跑着全屏
# 游戏，用户却要求"打开记事本" —— 画面里根本没有桌面、没有任务栏、
# 没有开始菜单。但 enum 只给了 click/type/key 这些动作，模型**被迫**挑
# 一个，挑出来就是 `click` + 坐标 0,0（最保守的猜测），于是表现为
# "点了左上角，什么也没发生"，而且步日志里 status=ok，完全看不出异常。
#
# 给了 `not_found` 之后，模型可以如实回答"画面里没有你要找的东西"，
# 循环据此以明确原因收工。这是 agent 的基本设计：**动作集里必须有
# "我做不到"这一项**，否则模型只会用错误的动作掩盖不确定。
#
# **`move`（只移动不点击）同样被刻意删掉**：
# `DigitizerWriter.move(tip_down=False)` 的 docstring 写着 "Win PT drivers
# silently discard this" —— 宿主根本不认悬停帧，唯一能观察到的后果是
# 白烧一步推理、模型还以为鼠标动了。网页路径也不发悬停帧（没有按键时
# `movePress` 直接 return）。触屏数字板表达不了"移动但不按"，所以这里
# 不假装能：要移动就用 `drag`。
#
# 注意这个类**故意没有 docstring**（理由见上面规矩 2），也没继承
# LMOutput（规矩 1）。
ActionName = Literal[
    "click", "double_click", "right_click", "drag",
    "type", "key", "scroll", "wait", "not_found", "done",
]


@Generable
class AgentAction(BaseModel):
    action: ActionName = Field(
        description=(
            "下一步动作，只能从这些里选一个："
            "click 单击 / double_click 双击 / right_click 右击 / "
            "drag 从 (x,y) 拖到 (x2,y2) / "
            "type 输入一段文本到 text / key 按一个组合键到 text / "
            "scroll 滚动（x 为格数，负数向下）/ wait 等 x 秒 / "
            "not_found **这张图里找不到完成这个任务需要的东西** / "
            "done 任务已完成。"
            "（没有\"只移动不点击\"的动作：触屏表达不了悬停。）"
        ),
    )
    # x/y 缺省为 **None**，不是 0.0。
    #
    # 两个坑一起堵：
    #
    # 1) 曾经给过 default=0.0，模型吐 `{"action":"click","thought":"..."}`
    #    完全合法，pydantic 静默补 0 → 点在桌面左上角，status 还是 ok，
    #    症状是"点了但没有任何反应"且步日志看不出异常。
    # 2) 但**改成必填也不行**：x/y 是 required 时，模型想回答
    #    `not_found` / `done` / `wait` 也必须编两个坐标出来，而这三个
    #    动作根本没有坐标可言 —— 它只能填 0,0。那等于把"我做不到"
    #    又逼回成一个假坐标，于是同一批坐标再次撞上退化坐标闸。
    #
    # 所以：**坐标动作（click/double_click/right_click/drag）缺坐标就在
    # _execute 里明确报错；非坐标动作随便省略。** 「没说」与「说了 0」
    # 必须分得开，这就是 default=None 的全部意义。
    x: Optional[float] = Field(
        default=None,
        description=(
            "目标点 x，0–1000 归一化，相对整张方形图（坐标空间见 prompt）。"
            "click/double_click/right_click/drag 必填；"
            "not_found/done/wait 这类没有落点的动作可以省略。"
        ),
    )
    y: Optional[float] = Field(
        default=None,
        description=(
            "目标点 y，0–1000 归一化，相对整张方形图（坐标空间见 prompt）。"
            "click/double_click/right_click/drag 必填；"
            "not_found/done/wait 这类没有落点的动作可以省略。"
        ),
    )
    x2: float = Field(
        default=0.0,
        description="drag 终点 x，0–1000 归一化；其它动作为 0。",
    )
    y2: float = Field(
        default=0.0,
        description="drag 终点 y，0–1000 归一化；其它动作为 0。",
    )
    text: str = Field(
        default="",
        description=(
            "type 动作要输入的文本；key 动作要按的键，写成 "
            "\"ctrl+s\" / \"enter\" / \"alt+tab\" 这种形式。"
        ),
    )
    thought: str = Field(
        default="",
        description="一句话说明你现在看到了什么、为什么做这个动作。",
    )


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
_PROMPT_HEADER = """\
你在通过 USB HID 远程操作一台 Windows 电脑。上面那张图就是这台电脑当前的桌面截图。

用户要求你完成的任务：
{task}

{history}\

坐标空间（必须严格遵守，否则点击会落到屏幕外面）：

{geometry}
规则：
- 一次只输出一个动作，然后由系统执行、执行完给你一张新截图。
- **thought 必填**：每一步都要先用一句话写清楚"你现在在这张图里看到了什么、
  为什么要点这个位置"。这不是可选的礼貌用语 —— 调试时它是唯一能核对
  "你的意图"和"你的落点"是否一致的依据；空 thought 会让整轮排查失去方向。
- x 和 y 是**必填**的，任何动作都不能省略，也不要填 0 充数。
- **如果你在这张图里找不到完成这个任务需要的东西**（比如要求"打开记事本"
  但图里是个全屏程序、没有桌面和任务栏），直接输出 not_found，并在 thought
  里说明你看到了什么。**不要**因为找不到就随便挑一个坐标去点 —— 那只会
  在屏幕上乱点，比不做更糟。
- 只有当任务确实完成时才输出 done。界面没反应、或需要用户介入时，
  同样用 not_found 或 thought 说明卡在哪里，不要硬猜坐标。
- 不要重复执行已经成功执行过的同一个动作；画面变了就重新判断。
"""


def _active_area_1000(frame) -> Optional[tuple[int, int, int, int]]:
    """Where is the real picture inside the frame, in 0-1000 coords?

    PC 上跑的程序不一定填满整幅：16:10 的画面被 HDMI-IN 塞进 1920×1080
    后左右各有留边。留边上点什么都没用，所以需要知道它有多宽。

    长度不是写死的 —— 每一帧重算，采样步长 16px，分辨率 ±1% 屏宽。
    问题出在**判据**上：原来写死 `mean > 12`，而实测黑边像素是字面上的
    `[16, 16, 16]`（这条 HDMI 链路有黑电平抬升，不是 0），永远过不了
    12 这道门 → `c.min()` 恒为 0 → 函数**一直返回 None**，等于这个函数
    从上线起就没工作过。

    判据改成**看平不平**，而不是看黑不黑。实测留边那几列纵向
    std = **0.0**（逐像素完全恒定），而内容列 std 在 18~57 —— 任何
    Windows 界面都不可能整列恒定。这比"够不够黑"可靠：深色壁纸亮度
    接近留边时，亮度法会整片漏检（实测内容均值 35 时四种亮度判据全部
    漏检，平度法照常检出）。

    两级：平度为主，亮度为辅（平度在"整幅都很平"时无能为力，退回亮度
    法）。两侧都有"别信"的保护 —— 平度段必须从画面边缘开始数且左右
    两段不得重叠，亮度法要求检出区确实比整幅窄。

    **结果只用于日志告警，不进 prompt。** 曾把它写成一句行动建议
    （"真实画面在 x∈[50,950]，离边界留 ≥27 余量"）发给模型，结果模型
    顺从地往里挪，把开始按钮（x=69.1，恰好不满足 ≥77 这条建议）推到了
    下一个图标（资源管理器 x=95.3）上 —— 用户实测"点开始变点文件夹"。
    **检测结果可以给人看，不能拿来指挥模型**；模型的坐标本来就包含
    留边，它瞄不准是普通定位误差，不需要额外补偿。

    抽样 (::16) 后约 7k 列 × 68 行，单帧 ~0.5 ms。
    """
    import numpy as _np

    FLAT_EPS = 2.0     # 纵向 std 低于此值算"平"（实测留边是 0.0，内容 ≥3.8）
    MIN_RUN = 2        # 至少连续 2 个采样列 = 32px 才算留边，滤掉单列噪声

    def _flat_run(std_seq, eps, min_run):
        """从序列开头数连续多少个元素"平"。返回 0 表示这一侧没有留边。"""
        i = 0
        n = std_seq.size
        while i < n and std_seq[i] < eps:
            i += 1
        return i if i >= min_run else 0

    try:
        sub = frame[::16, ::16]
        gray = sub.mean(axis=2)
        col = gray.mean(axis=0)
        row = gray.mean(axis=1)
        col_std = gray.std(axis=0)
        row_std = gray.std(axis=1)
        nc, nr = col.size, row.size

        l_flat = _flat_run(col_std, FLAT_EPS, MIN_RUN)
        r_flat = _flat_run(col_std[::-1], FLAT_EPS, MIN_RUN)
        t_flat = _flat_run(row_std, FLAT_EPS, MIN_RUN)
        b_flat = _flat_run(row_std[::-1], FLAT_EPS, MIN_RUN)
        # 左右/上下两段撞在一起 = 整幅都平（无信号、纯色、纯色壁纸），
        # 不是留边。这种情况必须返回 None 而不是给一个反过来的区间。
        col_by_flat = (l_flat + r_flat) < nc and (l_flat or r_flat)
        row_by_flat = (t_flat + b_flat) < nr and (t_flat or b_flat)

        col_thr = max(8.0, float(_np.percentile(col, 95)) * 0.35)
        row_thr = max(8.0, float(_np.percentile(row, 95)) * 0.35)
        c = _np.where(col > col_thr)[0]
        r = _np.where(row > row_thr)[0]
        col_by_lum = c.size > 0
        row_by_lum = r.size > 0

        h, w = frame.shape[:2]
        if col_by_flat:
            x0 = int(l_flat * 16 / w * 1000)
            x1 = int((nc - r_flat) * 16 / w * 1000)
        elif col_by_lum:
            x0 = int(c.min() * 16 / w * 1000)
            x1 = int((c.max() + 1) * 16 / w * 1000)
        else:
            return None
        if row_by_flat:
            y0 = int(t_flat * 16 / h * 1000)
            y1 = int((nr - b_flat) * 16 / h * 1000)
        elif row_by_lum:
            y0 = int(r.min() * 16 / h * 1000)
            y1 = int((r.max() + 1) * 16 / h * 1000)
        else:
            y0, y1 = 0, 1000

        # 必须真的比整幅窄，否则"没检出"和"整幅都是内容"会变成同一件事。
        if x0 >= x1 or y0 >= y1:
            return None
        # 真的存在黑边才报，否则纯噪声会让这段文字变成废话。
        if x0 < 5 and x1 > 995 and y0 < 5 and y1 > 995:
            return None
        return (max(0, x0), max(0, y0), min(1000, x1), min(1000, y1))
    except Exception as e:  # noqa: BLE001
        LOG.debug("agent: active-area probe failed (%s)", e)
        return None


def _geometry_block(meta, frame=None) -> str:
    """Spell out where the desktop actually sits inside the square canvas.

    为什么必须写这段：RK3588 的 vision 变体是正方形（768×768），1920×1080
    的桌面 letterbox 进去后上下各留一条灰边 —— 实测 pad_top=168、
    content 768×432，也就是**只有 y∈[219,781] 这一段是真实画面**。

    不告诉模型这件事，它就有两个同样说得通的"0-1000"：
      * 按整张方图（含灰边）理解 → 我的换算正确
      * 按"这就是一块屏幕"理解 → 中部误差最大到 **±210 px**
        （y=250 本意是 host 270，算出来是 60）
    顶部/底部因为后面会 clamp，看起来"还挺准"，把中部的错误完全盖住了，
    表现就是"有时候点得准有时候差很远"，极难归因。

    数字全部从当帧的 meta 现算，不是写死的 —— 换送检尺寸（384）后
    pad_top 会变，这里自动跟着变。

    **不要在这里写左右黑边的信息**（曾写过"真实画面在 x∈[50,950]，
    离边界留 ≥27 余量"，已删）：实测模型的坐标**本来就包含黑边**，
    它瞄开始按钮给 x=50 而按钮在 x=69，那 19 个单位是普通定位误差，
    不是"没扣黑边"。给它一条"要往里挪"的建议，它就真往里挪 ——
    而开始菜单在 x=69 恰好**不满足**"≥77"这条建议，于是被推到了
    下一个图标（资源管理器 x=95）上。用户实测：点开始按钮 → 点成了
    右侧文件夹。**检测结果可以给日志看，不能当行动建议喂给模型。**
    `_active_area_1000` 现在只用于 `_note_out_of_range` 的越界告警。
    """
    size = float(meta.size or 1)
    top = meta.pad_top / size * 1000.0
    bottom = (meta.pad_top + meta.content_h) / size * 1000.0
    left = meta.pad_left / size * 1000.0
    right = ((meta.pad_left + meta.content_w) / size) * 1000.0
    return (
        f"- 这是一张 {int(size)}×{int(size)} 的正方形图，但电脑桌面只有中间一条：\n"
        f"  x 从 {left:.0f} 到 {right:.0f}，y 从 **{top:.0f} 到 {bottom:.0f}**。\n"
        f"- 上下（y < {top:.0f} 或 y > {bottom:.0f}）是灰色留边，不是屏幕内容。\n"
        f"- 所以 y 必须落在 {top:.0f}~{bottom:.0f} 之间；y=0 或 y=1000 "
        f"点到的是屏幕外的灰边。\n"
        f"- **刻度是 0 到 1000，不是 0 到 1。** 具体锚点：x=500 是画面正"
        f"中间、x=1000 是最右边缘；y={top:.0f} 是画面最上沿、"
        f"y={bottom:.0f} 是最下沿。（如果你算出 0.5 这种小数，它指的是 500，"
        f"不是最左边 0.5%。）\n"
        f"- 找目标时按**桌面自己的相对位置**估算：任务栏在最下面一行"
        f"（y≈{bottom - (bottom - top) * 0.03:.0f}），"
        f"桌面图标在左上，标题栏在最上面（y≈{top + (bottom - top) * 0.03:.0f}）。"
    )


def _extract_thinking(raw: str, limit: int = 1200) -> str:
    """Pull the model's ``<think>…</think>`` chain out of its raw output.

    ``respond()`` 返回的是 ``_parse_output`` 之后的产物，think 块已经被
    ``strip_thinking_blocks`` 摘干净，所以调试时想看"模型为什么这么选"
    只能在原始文本里捞。开着 ``enable_thinking`` 后每个动作前面都会带
    一段，正是"意图 vs 落点"对不上的地方。

    截断到 ``limit``：推理链动辄几百上千字，全量塞进步日志会把页面
    撑爆，而排障只需要开头那几句。
    """
    if not raw:
        return ""
    import re
    m = re.search(r"<think>(.*?)(?:</think>|$)", raw, flags=re.DOTALL)
    if not m:
        return ""
    text = " ".join(m.group(1).split())
    if len(text) > limit:
        text = text[:limit] + " …（已截断）"
    return text


def _format_history(steps: list[dict[str, Any]]) -> str:
    """Render the already-executed actions into the prompt.

    这一段是循环能收敛的关键：没有它，模型每轮都看到同一张"需要点击
    这里"的截图，就会无限重复同一个 click（实测的卡死形态）。

    **失败步骤必须带上失败原因**，否则这个反馈通道等于没有：
    成功步骤有 ``summary``（落点），失败步骤的 ``summary`` 是空的 ——
    只写 ``✗ click`` 的话，模型下一轮拿到的信息和第一轮完全一样，
    它没有理由改变主意，于是原样重复同一个动作，撞上重复检测收工。
    实测就卡在这：模型连吐 4 次 ``click(0,0)``，每一步都被坐标闸拒掉，
    但它每轮看到的反馈都是"我刚点了一下"，于是永远不知道自己错在哪。
    """
    if not steps:
        return ""
    lines = ["已经执行过的动作（按时间顺序，最新的在最后）："]
    for i, s in enumerate(steps, 1):
        ok = s.get("status") == "ok"
        mark = "✓" if ok else "✗"
        if ok:
            tail = str(s.get("summary") or "")
        else:
            # ``detail`` 是 "ValueError: xxx" 的形式，那个前缀是给页面上
            # 的人看的，喂给模型没有意义，剥掉只留原因本身。
            tail = str(s.get("detail") or "").strip()
            if ":" in tail:
                tail = tail.split(":", 1)[1].strip()
            tail = tail or "这一步没有生效（没有任何动作被下发到电脑）"
        lines.append(f"{i}. [{mark}] {s.get('action', '')} {tail}".rstrip())
    lines.append("")
    lines.append(
        "标 ✗ 的那些**完全没有生效**，屏幕没有变化。"
        "不要原样重做：换一个真正不同的落点，或者用 not_found 说清"
        "你在这张图里找不到目标。"
    )
    return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------- #
# Step bookkeeping
# --------------------------------------------------------------------------- #
def _new_step(idx: int, action: dict[str, Any],
              thinking: str = "") -> dict[str, Any]:
    # x/y/x2/y2 必须落进步日志。之前只记 action/thought/text，而 thought
    # 经常是空的（模型吐最小对象 `{"action":"click","x":0,"y":0}`），
    # 于是页面上只剩一句"坐标落在左上角"—— **看不到模型到底选了哪**，
    # 无法区分"模型漏了坐标"、"模型以为那是屏幕角落"和"换算算错了"。
    def _num(key):
        v = action.get(key)
        try:
            return None if v is None else round(float(v), 1)
        except (TypeError, ValueError):
            return None

    return {
        "idx": idx,
        "ts": time.time(),
        "action": str(action.get("action", "")),
        "thought": str(action.get("thought", "") or ""),
        # think 链:模型开启 enable_thinking 后吐的推理过程。仅调试用 ——
        # 它解释"为什么选这个落点",而 thought 是模型自己写的结论。
        # 两者一起才能回答"意图和点击是否匹配"。
        "thinking": thinking or "",
        "text": str(action.get("text", "") or ""),
        "x": _num("x"),
        "y": _num("y"),
        "x2": _num("x2"),
        "y2": _num("y2"),
        "status": "pending",
        "summary": "",
        "detail": "",
    }


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class ComputerControlAgent:
    """One task loop at a time, driven from a daemon thread.

    The HTTP layer is request/response; the loop lives on a thread so
    ``POST /task/start`` returns immediately and the frontend can poll
    ``GET /task/status`` (1 Hz is plenty — a single VLM step costs
    seconds).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._task: str = ""
        self._state: str = "idle"          # idle|running|done|stopped|error
        self._error: str = ""
        self._steps: list[dict[str, Any]] = []
        self._started_at: float = 0.0
        self._finished_at: float = 0.0
        self._max_steps: int = DEFAULT_MAX_STEPS
        self._settle_s: float = DEFAULT_SETTLE_S
        # Letterbox metadata of the frame the *next* action will be
        # mapped through, written by _ask_model and read by _execute.
        # Both run on the loop thread in that order, so no lock needed —
        # but they are documented here rather than left implicit because
        # getting the pairing wrong silently shifts every click.
        self._meta: Any = None
        self._frame_size: tuple[int, int] = (HOST_SCREEN_W, HOST_SCREEN_H)
        # 上一轮推理的原始文本 / think 链,由 _ask_model 写、_loop 读。
        # 只用于调试展示,不参与任何决策。
        self._last_raw: str = ""
        self._last_thinking: str = ""

    # -- public API ---------------------------------------------------- #
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, task: str, *, max_steps: int = DEFAULT_MAX_STEPS,
              settle_s: float = DEFAULT_SETTLE_S) -> dict[str, Any]:
        """Kick off the loop on a background thread.

        Raises :class:`RuntimeError` when a task is already running —
        two loops writing the same HID endpoints would interleave into
        garbage on the host.
        """
        task = (task or "").strip()
        if not task:
            raise ValueError("任务不能为空")
        with self._lock:
            if self.running:
                raise RuntimeError("已有任务在执行中")
            try:
                steps = int(max_steps)
            except (TypeError, ValueError):
                steps = DEFAULT_MAX_STEPS
            steps = max(1, min(HARD_MAX_STEPS, steps))
            self._cancel.clear()
            self._task = task
            self._state = "running"
            self._error = ""
            self._steps = []
            self._started_at = time.time()
            self._finished_at = 0.0
            self._max_steps = steps
            self._settle_s = max(0.0, min(10.0, float(settle_s)))
            self._thread = threading.Thread(
                target=self._run, name="usb-hid-agent", daemon=True,
            )
            self._thread.start()
        LOG.info("agent: task started (max_steps=%d) %r", steps, task)
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Ask the loop to wind down. Idempotent.

        Returns immediately; the thread notices the flag at the next
        checkpoint (before screenshot / before inference / before the
        action lands). The HID release happens on the worker thread so
        the host is never left with a stuck key.
        """
        self._cancel.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._release_host()
        with self._lock:
            if self._state == "running":
                self._state = "stopped"
                self._finished_at = time.time()
        return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            elapsed = ((self._finished_at or time.time()) - self._started_at) \
                if self._started_at else 0.0
            return {
                "running": self.running,
                "state": self._state,
                "task": self._task,
                "error": self._error,
                "steps": list(self._steps),
                "step_count": len(self._steps),
                "max_steps": self._max_steps,
                "elapsed_s": round(elapsed, 1),
                "host_screen": [HOST_SCREEN_W, HOST_SCREEN_H],
                "capability": CAPABILITY,
            }

    # -- loop ---------------------------------------------------------- #
    def _run(self) -> None:
        try:
            self._loop()
        except Exception as e:  # noqa: BLE001 — never let the thread die silently
            LOG.exception("agent: loop crashed")
            with self._lock:
                self._state = "error"
                self._error = f"{type(e).__name__}: {e}"
                self._finished_at = time.time()
        finally:
            self._release_host()

    def _loop(self) -> None:
        # Fail fast on "模型没开" before touching the PC at all — the
        # whole point of the swcap is that this is a clean, explainable
        # failure, not a mystery timeout on step 1.
        session = self._get_session()

        last_sig: Optional[tuple] = None
        repeats = 0

        for i in range(1, self._max_steps + 1):
            if self._cancel.is_set():
                return self._finish("stopped")

            frame = self._grab_screenshot()
            if frame is None:
                return self._fail("截不到 HDMI-IN 画面：PC 没接上 HDMI-IN 线，或采集没起来。")
            # 全黑/纯色画面必须在**送去推理之前**停掉。
            #
            # 放在 _grab_screenshot 里是错的：那条路径可以被替换/桩掉，
            # 守卫就跟着一起消失，测试和将来任何新的取帧方式都绕得过去。
            # 放这里，"帧从哪来"和"帧能不能用"是两件事。
            #
            # 更要紧的是可观测性：不查的话报错会是"模型没有给出可执行
            # 动作"，而真实原因是 PC 没真正输出到 HDMI-IN（线插了但
            # 源没切 / 显示器没亮 / 采集到没信号的帧）。此时 (0,0) 恰恰
            # 是模型唯一能给的答案，于是每一步都点在桌面左上角 ——
            # 排查方向会完全跑偏（去调 prompt、换模型，而真正该动的是
            # HDMI 源）。
            if _is_blank_frame(frame):
                return self._fail(
                    "截到的画面是全黑/纯色的：PC 没有真正输出到 HDMI-IN。"
                    "请确认 PC 画面已复制到接 RK3588 的那个输出口、显示器已"
                    "点亮、分辨率已切到 1080p。（这时模型看不到任何东西，"
                    "只会在屏幕左上角空点。）")
            if self._cancel.is_set():
                return self._finish("stopped")

            action, why = self._ask_model(session, frame, i)
            if self._cancel.is_set():
                return self._finish("stopped")
            if action is None:
                # 记一步 error 再收工。直接 _fail 会让 step log 是空的
                # —— 用户在页面上只看到一句"没有给出可执行的动作"，
                # 既不知道模型到底吐了什么、也不知道是缺坐标还是
                # 全是乱码。踩过的坑：'{"action":"click"}'（漏坐标）
                # 和 'I cannot help' 在页面上长得一模一样。
                step = _new_step(i, {})
                step.update(status="error", action="(无动作)",
                            detail=why or "模型没有给出可执行的动作。")
                self._append_step(step)
                return self._fail(step["detail"])

            name = str(action.get("action", "") or "").strip()
            thought = str(action.get("thought", "") or "")
            thinking = self._last_thinking
            LOG.info("agent step %d: action=%s thought=%r thinking=%r",
                     i, name, thought, thinking[:200])
            # thought 空着不算错(模型有权只给动作),但**调试期**必须看得见:
            # 上一轮 9 个成功步骤的 thought 全空,导致"意图和点击是否匹配"
            # 这个问题根本无法回答 —— 看不到意图就只能猜。
            if not thought and not thinking:
                LOG.warning("agent step %d: 模型既没写 thought 也没吐 think 链,"
                            "只给了一个动作", i)

            if name == "done":
                step = _new_step(i, {"action": "done", "thought": thought},
                                 thinking)
                step.update(status="ok", summary="", detail="任务完成")
                self._append_step(step)
                return self._finish("done", task_done=thought or None)

            if name == "not_found":
                # 模型如实说"画面里没有我要找的东西"。这不是错误，是
                # 它终于有了"做不到"这个选项 —— 在这之前它只能挑个
                # click + (0,0) 把"找不到"伪装成"点了但没反应"。
                step = _new_step(i, {"action": "not_found", "thought": thought},
                                 thinking)
                reason = (
                    f"模型在这张截图里找不到完成「{self._task}」需要的东西。"
                    + (f"它说：{thought}" if thought else "")
                    + "（画面里可能不是桌面 —— 比如某个程序全屏了，"
                      "没有任务栏和开始菜单。先切回桌面再试。）"
                )
                step.update(status="error", summary="", detail=reason)
                self._append_step(step)
                return self._fail(reason)

            step = _new_step(i, action, thinking)
            self._append_step(step)          # append now so the UI shows
                                            # the step while it executes
            sig = self._action_signature(action)
            repeats = repeats + 1 if sig == last_sig else 0
            last_sig = sig
            if repeats >= REPEAT_LIMIT:
                step.update(status="error",
                            detail=f"连续 {REPEAT_LIMIT} 次输出同一个动作，判定卡死")
                return self._fail(
                    f"模型连续 {REPEAT_LIMIT} 次输出同一个动作，已中止（"
                    "可能是画面没变化，或它看不懂这个界面）。")

            try:
                self._execute(action, step)
            except Exception as e:  # noqa: BLE001
                step.update(status="error", detail=f"{type(e).__name__}: {e}")
                LOG.warning("agent step %d action=%s failed: %s",
                            i, name, e)

            if self._cancel.is_set():
                return self._finish("stopped")

            # Let the PC react before the next screenshot. Also releases
            # the GIL, so the hdmiin capture thread keeps running.
            if self._cancel.wait(self._settle_s):
                return self._finish("stopped")

        return self._fail(
            f"已达到最大步数 {self._max_steps}，任务未完成（可调高「最大执行步数」后重试）。")

    # -- model --------------------------------------------------------- #
    def _get_session(self):
        from app.core.compute.registry import get_registry
        # RuntimeError("功能未启用: swcap_computer_control") 冒泡到 HTTP
        # 层变成 502 —— 前端能直接看到该去算力配置里开哪个模型。
        return get_registry().get_session(CAPABILITY)

    def _ask_model(self, session, frame, step_idx: int):
        """One VLM turn.

        Returns ``(action_or_None, reason_or_None)``. ``reason`` is the
        user-facing explanation when the turn is unusable — it is what
        gets written into the step log, so "模型漏了坐标" and "模型吐了
        一句英文废话" must not collapse into one generic string.
        """
        from wdnai import Attachment
        from wdnai.utils import letterbox_resize_with_meta

        why: list[str] = []
        target = self._letterbox_size(frame.shape[0], frame.shape[1])
        try:
            canvas, meta = letterbox_resize_with_meta(frame, target)
        except Exception as e:  # noqa: BLE001
            LOG.error("agent: letterbox failed: %s", e)
            return None, f"第 {step_idx} 步截图缩放失败：{e}"
        # Publish the meta *now* — _execute maps this step's coordinates
        # through it, and they must come from this very canvas.
        self._meta = meta

        prompt = _PROMPT_HEADER.format(
            task=self._task,
            history=_format_history(self._steps[:-1]),
            geometry=_geometry_block(meta, frame),
        )
        try:
            # ``image_size`` 必须和脚本侧 letterbox 的边长严格一致，
            # 否则 backend 会自作主张选另一个 vision 变体，0-1000 坐标
            # 空间就和 meta 对不上（见 detector_base 里的同一段注释）。
            #
            # ``enable_thinking=True`` 打开模型的推理模式：它会先吐一段
            # ``<think>…</think>`` 再给 JSON。调试期间这是最有价值的一段
            # 信息 —— 上一轮 9 个成功步骤的 ``thought`` **全是空的**，
            # 动作序列又明显偏离任务（跳过了"开始菜单 → notepad → 回车"
            # 整段），没有推理链就只能靠猜它在干什么。
            #
            # ``on_delta`` 用来把**原始文本**收回来：``respond`` 在
            # ``_parse_output`` 里会把 think 块 strip 掉
            # （``strip_thinking_blocks``），返回的 AgentAction 里
            # 根本没有它。delta 累加后与最终文本一致（见 respond 的
            # on_delta 契约第 2 条），所以这是唯一能在主进程侧拿到
            # think 原文的办法。回调跑在 cshim 守护线程上，只做 list
            # append，不阻塞。
            stream: list[str] = []
            result = asyncio.run(session.respond(
                prompt=prompt,
                attachment=[Attachment(canvas)],
                generating=AgentAction,
                image_size=target,
                enable_thinking=True,
                on_delta=stream.append,
            ))
        except Exception as e:  # noqa: BLE001
            LOG.warning("agent step %d: respond failed: %s", step_idx, e)
            return None, f"第 {step_idx} 步模型推理失败：{e}"

        self._last_raw = "".join(stream)
        self._last_thinking = _extract_thinking(self._last_raw)
        raw = result if isinstance(result, str) else type(result).__name__
        payload = self._coerce_action(result, why)
        if payload is None:
            LOG.warning("agent step %d: unusable model output %r (%s)",
                        step_idx, raw, "; ".join(why) or "no reason")
        return payload, ("；".join(why) if payload is None else None)

    @staticmethod
    def _letterbox_size(h: int, w: int) -> int:
        """Which vision variant to target — same auto rule as the video
        cards, so we and the backend always pick the same one."""
        try:
            from app.core.compute.registry import get_registry
            from app.core.llm.detector_base import _pick_letterbox_size
            sizes: list[int] = []
            for inst in get_registry().list_with_source():
                if CAPABILITY not in (inst.get("capability_types") or []):
                    continue
                # ``list_with_source`` also emits an ``unavailable``
                # placeholder for every not-running model, and it
                # carries the same ``capability_types`` but an EMPTY
                # ``available_image_sizes``. Keep looking instead of
                # breaking on it, otherwise the first match is always
                # the placeholder and the real vision variants never
                # reach ``_pick_letterbox_size``.
                for s in (inst.get("available_image_sizes") or []):
                    if isinstance(s, bool):
                        continue
                    try:
                        sizes.append(int(s))
                    except (TypeError, ValueError):
                        continue
                if sizes:
                    break
            # No live instance (or it reports no variants) → the helper's
            # own 768 fallback, which is qwen3.5-vl-4b's largest variant.
            return _pick_letterbox_size(None, sizes, h, w)
        except Exception as e:  # noqa: BLE001
            LOG.debug("agent: letterbox size lookup failed (%s) — 768", e)
            return 768

    @staticmethod
    def _scale_0_1(d: dict[str, Any]) -> dict[str, Any]:
        """Rescale 0-1 coordinates to the 0-1000 space the prompt asks for.

        这是本次「全部点在屏幕左上角」的真凶。

        模型在这个 schema 上有相当高的概率把坐标给成 0-1（实测一整轮
        任务连出 ``0.32/0.41``、``0.25/0.48``），而 schema 里 x/y 只是
        ``float``，**校验完全通过** —— 于是 happy path 直接
        ``model_dump()`` 走人，0-1 被当成 0-1000 用：0.41 在 0-1000
        空间里就是屏幕最上面 0.4% 的位置，正好是灰边/左上角。

        换算以前**只写在恢复路径**（``_normalize_action``，即模型 JSON
        没通过校验、我们从原始文本里救回来的时候）。而这恰恰是最不
        需要救的那条路径 —— 校验通过才走到 ``model_dump()``。等于
        同一个已知坑在两条路径上只堵了一条：实测就是"坐标全都落在
        左上角"，而步日志里显示 ``(0.25, 0.41)``，看着像个正常坐标。

        所以换算必须放在**两条路径的汇合处**，而不是其中一条里。

        取舍：0-1000 空间里 x<=1.0 的合法落点（屏宽最左边 0.1%）会
        被误放大 1000 倍。这是有代价的，但比"每一次点击都落在屏幕
        左上角"划算得多 —— 而且真要贴左边缘点的动作，模型给出来的
        更可能是 0.0 而不是 0.0005。
        """
        for k in ("x", "y", "x2", "y2"):
            v = d.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if 0.0 <= float(v) <= 1.0:
                d[k] = float(v) * 1000.0
        return d

    def _coerce_action(self, result: Any, why: list[str]) -> Optional[dict[str, Any]]:
        """Normalise whatever ``respond`` returned into a plain dict.

        ``sess.respond(generating=...)`` returns either an
        ``AgentAction`` instance or — when the model's JSON fails
        schema validation — the raw ``str`` (wdnai's ``_safe_validate``
        fallback). The second case is common enough with a 4B model that
        it needs a real recovery path rather than a log line.

        ``why`` collects the rejection reason(s) so the caller can put a
        specific message in the step log instead of "没有给出可执行的
        动作" —— the whole point of the click(0,0) investigation was
        that "no coordinates" and "the model wrote prose" were
        indistinguishable from the outside.
        """
        if isinstance(result, AgentAction):
            # 也要过 0-1 换算 —— 见 _scale_0_1 的说明：校验通过不代表
            # 坐标就是 0-1000，而这条路径以前恰恰是漏掉换算的那条。
            return self._scale_0_1(result.model_dump())
        if isinstance(result, str):
            if not result.strip():
                why.append("模型返回了空内容")
            else:
                got = _parse_action_json(result, why)
                if got is not None:
                    return got
            if not why:
                why.append(f"模型输出里没有可识别的动作（原文：{result.strip()[:120]}）")
            return None
        # 兼容旧式"包裹对象"会话：有 .text 就当原始文本再试一次。
        text = getattr(result, "text", None)
        if isinstance(text, str) and text:
            got = _parse_action_json(text, why)
            if got is not None:
                return got
            why.append("模型输出里没有可识别的动作")
            return None
        why.append(f"模型返回了无法解析的类型 {type(result).__name__}")
        return None

    # -- coordinate mapping -------------------------------------------- #
    @staticmethod
    def _note_out_of_range(action: dict[str, Any], step: dict[str, Any],
                           meta: Any) -> None:
        """Flag model coordinates that fall outside the desktop band.

        灰色留边那一条(0-1000 里上下各占 22%)不是屏幕，点下去必然没
        东西。实测模型给过 y=950 —— 换算后被 clamp 到屏幕最底边，看着
        像"点了任务栏下方"，实际上它瞄的是画布底部、也就是灰边。
        不记这一笔，"点不准"就永远归因不到"模型瞄错了区域"。
        """
        if meta is None:
            return
        try:
            size = float(meta.size or 1)
            top = meta.pad_top / size * 1000.0
            bottom = (meta.pad_top + meta.content_h) / size * 1000.0
            left = meta.pad_left / size * 1000.0
            right = ((meta.pad_left + meta.content_w) / size) * 1000.0
        except Exception:  # noqa: BLE001
            return
        def _num(v):
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        x, y = _num(action.get("x")), _num(action.get("y"))
        bad = []
        if y is not None:
            if y < top:
                bad.append(f"y={y:.0f} 在灰色留边里（桌面从 {top:.0f} 开始）")
            elif y > bottom:
                bad.append(f"y={y:.0f} 在灰色留边里（桌面到 {bottom:.0f} 结束）")
        if x is not None:
            if x < left:
                bad.append(f"x={x:.0f} 在左侧留边里（桌面从 {left:.0f} 开始）")
            elif x > right:
                bad.append(f"x={x:.0f} 在右侧留边里（桌面到 {right:.0f} 结束）")
        if bad:
            note = "⚠ 模型给的坐标越界：" + "；".join(bad) + "。已 clamp 到桌面内。"
            step["warn"] = note
            LOG.warning("agent: step %s %s", step.get("idx"), note)

    @staticmethod
    def _tap(dig, hx: int, hy: int) -> None:
        """One tap, byte-identical to the hand-verified web path.

        **不要用 ``DigitizerWriter.click()``。** 它和网页那条路只差
        release 帧的 X/Y，而那一帧是宿主要按位置处理的报告：

        ```
        网页:  down  -> 010100400040   (count=1 tip=1  X=16384 Y=16384)
               up    -> 000000000000   (count=0 tip=0  X=0     Y=0    )
        AI:    down  -> 010100400040   同上
               up    -> 000000400040   (count=0 tip=0  X=16384 Y=16384)  ← 差异在这
        ```

        ``up()`` 的 docstring 解释了为什么网页那条要发 0,0：发真实坐标
        等于"抬手时又把触点报了一遍"，宿主会**按那个点**处理 ——
        实测就是同一次点击，网页打在副屏、AI 打在主屏。网页路径是
        人工验证过的那一条，所以这里逐字节对齐它，而不是"看起来等价"
        的 click()。

        ``TAP_HOLD_MS`` 取人手指尖的一次正常按压量级（网页路径是"人按
        多久就是多久"，实测 ~80-120ms）。这不是已验证的成因 —— 只是
        不再比人快 3 倍，免得短到被 Precision Touch 当成抖动丢掉。
        """
        dig.down(hx, hy, screen_w=HOST_SCREEN_W, screen_h=HOST_SCREEN_H)
        time.sleep(TAP_HOLD_MS / 1000.0)
        dig.up()

    def _to_host(self, xy_1000, meta, frame_w: int, frame_h: int):
        """0-1000 (letterboxed canvas) → PC virtual-desktop pixels."""
        from wdnai.utils import unletterbox_point
        fx, fy = unletterbox_point([float(xy_1000[0]), float(xy_1000[1])], meta)
        hx = fx * HOST_SCREEN_W / max(1, frame_w)
        hy = fy * HOST_SCREEN_H / max(1, frame_h)
        return (max(0, min(HOST_SCREEN_W - 1, int(round(hx)))),
                max(0, min(HOST_SCREEN_H - 1, int(round(hy)))))

    @staticmethod
    def _is_degenerate_origin(hx: int, hy: int) -> bool:
        """Did this coordinate land on the desktop's top-left corner?

        Why this check exists: host (0, 0) is reachable three
        indistinguishable ways — the model emitted ``0,0``; the model
        omitted x/y and the schema filled them; the model used the
        desktop's own frame and meant the gray border above the
        screen. All three clamp to the same pixel, so the step log
        shows a perfectly "ok" click that landed on the one spot on a
        Windows desktop where nothing is clickable. Symptom: "点了但
        没有任何反应".

        The repo already knows this corner is hazardous — app.js:160-163
        says outright that tip-up without X/Y "teleports the pen to
        origin, which would click whatever happens to live at (0,0)".
        So refusing to auto-click it is consistent with the rest of the
        card. A *deliberate* top-left click is impossible to distinguish
        from the degenerate cases, and losing one legitimate corner
        click is far cheaper than silently no-oping three steps.

        8 px ~= 0.4% of a 1920-wide screen: wide enough to be "the
        corner", narrow enough not to block a genuine edge click.
        """
        return hx <= 8 and hy <= 8

    @staticmethod
    def _action_signature(action: dict[str, Any]) -> tuple:
        """Identity used by the repeat detector — coordinates rounded to
        whole PC pixels so "almost the same click" still counts as the
        same click."""
        def r(v, default=0.0):
            try:
                return round(float(v))
            except (TypeError, ValueError):
                return default
        return (
            str(action.get("action", "")).strip(),
            r(action.get("x")), r(action.get("y")),
            r(action.get("x2")), r(action.get("y2")),
            str(action.get("text", "") or "").strip(),
        )

    # -- action execution ---------------------------------------------- #
    def _execute(self, action: dict[str, Any], step: dict[str, Any]) -> None:
        name = str(action.get("action", "") or "").strip().lower()
        text = str(action.get("text", "") or "")
        from app.apps.usb_hid.service import get_digitizer, get_keyboard

        if name in ("click", "double_click", "right_click"):
            # 越界坐标**照样执行**(clamp 到桌面内),但先记账。静默 clamp
            # 是"点不准"最难查的一种：模型给了 y=950,而桌面区只有
            # 219~781，clamp 之后看起来仍是一个正常落点，日志里完全
            # 看不出模型其实瞄到了灰边上。记下来才能把"模型给的"和
            # "实际落的"对上。
            self._note_out_of_range(action, step, self._meta)
            hx, hy = self._point(action, "x", "y")
            if self._is_degenerate_origin(hx, hy):
                # 证据写进提示：模型给的原始 0-1000 坐标 + 它自己的
                # thought。少了这两样，用户只会看到"点了左上角"而无法
                # 判断是模型没看图、模型找不到目标、还是换算算错了。
                thought = str(action.get("thought", "") or "").strip()
                raise ValueError(
                    f"模型给的落点是 ({action.get('x')}, {action.get('y')})"
                    "（0-1000），换算到 PC 上正好是屏幕左上角 —— Windows "
                    "桌面那一块什么都没有，点下去不会有任何反应。"
                    + (f"它这一轮写的 thought 是：{thought}" if thought
                       else "而且它**没有写 thought**（模型正常会说明"
                            "自己看到了什么），说明这一轮它大概率根本没"
                            "看图就吐了一个最小动作。")
                    + "本步已跳过。下一步请给一个真正的落点；"
                      "如果这张图里确实找不到要做的事，直接用 not_found。")
            dig = get_digitizer()
            n = 2 if name == "double_click" else 1
            for _ in range(n):
                self._tap(dig, hx, hy)
                if n > 1:
                    time.sleep(0.06)
            step.update(status="ok", summary=f"({hx}, {hy})")

        elif name == "drag":
            hx, hy = self._point(action, "x", "y")
            hx2, hy2 = self._point(action, "x2", "y2")
            if self._is_degenerate_origin(hx, hy):
                raise ValueError(
                    f"拖拽起点落在屏幕左上角（模型给的是 "
                    f"({action.get('x')}, {action.get('y')})，0-1000），已跳过。")
            if hx2 == hx and hy2 == hy:
                raise ValueError(
                    f"拖拽起点和终点相同（({hx}, {hy})），无法拖动。")
            dig = get_digitizer()
            dig.down(hx, hy, screen_w=HOST_SCREEN_W, screen_h=HOST_SCREEN_H)
            # 分段滑动而不是一步到位：Windows 的 Precision Touch 驱动
            # 对"一帧内从 A 跳到 B"的位移会当成瞬移，控件常常不响应。
            steps_n = 12
            for k in range(1, steps_n + 1):
                t = k / steps_n
                dig.move(int(hx + (hx2 - hx) * t), int(hy + (hy2 - hy) * t),
                         screen_w=HOST_SCREEN_W, screen_h=HOST_SCREEN_H,
                         tip_down=True)
                time.sleep(0.02)
            dig.up()
            step.update(status="ok", summary=f"({hx},{hy}) → ({hx2},{hy2})")

        elif name == "type":
            if not text:
                raise ValueError("type 动作没有给出 text")
            res = get_keyboard().type_text(text, hold_ms=10.0)
            step.update(status="ok",
                        summary=f"{res.get('chars_typed', 0)} 字符")

        elif name == "key":
            if not text.strip():
                raise ValueError("key 动作没有给出键名")
            keys, mods = _parse_hotkey(text)
            kb = get_keyboard()
            for kc_val in keys:
                kb.tap(kc_val, mods, hold_ms=24.0)
            step.update(status="ok", summary=f"{text.strip()}")

        elif name == "scroll":
            # 数字版滚轮：模型只给"往上还是往下 + 几格"，方向和格数
            # 映射到 KP_8 / KP_2 是所有 PC 通用滚轮语义。
            amount = _as_int(action.get("x"), 3)
            down = amount < 0
            step_n = max(1, min(20, abs(amount)))
            kb = get_keyboard()
            key = "KP_2" if down else "KP_8"
            for _ in range(step_n):
                kb.tap(_name_to_keycode(key), 0, hold_ms=8.0)
                time.sleep(0.02)
            step.update(status="ok",
                        summary=f"{'向下' if down else '向上'} {step_n} 格")

        elif name == "wait":
            delay = max(0.2, min(5.0, _as_float(action.get("x"), 1.0)))
            time.sleep(delay)
            step.update(status="ok", summary=f"{delay:.1f}s")

        else:
            raise ValueError(f"不支持的动作: {name!r}")

    def _point(self, action: dict[str, Any], xk: str, yk: str):
        """Map one (x, y) pair through the current frame's letterbox meta.

        ``_meta`` / ``_frame_size`` are written by :meth:`_ask_model`
        and :meth:`_grab_screenshot` earlier on the *same* loop thread
        (both happen before this call for every step), so reading them
        here needs no lock. Getting that pairing wrong is not a crash —
        it silently shifts every click, so the ordering is load-bearing.
        """
        meta = self._meta
        if meta is None:
            raise RuntimeError("内部错误：坐标换算缺少 letterbox 元数据")
        vx, vy = action.get(xk), action.get(yk)
        # ``action.get(k, 0.0)`` 曾经把"模型没给坐标"悄悄变成 (0,0) ——
        # 退化坐标闸就是为了接住这个才存在的。现在宁可在这里炸：
        # 「没给」和「给了 0」是两种完全不同的模型输出，必须分开看。
        if vx is None or vy is None:
            raise ValueError(
                f"{action.get('action', '?')} 动作缺少 {xk}/{yk}"
                f"（xk={vx!r} yk={vy!r}）—— 没有坐标就没有落点，已跳过。")
        fw, fh = self._frame_size
        return self._to_host((float(vx), float(vy)), meta, fw, fh)

    def _grab_screenshot(self):
        from app.core.hdmiin.capture import get_capture
        cap = get_capture()
        if not cap.running:
            if not cap.start():
                raise RuntimeError(
                    "打不开 HDMI-IN 采集设备（检查 HDMI 线 / video 组权限）")
        frame = cap.grab_bgr(timeout=SCREENSHOT_TIMEOUT_S)
        if frame is None:
            return None
        self._frame_size = (frame.shape[1], frame.shape[0])
        return frame

    # -- bookkeeping --------------------------------------------------- #
    def _append_step(self, step: dict[str, Any]) -> None:
        with self._lock:
            self._steps.append(step)

    def _finish(self, state: str, task_done: Optional[str] = None) -> None:
        with self._lock:
            self._state = state
            if state == "done" and task_done:
                self._error = ""
            self._finished_at = time.time()
        LOG.info("agent: finished state=%s steps=%d", state, len(self._steps))

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._state = "error"
            self._error = reason
            self._finished_at = time.time()
        LOG.warning("agent: failed: %s", reason)

    @staticmethod
    def _release_host() -> None:
        """Leave the PC in a clean state — no stuck key, no stuck touch.

        Best-effort by design: this runs in ``stop()`` and in the loop's
        ``finally``, and the gadget may already be gone (user ran the
        teardown script) — that must not mask the original outcome.
        """
        try:
            from app.apps.usb_hid.service import get_digitizer, get_keyboard
            get_keyboard().release()
        except Exception:  # noqa: BLE001
            pass
        try:
            from app.apps.usb_hid.service import get_digitizer
            get_digitizer().up()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _as_int(v, default=None):
    """Parse an int, or return ``default`` when the value is absent or
    not a number.

    ``default`` defaults to **None** rather than 0 on purpose: a caller
    that wants to tell "the model didn't say" from "the model said 0"
    has to be able to see the difference, and 0 is a perfectly valid
    coordinate.
    """
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _as_float(v, default=None):
    """Same contract as :func:`_as_int` — absent stays None, not 0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:      # NaN: JSON 里能出现，pydantic 能过，必须挡住
        return default
    return f


def _name_to_keycode(name: str) -> int:
    from wdnhid import keycodes as kc
    return kc.name_to_keycode(name)


def _is_blank_frame(frame) -> bool:
    """Is this screenshot (near-)uniform, i.e. carries no usable UI?

    廉价检测是故意的 —— 每一步都要在 6 MB ndarray 上跑，真实桌面帧
    的信息量远超任何一条阈值，所以余量都很大：

    * ``max <= 3``  → 纯黑/近黑，"HDMI 无信号"的典型签名
    * ``std < 2``   → **任何亮度下的纯色**（HDMI 源的 "No Signal"
      OSD 常常是纯蓝或纯白，不是黑的；只判黑会漏掉这一整类）

    ``std`` 单独用而不是再叠一个 ``mean`` 条件：纯色帧 std 恒为 0，
    而真实桌面哪怕是纯黑壁纸 + 一个小窗口，std 也在几十量级
    （20~80），离 2 差两个数量级。反过来，叠 ``mean < 6`` 会让
    纯白/纯蓝 OSD 逃过检测 —— 那正是最常见的"接了线但没信号"画面。

    抽样算而不是全图算：``[..., ::16, ::16]`` 取约 4k 个样本足够
    判断"有没有结构"，uint8 下单帧 ~1 ms。
    """
    import numpy as _np

    try:
        sub = frame[..., ::16, ::16]           # ~4k samples is plenty
        if sub.size == 0:
            return True
        if int(sub.max()) <= 3:                # pure / near black
            return True
        return float(sub.std()) < 2.0          # flat at ANY brightness
    except Exception as e:  # noqa: BLE001
        LOG.debug("agent: blank-frame probe failed (%s) — assume visible", e)
        return False


def _parse_hotkey(spec: str) -> tuple[list[int], int]:
    """``"ctrl+shift+s"`` → ``([keycode(s)], modifier_bits)``.

    ``wdnhid.keycodes.parse_modifier`` maps exactly one name to one bit,
    so the OR of all modifier-looking segments is done here. The last
    segment is the actual key; anything before it is a modifier.
    """
    from wdnhid import keycodes as kc

    parts = [p.strip() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("空的按键组合")
    mods = 0
    for seg in parts[:-1]:
        bit = kc.parse_modifier(seg)
        if not bit:
            raise ValueError(f"未知修饰键 {seg!r}")
        mods |= bit
    keys = [kc.name_to_keycode(seg) for seg in parts[-1:]]
    return keys, mods


def _parse_action_json(raw: str, why: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Recover an action dict from a raw model string.

    Three shapes get tried, in order of how often a 4B VLM produces
    them:
    1. clean JSON (optionally ```json-fenced) — the normal path;
    2. the JSON echoed inside a ``{properties: {...}}`` hallucination
       (Qwen3.5-4B does this often enough to be worth a branch — see
       the ``_try_rescue_target_doc`` note in detector_base);
    3. give up, return None.

    ``why`` (optional) collects why each attempt failed, so the step
    log can say "坐标缺失" instead of a blanket "解析失败".
    """
    import json
    import re

    why = why if why is not None else []
    text = (raw or "").strip()
    if not text:
        return None
    # Strip <think>...</think> — Qwen3.5 emits reasoning before the JSON.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if not text:
        why.append("模型只输出了思考过程，没有给出动作")
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1))
    bare = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if bare:
        candidates.append(bare.group(1))
    if not candidates:
        why.append(f"输出里没有 JSON：{text[:100]}")
        return None
    reasons: list[str] = []
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError) as e:
            reasons.append(f"JSON 解析失败（{e}）")
            continue
        got = _normalize_action(obj, reasons)
        if got is not None:
            return got
    why.append("；".join(reasons) or f"输出无法解析：{text[:100]}")
    return None


def _normalize_action(obj: Any, why: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Coerce a decoded JSON blob into a usable action dict.

    Tolerates the two wrappers seen in practice: a bare ``{action: ...}``
    and pydantic's field-wrapping ``{properties: {...}}`` echo. Also
    accepts a ``point: [x, y]`` list as an alternative to flat x/y,
    which small models reach for unprompted.
    """
    why = why if why is not None else []
    if not isinstance(obj, dict):
        why.append(f"JSON 顶层是 {type(obj).__name__} 而不是对象")
        return None
    body = obj
    if "action" not in body and isinstance(body.get("properties"), dict):
        body = body["properties"]
    if "action" not in body:
        why.append("JSON 里没有 action 字段")
        return None
    action = str(body.get("action", "") or "").strip().lower()
    known = {
        "click", "double_click", "right_click", "drag",
        "type", "key", "scroll", "wait", "not_found", "done",
    }
    # Common synonyms the model reaches for despite the enum.
    action = {
        "left_click": "click", "tap": "click", "leftclick": "click",
        "dblclick": "double_click", "doubleclick": "double_click",
        "rightclick": "right_click", "rc": "right_click",
        "type_text": "type", "input": "type", "write": "type",
        "hotkey": "key", "press": "key", "keypress": "key",
        "wheel": "scroll", "scroll_down": "scroll", "scroll_up": "scroll",
        "sleep": "wait", "notfound": "not_found", "not-found": "not_found",
        "cannot_find": "not_found", "not_visible": "not_found",
        "missing": "not_found", "unclear": "not_found", "give_up": "not_found",
        "finish": "done", "completed": "done",
        "complete": "done", "stop": "done",
    }.get(action, action)
    if action not in known:
        why.append(f"不认识的动作 {action!r}（允许：{', '.join(sorted(known))}）")
        return None

    # 坐标动作必须有 x/y。这里**不能**对缺失值补 0 —— 那正是把
    # "模型没给坐标"变成"点在桌面左上角"的那个 bug（见 AgentAction.x
    # 的注释）。``None`` 让 _coerce_action 的调用方看到"这一步不可执行"。
    #
    # 顺序要紧：``point: [x, y]`` 是小模型不写 x/y 时常见的替代写法，
    # 必须**先**把它折进 x/y 再判缺失，否则这个兜底会被 needs_xy
    # 提前毙掉（正是它最该救的那批输出）。
    raw_x, raw_y = body.get("x"), body.get("y")
    if _as_float(raw_x) is None or _as_float(raw_y) is None:
        pt = body.get("point")
        if isinstance(pt, (list, tuple)) and len(pt) == 2:
            px, py = _as_float(pt[0]), _as_float(pt[1])
            if px is not None and py is not None:
                raw_x, raw_y = px, py

    needs_xy = action in ("click", "double_click", "right_click", "drag")
    if needs_xy and (_as_float(raw_x) is None or _as_float(raw_y) is None):
        # 这就是"打开记事本点了(0,0)"那次的真实成因：模型吐了
        # {"action":"click","thought":"..."}，坐标字段整个缺失。
        # 旧 schema 的 x/y 带 default=0.0，pydantic 静默补零，点在
        # 桌面左上角 —— 那儿什么都没有，于是表现为"点了没反应"。
        why.append(
            f"{action} 动作没有坐标（x={raw_x!r} y={raw_y!r}）。"
            "这正是之前一律点在屏幕左上角的原因：旧 schema 里 x/y "
            "带 default=0.0，漏了会被静默补成 (0,0)。")
        return None

    out = {
        "action": action,
        "x": _as_float(raw_x) or 0.0,
        "y": _as_float(raw_y) or 0.0,
        "x2": _as_float(body.get("x2"), 0.0),
        "y2": _as_float(body.get("y2"), 0.0),
        "text": str(body.get("text", "") or ""),
        "thought": str(body.get("thought", "") or ""),
    }
    # 0-1 归一化兜底：小模型在这个 schema 上约 30% 概率给 0-1（与
    # detector_base 的 _normalize_bbox_to_0_1000 同一个坑）。
    #
    # 和 happy path 共用 _scale_0_1：以前这段逻辑只长在恢复路径上，
    # 而模型 JSON 一旦校验通过就走 AgentAction.model_dump()，换算被
    # 绕过 —— 实测"每次点击都落在左上角"就是从这条缝里漏出去的。
    # 两边必须同一份实现，否则改了一边忘了另一边就又复现。
    return ComputerControlAgent._scale_0_1(out)


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #
_AGENT: Optional[ComputerControlAgent] = None
_AGENT_LOCK = threading.Lock()


def get_agent() -> ComputerControlAgent:
    global _AGENT
    if _AGENT is None:
        with _AGENT_LOCK:
            if _AGENT is None:
                _AGENT = ComputerControlAgent()
    return _AGENT
