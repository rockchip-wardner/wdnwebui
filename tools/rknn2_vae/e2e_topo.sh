#!/usr/bin/env bash
# The end-to-end image generation regression (Qwen-Image-2.1 / media-generator
# card)
#
# Usage: e2e_topo.sh [steps] [prompt]
#   default: 8 steps + "a cute cat sitting on a wooden chair"
#
# ⭐ The state snapshot is at `GET /api/media-generator/` (**with the
#   trailing slash**) — `GET /api/media-generator/status` does not exist and
#   returns `{"detail":"Not Found"}`.
#   ⚠️ The JSON that comes back is the compact form of `json.dumps`
#   (**no space after the colon**), so the grep has to be written as
#   '"capability_ready":true' (with a space it never matches).
#
# ⚠️ Wait for the model to finish loading before sending the request — the
#    worker needs ~140 s to become ready (TE + the 4 DiT stages + the VAE
#    all stay resident); a request sent before that is rejected by the
#    service.
set -u

STEPS="${1:-8}"
PROMPT="${2:-a cute cat sitting on a wooden chair}"
OUT=${OUT:-/tmp/e2e_topo.png}
BASE=${BASE:-http://127.0.0.1:8000}
PY=/home/rockchip/wdnwebui/.venv/bin/python

echo "== waiting for capability_ready =="
for i in $(seq 1 90); do
  R=$(curl -s "$BASE/api/media-generator/" 2>/dev/null)
  case "$R" in
    *'"capability_ready":true'*) echo "   ready"; break ;;
  esac
  sleep 5
done

echo "== generate (steps=$STEPS) =="
GEN=$(curl -s -X POST "$BASE/api/media-generator/generate" \
      -H 'Content-Type: application/json' \
      -d "{\"prompt\":\"$PROMPT\",\"steps\":$STEPS,\"fixed_noise\":true}")
echo "   $GEN"
RUN=$(printf '%s' "$GEN" | sed -n 's/.*"run_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
if [ -z "$RUN" ]; then echo "!! no run_id received"; exit 1; fi
echo "   run_id=$RUN"

for i in $(seq 1 120); do
  S=$(curl -s "$BASE/api/media-generator/")
  ST=$(printf '%s' "$S" | sed -n 's/.*"state"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  EL=$(printf '%s' "$S" | sed -n 's/.*"elapsed"[[:space:]]*:[[:space:]]*\([0-9.]*\).*/\1/p')
  echo "   [$i] state=$ST elapsed=$EL"
  [ "$ST" = "idle" ] && break
  [ "$ST" = "error" ] && { echo "   ERROR!"; printf '%s\n' "$S"; exit 1; }
  sleep 3
done

echo "== fetching the image =="
curl -s "$BASE/api/media-generator/image/$RUN" -o "$OUT"
SZ=$(stat -c %s "$OUT")
echo "   size=$SZ  md5=$(md5sum "$OUT" | cut -d' ' -f1)"
if [ "$SZ" -lt 1000 ]; then echo "!! the response is not a PNG:"; cat "$OUT"; echo; exit 1; fi
"$PY" - "$OUT" <<'PY'
import sys
import numpy as np
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB")
a = np.asarray(im).astype(float)
print("   size=%s std=%.2f colors=%d" % (im.size, a.std(), len(np.unique(a.reshape(-1, 3), axis=0))))
PY
