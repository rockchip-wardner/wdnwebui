#!/usr/bin/env bash
# Update the wdnav / wdnai / wdndds / wdnhid SDKs and the four application
# plugin packages wdnapps_video_analyzer / wdnapps_hdmi_processor /
# wdnapps_audio_chat / wdnapps_media_generator, all installed in editable
# mode under .venv/src
# Usage: ./update_rkwhl.sh
#
# The flow per package (since v1.3):
#   1. `git pull --ff-only` to fetch the latest code
#   2. Compare the git HEAD before and after the pull
#   3. HEAD unchanged → **do nothing at all** (the .venv is untouched, pip
#      does not run)
#   4. HEAD changed → first completely uninstall the old version
#      (purge_install_traces), then reinstall it with `pip install -e`; the
#      compile / .so landing assertion logic stays as it was
#
# Design notes:
#   * The **git HEAD** is the only update signal, not the pyproject version
#     number — in practice the repo version of the three packages stays
#     equal to the installed version for a long time, and a lot of commits
#     that change cshim_src / _build.py do not bump the version number, so
#     comparing versions would silently skip exactly the upgrades that
#     should rebuild the .so. The price: a documentation-only commit also
#     triggers a reinstall — in exchange, nothing is missed.
#   * Uninstall before upgrading (purge_install_traces). pip does not reclaim
#     the physical directory left behind by an earlier non-editable install
#     just because an editable one was installed later, and that physical
#     directory comes before the .pth on sys.path and therefore shadows the
#     editable install permanently. The root fix lives in
#     bootstrap_wdndds_native.sh v1.4 (switched to -e); purge is only the
#     self-healing fallback.
#   * pip >= 24 rejects the `--force-reinstall` + `-e` combination (PEP
#     660); and the non-editable flag must come before `-e`, otherwise pip
#     26.x treats it as an editable requirement and errors out.
#   * After every install we **assert that the .so landed** — setuptools
#     >=64 silently swallows the RuntimeError of the build_py hook: pip
#     returns 0, the log is all green, and the artifact was never built.
#   * The plugin packages (wdnapps_*) have no .so, so they assert the
#     **entry point → manifest id → static/ chain** instead. That assertion
#     blocks another silent failure: when the entry point name uses
#     underscores while the manifest "id" uses hyphens, pip reports success
#     all the way through, but the host skips those cards as FATAL at
#     startup — the page simply has no cards and the log holds a single
#     ERROR line.
#   * Force a rebuild: `UPDATE_RKWHL_FORCE_REBUILD=1 ./update_rkwhl.sh`.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_SRC="$SCRIPT_DIR/.venv/src"

# ---------------------------------------------------------------------------
# Package source (can be overridden from the outside)
#
# The default is the company intranet mirror — boards in the field often
# have no internet access and can only use the intranet.
# The public one-shot installer (https://github.com/rockchip-wardner/wardner)
# sets WARDNER_PKG_BASE to https://github.com/rockchip-wardner to switch to
# the public source.
#
# This variable is only really used on the "directory does not exist, fall
# back to installing from the URL" path: packages that were cloned before
# use `git pull` (with the remote they were cloned with), which has nothing
# to do with this.
#
#   intranet mirror: git+ssh://git@10.10.10.10/wardner
#   public GitHub:   git+https://github.com/rockchip-wardner
# ---------------------------------------------------------------------------
WARDNER_PKG_BASE="${WARDNER_PKG_BASE:-git+ssh://git@10.10.10.10/wardner}"

# The venv has to be activated first: wdndds._build.py / wdnav._build_wdnav.py
# may spawn child processes that call `python` / `pip` while rebuilding the
# .so; without activation those child processes fall back to the system
# python (especially under sudo / in a non-interactive shell), so the wheel
# lands in the system site-packages instead of the .venv and Debian's PEP
# 668 "externally-managed-environment" error triggers. After activation
# `python` is locked to the venv, `python -m pip` uses the venv
# site-packages and the paths are predictable.
if [ ! -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    echo "ERROR: $SCRIPT_DIR/.venv/bin/activate not found, please create the virtual environment first" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/.venv/bin/activate"

PIP_TARGET="python -m pip"

# Install wdnwebui's own runtime dependencies (fastapi / uvicorn /
# opencv-python-headless / python-multipart / av / gguf / lancedb /
# pyarrow / psutil and friends) from requirements.txt first — a dev machine
# often has a freshly cloned .venv where requirements.txt was never
# installed, or upstream bumped a web dependency version. Installing them
# before updating the SDKs avoids the SDK reinstall chain failing to pull
# the web side dependencies.
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo "==> syncing requirements.txt (wdnwebui's own dependencies)"
    ( cd "$SCRIPT_DIR" && $PIP_TARGET install -r requirements.txt )
    # ------------------------------------------------------------------------
    # The py3 compatibility patch for v4l2 (from PyPI)
    #
    # `v4l2>=0.2` in requirements.txt is a hard dependency of wdnav.v4l2 (the
    # SDK is installed with --no-deps, so the dependency does not come in
    # by itself). But upstream 0.2 (2014) still has the Python 2 idiom
    # `range(...) + [...]` in its source, which raises a TypeError at
    # **import** time under py3 (not an ImportError!) — so it is installed
    # and still unusable, and the HDMI-IN endpoint degrades from 503 to 500.
    # The patch is one line, the script is idempotent.
    #
    # A failure does not abort this script: v4l2 only affects the HDMI-IN
    # chain, the other SDK updates should not be held up by it. The log
    # states the exact remediation command.
    # ------------------------------------------------------------------------
    echo "==> patching the py3 compatibility issue of v4l2 (scripts/fix_v4l2_py3.py)"
    if ! ( cd "$SCRIPT_DIR" && python scripts/fix_v4l2_py3.py ); then
        echo "WARN: the v4l2 py3 compatibility patch did not succeed — HDMI-IN capture will be unavailable." >&2
        echo "      run it manually: python scripts/fix_v4l2_py3.py" >&2
    fi
else
    echo "==> requirements.txt not found, skipping the web dependency sync"
fi

# ---------------------------------------------------------------------------
# Artifact verification for the plugin packages (wdnapps_*)
#
# These packages are pure Python, so there is no .so to assert; but they
# have an even more fatal failure mode: **pip reports success and not a
# single card shows up**, without any error along the way. The most common
# cause is that the entry point name and the "id" in manifest.json do not
# match — the package directory uses underscores (cards/video_detection/)
# while the card id consistently uses hyphens ("video-detection"), so
# copying the module name gets every one of them wrong. After discover()
# marks it FATAL the host **silently skips** that card, so "it is
# installed" and "the card works" are two different things and have to be
# asserted here.
#
# importlib.util.find_spec is used instead of import_module: the former
# only imports the parent package (cards/__init__.py is empty, conveniently)
# and **does not execute the leaf module**; the latter would pull the whole
# app.core.* chain up through api.py (compute registry, decoder
# initialisation), which is both slow and has side effects in an update
# script.
#
# Output convention: one line per card, `OK <id> (<dir>)` or `BAD: <reason>`.
# The decision logic lives in scripts/check_plugin_entrypoints.py (which can
# also be run on its own); this script only calls it and renders the
# result. This block was originally an inline heredoc
# (`$(python - <<'PYEOF' ... )`), which failed with a **parse time**
# `command substitution: syntax error near unexpected token '||'` on bash
# 5.2 — the minimal case passes, the version inside a function does not,
# and that fragile style is not worth fighting any further.
# ---------------------------------------------------------------------------
verify_plugin_cards() {
    local pkg="$1"
    local checker="$SCRIPT_DIR/scripts/check_plugin_entrypoints.py"
    local out bad

    if [ ! -f "$checker" ]; then
        echo "FATAL: [$pkg] the checker script not found: $checker" >&2
        return 1
    fi

    # The decision is made with grep, not with the exit code of python — the
    # python side always exits 0, a line starting with `BAD:` means
    # failure. `cd "$SCRIPT_DIR"` is mandatory: the card modules import
    # `app.core.*` from the root of the host repo.
    out="$(cd "$SCRIPT_DIR" && python "$checker" "$pkg" 2>&1)" || true

    if [ -z "$out" ]; then
        echo "FATAL: [$pkg] the plugin check produced no output (python may not have run)" >&2
        return 1
    fi

    bad="$(printf '%s\n' "$out" | grep -c '^BAD' || true)"
    printf '%s\n' "$out" | sed 's/^OK /    ● /'
    if [ "$bad" != "0" ]; then
        printf '%s\n' "$out" | grep '^BAD' | sed 's/^BAD: /    × /' >&2
        echo "FATAL: [$pkg] the card verification failed ($bad items). The host drops cards silently" >&2
        echo "       at startup, so it has to be stopped here." >&2
        return 1
    fi
    return 0
}

# Verify the .so artifacts an SDK has to produce after the install.
# `pip install -e` **silently swallows** the RuntimeError of the
# ``build_py`` hook on setuptools >=64 (see the bootstrap_wdndds_native.sh
# comments / the "SDK native-.so rebuild policy" section of AGENTS.md) — the
# pip exit code is still 0, but the .so is never generated. So the script
# has to check the artifacts **itself** after the install, otherwise a
# failed build slides through and only shows up as a FileNotFoundError when
# the frontend imports it, which is very hard to trace.
#
# Usage: verify_outputs <pkg> <pip_stderr_log>
#   A missing .so → prints "build hook failure hint + the key .so paths +
#   the tail of the pip stderr", then exits 1, so set -e aborts the script
#   immediately.
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
            # wdnai is pure Python, it has no .so artifact; an advanced HEAD
            # is good enough.
            return 0
            ;;
        wdnhid)
            # wdnhid is pure Python (USB Gadget HID bindings), it has no .so
            # artifact. The card side of wdnwebui imports wdnhid, so it is
            # enough to guarantee that this package works.
            return 0
            ;;
        wdnapps_video_analyzer|wdnapps_hdmi_processor|wdnapps_audio_chat|wdnapps_media_generator)
            # The application plugin packages are pure Python as well, so
            # they have no .so either; but what they need verified matters
            # more — whether the cards really registered (see the notes of
            # verify_plugin_cards).
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

    echo "FATAL: [$pkg] the build hook did not generate the following .so (setuptools >=64 silently swallowed a RuntimeError):" >&2
    for f in "${missing[@]}"; do
        echo "    - $f" >&2
    done

    # Try to extract the real root cause from the captured stderr — that
    # hits the problem more directly than a generic "missing apt
    # dependency". The FileNotFoundError raised by subprocess usually means
    # the binary was not found (cmake / patchelf / make); the error of
    # cmake itself usually starts with "CMake Error"; a gcc/clang
    # link/compile failure usually carries "fatal error" or "undefined
    # reference".
    local cause=""
    if [ -f "$log" ] && [ -s "$log" ]; then
        if grep -qE "No such file or directory: 'cmake'" "$log"; then
            cause="the cmake binary is missing (apt install cmake)"
        elif grep -qE "No such file or directory: '(make|patchelf|cc|g\\+\\+)'" "$log"; then
            cause=$(grep -oE "No such file or directory: '[^']+'" "$log" | head -1)
            cause="$cause (apt install build-essential patchelf cmake)"
        elif grep -qE "Could NOT find (FastDDS|foonathan)" "$log"; then
            cause="the FastDDS 3.6 dev headers were not found (run .venv/src/wdndds/build_native.sh or set WDNDDS_FASTDDS_PREFIX)"
        elif grep -qE "fatal error:.*No such file" "$log"; then
            cause=$(grep -oE "fatal error: [^\\n]+" "$log" | head -1)
        elif grep -qE "undefined reference to" "$log"; then
            cause=$(grep -oE "undefined reference to '[^']+'" "$log" | head -1)
        fi
    fi
    if [ -n "$cause" ]; then
        echo "" >&2
        echo "  the root cause observed: $cause" >&2
    fi

    echo "" >&2
    echo "  common causes:" >&2
    case "$pkg" in
        wdndds)
            echo "    - missing the build toolchain: sudo apt install cmake patchelf build-essential" >&2
            echo "    - missing the C++ memory allocator library: sudo apt install libfoonathan-memory-dev libfoonathan-memory0.7.3" >&2
            echo "    - missing the FastDDS 3.6 dev headers: run bash .venv/src/wdndds/build_native.sh" >&2
            echo "      (no need to run it again when the vendor path .venv/src/wdndds/native/ already exists)" >&2
            echo "    - one-shot bootstrap: sudo ./bootstrap_wdndds_native.sh (apt + stub + non-editable pip install)" >&2
            ;;
        wdnav)
            echo "    - missing apt dependency: sudo apt install librockchip-mpp-dev (provides the /usr/include/rockchip headers)" >&2
            echo "    - the header directory exists but linking the .so fails: check the cc errors (usually a missing librockchip-mpp1 / librockchip-vpu0 runtime library)" >&2
            ;;
    esac
    echo "" >&2
    if [ -f "$log" ] && [ -s "$log" ]; then
        echo "  --- tail of the pip install stderr (captures the hook traceback swallowed by setuptools) ---" >&2
        tail -n 60 "$log" >&2 || true
        echo "  --- end ---" >&2
    else
        echo "  (no captured stderr; re-run with UPDATE_RKWHL_VERBOSE=1 for the detailed log)" >&2
    fi
    return 1
}

# Uninstall every installation trace of an SDK package, clearing the ground
# for the `pip install -e` that follows. All three actions are needed
# (pip uninstall only does the first one):
#   1. `pip uninstall -y` — removes it using pip's own metadata (including
#      the editable .pth)
#   2. Delete the physical directory `site-packages/<pkg>/` — pip uninstall
#      does not do this (it only knows the files recorded in its metadata).
#      A directory left behind by an earlier **non**-editable install
#      belongs to this category, and it shadows the editable install
#      permanently because of the sys.path order.
#   3. Delete the accumulated `__editable__.<pkg>-*.pth` — every editable
#      install writes a new versioned one and never reclaims it (observed
#      up to three of them: 0.7.0/0.8.0/0.9.0).
# Only the deterministic paths pip generates are deleted; the site-packages
# path is obtained dynamically via sysconfig, no version is hardcoded.
purge_install_traces() {
    local pkg="$1"
    local sp
    sp="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    if [ -z "$sp" ] || [ ! -d "$sp" ]; then
        echo "WARN: [$pkg] cannot locate site-packages ($sp), skipping the cleanup" >&2
        return 0
    fi

    echo "    [$pkg] pip uninstall -y $pkg"
    # pip uninstall returns non-zero when the package is not installed, which
    # is not a failure.
    $PIP_TARGET uninstall -y "$pkg" >/dev/null 2>&1 || true

    local removed=0 f
    if [ -d "$sp/$pkg" ]; then
        echo "    [$pkg] deleting the leftover physical directory of a non-editable install: $sp/$pkg"
        rm -rf "$sp/$pkg"
        removed=1
    fi
    # On upgrade/uninstall pip renames the old directory / dist-info with a
    # ``~`` prefix before deleting it; when the process is interrupted that
    # leaves junk such as ``~dndds`` / ``~-ndds`` /
    # ``~dndds-*.dist-info``. Their naming is **not fixed** (pip versions
    # differ in their renaming strategy; observed: the same wdndds left
    # three different ones), so globbing by package name does not catch
    # them all.
    #
    # But ``~`` is not a valid first character of a Python identifier — a
    # legal package name can never start with it, so every ``~*`` in
    # site-packages is **pip junk** and the whole set can be swept away.
    # Not doing this leads to importlib.metadata treating
    # ``~xxx.dist-info`` as an illegal distribution, pip printing
    # "Ignoring invalid distribution" on every command, and the package
    # possibly disappearing from pip list entirely (observed with wdndds).
    for f in "$sp"/'~'*; do
        [ -e "$f" ] || continue
        echo "    [$pkg] deleting the leftover of an interrupted pip run: $f"
        rm -rf "$f"
        removed=1
    done
    for f in "$sp/$pkg"-*.dist-info "$sp/$pkg".dist-info \
             "$sp/__editable__.$pkg"-*.pth "$sp/__editable__.$pkg.pth"; do
        [ -e "$f" ] || continue
        echo "    [$pkg] deleting $f"
        rm -rf "$f"
        removed=1
    done

    if [ "$removed" = "0" ]; then
        echo "    [$pkg] nothing left behind (a clean editable install)"
    fi
}


update_pkg() {
    local pkg="$1"
    local repo_url="$2"
    local dir="$VENV_SRC/$pkg"

    # The pip stderr goes to a temporary file, which verify_outputs prints
    # back when a .so is missing after the install (-v makes setuptools
    # forward the output of the hook internals).
    local pip_log
    pip_log="$(mktemp -t update_rkwhl.XXXXXX.log)"
    [ -n "$pip_log" ] || pip_log="/tmp/update_rkwhl.$$.log"
    trap "rm -f '$pip_log'" RETURN

    # Step 1: fetch the latest code
    if [ ! -d "$dir" ]; then
        echo "==> [$pkg] $dir not found, running a pip install -e"
        if ! ( cd "$SCRIPT_DIR" && $PIP_TARGET install -v --no-deps -e "$repo_url" ) 2> "$pip_log"; then
            echo "FATAL: [$pkg] pip install itself failed (exit != 0)" >&2
            tail -n 60 "$pip_log" >&2 || true
            return 1
        fi
        verify_outputs "$pkg" "$pip_log" || return 1
        return 0
    fi

    echo "==> [$pkg] git pull"
    local before after
    before=$(git -C "$dir" rev-parse HEAD)

    # A failed pull does not abort the whole script: it logs a warning,
    # skips this package and continues with the next one.
    # `--ff-only` refuses in exactly two cases, and neither touches the
    # working tree: local uncommitted changes would be overwritten, or the
    # local branch has diverged from the remote. That means "somebody is
    # working locally" — force pushing or merging is not a decision this
    # script should make (FATAL is reserved for real failures such as pip
    # or a missing .so).
    #
    # With retries: network flapping (RPC failed / curl 28 connection
    # timeout) is a different thing from "local changes" — the former
    # usually succeeds on a retry, the latter never does. `--ff-only`
    # guarantees that a failed attempt leaves the **working tree
    # untouched**, so retrying is safe.
    local _pull_ok=0 _try=1
    while [ "$_try" -le "${UPDATE_RKWHL_PULL_RETRY:-5}" ]; do
        # lowSpeedLimit/lowSpeedTime: git has **no** connect timeout by
        # default, so a TCP-level failure takes 135s to surface (observed:
        # "Failed to connect ... after 135137 ms"). Pulling 8 packages one
        # by one, a single hiccup can cost a quarter of an hour. Cap it at
        # 30s.
        if git -C "$dir" -c http.lowSpeedLimit=1 -c http.lowSpeedTime=30 \
            pull --ff-only; then
            _pull_ok=1
            break
        fi
        if [ "$_try" -lt "${UPDATE_RKWHL_PULL_RETRY:-5}" ]; then
            echo "WARN: [$pkg] git pull failed (attempt $_try), retrying in 5s" >&2
            sleep 5
        fi
        _try=$((_try + 1))
    done
    if [ "$_pull_ok" != "1" ]; then
        echo "WARN: [$pkg] git pull --ff-only failed, skipping this package (the .venv is unchanged)" >&2
        echo "      common causes: uncommitted local changes / the local branch has diverged from the remote / network flapping" >&2
        echo "      git does not lose your changes because of this — handle it yourself and re-run:" >&2
        echo "        git -C $dir status" >&2
        echo "        git -C $dir stash   # or commit / branch" >&2
        echo "      the remaining SDKs keep being processed." >&2
        return 1
    fi
    after=$(git -C "$dir" rev-parse HEAD)

    # Step 2/3: when the HEAD did not change the .venv stays untouched (see
    # the file header for why this signal was chosen)
    local force="${UPDATE_RKWHL_FORCE_REBUILD:-0}"
    if [ "$before" = "$after" ] && [ "$force" != "1" ]; then
        echo "==> [$pkg] HEAD unchanged (${after:0:7}), skipping the install (the .venv is unchanged)"
        echo "    to force a rebuild: UPDATE_RKWHL_FORCE_REBUILD=1 $0"
        return 0
    fi

    local reason
    if [ "$before" != "$after" ]; then
        reason="HEAD ${before:0:7}→${after:0:7}"
    else
        reason="HEAD unchanged (${after:0:7}), but FORCE_REBUILD=1"
    fi
    echo "==> [$pkg] $reason, uninstalling the old version and reinstalling"

    # Step 4a: uninstall completely
    purge_install_traces "$pkg"

    # Step 4b: install the new version. The local path is passed instead of
    # $repo_url to avoid a fresh SSH clone; the .so is compiled by the
    # build_py hook (which unconditionally unlinks and rebuilds), and when
    # it fails the old .so is already gone, so the runtime import raises
    # FileNotFoundError, which is easier to diagnose than a silent
    # undefined symbol.
    if ! ( cd "$SCRIPT_DIR" && $PIP_TARGET install -v --no-deps -e "$dir" ) 2> "$pip_log"; then
        echo "FATAL: [$pkg] pip install itself failed (exit != 0)" >&2
        tail -n 60 "$pip_log" >&2 || true
        return 1
    fi
    verify_outputs "$pkg" "$pip_log" || return 1
}

# Drive the packages one by one: a failing package only logs a warning and
# the next one is processed; the summary comes at the end.
# Early on this was a bare call + `set -e`, so a single failing package
# aborted the whole script and the SDKs behind it silently stayed on the
# old version — with three packages, if one of them has local changes the
# other two can never be upgraded.
FAILED_PKGS=()
run_pkg() {
    local pkg="$1"
    local repo_url="$2"
    if update_pkg "$pkg" "$repo_url"; then
        return 0
    fi
    echo "WARN: [$pkg] not updated this time (see the error above)" >&2
    FAILED_PKGS+=("$pkg")
    return 0   # swallow the failure so the following packages continue
}

# ---------------------------------------------------------------------------
# Dependency audit (the hedge against --no-deps)
#
# The SDKs are always installed with `pip install --no-deps -e` — this is
# **deliberate**, not legacy:
#   * the private packages are not on PyPI (wdndds → wdnai, the plugin
#     packages → wdnhid / wdnav), so letting pip resolve them fails
#     outright;
#   * the resolution would pull in opencv-python (the GUI build) next to
#     the installed opencv-python-headless, and the two fight over the same
#     cv2 package;
#   * av needs a hand-picked version;
#   * the target machine often cannot reach PyPI at all.
# See README §4b.
#
# The price is that **the dependencies the SDKs declare never land in the
# venv on their own**, and the symptom is far away from the cause:
#   * v4l2 missing → the HDMI-IN snapshot endpoint returns 503 forever;
#   * tokenizers missing → the compute page reports "model failed to
#     start".
#
# So after the install they have to be **checked on the spot**: read the
# [project].dependencies of .venv/src/*/pyproject.toml, look up every
# **installed** distribution (not the index → works offline), and **really
# import it once** — "installed but fails to import" is exactly the class
# of failure that v4l2 represents, and find_spec does not catch it.
#
# The decision lives in scripts/check_sdk_deps.py (which can be run on its
# own). Note that the audit reads the pyproject of the **source tree**: it
# may be newer than the installed version — which is exactly what we want
# (new dependencies are reported early).
# ---------------------------------------------------------------------------
audit_sdk_deps() {
    local checker="$SCRIPT_DIR/scripts/check_sdk_deps.py"
    if [ ! -f "$checker" ]; then
        echo "WARN: $checker not found, skipping the dependency audit" >&2
        return 0
    fi
    echo "==> auditing whether the SDK-declared dependencies are really usable (scripts/check_sdk_deps.py)"
    ( cd "$SCRIPT_DIR" && python "$checker" )
}

run_pkg wdnav  "$WARDNER_PKG_BASE/wdnav.git#egg=wdnav"
run_pkg wdnai  "$WARDNER_PKG_BASE/wdnai.git#egg=wdnai"
run_pkg wdndds "$WARDNER_PKG_BASE/wdndds.git#egg=wdndds"
run_pkg wdnhid "$WARDNER_PKG_BASE/wdnhid.git#egg=wdnhid"

# The application plugin packages (the cards of wdnwebui). They come after
# the SDKs: the usb_hid card of wdnapps_hdmi_processor imports wdnhid /
# wdnav, and installing the plugins only after those exist keeps the errors
# in the log from pointing at "an SDK that is not installed yet" — a false
# lead.
#
# **The package name must use underscores** (= the directory name = the
# dist-info / .pth name pip writes to disk). The canonical name of pip is
# the hyphenated wdnapps-video-analyzer, but what actually lands in
# site-packages is wdnapps_video_analyzer-0.1.0.dist-info and
# __editable__.wdnapps_video_analyzer-0.1.0.pth. purge_install_traces globs
# all of these with $pkg — passing the hyphenated name makes the cleanup
# silently miss, old .pth files pile up, and that is exactly the "physical
# directory shadows the editable install" problem the v1.3 file header set
# out to eliminate.
run_pkg wdnapps_video_analyzer "$WARDNER_PKG_BASE/wdnapps_video_analyzer.git#egg=wdnapps_video_analyzer"
run_pkg wdnapps_hdmi_processor "$WARDNER_PKG_BASE/wdnapps_hdmi_processor.git#egg=wdnapps_hdmi_processor"
run_pkg wdnapps_audio_chat "$WARDNER_PKG_BASE/wdnapps_audio_chat.git#egg=wdnapps_audio_chat"
# The media generation plugin package (text-to-image / image-to-image,
# Qwen-Image-2.1). Its cards run on the image capability of wdnai, so it
# comes after wdnai as well.
run_pkg wdnapps_media_generator "$WARDNER_PKG_BASE/wdnapps_media_generator.git#egg=wdnapps_media_generator"

# The dependency audit runs **after every package has been processed**:
# only then is the state of the venv the final state (the packages before
# it may just have been reinstalled, and pip may just have swapped a
# dependency). A failed audit does not modify the .venv, so it does not
# interrupt the update that already ran; it only affects the exit code of
# this script.
DEPS_AUDIT_OK=1
audit_sdk_deps || DEPS_AUDIT_OK=0

if [ ${#FAILED_PKGS[@]} -eq 0 ] && [ "$DEPS_AUDIT_OK" = "1" ]; then
    echo "Update complete (wdnav / wdnai / wdndds / wdnhid / wdnapps_video_analyzer / wdnapps_hdmi_processor / wdnapps_audio_chat / wdnapps_media_generator all succeeded)."
    exit 0
fi

echo "" >&2
if [ ${#FAILED_PKGS[@]} -ne 0 ]; then
    echo "The update partly completed, these packages were not updated: ${FAILED_PKGS[*]}" >&2
    echo "The remaining packages have been processed according to their own state; re-run $0 once the packages above are sorted out." >&2
fi
if [ "$DEPS_AUDIT_OK" != "1" ]; then
    echo "The dependency audit did not pass: the SDK-declared dependencies are missing, have the wrong version or fail to import." >&2
    echo "  to fill the gap: .venv/bin/pip install -r requirements.txt" >&2
    echo "  to check again: .venv/bin/python scripts/check_sdk_deps.py" >&2
fi
exit 1
