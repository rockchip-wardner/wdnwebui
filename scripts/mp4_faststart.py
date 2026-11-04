#!/usr/bin/env python3
"""把 mp4 改成 **faststart**（``moov`` 排在 ``mdat`` 之前）—— 纯容器改写，**不重编码**。

为什么需要：浏览器要拿到 ``moov``（索引）才知道怎么播。``moov`` 在文件尾
时，浏览器必须**下完整个文件**才起播 —— 应用面板里的预览视频会先黑一阵
才动。``moov`` 在文件头（faststart）时，拿到头部就能起播。

本脚本只搬 box + 修偏移表，**像素数据一个字节都不动**：

* 顶层顺序改成 ``ftyp`` → ``moov`` → ``mdat``（``free`` 之类的填充 box 丢掉，
  没有信息量）；
* ``mdat`` 的**载荷**整体后移了 ``delta`` 字节，于是 ``stbl`` 里
  ``stco``（32-bit chunk offset）/ ``co64``（64-bit）里的**绝对偏移**必须
  跟着加 ``delta`` —— 漏了就是「能播但画面是花的 / 直接解不出帧」。

用 ``stco`` / ``co64`` 之外的偏移机制（``sidx`` / ``saio``）的文件会被
**拒绝处理**（报出来），而不是默默改坏 —— 那些表的目标语义不同，不在
本脚本的射程内。

用法::

    # 只体检（不改文件）
    .venv/bin/python scripts/mp4_faststart.py --check a.mp4 b.mp4

    # 就地改写（先写临时文件，校验通过才替换原文件）
    .venv/bin/python scripts/mp4_faststart.py --fix a.mp4 b.mp4
"""
from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

#: 需要递归下钻的容器 box（其 payload 还是 box 序列）
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta",
              b"mvex", b"moof", b"traf"}

#: 含有**绝对文件偏移**的 box —— 搬 moov 时必须改写
OFFSET_BOXES = {b"stco": 4, b"co64": 8}

#: 本脚本不认识的 box —— 出现了就拒绝处理（见文件头说明）
UNSUPPORTED = {b"sidx", b"saio", b"iloc", b"mfra"}


class Unsupported(Exception):
    """文件里有本脚本不敢碰的结构。"""


def _boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int, int]]:
    """列出 ``[start, end)`` 里的 box：``(类型, 偏移, 总大小, 头大小)``。"""
    out: list[tuple[bytes, int, int, int]] = []
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", data[off:off + 4])[0]
        typ = data[off + 4:off + 8]
        hdr = 8
        if size == 1:
            if off + 16 > end:
                break
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            break
        out.append((typ, off, size, hdr))
        off += size
    return out


def _walk(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int, int]]:
    """递归列出所有 box（含容器内部）。"""
    out: list[tuple[bytes, int, int, int]] = []
    for typ, off, size, hdr in _boxes(data, start, end):
        out.append((typ, off, size, hdr))
        if typ in CONTAINERS:
            out.extend(_walk(data, off + hdr, off + size))
    return out


def _top(data: bytes) -> list[tuple[bytes, int, int, int]]:
    return _boxes(data, 0, len(data))


def is_faststart(path: Path) -> tuple[bool, str]:
    """体检：返回 ``(是否 faststart, 说明)``。"""
    data = path.read_bytes()
    tops = _top(data)
    order = [t for t, _, _, _ in tops]
    if b"moov" not in order:
        return False, "没有 moov box（不是可播的 mp4）"
    if b"mdat" not in order:
        return False, "没有 mdat box"
    i_moov = order.index(b"moov")
    i_mdat = order.index(b"mdat")
    if i_moov < i_mdat:
        return True, f"faststart（moov@第{i_moov}个，mdat@第{i_mdat}个）"
    return False, (f"moov 在 mdat 之后（第{i_moov} vs 第{i_mdat}）"
                   f" —— 浏览器要下完整个文件才起播")


def _patch_offsets(moov: bytearray, delta: int) -> int:
    """把 moov 里 ``stco`` / ``co64`` 的绝对偏移整体平移 ``delta``。"""
    data = bytes(moov)
    n = 0
    for typ, off, size, hdr in _walk(data, 0, len(data)):
        if typ not in OFFSET_BOXES:
            continue
        width = OFFSET_BOXES[typ]
        count = struct.unpack(">I", data[off + hdr + 4:off + hdr + 8])[0]
        base = off + hdr + 8
        for i in range(count):
            p = base + i * width
            if p + width > off + size:
                raise Unsupported(f"{typ.decode()} 条目数与 box 大小对不上")
            if width == 4:
                v = struct.unpack(">I", data[p:p + 4])[0]
                struct.pack_into(">I", moov, p, (v + delta) & 0xFFFFFFFF)
            else:
                v = struct.unpack(">Q", data[p:p + 8])[0]
                struct.pack_into(">Q", moov, p, v + delta)
            n += 1
    return n


def faststart_bytes(data: bytes) -> bytes:
    """返回 faststart 后的 mp4 字节（无损：只搬 box + 改偏移表）。"""
    tops = _top(data)
    names = [t for t, _, _, _ in tops]
    present = {t for t, _, _, _ in _walk(data, 0, len(data))}
    for bad in sorted(present & UNSUPPORTED):
        raise Unsupported(
            f"文件里有 {bad.decode()} box（偏移语义不同），本脚本不处理")
    if b"moov" not in names or b"mdat" not in names:
        raise Unsupported("缺 moov / mdat，不是可重排的 mp4")
    if names.index(b"moov") < names.index(b"mdat"):
        return data  # 已经是 faststart

    moov = next(b for b in tops if b[0] == b"moov")
    mdat = next(b for b in tops if b[0] == b"mdat")
    _, moov_off, moov_size, _ = moov
    _, mdat_off, mdat_size, mdat_hdr = mdat

    moov_payload = bytearray(data[moov_off:moov_off + moov_size])
    mdat_payload = data[mdat_off + mdat_hdr:mdat_off + mdat_size]

    # 新顺序：head（在 moov/mdat 之前的 box，通常只有 ftyp）→ moov → mdat
    #        → tail（在两者之后的 box）
    first = min(moov_off, mdat_off)
    last = max(moov_off + moov_size, mdat_off + mdat_size)
    head = bytearray()
    tail = bytearray()
    for typ, off, size, hdr in tops:
        if off < first:
            head += data[off:off + size]
        elif off + size > last:
            tail += data[off:off + size]

    new_mdat_off = len(head) + len(moov_payload)
    new_payload_off = new_mdat_off + mdat_hdr
    old_payload_off = mdat_off + mdat_hdr
    delta = new_payload_off - old_payload_off

    if delta:
        _patch_offsets(moov_payload, delta)

    out = bytearray()
    out += head
    out += moov_payload
    mdat_hdr_bytes = bytearray(data[mdat_off:mdat_off + mdat_hdr])
    if mdat_hdr == 16:
        # 64-bit 大小的 mdat：size 字段是 1，真实大小在后面 8 字节
        struct.pack_into(">Q", mdat_hdr_bytes, 8, mdat_size)
    else:
        struct.pack_into(">I", mdat_hdr_bytes, 0, mdat_size)
    out += mdat_hdr_bytes
    out += mdat_payload
    out += tail
    return bytes(out)


def _decode_signature(path: Path) -> list[str]:
    """用 OpenCV 解码，逐帧取哈希（用来证明改写前后像素一致）。"""
    import cv2
    import hashlib

    cap = cv2.VideoCapture(str(path))
    sig: list[str] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            sig.append(hashlib.sha256(
                frame.tobytes() + str(frame.shape).encode()).hexdigest()[:16])
    finally:
        cap.release()
    return sig


def fix(path: Path) -> int:
    """就地改写成 faststart。返回 0 成功、1 失败。"""
    data = path.read_bytes()
    try:
        new = faststart_bytes(data)
    except Unsupported as e:
        print(f"[FAIL] {path}: {e}")
        return 1
    if new == data:
        print(f"[SKIP] {path}: 已经是 faststart")
        return 0

    before = _decode_signature(path)
    tmp = path.with_suffix(path.suffix + ".faststart.tmp")
    tmp.write_bytes(new)
    try:
        after = _decode_signature(tmp)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        print(f"[FAIL] {path}: 改写后解不出帧: {e}")
        return 1
    if before != after:
        tmp.unlink(missing_ok=True)
        print(f"[FAIL] {path}: 改写后解码结果与原文件不一致"
              f"（{len(before)} 帧 → {len(after)} 帧），已放弃")
        return 1
    shutil.move(str(tmp), str(path))
    ok, detail = is_faststart(path)
    print(f"[{'PASS' if ok else 'FAIL'}] {path}: {detail}"
          f"（{len(before)} 帧逐帧哈希一致）")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="mp4 faststart 检查 / 改写")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--check", action="store_true", help="只检查，不改文件")
    ap.add_argument("--fix", action="store_true", help="就地改写成 faststart")
    args = ap.parse_args()

    if args.check == args.fix:
        ap.error("--check / --fix 至少给一个")

    rc = 0
    for f in args.files:
        p = Path(f)
        if not p.is_file():
            print(f"[FAIL] {p}: 文件不存在")
            rc = 1
            continue
        if args.check:
            ok, detail = is_faststart(p)
            print(f"[{'PASS' if ok else 'FAIL'}] {p}: {detail}")
            if not ok:
                rc = 1
        else:
            rc |= fix(p)
    return rc


if __name__ == "__main__":
    sys.exit(main())
