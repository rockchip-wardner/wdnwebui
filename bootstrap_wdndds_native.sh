#!/usr/bin/env bash
# Bootstrap wdndds native libraries (one-time, per host).
#
# The wdndds 0.4 wheel does NOT ship libwdndds_cshim.so /
# libwdndds_idl.so — they are source-compiled at pip install time by
# wdndds._build.py (a setuptools build_py subclass). On a fresh box
# after `pip install -e .venv/src/wdndds`, those .so files are missing
# and wdndds_daemon / ComputeRegistry's RpcClient cannot come up
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
#   3. Run `pip install --force-reinstall --no-deps .venv/src/wdndds`
#      to trigger _build.py. We use the non-editable install form
#      here on purpose: setuptools >=64 silently swallows build_py
#      hook failures for `-e` installs (with a deprecation warning),
#      but the non-editable path aborts loudly on failure — exactly
#      what you want for a bootstrap diagnostic.
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

# -- Fast path: both .so files already built ------------------------------
if [[ -f "${CSHIM}" ]] && [[ -f "${IDL}" ]]; then
    log "libwdndds_cshim.so + libwdndds_idl.so already built — skip pip rebuild"
    log "OK"
    exit 0
fi

# -- Step 4: trigger _build.py via non-editable install --------------------
log "running \`pip install --force-reinstall --no-deps ${WDNDDS_SRC}\` as ${ORIG_USER} to compile the .so"
PIP_CMD=()
if [[ "$(id -u)" -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]]; then
    PIP_CMD=(sudo -u "${ORIG_USER}" -H)
fi
PIP_CMD+=("${REPO_ROOT}/.venv/bin/pip" install --force-reinstall --no-deps "${WDNDDS_SRC}")
if ! "${PIP_CMD[@]}"; then
    die "pip install failed. Inspect the error above — common causes:
  - foonathan_memory apt packages not installed (see Step 1)
  - FastDDS 3.6 not vendored at ${NATIVE_LIB} (see Step 2)
  - cmake / g++ missing (apt install cmake g++ make)"
fi

# -- Step 5: ensure the build artefacts are owned by the original user ----
# (the bootstrap script may have run under sudo; if pip ran as root
# the resulting .so + wheel-install site-packages copy are root-owned
# and any later unprivileged rebuild will fail to overwrite them.)
if [[ "$(id -u)" -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]] \
   && id -u "${ORIG_USER}" >/dev/null 2>&1; then
    chown -R "${ORIG_UID}:${ORIG_UID}" \
        "${WDNDDS_SRC}/src/wdndds/dds/_native" \
        "${REPO_ROOT}/.venv/lib/python3.11/site-packages/wdndds" \
        2>/dev/null || true
fi

# -- Step 6: verify --------------------------------------------------------
for so in "${CSHIM}" "${IDL}"; do
    [[ -f "${so}" ]] || die "build hook ran but ${so} is still missing"
done

log "OK — libwdndds_cshim.so + libwdndds_idl.so built and in place"
log "next: sudo .venv/bin/python run.py"