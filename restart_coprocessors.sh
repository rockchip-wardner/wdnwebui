#!/bin/bash
# Restart all RK NPU coprocessors.
#
# Mirrors the reset sequence in `app/system/api/coprocessor.py` of the
# wdnwebui image:
#   1) wdnai.RknnSmi.hw_reset_device(0)  (internally loops 4 times, aligned
#      with the semantics of the old reset_182x.sh: "on some boards the
#      first reset does not take effect, so it has to be tried a few times")
#   2) sudo rknn3_startup restart
#
# wdnwebui forbids using the `rknn-smi` CLI to query the NPU state or to
# perform a reset. This script is kept so that it can still be run
# manually on very lean targets without wdnwebui (e.g. a factory fixture
# that only has reset_182x.sh installed); the "success" of a reset is
# decided by the same exit codes:
#   - `rknn-smi reset -t hw -d 0` returning 255 means "the target NPU is
#     already in the reset state, nothing to do" — a benign result, the
#     following steps continue.
#   - `rknn3_startup restart` has to return 0.
#   - Every step has a 60s timeout; on a timeout the whole process group
#     is killed and the error is written to the error log.
#
# Error log: /tmp/coprocessor_restart.err.log
# Run log: stdout (one line per step)
#
# Usage:
#   sudo ./restart_coprocessors.sh            # run quietly, print progress only
#   sudo ./restart_coprocessors.sh --verbose  # show the stdout/stderr of every step
#
# Exit codes:
#   0  every step succeeded
#   1  the user has no root privileges (sudo is required)
#   2  rknn-smi / rknn3_startup not found
#   3  a step failed (including a timeout)
#   4  the user interrupted the script (SIGINT)

set -u

ERR_LOG="/tmp/coprocessor_restart.err.log"
TIMEOUT_S=60
VERBOSE=0

for arg in "$@"; do
    case "$arg" in
        --verbose|-v) VERBOSE=1 ;;
        --help|-h)
            sed -n '2,35p' "$0"
            exit 0
            ;;
        *) echo "unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# --- preflight checks ------------------------------------------------------

log_err() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$ERR_LOG"
}

: > "$ERR_LOG"

if [ "$(id -u)" -ne 0 ]; then
    echo "root privileges are required, please run this script with sudo" >&2
    log_err "not enough privileges: the current user $(id -un) is not root"
    exit 1
fi

for bin in rknn-smi rknn3_startup; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "executable not found: $bin" >&2
        log_err "missing dependency: $bin is not in PATH"
        exit 2
    fi
done

if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo not found, but the current user is already root, continuing" >&2
    SUDO=""
else
    SUDO="sudo"
fi

# --- step definitions (strictly identical to _STEPS of coprocessor.py) -----

STEPS=(
    "rknn-smi reset -t hw -d 0"
    "rknn-smi reset -t hw -d 0"
    "rknn-smi reset -t hw -d 0"
    "rknn-smi reset -t hw -d 0"
    "rknn3_startup restart"
)

# The acceptable exit codes of every step; aligned with _RKNNSMI_RESET_OK /
# {0} on the Python side.
ACCEPTABLE_EXITS=( "0,255" "0,255" "0,255" "0,255" "0" )

# --- run a single step -----------------------------------------------------
#
# Arguments: $1 = the step index (1-based)
#            $2 = the command string
#            $3 = the acceptable exit codes (comma separated)
# Exit: returns the exit code of the step; 124 on a timeout
# Side effects: stdout/stderr are printed to the terminal in verbose mode;
#               every non-zero exit with an unacceptable code is written to
#               ERR_LOG.

run_step() {
    local idx="$1"
    local cmd_str="$2"
    local acceptable="$3"
    # shellcheck disable=SC2206
    local cmd=( $cmd_str )
    local tmp_out tmp_err
    tmp_out="$(mktemp)"
    tmp_err="$(mktemp)"

    echo "[$idx/5] >>> $cmd_str"

    local start end elapsed rc
    start=$(date +%s)

    # start_new_session=True is the equivalent of setsid: it puts the
    # process into a new process group, so that the timeout killpg also
    # takes down the children forked by rknn3_startup.
    setsid bash -c "exec $SUDO $(printf '%q ' "${cmd[@]}")" \
        >"$tmp_out" 2>"$tmp_err" &
    local pid=$!

    # wait + timeout (aligned with the behaviour of Python asyncio.wait_for)
    local now
    while kill -0 "$pid" 2>/dev/null; do
        now=$(date +%s)
        if [ $((now - start)) -ge "$TIMEOUT_S" ]; then
            # kill the whole process group
            kill -TERM -"$pid" 2>/dev/null
            sleep 1
            kill -KILL -"$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            end=$(date +%s)
            echo "[$idx/5] !!! timeout (${TIMEOUT_S}s): $cmd_str" >&2
            log_err "step $idx timed out: $cmd_str"
            rc=124
            elapsed=$((end - start))
            break
        fi
        sleep 0.5
    done

    if [ "${rc:-unset}" != "124" ]; then
        wait "$pid"
        rc=$?
        end=$(date +%s)
        elapsed=$((end - start))
    fi

    if [ "$VERBOSE" -eq 1 ] && [ -s "$tmp_out" ]; then
        sed 's/^/    | /' "$tmp_out"
    fi
    if [ -s "$tmp_err" ]; then
        sed 's/^/    ! /' "$tmp_err"
        cat "$tmp_err" >> "$ERR_LOG"
    fi

    # decide whether the exit code is acceptable
    local accepted=0
    IFS=',' read -ra ok_list <<< "$acceptable"
    for code in "${ok_list[@]}"; do
        if [ "$rc" = "$code" ]; then
            accepted=1
            break
        fi
    done

    if [ "$accepted" -eq 1 ]; then
        if [ "$rc" -ne 0 ] && [ "$VERBOSE" -eq 1 ]; then
            echo "[$idx/5] <<< exit $rc (acceptable, a no-op) [${elapsed}s]"
        else
            echo "[$idx/5] <<< exit $rc [${elapsed}s]"
        fi
        rm -f "$tmp_out" "$tmp_err"
        return 0
    else
        echo "[$idx/5] <<< exit $rc (failed) [${elapsed}s]" >&2
        log_err "step $idx failed: $cmd_str exit code=$rc (expected $acceptable) elapsed=${elapsed}s"
        rm -f "$tmp_out" "$tmp_err"
        return 1
    fi
}

# --- main flow --------------------------------------------------------------

# SIGINT fallback
trap 'echo "interrupted by the user" >&2; log_err "interrupted by the user (SIGINT)"; exit 4' INT TERM

overall_rc=0
for i in "${!STEPS[@]}"; do
    idx=$((i + 1))
    if ! run_step "$idx" "${STEPS[$i]}" "${ACCEPTABLE_EXITS[$i]}"; then
        echo "step $idx failed, aborting the remaining steps" >&2
        log_err "aborted: step $idx failed, the remaining steps were not executed"
        overall_rc=3
        break
    fi
done

trap - INT TERM

if [ "$overall_rc" -eq 0 ]; then
    echo "all coprocessors restarted"
else
    echo "the restart failed (exit code $overall_rc), see $ERR_LOG for the details" >&2
fi

exit "$overall_rc"
