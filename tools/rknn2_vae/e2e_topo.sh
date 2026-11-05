#!/usr/bin/env bash
# 端到端出图回归（Qwen-Image-2.1 / media-generator 卡）
#
# 用法： e2e_topo.sh [steps] [prompt]
#   默认 8 步 + "a cute cat sitting on a wooden chair"
#
# ⭐ 状态快照在 `GET /api/media-generator/`（**带斜杠**）——
#   `GET /api/media-generator/status` 不存在，会返回 `{"detail":"Not Found"}`。
#   ⚠️ 返回的 JSON 是 `json.dumps` 的紧凑格式（**冒号后没有空格**），
#   所以 grep 要写 '"capability_ready":true'（写成带空格就永远匹配不上）。
#
# ⚠️ 等模型加载完再发请求 —— worker 起来后要 ~140 s 才 ready
#    （TE + DiT 4 段 + VAE 全部常驻），没 ready 就发会被 service 拒掉。
set -u

STEPS="${1:-8}"
PROMPT="${2:-a cute cat sitting on a wooden chair}"
OUT=${OUT:-/tmp/e2e_topo.png}
BASE=${BASE:-http://127.0.0.1:8000}
PY=/home/rockchip/wdnwebui/.venv/bin/python

echo "== 等 capability_ready =="
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
if [ -z "$RUN" ]; then echo "!! 没拿到 run_id"; exit 1; fi
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

echo "== 取图 =="
curl -s "$BASE/api/media-generator/image/$RUN" -o "$OUT"
SZ=$(stat -c %s "$OUT")
echo "   size=$SZ  md5=$(md5sum "$OUT" | cut -d' ' -f1)"
if [ "$SZ" -lt 1000 ]; then echo "!! 返回的不是 PNG："; cat "$OUT"; echo; exit 1; fi
"$PY" - "$OUT" <<'PY'
import sys
import numpy as np
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB")
a = np.asarray(im).astype(float)
print("   size=%s std=%.2f colors=%d" % (im.size, a.std(), len(np.unique(a.reshape(-1, 3), axis=0))))
PY
