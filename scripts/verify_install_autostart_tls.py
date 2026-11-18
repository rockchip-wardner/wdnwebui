#!/usr/bin/env python3
"""TLS 证书引导链路回归。

覆盖三件事：

  1. 生成的 systemd 单元**结构**（ConditionPathExists 在 [Unit] 段、
     TLS 参数齐全、证书路径不是 $SCRIPT_DIR）；
  2. ``gen_tls_ca.sh`` / ``tls_cert_check.sh`` 的**行为**（非法参数报错、
     签发、SAN 不匹配时自动重签）；
  3. 现场证书状态（存在、可读、SAN 覆盖当前 IP、未过期）。

行为测试在临时目录里真跑脚本，**不读源码字符串** —— 改个函数名不该让
测试失败，但行为变了必须失败。

用法::

    .venv/bin/python scripts/verify_install_autostart_tls.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install_autostart.sh"
GEN_TLS_SH = REPO / "scripts" / "gen_tls_ca.sh"
CERT_CHECK_SH = REPO / "scripts" / "tls_cert_check.sh"
TLS_COMMON_SH = REPO / "scripts" / "tls_common.sh"
TLS_DIR = Path("/home/rockchip/.config/wdnwebui/tls")
BOARD_CERT = TLS_DIR / "cert.pem"
BOARD_KEY = TLS_DIR / "key.pem"

_FAILED: list[str] = []
_PASSED = 0


def check(ok: bool, name: str, detail: str = "") -> bool:
    global _PASSED
    if ok:
        _PASSED += 1
        print(f"  [OK]   {name}")
    else:
        _FAILED.append(name)
        print(f"  [FAIL] {name}" + (f"  —— {detail}" if detail else ""))
    return ok


def run(cmd, env=None) -> subprocess.CompletedProcess:
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=180)


def san_entries(cert: Path) -> set[str]:
    """证书 SAN 的全部条目（精确值集合）。"""
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout",
                          "-ext", "subjectAltName"],
                         capture_output=True, text=True).stdout
    entries = set()
    for part in out.replace("\n", ",").split(","):
        part = part.strip()
        if ":" in part:
            part = part.split(":", 1)[1].strip()
        if part:
            entries.add(part)
    return entries


def board_ip() -> str:
    """板子当前的对外 IP（走仓里的 helper，和脚本同源）。"""
    out = subprocess.run(["bash", "-c",
                          f'source "{TLS_COMMON_SH}"; wdn_tls_detect_ip'],
                         capture_output=True, text=True).stdout.strip()
    return out.splitlines()[0] if out else ""


def heredoc_lines(src: str, var: str) -> list[str]:
    """取 ``cat > "$var" <<EOF ... EOF`` 里的内容行。

    去掉注释与空行，并把 ``\\`` 续行拼成一条（单元的 ExecStart 是折行的）。
    """
    start = src.find(f'cat > "{var}" <<EOF')
    if start < 0:
        return []
    body = src[start:].split("\nEOF\n", 1)[0]
    out: list[str] = []
    buf = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1].strip() + " "
            continue
        out.append((buf + line).strip())
        buf = ""
    return out


def fake_bin(tmp: Path, name: str, body: str) -> Path:
    """写一个假的可执行文件到临时 bin 目录。"""
    d = tmp / ("bin_" + name)
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    p.chmod(0o755)
    return d


def check_unit_structure() -> None:
    print("\n--- systemd 单元结构 ---")
    src = INSTALL_SH.read_text(encoding="utf-8")
    web = heredoc_lines(src, "$WEB_UNIT")

    cond = [ln for ln in web if ln.startswith("ConditionPathExists=")]
    check(len(cond) == 2, "单元有两个 ConditionPathExists（证书 + 私钥）",
          f"实际 {len(cond)} 条")
    svc_at = next((i for i, ln in enumerate(web) if ln == "[Service]"), -1)
    cond_at = [i for i, ln in enumerate(web) if ln.startswith("ConditionPathExists=")]
    check(svc_at > 0 and all(i < svc_at for i in cond_at),
          "ConditionPathExists 在 [Unit] 段",
          "放 [Service] 会被 systemd 静默忽略 ⇒ 等于没有保护")

    exec_start = next((ln for ln in web if ln.startswith("ExecStart=")), "")
    check("--ssl-certfile" in exec_start and "--ssl-keyfile" in exec_start,
          "ExecStart 同时给 --ssl-certfile 和 --ssl-keyfile", exec_start[:120])
    check(bool(cond) and not any("SCRIPT_DIR" in ln for ln in cond),
          "证书路径不是 $SCRIPT_DIR（证书不在仓库里）", " | ".join(cond))

    cc = heredoc_lines(src, "$CERT_CHECK_UNIT")
    cc_exec = next((ln for ln in cc if ln.startswith("ExecStart=")), "")
    check("tls_cert_check.sh" in cc_exec,
          "巡检单元调用 scripts/tls_cert_check.sh", cc_exec[:120])
    check("After=network-online.target" in cc,
          "巡检单元等网络就绪（开机时 DHCP 可能还没拿到 IP）")


def check_gen_behaviour(tmp: Path) -> None:
    print("\n--- gen_tls_ca.sh 行为 ---")
    d = tmp / "tls"
    d.mkdir()
    env = {"WDNWEBUI_TLS_DIR": str(d)}

    r = run(["bash", str(GEN_TLS_SH), "--force-leaf"], env)
    check(r.returncode == 0, "--force-leaf 单独给（IP 自动探测）⇒ 成功",
          f"exit={r.returncode} {r.stderr[-200:]}")
    if r.returncode == 0:
        ip = board_ip()
        check(bool(ip) and ip in san_entries(d / "cert.pem"),
              f"签出的证书 SAN 覆盖当前 IP {ip}",
              f"SAN={sorted(san_entries(d / 'cert.pem'))}")

    for args, label in (
        (["999.999.999.999"], "非法 IP ⇒ exit 2"),
        (["--bogus"], "未知选项 ⇒ exit 2"),
        (["1.2.3.4", "5.6.7.8"], "两个 IP ⇒ exit 2"),
    ):
        r = run(["bash", str(GEN_TLS_SH), *args], env)
        check(r.returncode == 2, label, f"exit={r.returncode} {r.stderr[-160:]}")

    # --ip= 要在**空目录**里测：证书已存在时按设计会跳过重签（换 IP 必须
    # 显式加 --force-leaf）。
    d2 = tmp / "tls2"
    d2.mkdir()
    r = run(["bash", str(GEN_TLS_SH), "--ip=10.99.88.77"], {"WDNWEBUI_TLS_DIR": str(d2)})
    check(r.returncode == 0 and "10.99.88.77" in san_entries(d2 / "cert.pem"),
          "--ip=<addr> 形式可用且 IP 进了 SAN", f"exit={r.returncode}")


def check_repair_behaviour(tmp: Path) -> None:
    print("\n--- tls_cert_check.sh 自动修复 ---")
    d = tmp / "stale"
    d.mkdir()

    # 假 ip：签发时假装板子的 IP 是 10.99.88.77 ⇒ 证书不覆盖真实 IP
    bin_ip = fake_bin(tmp, "ip", """#!/bin/sh
case "$*" in
  "route get 1.1.1.1") echo "1.1.1.1 via 10.0.0.1 dev eth0 src 10.99.88.77 uid 0" ;;
  "-4 -o addr show scope global") echo "3: eth0 inet 10.99.88.77/24 scope global dynamic" ;;
  "-6 -o addr show scope global") echo "" ;;
  *) exit 0 ;;
esac
""")
    # 假 systemctl：让 `systemctl cat` 失败 ⇒ 脚本跳过重启生产服务
    bin_svc = fake_bin(tmp, "systemctl", "#!/bin/sh\nexit 1\n")

    env = {"WDNWEBUI_TLS_DIR": str(d), "PATH": f"{bin_ip}:{os.environ['PATH']}"}
    r = run(["bash", str(GEN_TLS_SH), "--ip=10.99.88.77"], env)
    check(r.returncode == 0, "准备一张只覆盖旧 IP 的证书", f"exit={r.returncode}")

    ip = board_ip()
    env = {"WDNWEBUI_TLS_DIR": str(d), "PATH": f"{bin_svc}:{os.environ['PATH']}"}
    r = run(["bash", str(CERT_CHECK_SH)], env)
    check(r.returncode == 0, "巡检发现 SAN 不匹配后自动修复 ⇒ exit 0",
          f"exit={r.returncode} {r.stdout[-200:]}")
    check(ip in san_entries(d / "cert.pem"),
          f"修复后 SAN 覆盖当前 IP {ip}", f"SAN={sorted(san_entries(d / 'cert.pem'))}")

    r = run(["bash", str(CERT_CHECK_SH)], env)
    check(r.returncode == 0 and "OK" in r.stdout,
          "再跑一次 ⇒ 正常通过且不重复重签", r.stdout[-200:])


def check_live_state() -> None:
    print("\n--- 现场证书状态 ---")
    ip = board_ip()
    check(ip != "", "能探测到本机对外 IP", "wdn_tls_detect_ip 无输出")
    check(BOARD_CERT.is_file() and BOARD_KEY.is_file(),
          "证书文件存在", f"{BOARD_CERT} / {BOARD_KEY}")
    check(BOARD_CERT.is_file() and BOARD_KEY.is_file()
          and BOARD_CERT.stat().st_mode & 0o444 and BOARD_KEY.stat().st_mode & 0o400,
          "证书文件可读（root 可读私钥）")
    if ip and BOARD_CERT.is_file():
        entries = san_entries(BOARD_CERT)
        check(ip in entries, f"证书 SAN 覆盖本机 IP {ip}", f"SAN={sorted(entries)}")
    if BOARD_CERT.is_file():
        rc = subprocess.run(["openssl", "x509", "-in", str(BOARD_CERT), "-noout",
                             "-checkend", "2592000"], capture_output=True, text=True)
        check(rc.returncode == 0, "证书 30 天内不会过期", rc.stderr.strip())


def main() -> int:
    print("=== TLS 证书引导链路回归 ===")
    if not shutil.which("openssl"):
        print("  [FAIL] 找不到 openssl")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="wdnwebui_tls_verify_"))
    tmp.chmod(0o777)   # 以 root 跑时，降权用户也要能写
    try:
        check_unit_structure()
        check_gen_behaviour(tmp)
        check_repair_behaviour(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check_live_state()

    total = _PASSED + len(_FAILED)
    print(f"\n=== {_PASSED}/{total} 通过 ===")
    if _FAILED:
        for name in _FAILED:
            print(f"  FAIL: {name}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
