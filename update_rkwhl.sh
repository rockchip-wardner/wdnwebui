#!/usr/bin/env bash
# 更新 .venv/src 下以 editable 模式安装的 wdnav / wdnai / wdndds
# 用法：./update_rkwhl.sh
#
# 设计要点（v1 起）：
#   * git pull 之后**无条件**对三个 SDK 都跑
#     `pip install --no-deps -e <path>`，强制触发
#     wdndds._build.py / wdnav._build_wdnav.py 的 .so rebuild hook。
#   * 注意：pip >= 24 拒绝 `--force-reinstall` + `-e` 组合（PEP 660
#     后 editable install 不再支持该 flag）；且所有非 editable 标志
#     必须放在 `-e` 之前，否则 pip 26.x 会把它们当成 editable
#     requirement 报 "is not a valid editable requirement"。
#   * 旧版只检测 `pyproject.toml` 变更；wrapper-only commit（典型：
#     在 _cshim.py 加 ctypes 绑定）会被漏掉，导致 stale .so + 运行时
#     `undefined symbol`。HEAD 推进就 rebuild，~5-20s 代价换彻底
#     关闭 silent-stale 窗口。
#   * 强制 rebuild 的兜底用环境变量：
#       UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh
#     （HEAD 没动也想 rebuild，比如刚改了 _build.py 自身时用）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_SRC="$SCRIPT_DIR/.venv/src"
PIP_TARGET=".venv/bin/python -m pip"
FORCE="${UPDATE_RKWHL_FORCE_REBUILD:-0}"

update_pkg() {
    local pkg="$1"
    local repo_url="$2"
    local dir="$VENV_SRC/$pkg"

    if [ ! -d "$dir" ]; then
        echo "==> [$pkg] 未找到 $dir，执行 pip install -e 安装"
        ( cd "$SCRIPT_DIR" && $PIP_TARGET install -e "$repo_url" )
        return 0
    fi

    echo "==> [$pkg] git pull"
    local before
    before=$(git -C "$dir" rev-parse HEAD)
    git -C "$dir" pull --ff-only
    local after
    after=$(git -C "$dir" rev-parse HEAD)

    if [ "$before" != "$after" ] || [ "$FORCE" = "1" ]; then
        local reason
        if [ "$before" != "$after" ]; then
            reason="HEAD ${before:0:7}→${after:0:7}"
        else
            reason="UPDATE_RKWHL_FORCE_REBUILD=1"
        fi
        echo "==> [$pkg] ${reason}，强制 pip install --no-deps -e 触发 build hook"
        # 直接传本地路径而非 $repo_url，避免 SSH 重新 clone。_build.py
        # / _build_wdnav.py 的 cmdclass 内部对 .so 做 os.unlink() +
        # 重新编译，所以即使没有 --force-reinstall 也会强制覆盖 stale
        # .so；若 build 失败旧 .so 已被删，运行时 import 抛
        # FileNotFoundError——比 silent undefined symbol 容易诊断。
        ( cd "$SCRIPT_DIR" && $PIP_TARGET install --no-deps -e "$dir" )
    else
        echo "==> [$pkg] HEAD 未推进，跳过 rebuild"
    fi
}

update_pkg wdnav  "git+ssh://git@10.10.10.10/wardner/wdnav.git#egg=wdnav"
update_pkg wdnai  "git+ssh://git@10.10.10.10/wardner/wdnai.git#egg=wdnai"
update_pkg wdndds "git+ssh://git@10.10.10.10/wardner/wdndds.git#egg=wdndds"

echo "更新完成。"
