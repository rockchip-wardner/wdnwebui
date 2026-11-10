#!/usr/bin/env bash
# Bootstrap the wdndds native libraries (one-time, per host).
#
# The wdndds 0.4 wheel does NOT ship libwdndds_cshim.so /
# libwdndds_idl.so — they are source-compiled at pip install time by
# wdndds._build.py (a setuptools build_py subclass). On a fresh box
# after `pip install -e .venv/src/wdndds`, those .so files are missing
# and ComputeRegistry's RpcClient cannot come up
# (`FileNotFoundError: wdndds: .../libwdndds_cshim.so not found`).
#
# This script does the missing piece in a series of idempotent steps:
#   1. apt install libfoonathan-memory-dev, libfoonathan-memory0.7.2,
#      patchelf — build-time deps the _build.py cmake configure step
#      needs to resolve foonathan_memory_vendor + patch RUNPATH.
#   2. Check FastDDS 3.6 is vendored under native/lib (hard-fails with
#      the build_native.sh hint if not).
#   3. Write native/bin/nodesize_dbg stub if missing — partial native
#      installs (vendored native/lib + native/include from a tarball
#      without bin/) trip cmake's IMPORTED-target file-existence check
#      and abort find_package(fastdds). The debugger helper itself is
#      never used at runtime (foonathan is statically linked into
#      libfastdds.so).
#   4. Run `pip install --force-reinstall --no-deps -e .venv/src/wdndds`
#      to trigger _build.py.
#   5. Make the build artefacts owned by the original user.
#   6. Patch the PyPI `v4l2` package for py3 compatibility.
#   7. Audit the SDK-declared dependencies (scripts/check_sdk_deps.py).
#   8. Verify the .so artefacts are in place.
#
# v1.4 (2026-10-10) — Step 3 now uses an **editable** install (`-e`). It
# deliberately used a non-editable one before, for two reasons: ① "an
# editable install does not copy the files, so it cannot build the .so" →
# measured wrong (the hook writes its output to the source tree, no matter
# which install form is used); ② "setuptools≥64 silently swallows the hook
# failure for -e" → still true, but it is now covered by the artefact
# assertion of Step 6 + verify_outputs of update_rkwhl.sh. The price of a
# non-editable install (the physical directory in site-packages permanently
# shadows the editable install → worker ImportError) is described in Step 4.
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
→Run the editable install first:
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
# feat(wdndds): add the create_topic_writer/reader streaming helpers),
# the pre-existing .so would still satisfy the existence check, bootstrap
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

# -- Step 4: trigger _build.py via an **editable** install -----------------
#
# v1.4: `-e` **must** be used here. It was a non-editable install before,
# for the reason "an editable install does not copy the files, so it
# cannot build the .so" — that premise is **wrong**: the build_py hook of
# _build.py writes its output to the source tree
# `src/wdndds/dds/_native/` (its `_THIS_DIR`), no matter which install
# form is used. Measured after deleting the .so: an `-e` install rebuilds
# them just fine and creates no physical directory in site-packages.
#
# The price of a non-editable install is very real: it leaves a physical
# directory in site-packages, and site-packages comes **before** the
# editable .pth on sys.path, so that directory permanently shadows the
# editable install; a later git pull + `pip install -e` does not reclaim
# it either (pip only knows the files recorded in its own metadata).
# Observed in practice: the source tree was already 0.9.0 while
# site-packages still held 0.6.0 → enabling the local model on the
# compute page failed with `ImportError: cannot import name
# 'serve_local_session'` in the worker → `compute_worker exited rc=1`,
# and the UI error box was empty (the worker stderr goes to
# /tmp/wdnwebui-worker/<model>.log, not into the PIPE). The root fix is
# to switch to `-e`.
#
# `--force-reinstall` is kept: otherwise pip may decide "already
# installed" and skip the build. pip >= 24 rejects the
# `--force-reinstall` + `-e` combination (PEP 660); drop the flag at that
# point, the hook itself unconditionally unlinks and rebuilds and does not
# depend on it.
log "running \`pip install --force-reinstall --no-deps -e ${WDNDDS_SRC}\` as ${ORIG_USER} to compile the .so"
PIP_CMD=()
if [[ "$(id -u)" -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]]; then
    PIP_CMD=(sudo -u "${ORIG_USER}" -H)
fi
PIP_CMD+=("${REPO_ROOT}/.venv/bin/pip" install --force-reinstall --no-deps -e "${WDNDDS_SRC}")
if ! "${PIP_CMD[@]}"; then
    die "pip install failed. Inspect the error above — common causes:
  - the foonathan_memory apt packages are not installed (see Step 1)
  - FastDDS 3.6 is not vendored at ${NATIVE_LIB} (see Step 2)
  - cmake / g++ are missing (apt install cmake g++ make)
  - pip >= 24 rejects the '--force-reinstall -e' combination → drop --force-reinstall and retry"
fi

# Remove the physical directory left behind by an **earlier** non-editable
# install. No new ones are produced after the switch to -e, but machines
# that ran the old bootstrap may still have one — it shadows the freshly
# installed editable one and has to go. Only this one known path is
# deleted, no other package is touched.
STALE_SP="${REPO_ROOT}/.venv/lib/python3.11/site-packages/wdndds"
if [[ -d "${STALE_SP}" ]]; then
    warn "found the leftover of a non-editable install ${STALE_SP} (it shadows the editable install), removed it"
    rm -rf "${STALE_SP}"
fi

# -- Step 5: ensure the build artefacts are owned by the original user ----
# (the bootstrap script may have run under sudo; if pip ran as root
# the resulting .so files would be root-owned and any later
# unprivileged rebuild would fail to overwrite them.)
#
# v1.4: with the editable install the artefacts only live in the **source
# tree**, on the site-packages side pip only writes the `.pth` +
# `dist-info` as ORIG_USER, so no extra chown is needed. The old version
# also chowned the physical `site-packages/wdndds` directory — that
# directory does not exist with an editable install, the chown was
# swallowed by `|| true` and only left a misleading trace.
if [[ "$(id -u)" -eq 0 ]] && [[ -n "${SUDO_USER:-}" ]] \
   && id -u "${ORIG_USER}" >/dev/null 2>&1; then
    chown -R "${ORIG_UID}:${ORIG_UID}" \
        "${WDNDDS_SRC}/src/wdndds/dds/_native" \
        2>/dev/null || true
fi

# -- Step 6: the py3 compatibility patch for v4l2 (PyPI) -------------------
#
# wdnav/src/wdnav/v4l2.py does a module level `import v4l2` (the third
# party PyPI package). The SDK is installed with `--no-deps`, so it does
# not come into the venv on its own — requirements.txt lists
# `v4l2>=0.2` explicitly. But upstream 0.2 (2014) still has the Python 2
# idiom `range(...) + [...]`, which raises a TypeError at **import** time
# under py3 (**not** an ImportError), so it is installed and still
# unusable: the HDMI-IN snapshot endpoint degrades from 503 to 500.
#
# An idempotent patch is applied here (the same script is also wired into
# update_rkwhl.sh). When v4l2 is not installed (the requirements have not
# been synced yet) it only warns and does not fail — v4l2 only affects
# the HDMI-IN chain and must not drag the whole bootstrap down.
if [[ -f "${REPO_ROOT}/scripts/fix_v4l2_py3.py" ]]; then
    if [[ -f "${REPO_ROOT}/.venv/bin/python" ]]; then
        log "patching v4l2 for py3 compatibility (scripts/fix_v4l2_py3.py)"
        if ! "${REPO_ROOT}/.venv/bin/python" "${REPO_ROOT}/scripts/fix_v4l2_py3.py"; then
            warn "v4l2 py3 compatibility patch did not succeed — HDMI-IN capture stays unavailable."
            warn "  fix manually: .venv/bin/python scripts/fix_v4l2_py3.py"
        fi
    else
        warn "no .venv/bin/python yet — skipping the v4l2 py3 patch"
    fi
fi

# -- Step 7: audit the SDK-declared dependencies ---------------------------
#
# The SDKs are installed with `--no-deps`, so the dependencies **they
# declare** do not come into the venv. The symptom is far away from the
# cause: v4l2 missing → the HDMI-IN endpoint returns 503 forever;
# tokenizers missing → the compute page reports "the model failed to
# start".
#
# check_sdk_deps.py reads the [project].dependencies of the
# .venv/src/*/pyproject.toml, looks up every **installed** distribution
# (importlib.metadata, never the index → works offline) and **really
# imports it once** — "installed but the import fails" is exactly the
# class of failure that v4l2 represents, and find_spec does not catch it.
#
# This does not die here: the job of bootstrap (the .so of wdndds) is
# already done, a missing dependency is another chain's business. But it
# **has to warn loudly** and give the fix command — otherwise the pit of
# that chain is only noticed when the user clicks on the page.
if [[ -f "${REPO_ROOT}/scripts/check_sdk_deps.py" ]] \
   && [[ -f "${REPO_ROOT}/.venv/bin/python" ]]; then
    log "auditing SDK-declared dependencies (scripts/check_sdk_deps.py)"
    if ! "${REPO_ROOT}/.venv/bin/python" "${REPO_ROOT}/scripts/check_sdk_deps.py"; then
        warn "the SDK dependency audit did not pass — the declared dependencies are missing / broken."
        warn "  fix:    ${REPO_ROOT}/.venv/bin/pip install -r ${REPO_ROOT}/requirements.txt"
        warn "  recheck: ${REPO_ROOT}/.venv/bin/python ${REPO_ROOT}/scripts/check_sdk_deps.py"
    fi
else
    warn "scripts/check_sdk_deps.py or .venv/bin/python missing — skipping the dependency audit"
fi

# -- Step 8: verify --------------------------------------------------------
for so in "${CSHIM}" "${IDL}"; do
    [[ -f "${so}" ]] || die "the build hook ran but ${so} is still missing"
done

log "OK — libwdndds_cshim.so + libwdndds_idl.so built and in place"
log "next: sudo .venv/bin/python run.py"
