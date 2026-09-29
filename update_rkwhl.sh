#!/usr/bin/env bash
# 更新 .venv/src 下以 editable 模式安装的 wdnav / wdnai / wdndds
# 用法：./update_rkwhl.sh
#
# 设计要点：
#   * v1.2 起：git pull 之后对三个 SDK 都**无条件**跑
#     `pip install -v --no-deps -e <path>`，让 wdndds._build.py /
#     wdnav._build_wdnav.py 的 build_py 钩子内部无条件 unlink + 重建 .so
#     （v0.3.1 / v0.6.1 起的 hook 行为）。不依赖 git HEAD 是否变化——
#     HEAD 未变也可能 .so 不新鲜（典型：改了 cshim_src / 改了 _cshim.py
#     的 ctypes 绑定 / 改了 _build.py 自身），HEAD 变化只是诊断信息。
#   * 注意：pip >= 24 拒绝 `--force-reinstall` + `-e` 组合（PEP 660
#     后 editable install 不再支持该 flag）；且所有非 editable 标志
#     必须放在 `-e` 之前，否则 pip 26.x 会把它们当成 editable
#     requirement 报 "is not a valid editable requirement"。
#   * v1.1 起：每次 pip install -e 之后**断言 .so 落地**——
#     setuptools ≥64 会静默吞掉 build_py 钩子的 RuntimeError，导致
#     wdndds / wdnav 的 cshim 编译失败时 pip 仍然返回 0、日志全绿。
#     缺产物时脚本打印「pip stderr 末尾 60 行」并 exit 1。

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
else
    echo "==> 未找到 requirements.txt，跳过 web 依赖同步"
fi

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

update_pkg() {
    local pkg="$1"
    local repo_url="$2"
    local dir="$VENV_SRC/$pkg"

    # 捕获 pip stderr 到临时文件，install 之后用于把 hook 失败的 traceback
    # 重新打出来。-v 让 setuptools 把 build_py 钩子内部的 print/stderr
    # 转发到我们的 log；setuptools ≥64 把 RuntimeError 当 warning 时仍会
    # 走 print 输出，再由 verify_outputs 通过 .so 缺失间接判定失败。
    local pip_log
    pip_log="$(mktemp -t update_rkwhl.XXXXXX.log)"
    # mktemp 在某些 coreutils 老版本没有 -t；兜底用 /tmp
    [ -n "$pip_log" ] || pip_log="/tmp/update_rkwhl.$$.log"
    trap "rm -f '$pip_log'" RETURN

    # v1.2 起：每次 update 都**无条件**跑 ``pip install -v --no-deps -e``，
    # 不再根据 git HEAD 是否变化决定是否触发 rebuild。理由——
    #   * wdndds._build.py / wdnav._build_wdnav.py 的 build_py 钩子内部
    #     已经会 os.unlink() 旧 .so 后无条件重新编译（v0.3.1 / v0.6.1
    #     之后的行为），build 产物本来就是 fresher-than-HEAD 的；
    #   * HEAD 未推进 ≠ .so 仍是新的 —— 改了 _build.py / 改了 cshim_src /
    #     改了 _cshim.py 的 ctypes 绑定（典型 wrapper-only commit）这些
    #     不会动 pyproject.toml，但 .so 必须重建；
    #   * 让 update_rkgwl.sh 默认"幂等且保守"：跑一次就保证 .so 是最新，
    #     不再需要 UPDATE_RKWHL_FORCE_REBUILD=1 这种 hint。
    # HEAD 变化仍会打 log 给运维看，仅作 diagnostic，不再是 gate。
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
    local before
    before=$(git -C "$dir" rev-parse HEAD)
    git -C "$dir" pull --ff-only
    local after
    after=$(git -C "$dir" rev-parse HEAD)

    local reason
    if [ "$before" != "$after" ]; then
        reason="HEAD ${before:0:7}→${after:0:7}"
    else
        reason="HEAD 未变，但强制 rebuild"
    fi
    echo "==> [$pkg] ${reason}，pip install -v --no-deps -e 触发 build hook（hook 内部无条件 unlink + 重建）"
    # 直接传本地路径而非 $repo_url，避免 SSH 重新 clone。_build.py
    # / _build_wdnav.py 的 cmdclass 内部对 .so 做 os.unlink() +
    # 重新编译（v0.3.1 / v0.6.1 起无条件），所以即使 HEAD 未变也会
    # 强制覆盖 stale .so；若 build 失败旧 .so 已被删，运行时 import 抛
    # FileNotFoundError——比 silent undefined symbol 容易诊断。
    # 这里额外加 -v 把 setuptools 吞掉的 hook traceback 抓到 $pip_log，
    # verify_outputs 在 .so 缺失时把它打到 stderr。
    if ! ( cd "$SCRIPT_DIR" && $PIP_TARGET install -v --no-deps -e "$dir" ) 2> "$pip_log"; then
        echo "FATAL: [$pkg] pip install 自身失败（exit≠0）" >&2
        tail -n 60 "$pip_log" >&2 || true
        return 1
    fi
    verify_outputs "$pkg" "$pip_log" || return 1
}

update_pkg wdnav  "git+ssh://git@10.10.10.10/wardner/wdnav.git#egg=wdnav"
update_pkg wdnai  "git+ssh://git@10.10.10.10/wardner/wdnai.git#egg=wdnai"
update_pkg wdndds "git+ssh://git@10.10.10.10/wardner/wdndds.git#egg=wdndds"

echo "更新完成。"
