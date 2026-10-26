#!/usr/bin/env bash
# 更新 .venv/src 下以 editable 模式安装的 wdnav / wdnai / wdndds / wdnhid
# 以及两个应用插件包 wdnapps_video_analyzer / wdnapps_hdmi_processor
# 用法：./update_rkwhl.sh
#
# 每个包的流程（v1.3 起）：
#   1. `git pull --ff-only` 拉最新代码
#   2. 比对 pull 前后的 git HEAD
#   3. HEAD 没变 → **什么都不做**（不碰 .venv，不跑 pip）
#   4. HEAD 变了 → 先彻底卸载旧版本（purge_install_traces），再
#      `pip install -e` 装新的；编译 / 断言 .so 落地的逻辑沿用原样
#
# 设计要点：
#   * 以 **git HEAD** 为唯一更新信号，不用 pyproject 版本号 —— 实测三包
#     的 repo 版本号与已装版本长期一致，大量「改 cshim_src / 改 _build.py」
#     的提交不 bump 版本号，只比版本号会把最该重建 .so 的升级静默跳过。
#     代价：纯文档提交也会重装一次，换来不漏。
#   * 升级前先卸载（purge_install_traces）。pip 不会因为后来装了 editable
#     就回收更早非 editable 安装留下的实体目录，而实体目录在 sys.path 上
#     排在 .pth 之前、会永久遮挡 editable 安装。根治见
#     bootstrap_wdndds_native.sh v1.4（改用 -e），purge 只做兜底自愈。
#   * pip >= 24 拒绝 `--force-reinstall` + `-e` 组合（PEP 660）；且非
#     editable 标志必须放在 `-e` 之前，否则 pip 26.x 会当成 editable
#     requirement 报错。
#   * 每次 install 后**断言 .so 落地**——setuptools ≥64 会静默吞掉
#     build_py 钩子的 RuntimeError，pip 返回 0、日志全绿但产物没生成。
#   * 插件包（wdnapps_*）没有 .so，改为断言 **entry point → manifest id →
#     static/ 三段链路**。这组断言挡的是另一个静默失败：entry point 名写了
#     下划线而 manifest "id" 是连字符时，pip 全程报成功，但宿主启动时把这
#     几张卡 FATAL 跳过，页面上就是"卡片不见了"且日志里只有一行 ERROR。
#   * 强制重建：`UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh`。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_SRC="$SCRIPT_DIR/.venv/src"

# 必须先 activate venv：wdndds._build.py / wdnav._build_wdnav.py 在
# 重建 .so 过程中可能 spawn 子进程调用 `python` / `pip`；未 activate
# 时这些子进程会回退到系统 python（尤其 sudo / 非交互 shell 场景），
# 导致 whl 装到系统 site-packages 而非 .venv，并触发 Debian PEP 668
# "externally-managed-environment" 报错。activate 后 `python` 锁定到
# venv，`python -m pip` 走 venv site-packages，路径可预测。
if [ ! -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    echo "ERROR: 未找到 $SCRIPT_DIR/.venv/bin/activate，请先创建虚拟环境" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.venv/bin/activate"

PIP_TARGET="python -m pip"

# 先把 wdnwebui 自己的运行时依赖（fastapi / uvicorn / opencv-python-headless /
# python-multipart / av / gguf / lancedb / pyarrow / psutil 等）按
# requirements.txt 装好——开发机经常是新克隆的 .venv，requirements.txt
# 还没执行过；或者上游 bump 了 web 依赖版本。装完再 update SDK，避免 SDK 重装
# 链上拉不到 web 侧依赖。
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "==> 同步 requirements.txt (wdnwebui 自身依赖)"
    ( cd "$SCRIPT_DIR" && $PIP_TARGET install -r requirements.txt )
    # ------------------------------------------------------------------------
    # v4l2 (PyPI) 的 py3 兼容补丁
    #
    # requirements.txt 里的 `v4l2>=0.2` 是 wdnav.v4l2 的硬依赖（SDK 用
    # --no-deps 装，依赖不会自己进来）。但上游 0.2（2014）源码里留着
    # Python 2 惯用法 `range(...) + [...]`，py3 下 import 阶段就抛
    # TypeError（不是 ImportError！）—— 装上了照样用不了，且 HDMI-IN
    # 端点会从 503 退化成 500。补丁一行搞定，脚本幂等。
    #
    # 失败不终止本脚本：v4l2 只影响 HDMI-IN 一条链路，其余 SDK 更新
    # 不该被它拖住。日志里会明确给出补救命令。
    # ------------------------------------------------------------------------
    echo "==> 修补 v4l2 的 py3 兼容问题 (scripts/fix_v4l2_py3.py)"
    if ! ( cd "$SCRIPT_DIR" && python scripts/fix_v4l2_py3.py ); then
        echo "WARN: v4l2 py3 兼容补丁未成功 —— HDMI-IN 采集将不可用。" >&2
        echo "      手动执行: python scripts/fix_v4l2_py3.py" >&2
    fi
else
    echo "==> 未找到 requirements.txt，跳过 web 依赖同步"
fi

# ---------------------------------------------------------------------------
# 插件包（wdnapps_*）的产物校验
#
# 这两个包是 pure-Python，没有 .so 可断言；但它们有一个更致命的失败模式：
# **pip 装成功了，卡片却一个都不出现**，而且全程不报错。最常见的原因是
# entry point 名与 manifest.json 的 "id" 不一致——包目录用下划线
# (cards/video_detection/)，卡 id 一贯用连字符 ("video-detection")，照着
# 模块名写就全错。宿主 discover() 判 FATAL 后**静默跳过**该卡，所以
# "装上了" 与 "卡片能用" 是两件事，必须在这里断言。
#
# 用 importlib.util.find_spec 而不是 import_module：前者只 import 父包
# (cards/__init__.py 是空的，正好)，**不执行叶子模块**；后者会顺着 api.py
# 拉起整个 app.core.*（compute registry、解码器初始化），在更新脚本里既慢
# 又带副作用。
#
# 输出约定：一行一卡，`OK <id> (<dir>)` 或 `BAD: <原因>`。判定逻辑在
# scripts/check_plugin_entrypoints.py（也可独立跑），这里只负责调用 + 呈现。
# 最初这段是 `$(python - <<'PYEOF' ... )` 的内联 heredoc，在本机 bash
# 5.2 上**解析期**就报 `command substitution: syntax error near unexpected
# token '||'`——最小用例能过、函数里不能过，不值得继续纠缠这种脆写法。
# ---------------------------------------------------------------------------
verify_plugin_cards() {
    local pkg="$1"
    local checker="$SCRIPT_DIR/scripts/check_plugin_entrypoints.py"
    local out bad

    if [ ! -f "$checker" ]; then
        echo "FATAL: [$pkg] 找不到校验脚本 $checker" >&2
        return 1
    fi

    # 判定交给 grep，不用 python 的退出码 —— python 侧恒定 exit 0，
    # 输出里 `BAD:` 开头即为失败。`cd "$SCRIPT_DIR"` 是必须的：
    # 卡片模块 import 的是宿主仓库根的 `app.core.*`。
    out="$(cd "$SCRIPT_DIR" && python "$checker" "$pkg" 2>&1)" || true

    if [ -z "$out" ]; then
        echo "FATAL: [$pkg] 插件校验无输出（python 可能没跑起来）" >&2
        return 1
    fi

    bad="$(printf '%s\n' "$out" | grep -c '^BAD' || true)"
    printf '%s\n' "$out" | sed 's/^OK /    ✅ /'
    if [ "$bad" != "0" ]; then
        printf '%s\n' "$out" | grep '^BAD' | sed 's/^BAD: /    ❌ /' >&2
        echo "FATAL: [$pkg] 插件卡片校验未通过（$bad 项）。宿主启动时会静默丢卡，" >&2
        echo "       不是启动时报错，所以必须在这里拦住。" >&2
        return 1
    fi
    return 0
}

# 校验某个 SDK 安装后必须落地的 .so 产物。pip install -e 在 setuptools ≥64
# 上会**静默吞掉** ``build_py`` 钩子的 RuntimeError（参见 bootstrap_wdndds_native.sh
# 注释 / AGENTS.md「SDK native-`.so` rebuild policy」段）——pip exit code 仍为 0，
# 但 .so 根本不会生成。所以脚本必须在 install 之后**亲自**检查产物，否则
# 编译失败会一路滑过去、前端 import 时才报 FileNotFoundError，很难溯源。
#
# 用法：verify_outputs <pkg> <pip_stderr_log>
#   缺失任一 .so → 打印 "build hook 失败提示 + 关键 .so 路径 + pip stderr 末尾"，
#                 然后 exit 1，让 set -e 在外层立即终止脚本。
verify_outputs() {
    local pkg="$1"
    local log="$2"
    local -a expected=()
    case "$pkg" in
        wdndds)
            expected=(
                "$VENV_SRC/wdndds/src/wdndds/dds/_native/libwdndds_idl.so"
                "$VENV_SRC/wdndds/src/wdndds/dds/_native/libwdndds_cshim.so"
            )
            ;;
        wdnav)
            expected=(
                "$VENV_SRC/wdnav/src/wdnav/lib/libshim_video.so"
            )
            ;;
        wdnai)
            # wdnai 是 pure-Python，无 .so 产物；HEAD 推进就当 OK。
            return 0
            ;;
        wdnhid)
            # wdnhid 是 pure-Python（USB Gadget HID bindings），无 .so 产物。
            # wdnwebui 卡片侧 import wdnhid，必须保证本包可用即可。
            return 0
            ;;
        wdnapps_video_analyzer|wdnapps_hdmi_processor)
            # 应用插件包也是 pure-Python，同样没有 .so；但要验的东西更重要
            # ——卡片是不是真的注册上了（见 verify_plugin_cards 的说明）。
            verify_plugin_cards "$pkg"
            return $?
            ;;
    esac

    local missing=()
    for f in "${expected[@]}"; do
        if [ ! -f "$f" ]; then
            missing+=("$f")
        fi
    done
    if [ ${#missing[@]} -eq 0 ]; then
        return 0
    fi

    echo "FATAL: [$pkg] build hook 未生成以下 .so（setuptools ≥64 静默吞了 RuntimeError）:" >&2
    for f in "${missing[@]}"; do
        echo "    - $f" >&2
    done

    # 试着从 captured stderr 抽出真正的根因，比笼统的「缺 apt 依赖」更直接
    # 命中。subprocess 抛的 FileNotFoundError 通常是 cmd 二进制找不到
    # （cmake / patchelf / make）；cmake 自身的报错通常以 "CMake Error" 开头；
    # gcc/clang 链接/编译失败通常带 "fatal error" 或 "undefined reference"。
    local cause=""
    if [ -f "$log" ] && [ -s "$log" ]; then
        if grep -qE "No such file or directory: 'cmake'" "$log"; then
            cause="cmake 二进制缺失（apt install cmake）"
        elif grep -qE "No such file or directory: '(make|patchelf|cc|g\\+\\+)'" "$log"; then
            cause=$(grep -oE "No such file or directory: '[^']+'" "$log" | head -1)
            cause="$cause （apt install build-essential patchelf cmake）"
        elif grep -qE "Could NOT find (FastDDS|foonathan)" "$log"; then
            cause="FastDDS 3.6 dev 头文件未找到（跑 .venv/src/wdndds/build_native.sh 或设 WDNDDS_FASTDDS_PREFIX）"
        elif grep -qE "fatal error:.*No such file" "$log"; then
            cause=$(grep -oE "fatal error: [^\\n]+" "$log" | head -1)
        elif grep -qE "undefined reference to" "$log"; then
            cause=$(grep -oE "undefined reference to '[^']+'" "$log" | head -1)
        fi
    fi
    if [ -n "$cause" ]; then
        echo "" >&2
        echo "  实测到的根因：$cause" >&2
    fi

    echo "" >&2
    echo "  通常原因：" >&2
    case "$pkg" in
        wdndds)
            echo "    - 缺 build 工具链：sudo apt install cmake patchelf build-essential" >&2
            echo "    - 缺 C++ 内存分配库：sudo apt install libfoonathan-memory-dev libfoonathan-memory0.7.3" >&2
            echo "    - 缺 FastDDS 3.6 dev 头文件：跑 bash .venv/src/wdndds/build_native.sh" >&2
            echo "      （vendor 路径 .venv/src/wdndds/native/ 已存在时不用重跑）" >&2
            echo "    - 一键 bootstrap：sudo ./bootstrap_wdndds_native.sh（apt + stub + non-editable pip install）" >&2
            ;;
        wdnav)
            echo "    - 缺 apt 依赖：sudo apt install librockchip-mpp-dev（提供 /usr/include/rockchip 头文件）" >&2
            echo "    - 头文件目录有但 .so 链接失败：检查 cc 错误（多半缺 librockchip-mpp1 / librockchip-vpu0 运行时库）" >&2
            ;;
    esac
    echo "" >&2
    if [ -f "$log" ] && [ -s "$log" ]; then
        echo "  --- pip install stderr 末尾（捕获 setuptools 吞掉的 hook traceback）---" >&2
        tail -n 60 "$log" >&2 || true
        echo "  --- end ---" >&2
    else
        echo "  （无 captured stderr；重跑时加 UPDATE_RKWHL_VERBOSE=1 看详细日志）" >&2
    fi
    return 1
}

# 卸载一个 SDK 包的所有安装痕迹，为紧接着的 `pip install -e` 扫场地。
# 三个动作缺一不可（pip uninstall 只做得到第 1 个）：
#   1. `pip uninstall -y` —— 按 pip 自己的元数据摘除（含 editable 的 .pth）
#   2. 删实体目录 `site-packages/<pkg>/` —— pip uninstall 不做（它只认元
#      数据里记的文件）。更早某次**非** editable 安装留下的目录属于这类，
#      且会因 sys.path 顺序永久遮挡 editable 安装。
#   3. 删累积的 `__editable__.<pkg>-*.pth` —— 每次装 editable 都会新写一个
#      带版本号的且从不回收（实测攒到过 0.7.0/0.8.0/0.9.0 三个）。
# 只删 pip 生成的确定性路径；site-packages 用 sysconfig 动态取，不硬编码版本。
purge_install_traces() {
    local pkg="$1"
    local sp
    sp="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    if [ -z "$sp" ] || [ ! -d "$sp" ]; then
        echo "WARN: [$pkg] 无法定位 site-packages（$sp），跳过清理" >&2
        return 0
    fi

    echo "    [$pkg] pip uninstall -y $pkg"
    # pip uninstall 对「当前没装」返回非 0，不算失败。
    $PIP_TARGET uninstall -y "$pkg" >/dev/null 2>&1 || true

    local removed=0 f
    if [ -d "$sp/$pkg" ]; then
        echo "    [$pkg] 删除非 editable 残留实体目录 $sp/$pkg"
        rm -rf "$sp/$pkg"
        removed=1
    fi
    for f in "$sp/$pkg"-*.dist-info "$sp/$pkg".dist-info \
             "$sp/__editable__.$pkg"-*.pth "$sp/__editable__.$pkg.pth"; do
        [ -e "$f" ] || continue
        echo "    [$pkg] 删除 $f"
        rm -rf "$f"
        removed=1
    done

    if [ "$removed" = "0" ]; then
        echo "    [$pkg] 无残留（干净的可编辑安装）"
    fi
}


update_pkg() {
    local pkg="$1"
    local repo_url="$2"
    local dir="$VENV_SRC/$pkg"

    # pip stderr 存临时文件，install 后由 verify_outputs 在 .so 缺失时
    # 回打（-v 让 setuptools 把钩子内部的输出转发进来）。
    local pip_log
    pip_log="$(mktemp -t update_rkwhl.XXXXXX.log)"
    [ -n "$pip_log" ] || pip_log="/tmp/update_rkwhl.$$.log"
    trap "rm -f '$pip_log'" RETURN

    # 步骤 1：拉取最新代码
    if [ ! -d "$dir" ]; then
        echo "==> [$pkg] 未找到 $dir，执行 pip install -e 安装"
        if ! ( cd "$SCRIPT_DIR" && $PIP_TARGET install -v --no-deps -e "$repo_url" ) 2> "$pip_log"; then
            echo "FATAL: [$pkg] pip install 自身失败（exit≠0）" >&2
            tail -n 60 "$pip_log" >&2 || true
            return 1
        fi
        verify_outputs "$pkg" "$pip_log" || return 1
        return 0
    fi

    echo "==> [$pkg] git pull"
    local before after
    before=$(git -C "$dir" rev-parse HEAD)

    # pull 失败不中止整个脚本，记 warn 跳过本包继续下一个。
    # `--ff-only` 只在两种情况下拒绝，且都不改动工作区：本地有未提交
    # 修改会被覆盖、或本地与远程已分叉。这属于「本地有人在工作」，
    # 强推/merge 都不是脚本该做的决定（FATAL 留给 pip / .so 这类真故障）。
    if ! git -C "$dir" pull --ff-only; then
        echo "WARN: [$pkg] git pull --ff-only 失败，跳过本包（.venv 未改动）" >&2
        echo "      常见原因：本地有未提交修改 / 本地与远程已分叉。" >&2
        echo "      git 不会因此丢失你的修改——请自行处理后重跑：" >&2
        echo "        git -C $dir status" >&2
        echo "        git -C $dir stash   # 或 commit / branch" >&2
        echo "      其余 SDK 继续处理。" >&2
        return 1
    fi
    after=$(git -C "$dir" rev-parse HEAD)

    # 步骤 2/3：HEAD 没变就不动 .venv（信号选择理由见文件头）
    local force="${UPDATE_RKWHL_FORCE_REBUILD:-0}"
    if [ "$before" = "$after" ] && [ "$force" != "1" ]; then
        echo "==> [$pkg] HEAD 未变（${after:0:7}），跳过安装（.venv 未改动）"
        echo "    需要强制重建时：UPDATE_RKWHL_FORCE_REBUILD=1 $0"
        return 0
    fi

    local reason
    if [ "$before" != "$after" ]; then
        reason="HEAD ${before:0:7}→${after:0:7}"
    else
        reason="HEAD 未变（${after:0:7}），但 FORCE_REBUILD=1"
    fi
    echo "==> [$pkg] ${reason}，卸载旧版本后重装"

    # 步骤 4a：彻底卸载
    purge_install_traces "$pkg"

    # 步骤 4b：安装新版本。传本地路径而非 $repo_url 以免 SSH 重新 clone；
    # .so 编译走 build_py 钩子（内部无条件 unlink + 重建），失败时旧 .so
    # 已被删，运行时 import 抛 FileNotFoundError，比 silent undefined
    # symbol 容易诊断。
    if ! ( cd "$SCRIPT_DIR" && $PIP_TARGET install -v --no-deps -e "$dir" ) 2> "$pip_log"; then
        echo "FATAL: [$pkg] pip install 自身失败（exit≠0）" >&2
        tail -n 60 "$pip_log" >&2 || true
        return 1
    fi
    verify_outputs "$pkg" "$pip_log" || return 1
}

# 逐包驱动：某个包失败只记 warn 并继续下一个，跑完全部再汇总。
# 早期用裸调用 + `set -e`，一个包出错就打断整个脚本，后面的 SDK 静默
# 停留在旧版——三包只要有一个处于「本地有改动」，另外两个就永远升不了。
FAILED_PKGS=()
run_pkg() {
    local pkg="$1"
    local repo_url="$2"
    if update_pkg "$pkg" "$repo_url"; then
        return 0
    fi
    echo "WARN: [$pkg] 本次未更新（详见上方错误）" >&2
    FAILED_PKGS+=("$pkg")
    return 0   # 吞掉失败，保证后续包继续
}

# ---------------------------------------------------------------------------
# 依赖审计（--no-deps 的对冲）
#
# SDK 一律用 `pip install --no-deps -e` 装 —— 这是**有意为之**，不是历史遗留：
#   * 私有包不在 PyPI（wdndds → wdnai、插件包 → wdnhid / wdnav），让 pip 去
#     解析必挂；
#   * 解析会把 opencv-python（GUI 版）一起拉进来，和已装的
#     opencv-python-headless 抢同一个 cv2 包；
#   * av 需要手挑版本；
#   * 目标机常常根本连不上 PyPI。
# 详见 README §4b。
#
# 代价是 **SDK 自己声明的依赖永远不会自己进 venv**，而故障现场离原因很远：
#   * v4l2 缺失 → HDMI-IN 快照端点一直 503；
#   * tokenizers 缺失 → 算力页「模型启动失败」。
#
# 所以装完必须**当场核一遍**：读 .venv/src/*/pyproject.toml 的
# [project].dependencies，逐条查**已装**发行版（不查索引 → 离线可用），
# 并**真的 import 一次** —— 「装了但 import 失败」正是 v4l2 那类故障，
# find_spec 查不出来。
#
# 判定在 scripts/check_sdk_deps.py（可独立跑）。注意审计读的是**源码树**的
# pyproject：它可能比已装版本新 —— 这正是我们要的（提前把新依赖报出来）。
# ---------------------------------------------------------------------------
audit_sdk_deps() {
    local checker="$SCRIPT_DIR/scripts/check_sdk_deps.py"
    if [ ! -f "$checker" ]; then
        echo "WARN: 找不到 $checker，跳过依赖审计" >&2
        return 0
    fi
    echo "==> 审计 SDK 声明的依赖是否真的可用 (scripts/check_sdk_deps.py)"
    ( cd "$SCRIPT_DIR" && python "$checker" )
}

run_pkg wdnav  "git+ssh://git@10.10.10.10/wardner/wdnav.git#egg=wdnav"
run_pkg wdnai  "git+ssh://git@10.10.10.10/wardner/wdnai.git#egg=wdnai"
run_pkg wdndds "git+ssh://git@10.10.10.10/wardner/wdndds.git#egg=wdndds"
run_pkg wdnhid "git+ssh://git@10.10.10.10/wardner/wdnhid.git#egg=wdnhid"

# 应用插件包（wdnwebui 的卡片）。放在 SDK 之后：wdnapps_hdmi_processor 的
# usb_hid 卡 import wdnhid / wdnav，先有它们再装插件，日志里的报错才不会
# 指向"还没装的 SDK"这种假线索。
#
# **包名必须用下划线**（= 目录名 = pip 落盘的 dist-info / .pth 名）。
# pip 的规范名是连字符的 wdnapps-video-analyzer，但 site-packages 上实际
# 生成的是 wdnapps_video_analyzer-0.1.0.dist-info 和
# __editable__.wdnapps_video_analyzer-0.1.0.pth。purge_install_traces 全靠
# $pkg 拼这些 glob —— 传连字符名会让清理静默落空，旧 .pth 越攒越多，
# 而那正是文件头 v1.3 要根治的"实体目录遮挡 editable 安装"问题。
run_pkg wdnapps_video_analyzer "git+ssh://git@10.10.10.10/wardner/wdnapps_video_analyzer.git#egg=wdnapps_video_analyzer"
run_pkg wdnapps_hdmi_processor "git+ssh://git@10.10.10.10/wardner/wdnapps_hdmi_processor.git#egg=wdnapps_hdmi_processor"

# 依赖审计放在**所有包都处理完之后**：这时 venv 的状态才是最终状态
# （前面的包可能刚被重装，依赖也可能刚被 pip 换掉）。审计失败不改 .venv，
# 所以不打断已经跑完的更新，只影响本脚本的退出码。
DEPS_AUDIT_OK=1
audit_sdk_deps || DEPS_AUDIT_OK=0

if [ ${#FAILED_PKGS[@]} -eq 0 ] && [ "$DEPS_AUDIT_OK" = "1" ]; then
    echo "更新完成（wdnav / wdnai / wdndds / wdnhid / wdnapps_video_analyzer / wdnapps_hdmi_processor 全部成功）。"
    exit 0
fi

echo "" >&2
if [ ${#FAILED_PKGS[@]} -ne 0 ]; then
    echo "更新部分完成，以下包未更新：${FAILED_PKGS[*]}" >&2
    echo "其余包已按各自状态处理完毕；上述包解决后重跑 $0 即可。" >&2
fi
if [ "$DEPS_AUDIT_OK" != "1" ]; then
    echo "依赖审计未通过：SDK 声明的依赖有缺失 / 版本不符 / import 失败。" >&2
    echo "  补齐： .venv/bin/pip install -r requirements.txt" >&2
    echo "  再查： .venv/bin/python scripts/check_sdk_deps.py" >&2
fi
exit 1
