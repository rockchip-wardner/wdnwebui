#!/bin/bash
# The full board-side acceptance flow: probe comparison → deployment into
# the model package → end-to-end image generation (3 cores).
#
# Usage: tmp_rknn/accept_vae2.sh <model.rknn> [tag]
#
# Steps:
#   1) probe_vae4d: init + run (3 cores) + compare against the ORT
#      reference (rel_L2 / cosine)
#   2) back up the vae_qwenimage21-512.3588.rknn that is currently in the
#      model package and copy the new model in
#   3) /tmp/e2e_qwenimage21.py generates 2 images (verifies zero
#      model_init)
set -u

MODEL="${1:?usage: accept_vae2.sh <model.rknn> [tag]}"
TAG="${2:-$(basename "$MODEL" .rknn)}"
PKG=/home/rockchip/models/qwenimage21
WHOLE="$PKG/vae_qwenimage21-512.3588.rknn"
PROBE=/tmp/probe_vae4d
IN=/tmp/vae512_in_c2.bin
REF=/tmp/vae512_ref.bin
OUT=/tmp/out_${TAG}.bin

echo "════════ 1) probe comparison: $MODEL (core_mask=7 → 3 cores) ════════"
"$PROBE" "$MODEL" "$IN" "$OUT" "$REF" 7 2>&1 | tee "/tmp/probe_${TAG}.log"
if ! grep -q "rel_L2=" "/tmp/probe_${TAG}.log"; then
  echo "× the probe did not produce an accuracy result (init or run failed)"; exit 1
fi

echo
echo "════════ 2) deployment into the model package ════════"
if [ ! -f "$MODEL" ]; then echo "× the model does not exist: $MODEL"; exit 1; fi
if [ -f "$WHOLE" ] && [ ! -f "$WHOLE.bak_fp16" ]; then
  echo "--- backing up the current full model -> $WHOLE.bak_fp16"
  mv "$WHOLE" "$WHOLE.bak_fp16"
fi
cp "$MODEL" "$WHOLE.new" && mv "$WHOLE.new" "$WHOLE"
ls -la "$WHOLE"

echo
echo "════════ 3) end-to-end image generation (2 images) ════════"
cd /home/rockchip/wdnwebui || exit 1
timeout 1200 .venv/bin/python /tmp/e2e_qwenimage21.py "a red apple on a wooden table" 8 \
  2>&1 | tee "/tmp/e2e_${TAG}.log" | tail -n 45

echo
echo "════════ key log lines ════════"
# The Chinese alternatives match the log lines of probe_vae4d / e2e_qwenimage21,
# which still log in Chinese — the ASCII alternatives are for when those
# helpers are translated too.
grep -E "rknn2|vae-rknn2|rel_L2|第 [0-9]+ 张|零 model_init|图像 shape|image [0-9]+|zero model_init|image shape" \
  "/tmp/probe_${TAG}.log" "/tmp/e2e_${TAG}.log" 2>/dev/null | head -n 60
