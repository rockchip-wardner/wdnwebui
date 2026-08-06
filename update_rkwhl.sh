#!/usr/bin/env bash
# 更新 .venv/src 下以 editable 模式安装的 rkv / rkai / wdndds
# 用法：./update_rkwhl.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_SRC="$SCRIPT_DIR/.venv/src"
PIP_TARGET=".venv/bin/python -m pip"

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

    if [ "$before" != "$after" ] && git -C "$dir" diff --name-only "$before" "$after" -- pyproject.toml | grep -q .; then
        echo "==> [$pkg] pyproject.toml 已变更，重新执行 pip install -e 同步元数据"
        ( cd "$SCRIPT_DIR" && $PIP_TARGET install -e "$repo_url" )
    else
        echo "==> [$pkg] 无 pyproject.toml 变更，跳过 pip install"
    fi
}

update_pkg rkv  "git+ssh://git@10.10.10.10/pythonsdk/pyrkv.git#egg=rkv"
update_pkg rkai "git+ssh://git@10.10.10.10/pythonsdk/pyrkai.git#egg=rkai"
update_pkg wdndds "git+ssh://git@10.10.10.10/pythonsdk/wdndds.git#egg=wdndds"

echo "更新完成。"
