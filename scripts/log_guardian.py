#!/usr/bin/env python3
"""磁盘空间保护守护 —— **本仓自有实现**，取代系统镜像自带的 ``/usr/bin/log-guardian``。

为什么自己写（而不是给系统那份打补丁）
------------------------------------
系统镜像里的 ``/usr/bin/log-guardian`` **没有源副本在仓库里**，它是包管理器
的文件。两个后果：

1. **它的磁盘保护从来没生效过**。它用 ``awk '{print $4 * 1024}'`` 算可用空间，
   mawk 的默认输出格式是 ``%.6g`` ⇒ 6.2 GB 会输出成 ``6.22e+09`` ⇒ dash 的
   ``[`` 无法比较 ⇒ ``is_full()`` **恒假** ⇒ 它一次都没截断过日志
   （实测 ``journalctl -u log-guardian | grep -c Rotating`` = 0）。
2. **它无条件截断目录下每一个普通文件**：不看文件年龄、不看是不是正在写。
   ``/tmp`` 下正在写的 ``mdns_discovery.log``、``wdnwebui-worker/*.log`` 都在
   射程内。它还用 ``find | xargs ls`` 解析路径，文件名带空格就乱。

本实现装到 ``/usr/local/sbin/log-guardian``，配套 unit 装到
``/etc/systemd/system/log-guardian.service``：

* ``/usr/local`` 与 ``/etc/systemd/system`` 都是 Debian 留给管理员的目录，
  **系统包升级 / 重刷系统不会覆盖** ⇒ 不用再「升级后重打补丁」；
* 单元名与系统那份**同名** ⇒ systemd 只加载 ``/etc`` 这一份 ⇒ 不会两个守护
  同时跑、互相抢着截日志；
* 本实现是**前台循环**（系统那份把自己丢到后台 ⇒ systemd 里 MainPID=0、
  ``systemctl status`` 看不到真正干活的进程）。

策略（按目录分别定）
--------------------
====================  =========  ==================================
目录                  最小年龄   行为
====================  =========  ==================================
``/var/log``          0          活跃日志也要截 —— 截断正在写的日志是
                                 标准做法（logrotate 的 copytruncate），
                                 但只处理「普通文件」，不碰 socket/设备/
                                 符号链接，也不碰 ``journal/``（交给
                                 ``journalctl --vacuum-size``）。
``/tmp``              1 小时     **只动 1 小时前改过的文件** ⇒ 正在写的
                                 临时文件（模型缓存、worker 日志）不受影响。
====================  =========  ==================================

回收顺序：删旧轮转文件（``*.1`` / ``*.gz`` / ``*~``）→ journald vacuum →
大文件**轮转**（保留尾部 ``--reserve`` 字节到 ``.1`` 再截断原文件）→
仍不够就**直接截断**（从最大的开始），单轮有 ``--budget`` 上限。

用法
----
::

    sudo /usr/local/sbin/log-guardian                 # 前台循环（systemd 用）
    sudo /usr/local/sbin/log-guardian --once          # 跑一轮就退出
    sudo /usr/local/sbin/log-guardian --once --dry-run # 只报告不动手

安装见 ``scripts/install_log_guardian.sh``；回归见
``scripts/verify_log_guardian.py``。
"""
from __future__ import annotations

import argparse
import errno
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

LOG = logging.getLogger("log-guardian")

#: 「旧轮转文件」：``foo.log.1`` / ``foo.log.2.gz`` / ``foo.log~``。
_ROTATED_RE = re.compile(r"(?:\.\d+(?:\.gz)?|~)$")

#: journald 自己的目录 —— 里面的文件只能由 ``journalctl --vacuum-size`` 动。
_JOURNAL_DIRS = ("/var/log/journal", "/run/log/journal")

#: **受保护的前缀**：即使磁盘满了也不碰。
#:
#: ``/tmp/wdnai_gguf_tok_*`` 是 wdnai 落地的 **GGUF 分词器缓存**
#: （vocab.json / merges.txt）—— 它是**模型派生产物**，截断它不会释放多少
#: 空间，却可能让正在并发的分词器加载读到半个文件。
_PROTECTED_PREFIXES = (
    "/tmp/wdnai_gguf_tok_",
    "/tmp/wdnwebui-worker",
)


def parse_size(text: str) -> int:
    """``100M`` / ``2G`` / ``512K`` / ``1048576`` → 字节数。

    刻意**不**用 shell 算术：``M`` 按 1024 算（和 ``df -k`` 一致）。
    """
    s = str(text).strip().upper()
    if not s:
        raise ValueError("empty size")
    mult = 1
    if s[-1] in "KMGT":
        mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[s[-1]]
        s = s[:-1]
    return int(float(s) * mult)


@dataclass
class DirPolicy:
    """一个被监控目录的策略。"""

    path: Path
    #: 只动「最后修改时间早于此秒数」的文件；0 = 不限（活跃日志也要动）。
    min_age: int = 0
    #: 小于这个大小不管（避免为了几十字节去截一堆文件）。
    min_size: int = 1024 * 1024
    #: 轮转时保留的尾部字节数。
    reserve: int = 10 * 1024 * 1024
    #: 单个目录单轮的回收预算（字节）。
    budget: int = 512 * 1024 * 1024
    #: 不进入的子目录（绝对路径前缀）。
    skip: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class Action:
    """一次回收动作。"""

    kind: str          # "delete" | "rotate" | "truncate" | "vacuum"
    path: Path
    size: int = 0
    freed: int = 0


def free_bytes(path: Path) -> int:
    """目标路径所在文件系统的可用字节。

    用 ``shutil.disk_usage`` 而不是 ``df`` + awk —— 没有列序问题、没有
    科学计数法、没有本地化问题（板上是中文 locale，``df`` 的表头是中文的）。
    """
    usage = shutil.disk_usage(str(path))
    return int(usage.free)


def iter_regular_files(root: Path, skip: Iterable[str] = ()) -> Iterator[tuple[Path, os.stat_result]]:
    """递归产出 ``(路径, stat)``，**只含普通文件**。

    * 不跟随符号链接（``os.walk(followlinks=False)``）；
    * 不含目录 / socket / fifo / 设备文件 ⇒ ``/tmp/*.sock``、
      ``/tmp/rknnsmi.lock``、``/var/log/journal`` 这类天然免疫；
    * ``skip`` 里的前缀整棵子树跳过。
    """
    skip_abs = [str(Path(s).resolve()) for s in skip]
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        here = Path(dirpath)
        # 就地剪枝，避免走进 journal/ 之类的子树
        dirnames[:] = [
            d for d in dirnames
            if not (here / d).is_symlink()
            and not any(str((here / d).resolve()).startswith(p) for p in skip_abs)
        ]
        for name in filenames:
            p = here / name
            try:
                st = os.lstat(p)          # lstat：符号链接本身不当作普通文件
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if any(str(p.resolve()).startswith(s) for s in skip_abs):
                continue
            yield p, st


def is_rotated(path: Path) -> bool:
    """是否是旧轮转文件（``*.1`` / ``*.2.gz`` / ``*~``）。"""
    return bool(_ROTATED_RE.search(path.name))


def select_actions(policy: DirPolicy, now: float) -> list[Action]:
    """挑出这一轮该做的动作，按「先删轮转、再轮转大文件、最后直接截断」排序。

    同类动作里按**体积从大到小**排序 ⇒ 用尽量少的文件把空间抢回来。
    """
    skip = policy.skip
    actions: list[Action] = []
    rotatable: list[Action] = []
    trimmable: list[Action] = []

    try:
        files = list(iter_regular_files(policy.path, skip))
    except OSError as e:
        LOG.warning("scan %s failed: %s", policy.path, e)
        return actions

    for p, st in files:
        if policy.min_age and (now - st.st_mtime) < policy.min_age:
            continue                      # 太新 ⇒ 可能正在写
        if st.st_size <= 0:
            continue
        if is_rotated(p):
            actions.append(Action("delete", p, st.st_size))
        elif st.st_size >= policy.reserve:
            rotatable.append(Action("rotate", p, st.st_size))
        elif st.st_size >= policy.min_size:
            trimmable.append(Action("truncate", p, st.st_size))

    rotatable.sort(key=lambda a: a.size, reverse=True)
    trimmable.sort(key=lambda a: a.size, reverse=True)
    return actions + rotatable + trimmable


def _copy_tail(src: Path, dst: Path, keep: int) -> int:
    """把 ``src`` 末尾最多 ``keep`` 字节写到 ``dst``，返回写入字节数。"""
    written = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fin.seek(0, os.SEEK_END)
        size = fin.tell()
        start = max(0, size - keep)
        fin.seek(start)
        while True:
            chunk = fin.read(256 * 1024)
            if not chunk:
                break
            fout.write(chunk)
            written += len(chunk)
    return written


def reclaim(policy: DirPolicy, now: float, dry_run: bool = False) -> tuple[int, list[Action]]:
    """在 ``policy.path`` 上执行一轮回收，返回 ``(释放字节数, 动作列表)``。"""
    freed = 0
    done: list[Action] = []
    budget = policy.budget

    for act in select_actions(policy, now):
        if freed >= budget:
            break
        # 预估收益：**先算再动**，否则一次大动作就能把预算冲穿
        # （先判断 freed>=budget 再执行 ⇒ 最后一次会超出预算）。
        if act.kind == "rotate":
            gain = act.size - min(policy.reserve, act.size)
        else:
            gain = act.size
        if freed + gain > budget:
            if freed > 0:
                LOG.info("budget reached (%d/%d) — stop this cycle", freed, budget)
                break
            # 一轮都没回收成功时，允许第一个动作超额（保证有进展）
            LOG.warning("single action (%d bytes) exceeds budget (%d) — "
                        "taking it anyway to make progress", gain, budget)
        if act.kind == "delete":
            size = act.size
            if not dry_run:
                try:
                    act.path.unlink()
                except OSError as e:
                    LOG.warning("delete %s failed: %s", act.path, e)
                    continue
            freed += size
            done.append(act)
            LOG.info("deleted rotated %s (%d bytes)", act.path, size)
        elif act.kind == "rotate":
            keep = min(policy.reserve, act.size)
            if not dry_run:
                try:
                    _copy_tail(act.path, act.path.with_name(act.path.name + ".1"), keep)
                    with open(act.path, "r+b") as f:
                        f.truncate(0)
                except OSError as e:
                    LOG.warning("rotate %s failed: %s", act.path, e)
                    continue
            freed += act.size - keep
            done.append(act)
            LOG.info("rotated %s (%d -> keep %d bytes)", act.path, act.size, keep)
        elif act.kind == "truncate":
            if not dry_run:
                try:
                    with open(act.path, "r+b") as f:
                        f.truncate(0)
                except OSError as e:
                    LOG.warning("truncate %s failed: %s", act.path, e)
                    continue
            freed += act.size
            done.append(act)
            LOG.info("truncated %s (%d bytes)", act.path, act.size)
    return freed, done


def vacuum_journal(reserve: int, dry_run: bool = False) -> int:
    """用 ``journalctl --vacuum-size`` 回收 journald 占用（比手删安全）。"""
    if not any(Path(d).exists() for d in _JOURNAL_DIRS):
        return 0
    if shutil.which("journalctl") is None:
        return 0
    cmd = ["journalctl", "--vacuum-size=%dM" % max(1, reserve // (1024 * 1024))]
    if dry_run:
        LOG.info("[dry-run] would run: %s", " ".join(cmd))
        return 0
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = (p.stdout or "").strip().splitlines()
        if out:
            LOG.info("journalctl vacuum: %s", out[-1])
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("journalctl vacuum failed: %s", e)
    return 0


def build_policies(args: argparse.Namespace) -> list[DirPolicy]:
    """按参数构造每个目录的策略。"""
    policies: list[DirPolicy] = []
    for raw in args.dirs.split(","):
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw)
        # /tmp 里的东西可能正在写 ⇒ 只动老文件；/var/log 的活跃日志正是目标。
        min_age = args.min_age if str(p) == "/tmp" else 0
        skip = tuple(_JOURNAL_DIRS) if str(p) == "/var/log" else ()
        # 受保护前缀：模型分词器缓存等 —— 磁盘满了也不碰
        skip = skip + tuple(x for x in _PROTECTED_PREFIXES
                            if str(p) == "/tmp" or x.startswith(str(p)))
        policies.append(DirPolicy(
            path=p,
            min_age=min_age,
            min_size=parse_size(args.min_size),
            reserve=parse_size(args.reserve),
            budget=parse_size(args.budget),
            skip=skip,
        ))
    return policies


def run_cycle(policies: list[DirPolicy], args: argparse.Namespace) -> int:
    """跑一轮：空间不足就回收。返回本轮释放的字节数。"""
    now = time.time()
    total_freed = 0
    need_vacuum = False

    for pol in policies:
        try:
            free = free_bytes(pol.path)
        except OSError as e:
            LOG.warning("statfs %s failed: %s", pol.path, e)
            continue
        if free >= parse_size(args.min_free):
            LOG.debug("%s: %d bytes free, ok", pol.path, free)
            continue
        LOG.warning("%s: only %d bytes free (< %s) — reclaiming",
                    pol.path, free, args.min_free)
        if str(pol.path) == "/var/log":
            need_vacuum = True
        freed, _ = reclaim(pol, now, dry_run=args.dry_run)
        total_freed += freed
        try:
            now_free = free_bytes(pol.path)
            LOG.warning("%s: freed %d bytes, now %d bytes free",
                        pol.path, freed, now_free)
        except OSError:
            pass

    if need_vacuum:
        vacuum_journal(parse_size(args.reserve), dry_run=args.dry_run)
    return total_freed


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Reclaim disk space by rotating/truncating stale log files.")
    ap.add_argument("--dirs", default="/var/log,/tmp",
                    help="comma separated directories to watch (default: /var/log,/tmp)")
    ap.add_argument("--min-free", default="100M",
                    help="reclaim when free space drops below this (default: 100M)")
    ap.add_argument("--reserve", default="10M",
                    help="bytes kept when rotating a big file (default: 10M)")
    ap.add_argument("--min-size", default="1M",
                    help="ignore files smaller than this (default: 1M)")
    ap.add_argument("--budget", default="512M",
                    help="max bytes reclaimed per cycle (default: 512M)")
    ap.add_argument("--min-age", type=int, default=3600,
                    help="only touch /tmp files older than this many seconds "
                         "(default: 3600)")
    ap.add_argument("--interval", type=int, default=600,
                    help="seconds between cycles (default: 600)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="report, do not touch")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    policies = build_policies(args)
    if not policies:
        LOG.error("no directories to watch")
        return 2

    if args.once:
        run_cycle(policies, args)
        return 0

    LOG.info("log-guardian (wdnwebui) watching: %s",
             ", ".join(str(p.path) for p in policies))
    running = True

    def _stop(signum, _frame):
        nonlocal running
        LOG.info("signal %s received — exiting", signum)
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running:
        try:
            run_cycle(policies, args)
        except Exception as e:  # noqa: BLE001 — 守护不能因为一次异常就退出
            LOG.exception("cycle failed: %s", e)
        # 分段睡眠，好让 SIGTERM 及时生效
        slept = 0.0
        while running and slept < args.interval:
            time.sleep(min(1.0, args.interval - slept))
            slept += 1.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
