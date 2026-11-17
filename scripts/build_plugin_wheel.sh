#!/usr/bin/env bash
# Build a plugin wheel from a THROWAWAY COPY of the source tree.
#
# ⛔ 为什么必须拷贝一份再构建（2026-11-17 踩过的坑）
#
# 宿主用 uvicorn 的 reload 盯着 `.venv/src/`（见 run.py 的 reload_dirs），
# 监听的是**源码目录本身**。而 `pip wheel .` 会跑 setuptools 的
# build_wheel，在工程目录旁边写出 `build/` —— 里面全是 .py 副本
# （build/lib/**.py、build/bdist.*/**.py）。
#
# 每一个 .py 写入都会让 WatchFiles 触发一次 reload，后果是：
#   - Web UI 进程重启，运行期状态全丢（已启用的卡片 / registry / 实例表）
#   - compute worker 被 SIGTERM，NPU 上的模型被回收
#   - 但显存释放是异步的：状态接口已经说"未启用"，显存却还占着，
#     此时再点"启用"就可能重复加载 ⇒ 显存超限
#
# 所以：**wheel 一律在临时副本里构建**，被监听的源码树保持干净。
#
# ✅ `pip install -e .` 是安全的，不需要副本
#
# PEP 660 的 setuptools editable 安装往源码树里**什么都不写**
# （没有 .py，也没有 .egg-info —— 用一个 scratch 包实测过）。
# 所以刷新 entry point / package-data 仍然可以直接在原地装。
#
# 用法：
#   scripts/build_plugin_wheel.sh                  # 构建 wheel → /tmp/wdnwebui_wheels
#   scripts/build_plugin_wheel.sh --install        # 构建 + 原地 pip install -e（安全）
#   scripts/build_plugin_wheel.sh --list           # 顺便打印 wheel 内文件清单
#   scripts/build_plugin_wheel.sh -p wdnai         # 换插件（默认 wdnapps_audio_chat）
#   scripts/build_plugin_wheel.sh -o /tmp/out       # 换输出目录
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -P "${SCRIPT_DIR}/.." && pwd)"
VENV_PY="${ROOT}/.venv/bin/python"

PLUGIN="wdnapps_audio_chat"
OUTDIR="/tmp/wdnwebui_wheels"
DO_INSTALL=0
DO_LIST=0

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--plugin) PLUGIN="${2:?--plugin 需要参数}"; shift 2 ;;
    -o|--out)    OUTDIR="${2:?--out 需要参数}"; shift 2 ;;
    --install)   DO_INSTALL=1; shift ;;
    --list)      DO_LIST=1; shift ;;
    -h|--help)   sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $1（-h 看用法）" >&2; exit 2 ;;
  esac
done

# checkout 目录名未必等于仓库名：仓库叫 ``wdnapps_audio_chat``（下划线），
# 本板的 checkout 目录是 ``wdnapps-audio-chat``（连字符）⇒ 两种都试。
SRC=""
for _cand in "${ROOT}/.venv/src/${PLUGIN}" "${ROOT}/.venv/src/${PLUGIN//_/-}"; do
    if [ -d "${_cand}" ]; then SRC="${_cand}"; break; fi
done
[ -n "${SRC}" ] || { echo "插件目录不存在: ${ROOT}/.venv/src/${PLUGIN}（也没找到连字符变体）" >&2; exit 1; }
[ -x "${VENV_PY}" ] || { echo "找不到虚拟环境: ${VENV_PY}" >&2; exit 1; }

mkdir -p "${OUTDIR}"
TMP="$(mktemp -d -t wdnplugin-XXXXXX)"
trap 'rm -rf "${TMP}"' EXIT

# 只拷贝构建需要的东西，顺带把上一轮构建留下的产物剔掉。
tar -C "${SRC}" \
    --exclude='./.git' \
    --exclude='./build' \
    --exclude='./dist' \
    --exclude='*.egg-info' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    -cf - . | tar -C "${TMP}" -xf -

echo "==> 在临时副本中构建 ${PLUGIN}（源码树保持干净，不触发热重载）"
( cd "${TMP}" && "${VENV_PY}" -m pip wheel . --no-deps --no-build-isolation -w "${OUTDIR}" -q )

WHEEL="$(ls -t "${OUTDIR}"/${PLUGIN//-/_}-*.whl 2>/dev/null | head -1 || true)"
[ -n "${WHEEL}" ] || WHEEL="$(ls -t "${OUTDIR}"/*.whl | head -1)"
echo "==> wheel: ${WHEEL}"

if [ "${DO_LIST}" -eq 1 ]; then
  echo "==> wheel 内容："
  "${VENV_PY}" -c "
import sys, zipfile
for n in sorted(zipfile.ZipFile(sys.argv[1]).namelist()):
    print('   ', n)
" "${WHEEL}"
fi

if [ "${DO_INSTALL}" -eq 1 ]; then
  # 原地 editable 安装：PEP 660 不往源码树写 .py，安全，不会触发热重载。
  echo "==> 原地刷新 editable 安装：${SRC}"
  ( cd "${SRC}" && "${VENV_PY}" -m pip install -e . --no-deps --no-build-isolation -q )
  # 万一某个 setuptools 版本又写了 build/，顺手清掉，免得下次构建白挨一次 reload。
  if [ -d "${SRC}/build" ]; then
    echo "==> 清理残留 build/ 目录"
    rm -rf "${SRC}/build"
  fi
fi

echo "==> 完成。被监听的源码树里没有新增 .py，Web UI 不会热重载。"
