"""TTS endpoints: REST control + WebSocket status stream.

Mirrors ``app/api/video_detection.py``:

* ``GET  /api/tts``         — current snapshot
* ``POST /api/tts/start``   — kick off a synthesis run
* ``POST /api/tts/stop``    — request graceful stop
* ``GET  /api/tts/history`` — recent output WAVs
* ``GET  /api/tts/output``  — stream the WAV produced by the latest run
* ``WS   /api/tts/ws``      — pushes snapshots on state change
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse

from app.apps.tts.service import (
    PLAY_DEVICE,
    PLAY_TARGETS,
    TTSRequest,
    VOICE_PREVIEW_DIR,
    default_ref_speaker,
    get_session,
    voice_catalog,
)


router = APIRouter(tags=["tts"])


# Plain-print so diagnostics land in uvicorn's stdout regardless of
# how the root logger is configured.
def _tts_log(msg: str) -> None:
    print(f"[tts_api] {msg}", flush=True)


@router.get("/")
async def get_tts() -> dict:
    return get_session().snapshot()


def _voice_catalog() -> dict:
    """音色 + 语种清单(取自 SDK 的表,不在前端硬编码)。"""
    try:
        return voice_catalog(get_session().model_dir)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"音色清单不可用: {e}")


@router.get("/voices")
async def tts_voices() -> dict:
    """可用音色 + 语种清单。前端下拉按这个渲染,不在页面里硬编码。"""
    return _voice_catalog()


@router.get("/voice-preview/{name}")
async def tts_voice_preview(name: str):
    """自带音色的试听样例(WAV)。

    样例是离线生成的(见 ``scripts/gen_voice_previews.py``)。名字必须白名单
    校验,否则 ``../`` 能把目录外面的文件读出来。
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise HTTPException(status_code=400, detail="音色名不合法")
    path = VOICE_PREVIEW_DIR / (name + ".wav")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="该音色没有试听样例")
    return FileResponse(str(path), media_type="audio/wav")


# --------------------------------------------------------------------------- #
# 用户音色（上传参考音频 → 现场提取 x-vector）                                  #
# --------------------------------------------------------------------------- #
# 上传体积上限 —— 提取器只用前 5 秒,但先挡住离谱的大文件。
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 参考音频的时长边界。
#   下限：太短的话 5 秒窗口里大半是补零,x-vector 没有意义
#         （实测 1.5 秒还能用,再短就开始不稳）。
#   上限：超过 30 秒基本是"传错文件"（整首歌/一段节目）—— 只取前
#         5 秒会提到伴奏或静音,不如直接报错让人裁。
_MIN_UPLOAD_SECONDS = 1.0
_MAX_UPLOAD_SECONDS = 30.0
# 提取走 NPU:开 RKNN 上下文(~0.45s)+ 推理(~20ms)。给 60s 余量。
_REGISTER_TIMEOUT_MS = 60_000


def _worker_rpc_session():
    """拿到 TTS worker 的 RPC session（app 进程侧）。

    和 :meth:`TtsSession._infer_one` 同一条路：算力配置页启用的实例。
    app 进程里 ``sess._backend`` 是 None（推理在 worker 进程），所以音色
    **提取**必须发到 worker 那边做。
    """
    from app.core.compute.registry import get_registry
    from wdnai import SoftwareCapability
    return get_registry().get_session(SoftwareCapability.TEXT_TO_SPEECH)


def _sanitize_voice_name(raw: str) -> str:
    """把用户给的名字收拾成能当文件名的 slug。

    规则和 SDK 的 ``Qwen3TTSDriver.normalize_user_speaker_name`` 一致
    （Unicode 字母 / 数字 / 下划线 / 连字符，以字母数字开头，最长 32 字符），
    这里先收一道是为了在写盘之前就给出 400，而不是等 worker 报错。

    **允许中文** —— 名字是给人看的（「我的声音」比 ``my_voice`` 好认），
    要挡的是文件系统上危险的东西（路径分隔符、点、控制字符）。

    统一做 **NFC 归一化**：macOS 会把带音标的字母拆成「基字母 + 组合符号」
    （NFD），不归一化的话同一个名字有多种字节形态，就会「明明注册过却查不到」。
    """
    import re
    import unicodedata
    name = unicodedata.normalize("NFC", (raw or "").strip().lower())
    if not name:
        raise HTTPException(status_code=400, detail="音色名不能为空")
    if len(name) > 32:
        raise HTTPException(status_code=400, detail="音色名过长（最多 32 个字符）")
    # [^\W_] = 字母或数字（\w 在 str 模式下 Unicode 感知，会匹配汉字；
    # 排除 _ 是因为下划线不能开头）
    if not re.match(r"^[^\W_][\w-]*$", name, re.UNICODE):
        raise HTTPException(
            status_code=400,
            detail="音色名只能包含字母、数字、下划线、连字符（支持中文），"
                   "且必须以字母或数字开头",
        )
    return name


class _WavProbeError(Exception):
    """WAV 头探测失败 —— 对外一律 400（是用户传的文件不对，不是服务端坏了）。"""


def _probe_upload_wav(path: str, blob: bytes) -> dict:
    """探一下上传的音频 → 头部信息 + 时长（秒）。

    优先用 SDK 的 ``probe_wav_bytes``（只读头，不解码）—— 20 MB 的文件
    解成 float32 就是 80 MB 内存,没必要。SDK 版本太老没这个接口时
    退回 ``read_wav``（会解码整段,慢一点但结论一样）。
    """
    try:
        from wdnai.backends.speaker_encode import probe_wav_bytes
    except ImportError:
        probe_wav_bytes = None
    if probe_wav_bytes is not None:
        try:
            return probe_wav_bytes(blob)
        except Exception as exc:  # noqa: BLE001
            raise _WavProbeError(str(exc))
    try:
        from wdnai.backends.speaker_encode import read_wav
        pcm, sr = read_wav(path)
    except Exception as exc:  # noqa: BLE001
        raise _WavProbeError(str(exc))
    return {"duration": len(pcm) / float(sr or 1), "channels": 1,
            "sample_rate": sr, "bits": 16}


def _drop_wav(path) -> None:
    """把没验过的 .wav 从盘上删掉（失败的上传不留半成品）。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _discard_user_voice(voice_name: str, wav_path) -> None:
    """把一次没走完的上传彻底退回去。

    两条失败路径都会调它:
    * worker 提取失败 —— 盘上那半截 .wav 得删,worker 里也可能已经注册上了;
    * embedding 存盘失败 —— worker 里已经有了但盘上没有,两边不一致。

    宁可"这次上传什么都没发生",也不要留一个看不见的半成品音色:
    用户重传同名文件时会被静默顶掉,那种坑最难查。
    """
    try:
        Path(wav_path).unlink(missing_ok=True)
    except OSError:
        pass
    try:
        rpc = _worker_rpc_session()
        rpc.call_op(
            "unregister_speaker", {"name": voice_name}, timeout_ms=10_000
        )
    except Exception:  # noqa: BLE001
        pass  # worker 不在 / 正忙 —— 后面注册那步会自己报错,这里不重复报错


async def _register_voice(voice_name: str, blob: bytes, source: str) -> dict:
    """把一段音频注册成用户音色。

    两条入口共用这一段：

    * ``POST /voices/upload`` —— 浏览器拖进来的文件（multipart）
    * ``POST /voices/upload-from-path`` —— 板子上已有的文件（设备端
      文件浏览器选中的那条路）
    """
    from app.core.uploads import get_tts_user_voices_root
    voices_root = get_tts_user_voices_root()

    # 同名**报错**而不是覆盖 —— 用户辛苦传的声音被静默顶掉是最难查的坑。
    # 要换音色先删掉旧的。
    if (voices_root / f"{voice_name}.npy").is_file():
        raise HTTPException(
            status_code=409,
            detail=f"音色「{voice_name}」已存在，换个名字，或先把它删掉",
        )

    # 和内置/参考音色重名同理：同一个名字只能指一个音色。
    # SDK 那边只挡了内嵌参考音色（ref），内置音色（builtin）撞名会
    # 在下拉里出现两个同名项、还会遮住原来的那个 —— 这里一起挡掉。
    # 清单拿不到时不挡（别让一次上传因为清单不可用直接失败）。
    try:
        catalog = voice_catalog(get_session().model_dir)
    except Exception:  # noqa: BLE001
        catalog = {}
    for v in catalog.get("voices", []):
        if v.get("id") == voice_name and v.get("kind") != "user":
            raise HTTPException(
                status_code=409,
                detail=f"音色「{voice_name}」与内置音色重名，换个名字",
            )

    wav_path = voices_root / f"{voice_name}.wav"

    if not blob:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(blob) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB）",
        )
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise HTTPException(
            status_code=400,
            detail="只支持 WAV/PCM 文件（RIFF/WAVE）；其它格式请先转成 WAV",
        )
    # 先把内容写盘 —— worker 要按路径读（同一块板、同一文件系统）。
    try:
        wav_path.write_bytes(blob)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"写入 {wav_path} 失败: {exc}")

    # 探一下音频头：结构不对 / 时长越界的在这里就挡掉 —— 别让 worker
    # 花 0.5s 开 NPU 上下文才发现白跑一趟。
    try:
        info = _probe_upload_wav(str(wav_path), blob)
    except _WavProbeError as exc:
        _drop_wav(wav_path)
        raise HTTPException(status_code=400, detail=f"无法解析音频: {exc}")
    dur = float(info.get("duration") or 0.0)
    if dur < _MIN_UPLOAD_SECONDS:
        _drop_wav(wav_path)
        raise HTTPException(
            status_code=400,
            detail=f"音频太短（{dur:.2f} 秒）：音色提取至少需要 "
                   f"{_MIN_UPLOAD_SECONDS:g} 秒有效人声",
        )
    if dur > _MAX_UPLOAD_SECONDS:
        _drop_wav(wav_path)
        raise HTTPException(
            status_code=400,
            detail=f"音频太长（{dur:.1f} 秒）：请裁到 {_MAX_UPLOAD_SECONDS:g} "
                   f"秒以内的片段（提取只用前 5 秒）",
        )

    # 让 worker 提取 + 注入音色表。
    try:
        rpc = _worker_rpc_session()
    except RuntimeError as exc:
        _drop_wav(wav_path)
        raise HTTPException(status_code=503, detail=f"算力未就绪: {exc}")
    try:
        reply = rpc.call_op(
            "register_speaker",
            {"name": voice_name, "path": str(wav_path)},
            timeout_ms=_REGISTER_TIMEOUT_MS,
        )
    except Exception as exc:  # noqa: BLE001
        _discard_user_voice(voice_name, wav_path)
        raise HTTPException(status_code=500, detail=f"音色提取失败: {exc}")

    key = str(reply.get("name") or voice_name)
    embedding = reply.get("embedding") or []
    dim = int(reply.get("dim") or 0)

    # 落盘：embedding + 元信息。worker 下次启动从这里恢复。
    import numpy as np
    try:
        vec = np.asarray(embedding, dtype=np.float32)
        np.save(voices_root / f"{key}.npy", vec)
        meta = {
            "name": key,
            "source": source or f"{key}.wav",
            "dim": int(dim or vec.shape[0]),
            "bytes": len(blob),
            "created": time.time(),
        }
        (voices_root / f"{key}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        _discard_user_voice(voice_name, wav_path)
        raise HTTPException(status_code=500, detail=f"保存音色失败: {exc}")

    _tts_log(f"registered user voice '{key}' (dim={dim})")
    return {
        "status": "ok",
        "name": key,
        "dim": dim,
        "path": str(wav_path),
        "voices": voice_catalog(get_session().model_dir).get("voices", []),
    }


def _resolve_board_file(raw_path: str) -> Path:
    """把用户给的板子路径解析成**根目录内的普通文件**。

    越界 / 黑名单 / 不存在 / 是目录 / 太大 —— 一律拒。
    根目录限制和设备端文件浏览器（``/api/fs``）共用同一份实现。
    """
    from app.system.api.fs_browser import resolve_within_root
    target = resolve_within_root(raw_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"不存在: {target}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"不是文件: {target}")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"无法读取文件信息: {exc}")
    if size > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB）",
        )
    return target


@router.post("/voices/upload")
async def tts_voice_upload(
    file: UploadFile = File(..., description="参考音频（WAV/PCM）"),
    name: str = Form("", description="音色名；留空则取文件名"),
) -> dict:
    """上传一段参考音频 → 现场提取 x-vector → 注册成可用音色。

    提取在 **worker 进程**里做（app 进程没有 NPU 上下文）；embedding
    由 worker 算好后返回，本进程负责落盘（``<resource_dir>/tts_voices/``
    下的 ``<name>.npy`` + ``<name>.json`` + 原始 ``<name>.wav``）。

    worker 重启时会从这个目录把音色读回来，所以用户音色不会丢。

    这条是**浏览器拖进来的文件**那条路；板子上已有的文件走
    ``POST /voices/upload-from-path``。
    """
    raw = (file.filename or "").strip()
    stem = Path(raw).stem if raw else ""
    voice_name = _sanitize_voice_name(name or stem)
    try:
        blob = await file.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"读取上传文件失败: {exc}")
    return await _register_voice(voice_name, blob, source=raw or voice_name)


# --------------------------------------------------------------------------- #
# 板子麦克风录音（浏览器录音的兜底路）
# --------------------------------------------------------------------------- #
_RECORD_SR = 16000            # 录 16 kHz，板端会重采样到 24 kHz
_RECORD_MIN_SECONDS = 1.0
_RECORD_MAX_SECONDS = 15.0
_RECORD_DEFAULT_SECONDS = 6.0


def _capture_devices() -> list:
    """列出板子上可用的**采集**设备（跳过 HDMI IN —— 那是视频源的音频）。

    返回 ``[("hw:1,0", "dailink-multicodecs"), ...]``。

    失败时**把原因记进日志**：吞掉异常只会让用户看到一句
    「没有可用的麦克风采集设备」，根本不知道是 arecord 没装、
    还是权限不够、还是输出格式变了。
    """
    import re
    import subprocess
    try:
        proc = subprocess.run(["arecord", "-l"], capture_output=True,
                              text=True, timeout=10)
        out = proc.stdout or ""
        if not out.strip():
            _tts_log("arecord -l 没有输出 (rc={}): {}".format(
                proc.returncode, (proc.stderr or "")[:300]))
            return []
    except Exception as exc:  # noqa: BLE001
        _tts_log("arecord -l 调用失败: {!r}".format(exc))
        return []
    devs: list = []
    in_capture = False
    for line in out.splitlines():
        # arecord -l 的分段标题长这样：
        #   ---- List of CAPTURE Hardware Devices ----
        #   card 0: xxx, device 0: yyy
        # 设备行**本身不含 CAPTURE 这个词**，得靠标题判断当前在哪个段。
        if "List of" in line:
            in_capture = "CAPTURE" in line
            continue
        if not in_capture:
            continue
        if "hdmi" in line.lower():
            continue                  # HDMI IN 的音频是视频源，不是麦克风
        m = re.search(r"card (\d+):.*device (\d+):\s*(\S+)", line)
        if not m:
            continue
        devs.append(("hw:{},{}".format(m.group(1), m.group(2)), m.group(3)))
    return devs


def _write_wav_pcm16(path: Path, samples, sr: int, channels: int = 1) -> None:
    """把 ``[-1, 1]`` 的浮点采样写成 16 bit PCM 的 RIFF/WAVE。

    **必须自己乘 32767**：``np.astype("<i2")`` 是直接截断而不是换算，
    0.016 会被截成 0 —— 整段就成静音了（踩过一次：板子麦克风录出来
    的音色全是零，提取出来的 x-vector 其实是空 Audio 的）。
    """
    import struct

    import numpy as np
    data = np.clip(np.asarray(samples, dtype=np.float64) * 32767.0,
                   -32768.0, 32767.0).astype("<i2").tobytes()
    block_align = channels * 2
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sr,
                                   sr * block_align, block_align, 16)
    header += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(header)


def _normalize_wav(path: Path, target_rms: float = 0.05) -> None:
    """把录音的响度拉到一个合理区间。

    板子麦克风的增益很低（实测静音房间 RMS ≈ 0.0008，约 -62 dBFS），
    而 x-vector 对绝对电平敏感 —— 太安静的音频提出来的音色不可靠。
    最多放大 20 倍，避免把噪声底也一起抬上去。
    """
    import numpy as np
    from wdnai.backends.speaker_encode import read_wav
    pcm, sr = read_wav(str(path))
    if pcm.size == 0:
        return
    rms = float(np.sqrt((pcm.astype(np.float64) ** 2).mean()))
    if rms <= 1e-6:
        return                       # 全静音，没什么可归一化的
    gain = min(target_rms / rms, 20.0)
    if abs(gain - 1.0) < 0.2:
        return
    scaled = np.clip(pcm * gain, -1.0, 1.0)
    _write_wav_pcm16(path, scaled, sr, 1)


def _wav_probe(path: Path) -> tuple:
    """读 WAV 头 → ``(采样率, 秒数)``。解不了就返回 ``(0, 0.0)``。"""
    try:
        from wdnai.backends.speaker_encode import probe_wav_bytes
        info = probe_wav_bytes(Path(path).read_bytes())
        return int(info.get("sample_rate") or 0), float(info.get("duration") or 0.0)
    except Exception:  # noqa: BLE001
        return 0, 0.0


async def _record_board_wav(seconds: float, out_path: Path) -> None:
    """用板子的麦克风录一段音频到 ``out_path``（WAV/PCM16）。

    两个坑要挨个试：

    * **声道数**：ES8388 的采集只支持双声道（单声道会报
      ``Channels count non available``）。
    * **采样率不由我们定**：设备实际跑多少率取决于当前音频图（实测见过
      8k / 16k / 48k）。所以**不能用 arecord 的 ``-d``** —— 它是按
      *请求*的率数帧的：请求 16 kHz 而设备跑 48 kHz 时，3 秒的请求
      只录到 1 秒的音频（够不着提取器的 1 秒下限 → 400「音频太短」）。
      改成自己掐表、到点给 arecord 发 SIGINT（它会正常收尾并补齐
      WAV 头），这样录多长只取决于真实时间，和率无关。

    录完还要**核对实际时长**：对不上就重试，宁可报错也不要交一段
    时长不对的音频 —— 那种提取出来的是废音色，用户还听不出为什么。
    """
    import signal

    devs = _capture_devices()
    if not devs:
        raise RuntimeError("板子上没有可用的麦克风采集设备")
    last_err = ""
    for dev, _name in devs:
        for ch in (2, 1):
            cmd = ["arecord", "-D", dev, "-f", "S16_LE",
                   "-r", str(_RECORD_SR), "-c", str(ch), str(out_path)]
            t0 = time.monotonic()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            try:
                # 多等 0.3s：arecord 打开设备要一会儿，那段时间不采到东西
                await asyncio.sleep(seconds + 0.3)
            finally:
                try:
                    proc.send_signal(signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass            # 已经自己退出了
            _o, err = await proc.communicate()
            wall = time.monotonic() - t0
            if not (out_path.is_file() and out_path.stat().st_size > 44):
                last_err = (err or b"").decode("utf-8", "replace")[-200:]
                _tts_log("arecord 失败 (dev={} ch={}): {}".format(dev, ch, last_err))
                break           # 这个声道开不起来 → 换声道数
            sr, dur = _wav_probe(out_path)
            if seconds * 0.7 <= dur <= seconds * 1.6:
                _tts_log("板子录音完成: dev={} ch={} sr={} {:.2f}s（墙钟 {:.2f}s）"
                         .format(dev, ch, sr, dur, wall))
                return
            last_err = ("录到 {:.2f}s（要 {:.0f}s，采样率 {}，墙钟 {:.2f}s）"
                        .format(dur, seconds, sr, wall))
            _tts_log("arecord 时长对不上: " + last_err)
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
    raise RuntimeError("麦克风录制失败: {}".format(last_err or "未知原因"))


@router.post("/voices/record-from-board")
async def tts_voice_record_from_board(payload: Optional[dict] = None) -> dict:
    """用**板子自己的麦克风**录一段话 → 现场提取 x-vector → 注册音色。

    浏览器录音的兜底路：浏览器只在**安全上下文**（https 或
    http://localhost）里才有 ``navigator.mediaDevices``，明文
    ``http://<板子IP>:8000`` 这种局域网访问里它是 undefined，点了
    「开始录音」什么都不会发生（实测）。板子自己带麦克风（ES8388），
    这条路在明文 HTTP 下照样能录。

    Body: ``{"seconds": 6, "name": "rec_143052"}``
    """
    import tempfile
    payload = payload or {}
    try:
        seconds = float(payload.get("seconds") or _RECORD_DEFAULT_SECONDS)
    except (TypeError, ValueError):
        seconds = _RECORD_DEFAULT_SECONDS
    seconds = max(_RECORD_MIN_SECONDS + 0.5,
                  min(seconds, _RECORD_MAX_SECONDS))
    raw_name = str(payload.get("name") or "").strip()
    voice_name = _sanitize_voice_name(raw_name or "board_rec")

    fd, tmp_name = tempfile.mkstemp(prefix="tts_rec_", suffix=".wav")
    import os
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        try:
            await _record_board_wav(seconds, tmp)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=f"录不了：{exc}")
        _normalize_wav(tmp)
        blob = tmp.read_bytes()
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return await _register_voice(voice_name, blob, source="板子麦克风录音")


@router.post("/voices/upload-from-path")
async def tts_voice_upload_from_path(payload: Optional[dict] = None) -> dict:
    """从**板子上已有的文件**注册音色（设备端文件浏览器选中的那条路）。

    为什么要有这条：``<input type="file">`` 打开的是**浏览器所在机器**
    的文件对话框 —— 在板子自己的浏览器上（HDMI 演示那种用法）它要
    枚举板子的文件系统，慢到几十秒；远程访问时它又只能看到 PC 的
    文件、看不到板子上的。设备端文件浏览器（``/api/fs``）列的是板子
    上的真实文件，选中后直接把路径交给后端读，不经过浏览器。

    Body: ``{"path": "/userdata/xxx/a.wav", "name": "my_girl"}``
    """
    payload = payload or {}
    raw_path = str(payload.get("path") or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="缺少 path")
    target = _resolve_board_file(raw_path)
    voice_name = _sanitize_voice_name(
        str(payload.get("name") or "").strip() or Path(str(target)).stem
    )
    try:
        blob = target.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"读取文件失败: {exc}")
    return await _register_voice(voice_name, blob, source=str(target))


@router.delete("/voices/{name}")
async def tts_voice_delete(name: str) -> dict:
    """注销一个用户音色（从音色表移除 + 删掉落盘的文件）。

    **先删盘上的文件，再去 worker 那边注销**，而且 worker 那步是
    *尽力而为*的：

    * 反过来（先 worker 后删文件）的话，worker 没就绪（模型还在
      加载）时整个删除会失败 —— 用户连"把音色删掉"都做不到，文件
      还赖在盘上。
    * 文件删了、worker 没同步成功的残留**是安全的**：清单是从盘上
      算的（`voice_catalog` → `_user_voice_entries`），用户再也看不到
      也用不了这个音色；worker 内存里那份下次重启就没了。反过来
      （worker 删了、文件还在）才会出事 —— 清单里还列着它，合成时
      SDK 在 `speaker_embeddings` 里查不到，**静默回落到默认音色**，
      用户听不出自己选的音色没了。
    """
    key = _sanitize_voice_name(name)

    from app.core.uploads import get_tts_user_voices_root
    voices_root = get_tts_user_voices_root()

    for suffix in (".npy", ".json", ".wav"):
        try:
            (voices_root / f"{key}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass

    removed = False
    worker_synced = False
    worker_error = ""
    try:
        rpc = _worker_rpc_session()
        reply = rpc.call_op(
            "unregister_speaker", {"name": key}, timeout_ms=_REGISTER_TIMEOUT_MS
        )
        removed = bool(reply.get("removed"))
        worker_synced = True
    except Exception as exc:  # noqa: BLE001
        worker_error = str(exc)

    _tts_log(f"unregistered user voice '{key}' (removed={removed}, "
             f"worker_synced={worker_synced})")
    if not worker_synced:
        _tts_log(f"  worker 未同步（{worker_error}）—— 盘上文件已删，"
                 f"worker 内存里可能还有残留，重启后消失")
    return {
        "status": "ok",
        "name": key,
        "removed": removed,
        "worker_synced": worker_synced,
        "voices": voice_catalog(get_session().model_dir).get("voices", []),
    }


@router.post("/start")
async def tts_start(payload: Optional[dict] = None) -> dict:
    """Body fields match ``TTSRequest``: ``text`` (required), ``language``,
    ``speaker``, ``ref_speaker`` (default: the first bundled reference
    speaker),
    ``volume`` (0.0–1.0, default 0.1), ``play_to`` (``"device"`` /
    ``"browser"`` / ``"both"``, default ``"device"``), ``no_play``,
    ``non_streaming_mode``, ``output`` (auto-filled when empty).

    ``instruct`` (风格提示) 只作 SDK 透传,**页面不暴露**: 官方能力表里
    Base 权重没有 instruction control,填了不改变语气。

    When ``play_to`` contains ``"browser"`` the WAV written to ``output``
    is served back via ``GET /api/tts/output?path=...`` so the page can
    play it through a hidden ``<audio>`` element after generation."""
    payload = payload or {}
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        volume = float(payload.get("volume", 0.1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="volume 必须为 0.0 ~ 1.0 之间的数")
    volume = max(0.0, min(1.0, volume))

    play_to = str(payload.get("play_to", "") or PLAY_DEVICE).strip().lower()
    if play_to not in PLAY_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"play_to 必须是 {', '.join(PLAY_TARGETS)} 之一",
        )

    speaker = str(payload.get("speaker", "") or "").strip()
    ref_speaker = str(payload.get("ref_speaker", "") or "").strip()
    # 两个都不给才回落默认参考音色 —— 无条件补会把 speaker 的路堵死
    # (SDK 里 ref_speaker 优先级高于 speaker)。
    if not speaker and not ref_speaker:
        ref_speaker = default_ref_speaker()

    # 音色必须是本模型真有的:Base 权重里内置音色那几行是未训练的噪声,
    # 塞给 talker 等于没给音色条件 → 每次生成自由发挥。按清单拒掉。
    avail = {v["id"] for v in _voice_catalog().get("voices", [])}
    for name, kind in ((speaker, "内置音色"), (ref_speaker, "参考音色")):
        if name and name not in avail:
            raise HTTPException(
                status_code=400,
                detail=f"本模型没有{kind} {name!r};"
                       f"当前可用: {', '.join(sorted(avail)) or '无'}",
            )

    req = TTSRequest(
        text=text,
        language=str(payload.get("language", "auto") or "auto"),
        speaker=speaker,
        ref_speaker=ref_speaker,
        instruct=str(payload.get("instruct", "") or ""),
        volume=volume,
        no_play=bool(payload.get("no_play", False)),
        play_to=play_to,
        output=str(payload.get("output", "") or ""),
        non_streaming_mode=bool(payload.get("non_streaming_mode", False)),
    )

    try:
        return get_session().start(req)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        # Service unavailable (upstream missing / busy)
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/stop")
async def tts_stop() -> dict:
    return get_session().stop()


@router.get("/history")
async def tts_history() -> dict:
    return {"items": get_session().history()}


@router.get("/output")
async def tts_output(path: str) -> FileResponse:
    """Stream the WAV the most recent (or any historical) run wrote.

    Constrained to the TTS output directory so this endpoint can't be
    turned into an arbitrary-file proxy. The path is canonicalised
    and must resolve under ``<resource_dir>/tts``(典型设备端
    ``/userdata/test/tts``,off-device ``<HOME>/test/tts``;由
    :func:`app.core.uploads.get_tts_output_root` 解析,**不**走 repo
    内的 ``app/uploads/tts/`` —— 那个在 emmc 上,TTS 生成的音频写多了
    会踩爆 emmc 寿命)。"""
    if not path:
        raise HTTPException(status_code=400, detail="path 必填")

    from app.core.uploads import get_tts_output_root
    uploads_root = get_tts_output_root().resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(uploads_root)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"path 必须在 {uploads_root} 之下",
        )

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if target.stat().st_size == 0:
        raise HTTPException(status_code=404, detail="输出为空")

    return FileResponse(
        path=str(target),
        media_type="audio/wav",
        filename=target.name,
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------- #
# WebSocket status stream
# --------------------------------------------------------------------- #
@router.websocket("/ws")
async def tts_ws(ws: WebSocket) -> None:
    """Push TTS session snapshots on state change.

    Audio stream is opt-in: client sends ``{"type":"subscribe_audio"}``
    after connecting, then receives one ``audio_start`` JSON frame,
    followed by binary frames of 12-byte header
    ``(sr:u32, ch:u32, n:u32 little-endian)`` + ``n*ch*4`` bytes of
    float32 LE PCM, terminated by an ``audio_end`` JSON frame. The
    server's pump runs as a per-subscription background task; on
    pump exit it clears ``sub_id`` so the next ``subscribe_audio``
    always builds a fresh pump."""
    await ws.accept()
    sess = get_session()
    last_state: Optional[str] = None
    last_frames = -1
    last_msg: Optional[str] = None
    # capability_ready 单独记一份:模型被启用/停用时**会话状态不变**
    # (state 仍是 idle),原来的变化判据看不到 → 前端拿不到新快照 →
    # 启动按钮一直是 disabled,直到下一次 5s 轮询才恢复。
    # 把「能力就绪」也纳入推送判据,这一拍延迟就没了。
    last_cap: Optional[bool] = None

    sub_id: Optional[str] = None
    audio_q: Optional[asyncio.Queue] = None
    audio_sr = 24000
    audio_ch = 1
    stop_event = asyncio.Event()

    async def _pump_audio() -> None:
        nonlocal sub_id, audio_q
        assert audio_q is not None
        try:
            await ws.send_json({
                "type": "audio_start",
                "sample_rate": audio_sr,
                "channels": audio_ch,
            })
            chunks_sent = 0
            while not stop_event.is_set():
                data = await audio_q.get()
                if data == b"EOF":
                    break
                try:
                    await ws.send_bytes(data)
                    chunks_sent += 1
                except Exception:
                    return
            try:
                await ws.send_json({"type": "audio_end"})
            except Exception:
                pass
            _tts_log(f"ws[{id(ws)}] audio_end ({chunks_sent} chunks)")
        finally:
            # Clear the sub so the NEXT subscribe_audio creates a
            # fresh pump; without this, the ``and sub_id is None``
            # guard in ``_client_msg_loop`` would short-circuit.
            if sub_id is not None:
                sess.unsubscribe_audio(sub_id)
            sub_id = None
            audio_q = None

    async def _client_msg_loop() -> None:
        nonlocal sub_id, audio_q
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = (msg or {}).get("type")
                if mtype == "ping":
                    try:
                        await ws.send_json({"type": "pong"})
                    except Exception:
                        return
                elif mtype == "subscribe_audio" and sub_id is None:
                    sub_id = f"ws-{id(ws)}"
                    audio_q = sess.subscribe_audio(
                        sub_id, asyncio.get_running_loop())
                    stop_event.clear()
                    _tts_log(f"ws[{id(ws)}] subscribe_audio sub_id={sub_id}")
                    asyncio.create_task(_pump_audio())
                elif mtype == "unsubscribe_audio" and sub_id is not None:
                    stop_event.set()
                    sess.unsubscribe_audio(sub_id)
                    _tts_log(f"ws[{id(ws)}] unsubscribe")
                    sub_id = None
                    audio_q = None
        except WebSocketDisconnect:
            return

    def _push_snap() -> dict:
        snap = sess.snapshot()
        snap["type"] = "status"
        return snap

    client_task = asyncio.create_task(_client_msg_loop())

    try:
        await ws.send_json(_push_snap())
        snap0 = sess.snapshot()
        last_state = snap0["state"]
        last_frames = snap0["frames"]
        last_msg = snap0["error"]
        last_cap = snap0.get("capability_ready")

        while True:
            snap = sess.snapshot()
            changed = (
                snap["state"] != last_state
                or snap["frames"] != last_frames
                or (snap["error"] or "") != (last_msg or "")
                or snap.get("capability_ready") != last_cap
            )
            if changed:
                await ws.send_json(_push_snap())
                last_state = snap["state"]
                last_frames = snap["frames"]
                last_msg = snap["error"]
                last_cap = snap.get("capability_ready")
            await asyncio.sleep(0.2)
    except WebSocketDisconnect:
        return
    except Exception as e:                       # pragma: no cover
        try:
            await ws.send_json({"type": "error", "detail": str(e)})
        except Exception:
            pass
    finally:
        stop_event.set()
        if sub_id is not None:
            sess.unsubscribe_audio(sub_id)
        client_task.cancel()
        try:
            await client_task
        except (asyncio.CancelledError, Exception):
            pass