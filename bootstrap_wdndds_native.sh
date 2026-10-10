#!/usr/bin/env bash
# Bootstrap wdndds native libraries (one-time, per host).
#
# The wdndds 0.4 wheel does NOT ship libwdndds_cshim.so /
# libwdndds_idl.so — they are source-compiled at pip install time by
# wdndds._build.py (a setuptools build_py subclass). On a fresh box
# after `pip install -e .venv/src/wdndds`, those .so files are missing
# and ComputeRegistry's RpcClient cannot come up
# (`FileNotFoundError: wdndds: .../libwdndds_cshim.so not found`).
#
# This script does the missing piece in three idempotent steps:
#   1. apt install libfoonathan-memory-dev, libfoonathan-memory0.7.2,
#      patchelf — build-time deps the _build.py cmake configure step
#      needs to resolve foonathan_memory_vendor + patch RUNPATH.
#   2. Write native/bin/nodesize_dbg stub if missing — partial native
#      installs (vendored native/lib + native/include from a tarball
#      without bin/) trip cmake's IMPORTED-target file-existence check
#      and abort find_package(fastdds). The debugger helper itself is
#      never used at runtime (foonathan is statically linked into
#      libfastdds.so).
#   3. Run `pip install --force-reinstall --no-deps -e .venv/src/wdndds`
#      to trigger _build.py.
#
# v1.4 (2026-10-10) — Step 3 改用 **editable**（`-e`）。此前刻意用非
# editable，理由有二：①「editable 不 copy 文件所以编不出 .so」→ 实测错误
# （钩子写死源码树输出，与 install 形式无关）；②「setuptools≥64 对 -e 静默
# 吞掉 hook 失败」→ 仍成立，但已由 Step 6 的产物断言 + update_rkwhl.sh 的
# verify_outputs 覆盖。非 editable 的代价（site-packages 实体目录永久遮挡
# editable 安装 → worker ImportError）详见 Step 4。
#
# Idempotent: skips any step that's already done. Fast path when
# everything is in place takes <1s.
#
# Requires sudo (for the apt step only).
#
# Usage:
#   sudo ./bootstrap_wdndds_native.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WDNDDS_SRC="${REPO_ROOT}/.venv/src/wdndds"
NATIVE_LIB="${WDNDDS_SRC}/native/lib"
NATIVE_BIN="${WDNDDS_SRC}/native/bin"
CSHIM="${WDNDDS_SRC}/src/wdndds/dds/_native/libwdndds_cshim.so"
IDL="${WDNDDS_SRC}/src/wdndds/dds/_native/libwdndds_idl.so"

# Capture the original user (sudo sets SUDO_USER); we drop back to
# that user for the pip + chmod steps so the resulting .so files end
# up owned by the human, not by root. That way a later unprivileged
# `pip install --force-reinstall --no-deps .venv/src/wdndds` can
# overwrite them without sudo.
ORIG_USER="${SUDO_USER:-$(id -un)}"
ORIG_UID="${SUDO_UID:-$(id -u)}"
ORIG_HOME="$(getent passwd "$ORIG_USER" | cut -d: -f6 || echo "$HOME")"

log() { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[bootstrap]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

[[ -d "${WDNDDS_SRC}" ]] || die \
    "wdndds source not found at ${WDNDDS_SRC}.
Run the editable install first:
    .venv/bin/pip install -e .venv/src/wdndds"

# -- Step 1: apt build deps -----------------------------------------------
NEED_APT=()
for pkg in libfoonathan-memory-dev libfoonathan-memory0.7.2 patchelf; do
    dpkg -s "$pkg" >/dev/null 2>&1 || NEED_APT+=("$pkg")
done
if (( ${#NEED_APT[@]} > 0 )); then
    [[ "$(id -u)" -eq 0 ]] || die \
        "apt deps missing (${NEED_APT[*]}). Re-run with sudo."
    log "installing apt deps: ${NEED_APT[*]}"
    apt-get update
    apt-get install -y "${NEED_APT[@]}"
else
    log "apt deps present (libfoonathan-memory-dev, libfoonathan-memory0.7.2, patchelf)"
fi

# -- Step 2: FastDDS 3.6 vendored at native/lib ----------------------------
if [[ ! -f "${NATIVE_LIB}/libfastdds.so.3.6.2.0" ]] \
   || [[ ! -f "${NATIVE_LIB}/libfastcdr.so.2.3.6" ]]; then
    die "FastDDS 3.6 not vendored at ${NATIVE_LIB}.

The wdndds _build.py hook links the cshim against libfastdds.so.3.6 +
libfastcdr.so.2; both must be present before pip can compile.

Run ONCE (~20-30 min on a board; ~6 min on x86_64):
    sudo bash ${WDNDDS_SRC}/build_native.sh

That script downloads + builds FastDDS 3.6.2, FastCDR 2.3.6,
foonathan_memory_vendor 1.4.1, FastDDS-Gen 4.3.0, then the wdndds IDL
library + cshim into ${NATIVE_LIB}. After it finishes, re-run this
bootstrap."
fi
log "FastDDS 3.6 vendored at ${NATIVE_LIB}"

# -- Step 3: nodesize_dbg stub for partial installs ------------------------
if [[ ! -f "${NATIVE_BIN}/nodesize_dbg" ]]; then
    log "writing stub at ${NATIVE_BIN}/nodesize_dbg (partial-install workaround)"
    mkdir -p "${NATIVE_BIN}"
    cat > "${NATIVE_BIN}/nodesize_dbg" <<'STUB'
#!/bin/sh
# Stub for foonathan_memory_vendor's foonathan_memory_node_size_debugger
# imported target. The actual node-size profiler is not used at runtime
# (foonathan is statically linked into libfastdds.so). This file exists
# only to satisfy cmake's IMPORTED-target file-existence check in
# native/lib/foonathan_memory/cmake/foonathan_memory-config.cmake.
exec /bin/true
STUB
    chmod +x "${NATIVE_BIN}/nodesize_dbg"
fi

# -- Always rebuild: no fast-path skip -----------------------------------
#
# v0.6.1: the previous "if both .so exist, exit 0" fast path was a
# silent correctness trap — after a `git pull` of wdndds source that
# added new bindings to _cshim.py (e.g. commit 871d00f
# feat(wdndds): 新增 create_topic_writer/reader 流式分片助手), the
# pre-existing .so would still satisfy the existence check, bootstrap
# would exit 0, and the user would later hit
# `AttributeError: undefined symbol: wdndds_topic_writer_create` from
# the wrapper. The fix lives in two places:
#
#   1. `_build.py::build_py.run()` unconditionally `unlink()`s both
#      .so files at the start of every invocation, so any
#      `pip install` (editable or not) re-creates them from scratch
#      against the current cshim_src + _cshim.py.
#   2. bootstrap no longer skips pip when .so files exist, so even
#      a user who ONLY ran bootstrap (no `pip install -e` after
#      `git pull`) gets a fresh build. ~20s cost on every invocation.
#
# For production images that pre-stage the .so files out-of-band, gate
# the bootstrap itself with `WDNWEBUI_SKIP_WDNDDS_BOOTSTRAP=1`
# (see run.py). Don't bypass bootstrap from inside this script.

# -- Step 4: trigger _build.py via **editable** install ---------------------
#
# v1.4：这里**必须**用 `-e`。此前是非 editable，理由是「editable 不 copy
# 文件所以编不出 .so」——实测该前提**错误**：_build.py 的 build_py 钩子
# 把输出写死在源码树 `src/wdndds/dds/_native/`（其 `_THIS_DIR`），与
# install 形式无关。删掉 .so 后实测 `-e` 装照常重建，且不产生
# site-packages 实体目录。
#
# 而非 editable 的代价很实在：它会在 site-packages 留一个实体目录，而
# site-packages 在 sys.path 上排在 editable 的 .pth **之前**，于是该目录
# 永久遮挡 editable 安装；后续 git pull + `pip install -e` 也不会回收它
# （pip 只认自己元数据里记的文件）。实测踩过：源码树已是 0.9.0，
# site-packages 里却仍是 0.6.0 → 算力页启用本地模型时 worker
# `ImportError: cannot import name 'serve_local_session'` →
# `compute_worker exited rc=1`，且 UI 错误框为空（worker stderr 落
# /tmp/wdnwebui-worker/<model>.log，不进 PIPE）。根治即改用 `-e`。
#
# `--force-reinstall` 保留：否则 pip 可能判定「已安装」而跳过构建。
# pip >= 24 拒绝 `--force-reinstall` + `-e` 组合（PEP 660）；届时去掉
# 该 flag 即可，钩子本身无条件 unlink + 重建，不依赖它。
log "running \`pip install --force-reinstall --no-deps -e ${WDNDDS_SRC}\` as ${ORIG_USER} to compile the .so"
PIP_CMD=()
if [[ "$(id -u)" -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]]; then
    PIP_CMD=(sudo -u "${ORIG_USER}" -H)
fi
PIP_CMD+=("${REPO_ROOT}/.venv/bin/pip" install --force-reinstall --no-deps -e "${WDNDDS_SRC}")
if ! "${PIP_CMD[@]}"; then
    die "pip install failed. Inspect the error above — common causes:
  - foonathan_memory apt packages not installed (see Step 1)
  - FastDDS 3.6 not vendored at ${NATIVE_LIB} (see Step 2)
  - cmake / g++ missing (apt install cmake g++ make)
  - pip >= 24 拒绝 '--force-reinstall -e' 组合 → 去掉 --force-reinstall 重试"
fi

# 清掉**更早**的非 editable 安装残留实体目录。本次改成 -e 之后不会再
# 产生新的，但历史上跑过旧版 bootstrap 的机器上可能还留着——它会遮挡
# 刚装好的 editable 安装，必须删。只删这一个确定路径，不动其它包。
STALE_SP="${REPO_ROOT}/.venv/lib/python3.11/site-packages/wdndds"
if [[ -d "${STALE_SP}" ]]; then
    warn "发现非 editable 安装残留 ${STALE_SP}（会遮挡 editable 安装），已删除"
    rm -rf "${STALE_SP}"
fi

# -- Step 5: ensure the build artefacts are owned by the original user ----
# (the bootstrap script may have run under sudo; if pip ran as root
# the resulting .so files would be root-owned and any later
# unprivileged rebuild would fail to overwrite them.)
#
# v1.4：改用 editable 后产物只在**源码树**里，site-packages 侧只剩 pip 以
# ORIG_USER 身份写下的 `.pth` + `dist-info`，无需额外 chown。原先还 chown
# 了 `site-packages/wdndds` 实体目录——editable 下该目录不存在，chown 会
# 被 `|| true` 静默吞掉，留着只会误导。
if [[ "$(id -u)" -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]] \
   && id -u "${ORIG_USER}" >/dev/null 2>&1; then
    chown -R "${ORIG_UID}:${ORIG_UID}" \
        "${WDNDDS_SRC}/src/wdndds/dds/_native" \
        2>/dev/null || true
fi

# -- Step 6: verify --------------------------------------------------------
for so in "${CSHIM}" "${IDL}"; do
    [[ -f "${so}" ]] || die "build hook ran but ${so} is still missing"
done

log "OK — libwdndds_cshim.so + libwdndds_idl.so built and in place"
log "next: sudo .venv/bin/python run.py"