#!/usr/bin/env python3
"""回归防护：``run.py`` **不许**在生产端口 8000 上以明文启动。

背景
----
8000 已在 2026-11-16 改成 **uvicorn 原生 TLS**（systemd 单元带
``--ssl-certfile`` / ``--ssl-keyfile``）。而 ``run.py`` 的默认行为是
「杀掉占着这个端口的旧实例再接管」⇒ 一次「本地调试」的 ``python run.py``
如果没带 TLS 参数，会先把生产的 HTTPS 实例杀掉，然后自己以**明文**接管
8000 —— 现场只看得到「服务还在」，看不出协议被换掉了。

所以 ``run.py`` 加了硬护栏：端口是 8000 且没配 TLS ⇒ **SystemExit(2)**，
而且这个判断必须发生在 ``_kill_stale_wdnwebui_processes`` **之前**。

本脚本断言（不碰生产服务、不占 NPU）：

  1. 8000 + 无 TLS ⇒ 退出码 2，且**没有**触发 kill；
  2. 8000 + 只给证书不给私钥 ⇒ 退出码 2（不静默降级成明文）；
  3. 8000 + 证书文件不存在 ⇒ 退出码 2；
  4. 8000 + 合法 TLS ⇒ 放行（走到 kill 那一步）；
  5. 8000 + ``--allow-plaintext`` ⇒ 放行；
  6. 8000 + 环境变量 ``WDNWEBUI_ALLOW_PLAINTEXT=1`` ⇒ 放行；
  7. 其它端口 + 无 TLS ⇒ 放行（本地明文调试不受影响）。

「有没有触发 kill」用**替身**判定：把 ``run._kill_stale_wdnwebui_processes``
换成一个会抛 ``_Killed`` 的函数 ⇒ 抛出来就说明走到了那一步。
**绝不能**真的去杀生产实例。

用法::

    .venv/bin/python scripts/verify_run_py_tls_guard.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

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


class _Killed(Exception):
    """替身 ``_kill_stale_wdnwebui_processes`` 抛的标记 —— 走到那一步了。"""


def main() -> int:
    print("=== run.py TLS 护栏回归 ===\n")

    # 只导入 run 模块 —— 它在 import 时只做「wdndds 原生库在不在」的
    # fail-fast 检查，没有别的副作用。
    import run

    # **替身**：绝不能真的去杀占着 8000 的生产实例。
    run._kill_stale_wdnwebui_processes = lambda *a, **k: (_ for _ in ()).throw(
        _Killed())

    def run_main(argv: list[str], env: dict[str, str] | None = None):
        """跑一次 run.main()，返回 ('exit', code) / ('killed',) / ('other', exc)。"""
        old_argv = list(sys.argv)
        old_env = dict(os.environ)
        try:
            sys.argv = ["run.py"] + argv
            for key in ("WDNWEBUI_ALLOW_PLAINTEXT",
                        "WDNWEBUI_SSL_CERTFILE", "WDNWEBUI_SSL_KEYFILE"):
                os.environ.pop(key, None)
            if env:
                os.environ.update(env)
            run.main()
            return ("other", "main() 没抛异常就返回了 —— 不该发生")
        except SystemExit as exc:
            return ("exit", exc.code)
        except _Killed:
            return ("killed", "")
        except Exception as exc:                    # noqa: BLE001
            return ("other", f"{type(exc).__name__}: {exc}")
        finally:
            sys.argv = old_argv
            os.environ.clear()
            os.environ.update(old_env)

    # ---- 1. 8000 + 无 TLS ⇒ 退出 2，且没碰生产 -------------------------- #
    kind, detail = run_main(["--port", "8000"])
    check(kind == "exit" and detail == 2,
          "8000 + 无 TLS ⇒ SystemExit(2)", f"实际: {kind} {detail}")
    check(kind != "killed", "8000 + 无 TLS ⇒ 没有触发 kill（没动生产）",
          "已经触发 kill —— 护栏位置错了！")

    # ---- 2. 只给证书不给私钥 ⇒ 退出 2 ----------------------------------- #
    if BOARD_CERT.is_file():
        kind, detail = run_main(["--port", "8000",
                                 "--ssl-certfile", str(BOARD_CERT)])
    else:
        kind, detail = run_main(["--port", "8000",
                                 "--ssl-certfile", "/tmp/does-not-exist.pem"])
    check(kind == "exit" and detail == 2,
          "8000 + 只给证书不给私钥 ⇒ SystemExit(2)（不静默降级成明文）",
          f"实际: {kind} {detail}")
    check(kind != "killed", "8000 + 只给证书 ⇒ 没有触发 kill")

    # ---- 3. 证书文件不存在 ⇒ 退出 2 -------------------------------------- #
    kind, detail = run_main(["--port", "8000",
                             "--ssl-certfile", "/tmp/nope-cert.pem",
                             "--ssl-keyfile", "/tmp/nope-key.pem"])
    check(kind == "exit" and detail == 2,
          "8000 + 证书文件不存在 ⇒ SystemExit(2)", f"实际: {kind} {detail}")
    check(kind != "killed", "8000 + 证书不存在 ⇒ 没有触发 kill")

    # ---- 4. 合法 TLS ⇒ 放行 ---------------------------------------------- #
    if BOARD_CERT.is_file() and BOARD_KEY.is_file():
        kind, detail = run_main(["--port", "8000",
                                 "--ssl-certfile", str(BOARD_CERT),
                                 "--ssl-keyfile", str(BOARD_KEY)])
        check(kind == "killed",
              "8000 + 合法 TLS ⇒ 放行（走到了启动那一步）",
              f"实际: {kind} {detail}")
    else:
        print("  [SKIP] 板子证书不在，跳过「合法 TLS 放行」用例")

    # ---- 5. --allow-plaintext ⇒ 放行 ------------------------------------- #
    kind, detail = run_main(["--port", "8000", "--allow-plaintext"])
    check(kind == "killed", "8000 + --allow-plaintext ⇒ 放行",
          f"实际: {kind} {detail}")

    # ---- 6. 环境变量 ⇒ 放行 ---------------------------------------------- #
    kind, detail = run_main(["--port", "8000"],
                            env={"WDNWEBUI_ALLOW_PLAINTEXT": "1"})
    check(kind == "killed", "8000 + WDNWEBUI_ALLOW_PLAINTEXT=1 ⇒ 放行",
          f"实际: {kind} {detail}")

    # ---- 7. 其它端口 ⇒ 放行（本地明文调试不受影响） ---------------------- #
    kind, detail = run_main(["--port", "8001"])
    check(kind == "killed", "8001 + 无 TLS ⇒ 放行（本地明文调试仍可用）",
          f"实际: {kind} {detail}")

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
