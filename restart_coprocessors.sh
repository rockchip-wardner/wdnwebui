#!/bin/bash
# 重启所有 RK NPU 协处理器
#
# 镜像 pyrkaiweb `app/api/coprocessor.py` 里的复位序列:
#   1) sudo rknn-smi reset -t hw -d 0    ×4
#      (某些板子第一次复位不生效,需要多试几次 — 与
#       /userdata/samples/reset_182x.sh 行为一致)
#   2) sudo rknn3_startup restart
#
# 与 Python 端一致:
#   - `rknn-smi reset -t hw -d 0` 退出码 255 表示"目标 NPU 已处于复位后
#     状态,无需再操作",属于良性结果,继续后续步骤。
#   - `rknn3_startup restart` 必须返回 0。
#   - 每个步骤 60s 超时;超时直接 kill 整个进程组并写入错误日志。
#
# 错误日志: /tmp/coprocessor_restart.err.log
# 运行日志: stdout (每步一行)
#
# 用法:
#   sudo ./restart_coprocessors.sh            # 全程静默运行,只输出进度
#   sudo ./restart_coprocessors.sh --verbose  # 显示每步的 stdout/stderr
#
# 退出码:
#   0  所有步骤成功
#   1  用户无 root 权限 (需要 sudo)
#   2  rknn-smi / rknn3_startup 未找到
#   3  步骤执行失败 (含超时)
#   4  用户中断 (SIGINT)

set -u

ERR_LOG="/tmp/coprocessor_restart.err.log"
TIMEOUT_S=60
VERBOSE=0

for arg in "$@"; do
    case "$arg" in
        --verbose|-v) VERBOSE=1 ;;
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *) echo "未知参数: $arg" >&2; exit 1 ;;
    esac
done

# --- 前置检查 -----------------------------------------------------------

log_err() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$ERR_LOG"
}

: > "$ERR_LOG"

if [ "$(id -u)" -ne 0 ]; then
    echo "需要 root 权限,请用 sudo 运行此脚本" >&2
    log_err "权限不足: 当前用户 $(id -un) 非 root"
    exit 1
fi

for bin in rknn-smi rknn3_startup; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "未找到可执行文件: $bin" >&2
        log_err "依赖缺失: $bin 不在 PATH 中"
        exit 2
    fi
done

if ! command -v sudo >/dev/null 2>&1; then
    echo "未找到 sudo,但当前已是 root,继续执行" >&2
    SUDO=""
else
    SUDO="sudo"
fi

# --- 步骤定义 (与 coprocessor.py 的 _STEPS 严格一致) --------------------

STEPS=(
    "rknn-smi reset -t hw -d 0"
    "rknn-smi reset -t hw -d 0"
    "rknn-smi reset -t hw -d 0"
    "rknn-smi reset -t hw -d 0"
    "rknn3_startup restart"
)

# 每个步骤可接受的退出码;与 Python 端的 _RKNNSMI_RESET_OK / {0} 对齐。
ACCEPTABLE_EXITS=( "0,255" "0,255" "0,255" "0,255" "0" )

# --- 运行单个步骤 --------------------------------------------------------
#
# 参数: $1 = 步骤索引 (1-based)
#        $2 = 命令字符串
#        $3 = 可接受退出码 (逗号分隔)
# 出口: 返回步骤的退出码;超时返回 124
# 副作用: stdout/stderr 在 verbose 下打印到终端;
#         任何非零退出 + 不可接受的退出码都会写到 ERR_LOG。

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

    # start_new_session=True 等价于 setsid,把进程放到新进程组,
    # 这样超时 killpg 时能干掉 rknn3_startup fork 出来的子进程。
    setsid bash -c "exec $SUDO $(printf '%q ' "${cmd[@]}")" \
        >"$tmp_out" 2>"$tmp_err" &
    local pid=$!

    # 等待 + 超时 (与 Python asyncio.wait_for 行为对齐)
    local now
    while kill -0 "$pid" 2>/dev/null; do
        now=$(date +%s)
        if [ $((now - start)) -ge "$TIMEOUT_S" ]; then
            # kill 整个进程组
            kill -TERM -"$pid" 2>/dev/null
            sleep 1
            kill -KILL -"$pid" 2>/dev/null
            wait "$pid" 2>/dev/null
            end=$(date +%s)
            echo "[$idx/5] !!! 超时 (${TIMEOUT_S}s): $cmd_str" >&2
            log_err "步骤 $idx 超时: $cmd_str"
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

    # 判定退出码是否可接受
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
            echo "[$idx/5] <<< 退出 $rc (可接受,空操作) [${elapsed}s]"
        else
            echo "[$idx/5] <<< 退出 $rc [${elapsed}s]"
        fi
        rm -f "$tmp_out" "$tmp_err"
        return 0
    else
        echo "[$idx/5] <<< 退出 $rc (失败) [${elapsed}s]" >&2
        log_err "步骤 $idx 失败: $cmd_str 退出码=$rc (期望 $acceptable) 耗时=${elapsed}s"
        rm -f "$tmp_out" "$tmp_err"
        return 1
    fi
}

# --- 主流程 --------------------------------------------------------------

# SIGINT 兜底
trap 'echo "用户中断" >&2; log_err "用户中断 (SIGINT)"; exit 4' INT TERM

overall_rc=0
for i in "${!STEPS[@]}"; do
    idx=$((i + 1))
    if ! run_step "$idx" "${STEPS[$i]}" "${ACCEPTABLE_EXITS[$i]}"; then
        echo "在步骤 $idx 失败,中止后续步骤" >&2
        log_err "中止: 步骤 $idx 失败,后续步骤未执行"
        overall_rc=3
        break
    fi
done

trap - INT TERM

if [ "$overall_rc" -eq 0 ]; then
    echo "所有协处理器重启完成"
else
    echo "重启失败 (退出码 $overall_rc),详情见 $ERR_LOG" >&2
fi

exit "$overall_rc"