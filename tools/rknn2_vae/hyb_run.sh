#!/bin/bash
# 混合量化第 ② 步的并行 runner：同一份 step1 产物 (.model/.data)，不同的 cfg。
#
# step1 贵（逐层标定 + 自动填 custom_quantize_layers），step2 便宜（编译 + 出模型）。
# 所以 step1 只跑一次，之后每换一份 cfg 就跑一次 step2。
#
# 三档对照：
#   theirs —— 只用工具链自己自动填的 custom_quantize_layers
#   ours   —— 只用逐层精度分析挑出来的层留 fp16
#   union  —— 两者合并
#
# 用法: hyb_run.sh <workdir> <tag> <mode> [fp16_layers_file]
set -eu

WORK="${1:?用法: hyb_run.sh <workdir> <tag> <mode> [fp16_file]}"
TAG="${2:?}"
MODE="${3:?}"
FP16="${4:-}"

ROOT=/datasets/Qwen-Image-2.1/rknn2_vae
PY=/datasets/Qwen-Image-2.1/.venv2/bin/python
CWD="$ROOT/hybrun/$TAG"
mkdir -p "$CWD" "$ROOT/logs"
cd "$CWD"

# 每个 run 独立 TMPDIR，避免并发时互相覆盖工具链的临时文件
export TMPDIR=/datasets/tmp/rknnbuild_$TAG
mkdir -p "$TMPDIR"

MODEL=$(ls "$WORK"/*.model | head -1)
DATA=$(ls "$WORK"/*.data | head -1)
SRC_CFG=$(ls "$WORK"/*.quantization.cfg | head -1)
CFG="$CWD/vae512_c2.quantization.cfg"

echo "[$TAG] model=$MODEL"
echo "[$TAG] data =$DATA"
echo "[$TAG] cfg  =$SRC_CFG"

# 改写 cfg
if [ "$MODE" = "theirs" ]; then
  "$PY" "$ROOT/hyb_cfg.py" --cfg "$SRC_CFG" --out "$CFG" --mode theirs
else
  "$PY" "$ROOT/hyb_cfg.py" --cfg "$SRC_CFG" --out "$CFG" --mode "$MODE" \
        --fp16-file "$FP16"
fi

# step2 + 出模型 + 模拟器对拍
"$PY" -u "$ROOT/hyb_step2.py" \
    --tag "$TAG" \
    --model "$MODEL" --data "$DATA" --cfg "$CFG" \
    --out "$ROOT/rknn4/${TAG}.rknn" \
    --input-npy "${IN_NPY:-/datasets/tmp/vae512_in_c2.npy}" \
    --ref "${REF_NPY:-/datasets/tmp/vae512_ref.npy}" \
    > "$ROOT/logs/hyb_${TAG}.log" 2>&1
rc=$?
echo "[$TAG] exit=$rc"
grep -E "^RESULT|^### (ACC|STEP2|EXPORT|MEM)" "$ROOT/logs/hyb_${TAG}.log" || true
exit $rc
