#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# **开机后自动跑真机验证**（2026-11-26）
#
# 背景
# ----
# 这块板子的两张 RK182X NPU 卡的 PCIe 端点卡死过 ⇒ 只能整机重启恢复。
# 重启会把 **agent 会话**（跑在 ssh 会话里，不是 systemd ⇒ 不会自己回来）
# 一起干掉 ⇒ 所以把验证做成 **systemd oneshot**（``wdnwebui-post-reboot-verify.service``）
# ⇒ 开机自动跑完 ⇒ 结果落盘 ⇒ 谁在都能读。
#
# 它做什么
# --------
# 1. 等 ``wdnwebui.service`` 起来；
# 2. 等三个模型（ASR / MT / TTS）**都 running**；
# 3. 跑 **真 TTS 预合成闭环**（``verify_prefetch_cache.py``）；
# 4. 跑 **端到端**（``verify_interp_video_sync.py``，含新增的 4 条预合成断言）；
# 5. 把每一步的输出和最终结论写进 ``/userdata/wdnwebui/.post-reboot-verify.log``。
#
# ⚠️ **会占用服务进程里那一个 TTS 队列**（真 TTS 只能在那儿跑）⇒
#    开机后**别同时**在用同传。
set -u

ROOT=/userdata/wdnwebui
LOG="$ROOT/.post-reboot-verify.log"
PY="$ROOT/.venv/bin/python"

# ⚠️⚠️ **日志文件必须由本脚本自己创建**（2026-11-26 实测修）——
#
# 本脚本以 **rockchip** 身份跑（Playwright 浏览器在
# /home/rockchip/.cache/ms-playwright）⇒ 而**上一轮**留下的日志文件是
# **root:root 644** ⇒ rockchip **没有写权限** ⇒ 原来的 ``: > "$LOG"``
# **静默失败**（本脚本没开 ``set -e``）⇒ 新旧日志**混在一起**，
# 排查时分不清哪行是哪轮的。
#
# ⇒ 单元里的 ``ExecStartPre=rm -f`` 先以 root 身份删掉旧文件（root 删
#    root 的文件没问题）⇒ 这里再由 rockchip **自己创建** ⇒ 属主就是
#    rockchip ⇒ 后续追加有权限。
rm -f "$LOG" 2>/dev/null
: > "$LOG" 2>/dev/null || true

# ⚠️ **不要用 `tee -a "$LOG"`**（2026-11-26 实测修）——
# 单元里若同时配了 ``StandardOutput=append:<同一个文件>`` ⇒
# **systemd 和脚本各写一遍** ⇒ **每一行在日志里出现两遍**
# （上次 06:32 的日志就是这样：所有行都是成对的重复）⇒
# 看日志时每句话都要去重 ⇒ 极易误判。
#
# ⇒ 现在：**systemd 只收 journal**（``journalctl -u
# wdnwebui-post-reboot-verify.service``），**文件由本脚本独家写**。
say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

say "=== 开机后真机验证开始 ==="

# ---- 1. 等服务 ----
say "等 wdnwebui.service …"
for _ in $(seq 1 120); do
    if systemctl is-active --quiet wdnwebui.service; then
        say "  wdnwebui.service = active"
        break
    fi
    sleep 2
done
if ! systemctl is-active --quiet wdnwebui.service; then
    say "!! wdnwebui.service 没起来 ⇒ 放弃验证"
    exit 1
fi

# ---- 2. 等模型 ----
say "等三个模型（ASR / MT / TTS）running …"
models_ready() {
    local tts asr mt
    tts=$(curl -sk "https://127.0.0.1:8000/api/compute/instances-by-capability?capability=swcap_text_to_speech" 2>/dev/null)
    asr=$(curl -sk "https://127.0.0.1:8000/api/compute/instances-by-capability?capability=swcap_audio_asr" 2>/dev/null)
    mt=$(curl -sk "https://127.0.0.1:8000/api/compute/instances-by-capability?capability=swcap_translate" 2>/dev/null)
    python3 - "$tts" "$asr" "$mt" <<'PYEOF'
import json, sys
ok = True
for name, raw in zip(("TTS", "ASR", "MT"), sys.argv[1:4]):
    try:
        d = json.loads(raw)
    except Exception:
        print("  %s: 解析失败" % name); ok = False; continue
    n = len(d.get("instances") or [])
    print("  %s: %d 个 running" % (name, n))
    if n < 1:
        ok = False
sys.exit(0 if ok else 1)
PYEOF
}
ready=0
for i in $(seq 1 60); do
    if models_ready; then ready=1; break; fi
    sleep 5
done
if [ "$ready" != "1" ]; then
    say "!! 模型没都起来 ⇒ 放弃验证（看上面的计数）"
    exit 1
fi
say "  三个模型都 running"

# ---- 3. 真 TTS 预合成闭环 ----
say ""
say "=== [1/2] verify_prefetch_cache.py（真 TTS 预合成缓存闭环）==="
# ⚠️⚠️ **必须加 `-u`**（2026-11-26 实测修）——
# ``python`` 检测到 stdout 是**管道**（``| tee``）⇒ 切换成**全缓冲**
# ⇒ 整个脚本跑完才刷出来 ⇒ 中途日志**一行都没有** ⇒ 看着像「卡死了」。
# （实测：``[2/2]`` 那行之后整整两分钟日志空白，实际浏览器早已启动。）
timeout 600 "$PY" -u "$ROOT/scripts/verify_prefetch_cache.py" 2>&1 | tee -a "$LOG"
rc1=${PIPESTATUS[0]}
say "[1/2] 退出码 = $rc1"

# ---- 4. 端到端 ----
say ""
say "=== [2/2] verify_interp_video_sync.py（端到端，含 4 条预合成断言）==="
timeout 900 "$PY" -u "$ROOT/scripts/verify_interp_video_sync.py" 2>&1 | tee -a "$LOG"
rc2=${PIPESTATUS[0]}
say "[2/2] 退出码 = $rc2"

say ""
say "=== 结论：verify_prefetch_cache=$rc1  verify_interp_video_sync=$rc2 ==="
if [ "$rc1" = "0" ] && [ "$rc2" = "0" ]; then
    say "=== ALL PASS ==="
else
    say "=== 有失败 ⇒ 看上面的 FAIL 行 ==="
fi
say "=== 开机后真机验证结束 ==="
exit 0
