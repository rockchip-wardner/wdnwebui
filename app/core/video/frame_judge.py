"""帧判据 —— 决定"这一帧要不要送去跑推理"，并把分数暴露给 UI。

**这是四张视频卡（vas / vd / vtd / vtl）共用的唯一判据入口。** 判据算法
全部在 wdnwebui 侧：直接调本文件里的 ``gscore()``，**完全不走任何
``wdnav.scene.*`` 接口**（业务算法不污染通用 SDK；SDK 保持通用）。
缩略图由 video/session.py 在 ``cap.read0(w, h, fmt='NV12')`` 阶段就
缩到判据要的 long_side 尺寸并取 Y 平面，wdnwebui 侧不再二次缩放。

* **模式**。``manual`` = 手动跳帧（固定每 ``every_n`` 帧送一次，不做差分、
  零额外开销）；其余取值是 :data:`EXPOSED_METRICS` 里的度量名。**当前
  唯一可用的自动度量是 ``gscore``**：仿射归一化 + 块级 p99 残差，对"挡
  镜头""曝光""相机被动"这类大范围变化（conc 会在 1.7~1.8 失明，gscore
  仍能 60~110 分）保留高分离度。
* **阈值与兜底间隔**。自动模式下分数 > ``threshold`` 判为"变了"；距上次
  判定"变了"满 ``every_n`` 帧时，若分数**至少到阈值的一半**也判为"变了"——
  兜底不是无条件送检，静止画面因此彻底安静（详见 :meth:`FrameJudge.observe`）。
* **分数留痕**。把每帧分数写进环形缓冲，前端据此画实时分数曲线、右上角
  浮框据此显示"这一帧变了没有"。

为什么自己写决策而不用 ``FrameDiffer.should_sample()``
--------------------------------------------------------
``should_sample`` 只返回 bool，**不吐分数**，而曲线和浮框都需要逐帧分数。
硬调两次（一次 ``should_sample`` 拿判定、一次 ``score()`` 拿分数）会让
RGA 缩放每帧跑两遍 —— 实测 960x540→160x90 约 1ms/帧，在每帧热路径上是
白扔的开销。所以这里**完全自管参照帧**（存 ``_last_luma``），自己算
``gscore(last, curr)``。缩放早就在 session.py 的 read0 阶段由 wdnav RGA
做掉了 —— 整条管线没有任何 wdnav 注册表 / scene 耦合，wdnav 保持通用。

**兜底判定有意与 SDK 不一致**：SDK 是 ``gap >= max_gap`` 无条件送检，
本模块额外要求分数至少到阈值的一半。理由见 :meth:`FrameJudge.observe`
里那段注释 —— 静止画面送出去的结果与上一次逐位相同，刷新的是同一个
答案，不存在"漏检"，纯徒增功耗。**除这一条外**，其余判定规则（首帧必送、
超阈值触发、异常按送检处理、参照帧只在送检时推进）都保持一致。

线程模型：``observe`` 只在 decode 单线程调用；``snapshot`` 由 HTTP / WS
线程调用，故留一把短临界区锁 —— 判帧器内部无锁，避免与调用方已有的锁
顺序死锁。
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from typing import Deque, Dict, List, Optional

import numpy as np
# 故意**不** import wdnav.scene：业务算法不污染通用 SDK，判据自管缩略图
# —— video/session.py 已经在 read0(w, h, fmt='Y') 阶段把帧缩到目标尺寸并取
# Y 平面，本模块只接受"已经缩好的 2D 灰度 ndarray"，不二次处理。

_LOG = logging.getLogger("frame_judge")

# 手动跳帧：固定间隔，不做差分。
MODE_MANUAL = "manual"
# 判帧缩略图长边。固定 160 —— 与线上各卡一致；分数口径必须与线上相同，
# 否则这里调出来的阈值搬过去就是失效的。
DEFAULT_LONG_SIDE = 160

# 默认帧间隔。与三张视频卡历史上 detector 的 every_n 默认值一致，所以把
# 节流从 detector 移到判据器**不改变**开箱行为（同样每 30 帧送一次）。
DEFAULT_EVERY_N = 30

# 解码线程**多久算一次分**（帧）。不是送检节流 —— 那是 every_n 的事。
# 必须有间隔：算分前要先 RGA 缩一张 long_side 缩略图，而 RGA 是全机共享的
# 串行队列（32 线程并发时吞吐恒在 ~1050 次/秒，加线程只加排队）。每帧多打
# 一次 RGA，32 路总吞吐从 774 掉到 265 帧/秒。取 25 是因为 25fps 源上正好
# ~1s 一次，与 WS 状态 1Hz 推送同频 —— 角标与曲线本来每秒才刷一次。
DEFAULT_SAMPLE_EVERY = 25

# 分数环形缓冲长度。200 点够画一条有起伏的曲线，又不至于让每帧快照变大。
HISTORY_MAX = 200

# 分数为 0 的点（首帧、循环重播后第一帧）不进曲线 —— 否则曲线左端会出现一
# 段假的"平静期"，让人误以为前面那些帧真的没变化。
SCORE_EPS = 1e-6


# --------------------------------------------------------------------------- #
# conc 度量 —— 本项目默认（也是唯一暴露给用户）的判帧算法
# --------------------------------------------------------------------------- #
#
#: 判据的实现仅 gscore（``app/core/video/frame_judge.gscore``），集中在下面
#: 一处。conc 在 v0.21 之前是主力，但被实测证伪：它是比值，"挡镜头""相机
#: 被动""整屏改变"这类**整幅一起变**的场景下，分子分母同步抬升、比值回到 1，
#: 分离度只有 0.1~0.2 倍底噪（比底噪还低，阈值怎么调都测不出）。gscore 改用
#: 仿射归一化 + 块级 p99 残差，把整幅光照变化先吃掉再算结构残差，**没有
#: 这个归一化陷阱**。详见 ``gscore()`` 的 docstring。


# --------------------------------------------------------------------------- #
# gscore 度量 —— v0.21 起默认（conc 保留供对比）
# --------------------------------------------------------------------------- #
#
#: 统计块边长（缩略图像素）。与 :data:`_CONC_BLOCK` 同值，两者的分数**刻度
#: 完全不同**，阈值不能互搬（见 :data:`DEFAULT_THRESHOLD_BY_MODE`）。
_GSCORE_BLOCK = 8

#: 残差的分位数。99 而不是 100（max）或 50（mean）—— 见下方选型实测。
_GSCORE_PCTL = 99.0

#: 参照帧几乎没有纹理时（方差过低）求回归斜率会放大噪声，直接跳过归一化。
_GSCORE_MIN_VAR = 1.0


def gscore(a: np.ndarray, b: np.ndarray,
           bs: int = _GSCORE_BLOCK,
           pctl: float = _GSCORE_PCTL) -> float:
    """仿射归一化后的结构残差（块级 p99 分位）。**越大越不像。**

    两步：先把两帧的块均值做一次最小二乘仿射拟合（``b ≈ g·a + c``），把**整幅
    亮度/对比度变化**整个吃掉；再看剩下的残差，取 99 分位。

    为什么是"归一化后再看残差"
    --------------------------
    监控场景里最常见的干扰不是噪声，而是**整幅一起变**：自动曝光、灯开关、
    昼夜红外切换、相机被动微抖。它们让全图平均绝对差暴涨，于是朴素的帧差
    （``absdiff``）在这些场景能报出 1000 倍底噪，阈值根本没法定。

    仿射拟合正好吃掉这一整类：整幅亮度平移落在 ``c`` 上，整幅对比度变化落在
    ``g`` 上，两者都被吸收掉，残差里只剩"结构变了"。

    为什么取 99 分位而不是 mean 或 max
    ---------------------------------
    同一条真实监控流（``rtsp://…192.168.169.25``，900 帧，180x320 缩略图）上
    对三种聚合实测，数字 = 场景响应 ÷ 该算法自己的底噪 p95：

    ==================  =========  ==========  ========
    聚合                几何平均    最差场景     小目标
    ==================  =========  ==========  ========
    ``mean``            46.4x      2.7x        33.2x
    ``max``              9.5x      1.6x        12.1x
    **``p99``**         **25.6x**  **2.8x**    **41.1x**
    ==================  =========  ==========  ========

    * ``mean``：小目标被整幅摊薄（2.3% 面积只贡献 2.3% 的残差），闯入信号
      偏弱。
    * ``max``：被单个坏像素 / 编码残差尖刺主导，底噪 p95 从 1.58 抬到 6.15，
      而且**小目标响应反而更低**（块级 max 容易被边界处的大块差吃掉）。
    * ``p99``：压掉尖刺又保住集中度，是三者的拐点。

    与 conc 的关系（这是选 gscore 的全部理由）
    ------------------------------------------
    conc 是**比值**，分子分母在大范围变化时同步抬升，比值回到 1，于是"挡
    镜头""相机被动""下半屏改变"全部结构性失明。同一条真实监控流上：

    ==================  ===============  ===============
    场景                 conc 绝对响应     gscore 绝对响应
    ==================  ===============  ===============
    曝光 +45 灰阶        1.000            ≈0（被归一掉）
    对比度 0.5x         1.170            0.33（被归一掉）
    镜头全挡（纯色）     1.730            108.7
    手挡镜头（半透软边）  1.791            87.0
    下半部 +60 灰阶      1.790            60.0
    整幅右移 8px         1.870            85.3
    小目标 2.3%         86.000            70.8
    ==================  ===============  ===============

    那一栏 conc 在前六个场景里全塌在 1.0~1.9，而它自己的底噪 p95 是
    **11.95** —— 分离度只有 **0.1~0.2 倍**，**比底噪还低，阈值再怎么调都测
    不出来**。这与线上用户报的"手挡摄像头、画面移动都做了，浮框仍显示
    1.7"完全对上：1.7 就是 conc 在这些场景下的**天花板**。gscore 在同一批
    场景上高 34~108 倍，而底噪反而更低（0.109），分离度 550~999 倍。

    **这是否定性质**：conc 只在"局部小目标"这一类场景上有效（86 分），一旦
    变化是大范围的，它就退化成"画面没变"。闯入监控两类都要（人走过去是
    局部的，手挡镜头 / 相机被动是全局的），所以只保 conc 等于把一半场景
    交给兜底间隔，而兜底间隔在实测里从来没生效过。

    **已知边界（有意接受，不是缺陷）**
    * **曝光 / 对比度变化被完全归一**（绝对响应 ≈ 0）—— 灯开关、日夜红外切换、
      摄像头自动曝光**不会触发**告警。这正是仿射归一换来的免疫；但如果哪天
      业务需要"灯开关报警"，这个算法给不了，得另配一路。
    * **不免疫全局位移**：仿射拟合吸收不了平移。移动相机（车载）上底噪天然
      高一个量级，详见 :data:`DEFAULT_THRESHOLD_BY_MODE` 的双素材标定。
    """
    if a.shape != b.shape:
        raise ValueError(
            f"gscore operands must have the same shape, got "
            f"{a.shape} vs {b.shape}")
    h, w = a.shape
    # 同 conc：缩略图边长不保证能被 bs 整除，边缘复制补齐（补零会让右/下
    # 边缘凭空多出一块恒定残差，把 p99 直接抬起来）。
    ph, pw = (-h) % bs, (-w) % bs
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw)), mode="edge")
        b = np.pad(b, ((0, ph), (0, pw)), mode="edge")
    hh, ww = a.shape[0] // bs, a.shape[1] // bs
    A = a.astype(np.float64).reshape(hh, bs, ww, bs).mean(axis=(1, 3)).ravel()
    B = b.astype(np.float64).reshape(hh, bs, ww, bs).mean(axis=(1, 3)).ravel()

    var_a, var_b = float(A.var()), float(B.var())
    if var_a < _GSCORE_MIN_VAR and var_b < _GSCORE_MIN_VAR:
        # 两边都没有纹理（几乎纯色）：无从比较，判为无变化。
        return 0.0
    # **双向拟合**：B≈g·A+c 与 A≈g'·B+c' 各算一次，取两者残差的大者。
    #
    # 为什么必须双向：单向拟合常数项用的是 **B 自己的均值**，于是当 B 退化成
    # 纯色时（B ≈ c），残差恒等于 |B - mean(B)| = 0 —— 分数直接归零，把
    # "整幅纹理彻底丢失"这个**最大的变化**判成"完全没变"。实测镜头被全挡
    # （整幅变纯色）时单向 gscore 得 0.00，比底噪还低，是结构性盲点，而且
    # 正好落在"手挡摄像头"这个用户真实报过的场景上。
    #
    # 双向之后：整幅变暗（B = 0.3·A）两个方向都拟合得上，残差仍然小（免疫
    # 保留）；整幅变纯色只有 B→A 方向拟合得上，反向残差 = |A - mean(A)| 很大，
    # 正常检出。
    res = []
    if var_a >= _GSCORE_MIN_VAR:
        g = float(((A - A.mean()) * (B - B.mean())).mean() / var_a)
        res.append(np.abs(B - (g * A + B.mean() - g * A.mean())))
    else:
        res.append(np.abs(B - B.mean()))
    if var_b >= _GSCORE_MIN_VAR:
        g2 = float(((A - A.mean()) * (B - B.mean())).mean() / var_b)
        res.append(np.abs(A - (g2 * B + A.mean() - g2 * B.mean())))
    else:
        # B 是纯色：反向拟合的斜率不存在，退化成"A 离自身均值的偏离"。
        res.append(np.abs(A - A.mean()))
    return float(np.percentile(np.maximum(res[0], res[1]), pctl))


#: 实现在 wdnwebui 侧，**不通过** wdnav.scene.register_metric 注入 SDK —— 业务
#: 算法不污染通用 SDK。SDK 仍暴露 register_metric 给其他项目用，但本模块不调它。
#: FrameJudge.observe() 直接调 ``gscore(last_luma, cur_luma)``，SDK 完全不知道
#: 这个名字存在（``wdnav.scene.available_metrics()`` 列表里没有 gscore）。


#: **暴露给用户的判帧算法**。SDK 自带的 ``sad`` / ``ncc`` / ``z_mad`` 都不放行。
#:
#: v0.21 起 ``gscore`` 打头（默认推荐），``conc`` 保留只为让用户能当场对比 ——
#: 两者分数刻度完全不同，分数不能横向比较，只有"谁先触发"可比。
#:
#: 实测（本机真实 1080p 监控流 ``rtsp://…192.168.169.25``，900 帧，180x320
#: 缩略图，与线上判据同一管线；完整横评见 ``scripts/bench_conv_judge.py``）：
#:
#: * ``z_mad`` / ``sad``：**结构性无解**。30% 高的人闯入只得 1.88 / 0.98，
#:   而正常相邻帧中位数就有 2.19 / 2.14 —— 目标比噪声还低，阈值怎么调都抓不到。
#: * ``ncc``（``1 - NCC``）**刻度对不上**：取值范围只有 [0, 2]，而阈值是灰阶
#:   口径，结构上永远不可能 ``score > 阈值`` —— 选它等于自动判帧没开。
#: * ``conc``：**对大范围变化结构性失明**。同一条流上底噪 p95 = 11.95，而
#:   "手挡镜头""整幅变暗""下半屏改变""曝光"的绝对响应分别只有 1.79 / 1.73 /
#:   1.79 / 1.00 —— 分离度 0.1~0.2 倍，**比底噪还低**。这就是用户报的
#:   "手遮挡摄像头、画面移动都做了，浮框始终 1.7"的成因：1.7 就是它的天花板。
#: * ``gscore``：真实监控流上底噪 p95 仅 0.109，而小目标 70.8 / 手挡镜头 87.0
#:   / 镜头全挡 108.7 —— 分离度 550~999 倍，且曝光与对比度变化被**完全归一**
#:   （不会因灯开关误报）。同一批场景下 conc 只有 0.1~0.2 倍。
#:   代价：不免疫全局位移，移动相机上底噪高一个量级（阈值已按双素材标定）。
#:
#: 多给用户几个选项，带来的不是灵活性而是"选错还不知道自己选错了" —— 所以
#: 这里放行的是**两个**而不是一串；``conc`` 是待下架候选，不是并列推荐。
EXPOSED_METRICS = ("gscore",)


#: 各模式的默认阈值。**不能共用一个常量** —— 不同度量的刻度根本不可比：
#: SDK 的 ``DEFAULT_THRESHOLD = 6.0`` 是按 z_mad 的灰阶口径标定的，直接套到
#: conc 上会几乎每帧都触发；套到 gscore 上则会几乎每帧都触发（gscore 底噪
#: 只有 0.1 量级，6.0 之上几乎每帧都越过，等于判据没开）。
#:
#: **wdnwebui 不再做阈值标定 —— 阈值由客户按场景自行设置**。
#:
#: 历史：本仓库 v0.21 之前曾做过"双素材标定"（监控流 + 车载源），结论是同一
#: 个算法在两个素材上最优阈值不可迁移（车载相机整幅平移让仿射拟合吸收不完，
#: 底噪天然高一个量级）。该结论**仍然成立**——标定本身有意义，但**把它写
#: 成默认值是错的**：每个客户的相机、镜头、监控场景都不一样，wdnwebui
#: 不应该用一个素材上的标定结果给所有客户做默认。
#:
#: 当前 ``{"gscore": 10.0}`` **只是首启动的初始值**，给新装客户一个能"看到
#: 现象"的起点（10 在办公监控流底噪 ~0.1 上有几倍余量，触发不会太频繁；同
#: 时对镜头被全挡 / 手挡这类大变化能稳定检出）。客户按现场情况在 UI 上调
#: 高或调低，存进自己的 ``config.jsonc``，**别指望**任何"通用阈值"。
#:
#: 历史标定结论（仅供参考，不作为默认值依据）
#: ---------------------------------------------
#:
#: 双素材实测（双向拟合，见 :func:`gscore`，单向 demo 案例，未做产品承诺）：
#:
#: =====================  ==========================  ==========================
#:                         车载源（整幅平移，300 帧）     监控流（固定相机，700 帧）
#:   底噪 p50/p95/p99       3.74 / 12.60 / 14.74        0.062 / 0.109 / 0.275
#: =====================  ==========================  ==========================
#:
#: 信号绝对响应（同一批样本）：
#:
#: ==================  ==============  ==============
#:                      车载源            监控流
#: ==================  ==============  ==============
#: 小目标 0.5% / 2.3%   21.2 / 103.9     — / 70.8
#: 手挡镜头（半透软边）   —                87.0
#: 下半屏 +60 灰阶       40.6             60.0
#: 整幅右移 8px          53.7             85.3
#: 镜头全挡（纯色）      60.4            108.7
#: 曝光 +45 灰阶         0.00（免疫）      —
#: 对比度 0.5x          0.20（免疫）      0.33（免疫）
#: ==================  ==============  ==============
#:
#: 上述数字能让客户**直观感受**：固定监控相机上 gscore 底噪 0.1 量级、变化响
#: 应几十到一百多，10 在这种场景下"几倍到十几倍余量"是合理的初始值。**但这
#: 不是标定结果**——客户现场的相机、场景、光照、H.264 编码参数都不一样，
#: 阈值必须**实测自己那条流**。
DEFAULT_THRESHOLD_BY_MODE: Dict[str, float] = {"gscore": 10.0}

#: 存量配置里可能出现的**已下架**模式 → 当前模式。公开是因为
#: ``change_detector``（VAS 兼容外壳）也要认同一张表 —— 两处各认一份必然漂移。
#:
#: z_mad 从下拉下架了，但用户本地存的判据配置（config.jsonc / localStorage /
#: 旧前端发来的请求）里还有它。不能让它回落 ``manual`` —— 那等于把用户的
#: 自动判帧悄悄关掉，表象是"判据突然不生效了"，比阈值不准更难排查。
#: 所以显式迁到当前推荐度量 gscore，**并把阈值重置成 gscore 的默认**（见
#: :func:`_migrate`）。
#:
#: **为什么不是 conc**：conc 已经是待下架候选（对大范围变化结构性失明，见
#: :data:`EXPOSED_METRICS`），把存量用户迁到它上面，等于让一个已经测出
#: 结构性缺陷的选项继续吃用户流量。
LEGACY_MODE_ALIASES = {"z_mad": "gscore", "auto": "gscore"}


#: 判帧的单帧成本兜底值（ms/帧，960x540 → 长边160）。只有在实测跑不起来时
#: 才会用到（缺 numpy / RGA 初始化失败），正常路径一律用实测值。
_COST_FALLBACK_MS = {"gscore": 1.0}

#: 进程内只测一次（_measure_cost 的结果）。
_COST_CACHE: Dict[str, float] = {}


def cost_ms(mode: str) -> Optional[float]:
    """判帧跑一帧要多少毫秒 —— 给 UI 下拉标注用（"conc 约 2 ms/帧"）。

    **实测而不是写死**：成本随源分辨率、缩略图长边、CPU 负载都会变，写死一个
    常量只会在改了 ``DEFAULT_LONG_SIDE`` 之后悄悄过期 —— 而这正是"标注"这种
    东西最容易骗人的地方。进程内第一次调用时真跑一遍 960x540 的合成帧，之后
    走缓存。

    ``manual`` 恒为 0：它只是取模运算，不缩放也不做差分。
    """
    m = normalize_mode(mode)
    if m == MODE_MANUAL:
        return 0.0
    if m in _COST_CACHE:
        return _COST_CACHE[m]
    try:
        # 真实热路径是 video/session.py 已经把帧缩到 long_side 90x160
        # 再送进来（``cap.read0(w=160, h=90, fmt='NV12')`` 切 Y 平面）。
        # 直接生成 long_side 尺寸的随机灰度帧，避免再 import cv2
        # （frame_judge.py 刻意不依赖 cv2 —— 缩放由 session.py 完成）。
        rng = np.random.default_rng(0)
        long_side = 160
        thumbs = [rng.integers(0, 256, (90, long_side), dtype=np.uint8)
                  for _ in range(12)]
        judge = FrameJudge(mode=m, every_n=1)
        for i, f in enumerate(thumbs[:3]):
            judge.observe(f, i)              # 预热：首帧要建参照帧
        t0 = time.perf_counter()
        for i, f in enumerate(thumbs):
            judge.observe(f, i + 10)
        _COST_CACHE[m] = (time.perf_counter() - t0) / len(thumbs) * 1000.0
    except Exception:  # noqa: BLE001 - 测不出来就退回文档值，别拖垮接口
        _LOG.warning("frame_judge: 判帧耗时实测失败，退回参考值", exc_info=True)
        _COST_CACHE[m] = _COST_FALLBACK_MS.get(m, 0.0)
    return _COST_CACHE[m]


def metrics() -> List[str]:
    """前端下拉可用的算法名（手动跳帧之外）。**已刻意收窄**，见
    :data:`EXPOSED_METRICS` 的理由。

    **直接返回 :data:`EXPOSED_METRICS`** —— 不再走 ``wdnav.scene.available_metrics()``
    过滤。原因是 gscore 是 wdnwebui 业务实现，**没注册到 wdnav**（保持 SDK
    干净），如果还要先查 SDK 列表就永远是空。 :data:`EXPOSED_METRICS` 已经是
    我们想暴露给用户的精确白名单，直接返回它。
    """
    return list(EXPOSED_METRICS)


# 配置代次（rev）。**进程级**单调递增，不是每个判据器从 0 开始 ——
# VAS 每次 start() 都新建一个 ChangeDetector（gen 从 0 重来），若按实例
# 计数，前端记住的 rev 会比新 run 的还大，之后**所有**快照都被当成过期丢掉
# （浮框和曲线直接冻住）。进程级计数让"新配置 = 更大的 gen"恒成立。
_GEN = itertools.count(1)


# normalize_mode 每帧/每次配置校验都会调，别每次重扫一遍注册表。
_METRICS = frozenset(metrics())


def normalize_mode(mode: Optional[str]) -> str:
    """把外部传入的模式名收敛成合法值。

    已下架的历史模式（``z_mad`` / ``auto``）走 :data:`LEGACY_MODE_ALIASES`
    迁到当前模式，**不回落 manual** —— 回落等于把用户的自动判帧悄悄关掉。

    未知 / 空 → ``manual``（历史默认行为，且是唯一零 RGA 开销的分支）。
    """
    m = str(mode or "").strip().lower()
    if not m:
        return MODE_MANUAL
    if m in LEGACY_MODE_ALIASES:
        return LEGACY_MODE_ALIASES[m]
    if m == MODE_MANUAL or m in _METRICS:
        return m
    _LOG.warning("frame_judge: 未知模式 %r，回落 manual", m)
    return MODE_MANUAL


def default_threshold(mode: Optional[str]) -> float:
    """该模式下的默认阈值（不同度量的刻度不可比，见
    :data:`DEFAULT_THRESHOLD_BY_MODE`）。"""
    m = normalize_mode(mode)
    return DEFAULT_THRESHOLD_BY_MODE.get(
        m, DEFAULT_THRESHOLD_BY_MODE[EXPOSED_METRICS[0]])


def _migrate(mode: Optional[str],
             threshold: Optional[float]) -> tuple:
    """存量已下架模式的迁移：返回 ``(mode, threshold)``。

    关键在 **threshold 也要重置**：z_mad 的阈值是灰阶口径（线上实测值 1.0），
    直接套到 conc 上会几乎每帧都触发（conc 自然相邻帧 p50 就有 7.60）——
    那是比漏检更糟的故障。调用方把返回的 ``threshold=None`` 理解成"用默认值"。
    """
    raw = str(mode or "").strip().lower()
    if raw in LEGACY_MODE_ALIASES:
        target = LEGACY_MODE_ALIASES[raw]
        _LOG.info("frame_judge: 判据模式 %r 已下架，迁移到 %r 并重置阈值",
                  raw, target)
        return target, None
    return mode, threshold


class FrameJudge:
    """帧判据：``observe()`` 返回"这一帧要不要送检"，``snapshot()`` 给 UI。"""

    def __init__(self,
                 mode: str = MODE_MANUAL,
                 every_n: int = 600,
                 threshold: Optional[float] = None,
                 long_side: int = DEFAULT_LONG_SIDE,
                 sample_every: int = DEFAULT_SAMPLE_EVERY) -> None:
        mode, threshold = _migrate(mode, threshold)
        self._mode = normalize_mode(mode)
        # RLock 而非 Lock：observe() 可能被两个线程调（向导模式下解码线程采样
        # 打分，应用页面模式下轮转线程在送检时打分，见 multi_session 的
        # rr_owns_judge），且它尾部要调同样持锁的 _commit() —— 普通 Lock 不可
        # 重入，直接死锁。
        self._lock = threading.RLock()
        self._history: Deque[List] = deque(maxlen=HISTORY_MAX)
        self._every_n = max(1, int(every_n))
        self._threshold = float(
            default_threshold(self._mode) if threshold is None else threshold)
        self._long_side = int(long_side)
        self._sample_every = max(1, int(sample_every))
        self._evaluated = 0
        self._sampled = 0
        self._cur: Optional[float] = None
        self._changed = False
        self._last_pos = -1
        self._above = 0
        self._gen = next(_GEN)
        self._last_luma: Optional[np.ndarray] = None
        # 历史参参照帧（在 reconfigure 时丢弃以避免新旧口径混在一起）

    # ------------------------------------------------------------------ #
    def _drop_reference(self) -> None:
        """丢参照帧。``reconfigure`` / ``reset`` 时调：阈值或算法变了之后，
        旧的 last_luma 是按旧口径算的，再继续用会混进新分数里。"""
        self._last_luma = None

    @property
    def is_auto(self) -> bool:
        return self._mode != MODE_MANUAL

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def every_n(self) -> int:
        return self._every_n

    @property
    def threshold(self) -> float:
        return self._threshold

    @property
    def long_side(self) -> int:
        """判据要的缩略图长边（像素）。video/session.py 读这个值去给
        ``cap.read0(w=..., h=..., fmt='Y')`` 传参，让 wdnav RGA 在解码阶段
        就直接出目标尺寸的 Y 平面 ndarray，wdnwebui 侧不再二次缩放。
        """
        return self._long_side

    @property
    def sample_every(self) -> int:
        """解码线程多少帧算一次分。video/multi_session.py 读它传给每路的
        FrameJudge —— 原型是单例，各路共用同一份间隔配置。
        """
        return self._sample_every

    @property
    def current_score(self) -> Optional[float]:
        """最近一帧的判据分数（手动跳帧模式下没有分数，返回 None）。

        解码线程**每帧**都要读它去填检测事件，所以不能用 ``snapshot()``：
        那会把整个环形缓冲（最多 200 条）复制一遍，25fps 下纯粹是浪费。
        """
        with self._lock:
            return self._cur

    def reconfigure(self, mode: Optional[str] = None,
                    every_n: Optional[int] = None,
                    threshold: Optional[float] = None) -> None:
        """运行中改配置。

        **重建参照帧**：阈值/算法变了之后，"上一帧被判定为变了的那一帧"
        已经是按旧口径选出来的，继续用会让新旧两套口径混在一份分数里。
        所以任何实质变化都清空判帧器状态（下一帧重新当参照帧）。
        """
        mode, threshold = _migrate(mode, threshold)
        new_mode = self._mode if mode is None else normalize_mode(mode)
        new_n = self._every_n if every_n is None else max(1, int(every_n))
        mode_changed = new_mode != self._mode
        if threshold is None:
            # **阈值属于模式**。模式变了就跟着换到新模式的默认值 ——
            # 否则从 manual 切过来会带着 manual 遗留的旧刻度阈值（线上真
            # 出现过 conc + threshold=1.0，几乎每帧都送检）。
            # 模式没变就保留当前值：API 调用方只想改 every_n 时，
            # 不该把他调好的阈值冲掉。
            new_t = default_threshold(new_mode) if mode_changed else self._threshold
        else:
            new_t = float(threshold)
        if (new_mode, new_n, new_t) == (self._mode, self._every_n, self._threshold):
            return
        self._mode, self._every_n, self._threshold = new_mode, new_n, new_t
        # 代次 +1：前端据此丢弃"配置变更之前就已经在路上的"旧快照。
        # 那一拍的快照是按旧配置算出来的，晚到一步就会把用户刚改好的控件
        # 打回旧值（实测：改成 manual 后又跳回自动判据，来回翻）。
        self._gen = next(_GEN)
        # 分数历史一并清掉：旧配置下算出的分数和新的阈值不是同一把尺，
        # 留在曲线里会画出一条"跨口径"的假趋势。同时丢参照帧。
        self.reset()

    def reset(self) -> None:
        """新一轮分析前清空判帧状态 —— **必须调用**，否则上一轮的参照帧会
        污染新 run 的首帧判定（用户点「开始」后迟迟没结果）。

        整个函数体持锁：归属切换（multi_session._set_judge_owner）会和
        解码线程 / 轮转线程的 observe() 撞上，不加锁会出现「参照帧刚被
        丢掉、又被另一个线程写回去」。
        """
        with self._lock:
            self._last_pos = -1
            self._cur = None
            self._changed = False
            self._above = 0
            self._evaluated = 0
            self._sampled = 0
            self._history.clear()
            self._drop_reference()

    # ------------------------------------------------------------------ #
    def should_sample(self, frame_pos: int) -> bool:
        """**向导模式**下解码线程问「这一帧要不要算分」——**必须问在 RGA 之前**。

        判据的缩略图是 RGA 出来的，问晚了就不是「少算一次分」而是多打一次
        RGA，32 路能吃掉 2/3 的吞吐（见 :data:`DEFAULT_SAMPLE_EVERY`）。
        首帧必采：``observe`` 靠它建参照帧，跳掉会让第一次打分推迟一整个间隔。

        应用页面（检测在跑）**不走这里** —— 那时由轮转线程在送检那一刻调
        :meth:`observe`，参照帧语义不同。见 :meth:`observe` 的说明。
        """
        if not self.is_auto:
            return True
        p = int(frame_pos)
        return p <= 1 or p % self._sample_every == 0

    # ------------------------------------------------------------------ #
    def observe(self, frame, frame_pos: int) -> bool:
        """该帧是否应该送去做后续推理。

        **两套规则，同一时刻只有一个线程在调**（由 multi_session 的
        ``Channel.rr_owns_judge`` 保证，切换时参照帧会被丢掉重建）：
        * 向导模式（未启动检测）：解码线程按 ``sample_every`` 采样调用，只为
          让浮框/曲线动起来，门控在 :meth:`should_sample`。
        * 应用页面（检测在跑）：轮转线程在**送检那一刻**调用，比较的是
          「上一次送检的帧 vs 当前帧」。

        整个函数体持锁，切换窗口内两个线程各进一次也不会把 _last_luma 写花。

        ``frame`` 期望是**已经缩到 long_side 的 2D 灰度 ndarray**（Y 平面）。
        video/session.py 在 ``cap.read0(w=..., h=..., fmt='NV12').numpy()``
        阶段就拿到了（取前 h 行即 Y 平面），wdnwebui 侧不做任何颜色/尺寸
        变换 —— ``gscore`` 也要 2D 灰度图，直接喂它。

        **兼容层**：如果传进来的是 3 通道 BGR（来自测试 / 旧调用方），自动
        取第一通道当灰度。这条路径**不**走生产代码（生产是 session.py 已
        转好的 Y 平面），仅保护历史调用点不爆；不能替代真正的灰度抽取
        （BGR 第一个通道是 B 不是 Y，会偏蓝），所以测试里仍应喂真灰度。

        任何异常都返回 True —— 宁可多送检，不可漏检（与
        ``FrameDiffer.should_sample`` 同一取舍）。
        """
        with self._lock:
            pos = int(frame_pos)
        self._evaluated += 1
        if not self.is_auto:
            # 手动跳帧：逐位保持历史行为（``pos > 0 and pos % n == 0``），
            # 不构造判据器、不缩放。这里只留计数供 UI 画采样率。
            take = pos > 0 and pos % self._every_n == 0
            self._commit(pos, None, take)
            return take

        # 兼容层：3 通道 BGR → 取第一通道当灰度（仅测试/历史路径；生产
        # 路径 frame 已经是 2D 灰度，这条不进）
        if frame is None:
            self._commit(pos, None, True)
            return True
        if getattr(frame, "ndim", 2) == 3:
            frame = frame[..., 0]

        # 首帧 / 刚 reset 完：还没参照帧，**无条件送检**（用 frame 自己当参照）
        if self._last_luma is None:
            self._last_luma = frame
            self._commit(pos, 0.0, True)
            return True

        # 算分。**直接调本文件里的 gscore**，不通过 wdnav 注册表。
        try:
            score = float(gscore(self._last_luma, frame))
        except Exception:  # noqa: BLE001
            _LOG.warning("frame_judge: gscore 异常，本帧按送检处理", exc_info=True)
            self._commit(pos, None, True)
            return True

        gap = (pos - self._last_pos) if self._last_pos >= 0 else 0
        # 兜底**不是无条件送检**：画面本来就一样时，重跑一遍推理只会得到
        # 同一个答案，白白吃掉一次 NPU 调用。所以兜底要求"确实变了点"——
        # 分数至少到阈值的一半。静止画面因此彻底安静（只送首帧），
        # 而"变了但没到阈值"的中间地带仍有兜底保证最长 every_n 帧出一次结果。
        #
        # 这是**有意偏离** ``FrameDiffer.should_sample`` 的判定（那边是
        # ``gap >= max_gap`` 无条件送）。SDK 那套的出发点是"宁可多送不可漏检，
        # 保证结果会刷新"；而静止画面送出去的结果与上一次**逐位相同**，
        # 刷新的是同一个答案，不存在"漏检"。用户明确要求不要为此徒增功耗。
        fallback = gap >= self._every_n and score >= self._threshold * 0.5
        changed = bool(score > self._threshold or fallback)
        if changed:
            # 推进参照帧 + 推进 _last_pos。**注意**：fallback 路径也推进，
            # 因为"满了 every_n 又确实变了点"就是新一帧事件，参照帧应跟新。
            self._last_luma = frame
        self._commit(pos, score, changed)
        return changed

    def decide_take_for_rr(self, current_pos: int) -> bool:
        """轮转线程**只读**版门控：读解码线程 ``observe()`` 留下的状态。

        仅在 ``rr_owns_judge=False`` 的回退路径上用（正常路径由轮转线程自己
        调 ``observe()``）。只读是为了避免和解码线程并发写 ``_last_luma`` /
        ``_last_pos`` / ``_evaluated``。

        判定语义与 ``observe()`` 内的逻辑**逐字一致**：

        - ``score > threshold`` —— 真变化
        - 或 ``gap >= every_n and score >= threshold * 0.5`` —— 兜底
          （画面静止时满 every_n 也送一次，但前提是"确实变了点"，与
          ``observe()`` 内部 ``fallback`` 同取舍）

        读侧用 ``_lock`` 拿一致快照，避免被解码线程正在写的 ``_commit`` 撕开。

        ``current_pos`` 应传本路已解码帧数（与原 ``observe(frame_pos)`` 同口径）。

        返回 True = 送 VLM；False = 跳过本路。
        """
        with self._lock:
            cur = self._cur
            thr = self._threshold
            n = self._every_n
            last_pos = self._last_pos
        if cur is None:
            # 还没打过分（首帧 / 参照帧还没建）—— 放行一次，让首帧进推理
            # 之后判据就有得分了。下一次 observe() 会写 _cur。
            return True
        gap = (int(current_pos) - last_pos) if last_pos >= 0 else int(current_pos)
        fallback = gap >= n and cur >= thr * 0.5
        return bool(cur > thr or fallback)

    def _commit(self, pos: int, score: Optional[float], changed: bool) -> None:
        """记一笔观测结果。分数与计数统一在这里收口，便于 snapshot 读取。

        ``_last_pos`` 只在**判定为变化**时推进 —— 它要回答的是"距上一次
        送检过了多少帧"（兜底间隔的基准）。每帧都推进的话 gap 恒为 1，
        兜底间隔永远不会触发。
        """
        with self._lock:
            self._cur = score
            self._changed = changed
            if changed:
                self._last_pos = pos
                self._sampled += 1
                self._above += 1
            if score is not None and score > SCORE_EPS:
                self._history.append([pos, round(float(score), 4)])

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        """给前端 / 日志的只读视图（WS 与 HTTP 线程调用）。"""
        with self._lock:
            hist = [[p, s] for p, s in self._history]
            cur = self._cur
            changed = self._changed
            above = self._above
            evaluated = self._evaluated
            sampled = self._sampled
        n = max(evaluated, 1)
        # **实测送检间隔**（评估帧数 / 送检帧数）。前端要靠它回答用户最常问的
        # 那个问题："我把帧间隔设成 300，为什么还在一秒钟一次？"
        # 配了 every_n 却不生效的唯一原因就是判据几乎每帧都放行 ——
        # 阈值下界低于素材噪声底时就是这个形态，而浮框原来只显示**当前帧**
        # 的判定（绝大多数帧都是"无变化"），于是节流失效这件事在界面上完全
        # 看不见。没有这个数就只能靠猜，或者盯着事件列表数。
        eff = (n / sampled) if sampled > 0 else None
        return {
            "gen":        self._gen,
            "mode":       self._mode,
            "manual":     not self.is_auto,
            "every_n":    self._every_n,
            "threshold":  self._threshold,
            "long_side":  self._long_side,
            "cur":        cur,
            "changed":    bool(changed) if cur is not None else False,
            "evaluated":  evaluated,
            "sampled":    sampled,
            "above_ratio": above / n,
            "every_n_eff": eff,
            "recent":     hist,
        }