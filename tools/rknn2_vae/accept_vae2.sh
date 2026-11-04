#!/bin/bash
# 板端验收全流程：探针对拍 → 部署到模型包 → 端到端出图（3 核）。
#
# 用法: tmp_rknn/accept_vae2.sh <model.rknn> [tag]
#
# 步骤:
#   1) probe_vae4d：init + run(3核) + 与 ORT 参考对拍 rel_L2/cosine
#   2) 备份模型包里现有的 vae_qwenimage21-512.rknn2.rknn，拷入新模型
#   3) /tmp/e2e_qwenimage21.py 出 2 张图（验证零 model_init）
set -u

MODEL="${1:?用法: accept_vae2.sh <model.rknn> [tag]}"
TAG="${2:-$(basename "$MODEL" .rknn)}"
PKG=/home/rockchip/models/qwenimage21
WHOLE="$PKG/vae_qwenimage21-512.rknn2.rknn"
PROBE=/tmp/probe_vae4d
IN=/tmp/vae512_in_c2.bin
REF=/tmp/vae512_ref.bin
OUT=/tmp/out_${TAG}.bin

echo "════════ 1) 探针对拍：$MODEL (core_mask=7 → 3 核) ════════"
"$PROBE" "$MODEL" "$IN" "$OUT" "$REF" 7 2>&1 | tee "/tmp/probe_${TAG}.log"
if ! grep -q "rel_L2=" "/tmp/probe_${TAG}.log"; then
  echo "❌ 探针没跑出精度结果（init 或 run 失败）"; exit 1
fi

echo
echo "════════ 2) 部署到模型包 ════════"
if [ ! -f "$MODEL" ]; then echo "❌ 模型不存在: $MODEL"; exit 1; fi
if [ -f "$WHOLE" ] && [ ! -f "$WHOLE.bak_fp16" ]; then
  echo "--- 备份现有整图 -> $WHOLE.bak_fp16"
  mv "$WHOLE" "$WHOLE.bak_fp16"
fi
cp "$MODEL" "$WHOLE.new" && mv "$WHOLE.new" "$WHOLE"
ls -la "$WHOLE"

echo
echo "════════ 3) 端到端出图（2 张）════════"
cd /home/rockchip/wdnwebui || exit 1
timeout 1200 .venv/bin/python /tmp/e2e_qwenimage21.py "a red apple on a wooden table" 8 \
  2>&1 | tee "/tmp/e2e_${TAG}.log" | tail -n 45

echo
echo "════════ 关键日志行 ════════"
grep -E "rknn2|vae-rknn2|rel_L2|第 [0-9]+ 张|零 model_init|图像 shape" \
  "/tmp/probe_${TAG}.log" "/tmp/e2e_${TAG}.log" 2>/dev/null | head -n 60
