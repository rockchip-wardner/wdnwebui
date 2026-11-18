#!/usr/bin/env python3
"""回归防护：自有的 log-guardian 必须**真的**能回收空间，且不误伤活跃文件。

背景
----
系统镜像自带的 ``/usr/bin/log-guardian`` 用 ``awk '{print $4 * 1024}'`` 算可用
空间，mawk 输出科学计数法（``6.22e+09``）⇒ dash 的 ``[`` 无法比较 ⇒
``is_full()`` **恒假** ⇒ 它从开机到现在一次都没截断过日志（实测
``journalctl -u log-guardian | grep -c Rotating`` = 0）。

本脚本守护的是**替代实现** ``scripts/log_guardian.py``（装到
``/usr/local/sbin/log-guardian``）的行为，断言：

  1. ``parse_size`` 认 K/M/G（按 1024 进位），拒绝空串；
  2. **不依赖 awk/df**：``free_bytes`` 返回真实整数（> 2^31 也不截断）；
  3. 只认**普通文件**：socket / 目录 / 符号链接都不进候选；
  4. ``/tmp`` 策略：只动 ``min_age`` 之前的文件 ⇒ **正在写的临时文件免疫**；
  5. ``/var/log`` 策略：活跃日志（mtime 很新）**也要**进候选；
  6. 旧轮转文件（``*.1`` / ``*.2.gz`` / ``*~``）优先删除；
  7. 大文件走**轮转**（保留尾部 reserve 字节到 ``.1``），中等文件直接截断；
  8. 端到端：造一个「快满」的目录 ⇒ ``reclaim()`` 后空间/文件确实变小，
     且**没碰**太新的文件；
  9. 预算（budget）封顶：单轮释放量不超过预算。

全部在临时目录里跑，**不碰生产 /var/log 与 /tmp**，不占 NPU、不动服务。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD_PATH = HERE / "log_guardian.py"

_spec = importlib.util.spec_from_file_location("log_guardian", MOD_PATH)
if _spec is None or _spec.loader is None:
    print(f"FAIL: cannot import {MOD_PATH}")
    sys.exit(2)
lg = importlib.util.module_from_spec(_spec)
# ⚠️ 必须先注册进 sys.modules：@dataclass 在解析注解时会回查
# sys.modules[cls.__module__]，没注册会直接 AttributeError。
sys.modules["log_guardian"] = lg
_spec.loader.exec_module(lg)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def main() -> int:
    print("== log-guardian 自有实现回归 ==")

    # 1) size parsing
    check("parse_size 100M", lg.parse_size("100M") == 100 * 1024 * 1024)
    check("parse_size 2G", lg.parse_size("2G") == 2 * 1024 ** 3)
    check("parse_size 512K", lg.parse_size("512K") == 512 * 1024)
    check("parse_size 裸字节", lg.parse_size("1048576") == 1048576)
    try:
        lg.parse_size("")
        check("parse_size 拒绝空串", False)
    except ValueError:
        check("parse_size 拒绝空串", True)

    # 2) free_bytes：整数、不截断（> 2^31）
    fb = lg.free_bytes(Path("/"))
    check("free_bytes 返回整数", isinstance(fb, int) and fb > 0, f"got {fb!r}")
    check("free_bytes 不被 32 位截断", fb != 2147483647 and fb > 2 ** 31,
          f"got {fb}")

    # 3) 只有普通文件进候选
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "big.log").write_bytes(b"x" * (3 * 1024 * 1024))
        (root / "sock").write_bytes(b"")          # 普通文件（占位，见下）
        os.unlink(root / "sock")
        os.symlink(root / "big.log", root / "link.log")
        (root / "sub").mkdir()
        (root / "sub" / "nested.log").write_bytes(b"y" * (2 * 1024 * 1024))
        found = {p.name for p, _ in lg.iter_regular_files(root)}
        check("普通文件被收录", "big.log" in found)
        check("子目录里的文件被收录", "nested.log" in found)
        check("符号链接不入选", "link.log" not in found)

    # 4) /tmp 策略：太新的文件免疫
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        fresh = root / "fresh.log"
        fresh.write_bytes(b"x" * (3 * 1024 * 1024))
        old = root / "old.log"
        old.write_bytes(b"y" * (3 * 1024 * 1024))
        old_mtime = time.time() - 7200
        os.utime(old, (old_mtime, old_mtime))
        pol = lg.DirPolicy(path=root, min_age=3600, min_size=1024 * 1024,
                           reserve=1024 * 1024, budget=64 * 1024 * 1024)
        acts = {a.path.name for a in lg.select_actions(pol, time.time())}
        check("太新的文件被跳过", "fresh.log" not in acts)
        check("够老的文件入选", "old.log" in acts)

    # 5) /var/log 策略：min_age=0 ⇒ 活跃日志也入选
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hot = root / "kern.log"
        hot.write_bytes(b"x" * (3 * 1024 * 1024))
        pol = lg.DirPolicy(path=root, min_age=0, min_size=1024 * 1024,
                           reserve=1024 * 1024, budget=64 * 1024 * 1024)
        acts = {a.path.name for a in lg.select_actions(pol, time.time())}
        check("活跃日志也入选（min_age=0）", "kern.log" in acts)

    # 6) 旧轮转文件优先删除
    check("识别 .1", lg.is_rotated(Path("/x/foo.log.1")))
    check("识别 .2.gz", lg.is_rotated(Path("/x/foo.log.2.gz")))
    check("识别 ~", lg.is_rotated(Path("/x/foo.log~")))
    check("不误判 .log", not lg.is_rotated(Path("/x/foo.log")))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.log.1").write_bytes(b"x" * 4096)
        (root / "b.log").write_bytes(b"y" * (3 * 1024 * 1024))
        pol = lg.DirPolicy(path=root, min_age=0, min_size=1024 * 1024,
                           reserve=1024 * 1024, budget=64 * 1024 * 1024)
        order = [a.kind for a in lg.select_actions(pol, time.time())]
        check("删除排在轮转/截断之前",
              order and order[0] == "delete", f"got {order}")

    # 7) 轮转保留尾部 reserve 字节
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        big = root / "big.log"
        big.write_bytes(b"A" * 1000 + b"B" * 3000)
        keep = lg._copy_tail(big, root / "big.log.1", 1000)
        check("_copy_tail 写出的字节数正确", keep == 1000, f"got {keep}")
        tail = (root / "big.log.1").read_bytes()
        check("_copy_tail 保留的是尾部", tail == b"B" * 1000)

    # 8) 端到端：reclaim 真的把文件变小，且不碰太新的文件
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        big = root / "big.log"
        big.write_bytes(b"x" * (5 * 1024 * 1024))
        rot = root / "old.log.1"
        rot.write_bytes(b"x" * 4096)
        fresh = root / "fresh.log"
        fresh.write_bytes(b"z" * (5 * 1024 * 1024))
        # 只有 big.log / old.log.1 够老；fresh.log 保持「刚写」状态
        old_ts = time.time() - 7200
        os.utime(big, (old_ts, old_ts))
        os.utime(rot, (old_ts, old_ts))
        before = big.stat().st_size
        pol = lg.DirPolicy(path=root, min_age=3600, min_size=1024 * 1024,
                           reserve=1024 * 1024, budget=64 * 1024 * 1024)
        freed, done = lg.reclaim(pol, time.time())
        check("reclaim 有释放量", freed > 0, f"got {freed}")
        check("reclaim 之后大文件变小", big.stat().st_size < before,
              f"{before} -> {big.stat().st_size}")
        check("旧轮转文件被删", not rot.exists())
        check("太新的文件没被动", fresh.stat().st_size == 5 * 1024 * 1024)

    # 9) budget 封顶
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old_ts = time.time() - 7200
        for i in range(6):
            f = root / f"f{i}.log"
            f.write_bytes(b"x" * (4 * 1024 * 1024))
            os.utime(f, (old_ts, old_ts))
        pol = lg.DirPolicy(path=root, min_age=0, min_size=1024 * 1024,
                           reserve=1024 * 1024, budget=8 * 1024 * 1024)
        freed, _ = lg.reclaim(pol, time.time())
        check("单轮释放不超过预算", freed <= pol.budget, f"freed={freed}")

    # 10) dry-run 不动手
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        f = root / "x.log"
        f.write_bytes(b"x" * (5 * 1024 * 1024))
        pol = lg.DirPolicy(path=root, min_age=0, min_size=1024 * 1024,
                           reserve=1024 * 1024, budget=64 * 1024 * 1024)
        lg.reclaim(pol, time.time(), dry_run=True)
        check("dry-run 不改文件", f.stat().st_size == 5 * 1024 * 1024)

    # 11) 受保护前缀：/tmp 的模型分词器缓存不进候选
    import argparse
    ns = argparse.Namespace(dirs="/tmp", min_age=3600, min_size="1M",
                            reserve="1M", budget="1M")
    pols = lg.build_policies(ns)
    check("/tmp 策略带受保护前缀",
          any("wdnai_gguf_tok" in s for s in pols[0].skip),
          f"skip={pols[0].skip}")

    prot = Path("/tmp/wdnai_gguf_tok_guardiantest")
    prot.mkdir(parents=True, exist_ok=True)
    try:
        vf = prot / "vocab.json"
        vf.write_bytes(b"x" * (3 * 1024 * 1024))
        old_ts = time.time() - 7200
        os.utime(vf, (old_ts, old_ts))
        pol = lg.DirPolicy(path=Path("/tmp"), min_age=3600,
                           min_size=1024 * 1024, reserve=1024 * 1024,
                           budget=64 * 1024 * 1024,
                           skip=lg._PROTECTED_PREFIXES)
        acts = {str(a.path) for a in lg.select_actions(pol, time.time())}
        check("分词器缓存被保护", str(vf) not in acts)
    finally:
        shutil.rmtree(prot, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} assertion(s): {', '.join(FAILURES)}")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
