#!/bin/bash
# The parallel runner for step ② of the hybrid quantization: the same step1
# artefacts (.model/.data), a different cfg.
#
# step1 is expensive (per-layer calibration + filling custom_quantize_layers
# automatically), step2 is cheap (compile + produce the model). So step1 is
# run once, and after that every new cfg runs step2.
#
# The three variants to compare:
#   theirs —— only the custom_quantize_layers the toolchain filled in itself
#   ours   —— only the layers picked by the per-layer accuracy analysis are
#             kept in fp16
#   union  --- both of them combined
#
# Usage: hyb_run.sh <workdir> <tag> <mode> [fp16_layers_file]
set -eu

WORK="${1:?usage: hyb_run.sh <workdir> <tag> <mode> [fp16_file]}"
TAG="${2:?}"
MODE="${3:?}"
FP16="${4:-}"

ROOT=/datasets/Qwen-Image-2.1/rknn2_vae
PY=/datasets/Qwen-Image-2.1/.venv2/bin/python
CWD="$ROOT/hybrun/$TAG"
mkdir -p "$CWD" "$ROOT/logs"
cd "$CWD"

# Every run gets its own TMPDIR, so that concurrent runs do not overwrite
# each other's temporary files of the toolchain
export TMPDIR=/datasets/tmp/rknnbuild_$TAG
mkdir -p "$TMPDIR"

MODEL=$(ls "$WORK"/*.model | head -1)
DATA=$(ls "$WORK"/*.data | head -1)
SRC_CFG=$(ls "$WORK"/*.quantization.cfg | head -1)
CFG="$CWD/vae512_c2.quantization.cfg"

echo "[$TAG] model=$MODEL"
echo "[$TAG] data =$DATA"
echo "[$TAG] cfg  =$SRC_CFG"

# rewrite the cfg
if [ "$MODE" = "theirs" ]; then
  "$PY" "$ROOT/hyb_cfg.py" --cfg "$SRC_CFG" --out "$CFG" --mode theirs
else
  "$PY" "$ROOT/hyb_cfg.py" --cfg "$SRC_CFG" --out "$CFG" --mode "$MODE" \
        --fp16-file "$FP16"
fi

# step2 + produce the model + compare against the simulator
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
