#!/usr/bin/env python3
"""回归测试: 配置文件生成的 ``/userdata`` 回落逻辑。

Config 生成有两个发出口,共享 ``app.core.config.default_base_paths()``:

* ``ConfigStore._seed_defaults`` —— 首次启动 ``config.jsonc`` 不存在时,
  把首启模板写到磁盘。
* ``app/system/api/config._BASE_DEFAULTS`` —— ``GET /api/config/base``
  命中缺失字段时,用这套默认值填回(然后 set_section 持久化)。

两边必须一致,否则 ``base`` 的 ``model_path`` / ``resource_dir`` 在两种
boot 路径下会落到不同默认值,前端"基础配置"页和首次落盘会有诡异偏差。

model_path 解析走 ``default_models_root()`` 三段优先级:

    /userdata/models  →  /mnt/oem/models  →  <HOME>/models

⚠️ ``<HOME>`` 指**操作员家目录** (``app.core.config._user_home()``,
即 ``/home/<用户名>``),**不是** ``$HOME`` / ``Path.home()``:服务以 root
跑时 ``$HOME=/root``,而根分区只有 14G —— 模型该放 ``/home`` 那边。
``_user_home()`` 自己的解析链由 #10 单独覆盖。

resource_dir 走 ``default_resource_root()``,总是 ``<resolved_root>/assets``,
且会自动 ``mkdir -p``。命名历史:曾用 ``test``,改为 ``assets`` 后用 ``uploads/`` 当 wizard 上传第二级
"wizard 上传落地" 的实际用途。

测试覆盖:

 1. on-device (``/userdata`` 存在): ``default_base_paths()`` 返回
    ``{model_path: <models_root>/, resource_dir: <resolved_root>/assets}``
    —— 注意 model_path 和 resource_dir 各自独立(资源根固定是
    ``/userdata/assets``,跟模型根走不走 /userdata 没关系)。
 2. off-device (``/userdata`` + ``/userdata/models`` + ``/mnt/oem/models``
    都不存在): 落到 ``<HOME>/models`` 和 ``<HOME>/assets``。
 3. off-device 时 ``ConfigStore._seed_defaults`` 落盘的 ``base.model_path`` /
    ``base.resource_dir`` 与 #2 一致。
 4. off-device 时 ``app/system/api/config._BASE_DEFAULTS`` 也与 #2 一致。
 5. on-device 时二者各自仍指向 on-device 路径(防止 #2 的回落逻辑污染
    设备态)。
 6. ``default_models_root`` 三段优先级独立可观测:
     - 仅 /userdata/models 存在 → 选 /userdata/models
     - 仅 /mnt/oem/models 存在  → 选 /mnt/oem/models
     - 都不存在                  → 选 <HOME>/models
 7. ``default_resource_root()`` 会自动 mkdir 出一个 ``<resolved>/assets``
    目录(用临时 ``_user_home()`` 隔离,避免污染真文件系统)。
 8. **静态源码审计**: ``app/**/*.py`` 里除了 ``app/core/config.py``(中央
    真值源)之外,不得再出现任何硬编码的 ``/userdata`` 运行时字面量;
    注释 / docstring 里的描述性提及不算。docstring 用 ``ast`` 精确识别
    (只豁免 module/class/function 的第一条语句),普通字符串常量照旧被审计。
"""
from __future__ import annotations

import ast
import importlib
import json
import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

# 让本脚本可以从仓库根直接执行(.venv/bin/python scripts/xxx.py),
# 不依赖外部测试 runner 设 PYTHONPATH。其它 verify_*.py 也用同样 idiom。
sys.path.insert(0, ".")


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{PASS if ok else FAIL}] {label}" + (f" — {detail}" if detail else ""))
    return ok


# --------------------------------------------------------------------------- #
# Helpers used by the static-source audit (#8)
# --------------------------------------------------------------------------- #
# Allow-list: paths whose ``/userdata`` mentions are legitimate (中央真值源、
# 测试脚本等)。每个条目是相对仓库根的 POSIX 路径。
_AUDIT_ALLOWLIST: tuple[str, ...] = (
    # Central definition lives here; helpers themselves reference the
    # literal paths because they ARE the implementation.
    "app/core/config.py",
)


def _is_comment_line(line: str) -> bool:
    """True if the line is a comment (allowing leading whitespace)."""
    return line.lstrip().startswith("#")


def _docstring_line_indexes(source: str) -> set[int]:
    """返回所有 **docstring** 覆盖的行号（0-based）。

    用 ``ast`` 定位，而不是数引号 —— 数引号认不出**单行** docstring：
    一行里出现两次三引号，奇偶相消 → 整行永远不算在 docstring
    里。早先的启发式就是这么把 ``model_download.py`` 的一行单行 docstring
    误报成「硬编码 ``/userdata`` 运行时字面量」的。

    只豁免**真正的 docstring**（module / class / function 的第一条语句）；
    普通字符串常量仍然是运行时字面量，照样被审计。
    """
    out: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        end = first.end_lineno or first.lineno
        out.update(range(first.lineno - 1, end))
    return out


def _audit_source_for_hardcoded_userdata(repo_root: Path) -> list[tuple[Path, int, str]]:
    """Scan ``app/**/*.py`` (excluding ``app/core/config.py``) for lines
    containing the ``/userdata`` literal that are NOT comments/docstrings.

    Returns ``[(path, line_no, line), ...]`` for every violation found.
    Empty list = clean.
    """
    violations: list[tuple[Path, int, str]] = []
    app_dir = repo_root / "app"
    if not app_dir.is_dir():
        return violations

    for py_path in sorted(app_dir.rglob("*.py")):
        rel = py_path.relative_to(repo_root).as_posix()
        if rel in _AUDIT_ALLOWLIST:
            continue
        try:
            text = py_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        file_lines = text.splitlines()
        doc_lines = _docstring_line_indexes(text)
        for i, ln in enumerate(file_lines):
            if "/userdata" not in ln:
                continue
            if _is_comment_line(ln):
                continue
            if i in doc_lines:
                continue
            violations.append((py_path, i + 1, ln.rstrip()))
    return violations


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    results: list[bool] = []
    repo_root = Path(__file__).resolve().parents[1]

    # 0) 环境 sanity ──────────────────────────────────────────────────────
    userdata_mounted = Path("/userdata").is_dir()
    if not userdata_mounted:
        print(f"  [{FAIL}] 环境提示: /userdata 未挂载,#1 on-device 测将跳过,")
        print(f"          其余测试(mkdir / 优先级 / TTS / sudo)仍可独立跑。")

    # 1) on-device ────────────────────────────────────────────────────────
    print("[1] on-device (/userdata 存在) default_base_paths 返回值")
    from app.core.config import default_base_paths as live_paths_result
    live_paths = live_paths_result()
    if not userdata_mounted:
        # /userdata 当前未挂载(#0 提示过),无法对照真 on-device 路径,
        # 跳过本次断言但保持条目占位,避免后面 [#N] 编号错位。
        print(f"  [SKIP] /userdata 未挂载,跳过 on-device 真值断言")
        results.append(_check(
            "on-device 断言(/userdata 未挂载,跳过)",
            True, detail="skip — environment",
        ))
    else:
        # model_path 与 resource_dir 在 default_base_paths 里是独立的:
        # model_path 走三段优先级(/userdata/models → /mnt/oem/models → <HOME>/models),
        # resource_dir 在 on-device 时永远是 /userdata/assets。
        # 板上模型可能在 /mnt/oem/models(不是 /userdata/models),所以
        # 不能断言 resource_dir == models_root/assets(model_path 跟 resource_dir 各自独立)。
        models_root = live_paths["model_path"].rstrip("/")
        ok = (
            live_paths["model_path"].endswith("/models/")
            and Path(models_root).is_dir()
            and live_paths["resource_dir"] == "/userdata/assets"
            and Path("/userdata/assets").is_dir()
        )
        results.append(_check(
            "model_path 以 /models/ 结尾且真存在;resource_dir = /userdata/assets 且真存在",
            ok,
            detail=json.dumps(live_paths),
        ))

    # 2-5) off-device / on-device restore via Path.is_dir mock ─────────────
    print("[2-5] off-device / on-device restore 双向往返")
    cfg_module = importlib.import_module("app.core.config")
    original_is_dir = Path.is_dir
    try:
        # 这台测试机 /mnt/oem/models 真存在,所以 off-device 必须把它也
        # 设为不存在,才能逼出 <HOME>/models fallback。
        with mock.patch.object(Path, "is_dir", autospec=True) as mocked_is_dir:
            def fake_is_dir(self, *args, **kwargs):
                # 强制 off-device：/userdata 没挂载、两个模型根候选都不存在。
                # 注意候选是「/userdata/models」而不是「/userdata」——
                # 早先只挡了 "/userdata"，于是 /userdata/models 仍按真实
                # 存在处理，off-device 场景压根没被触发（#2/#3/#4 的
                # model_path 期望永远对不上）。
                if str(self) in ("/userdata", "/userdata/models",
                                 "/mnt/oem/models"):
                    return False
                return original_is_dir(self, *args, **kwargs)
            mocked_is_dir.side_effect = fake_is_dir

            importlib.reload(cfg_module)
            offdevice_paths = cfg_module.default_base_paths()
            # <HOME> = 操作员家目录（``_user_home()``），**不是** ``Path.home()``：
            # 服务以 root 跑时 $HOME=/root，但那是小分区，模型该放
            # /home/<user>。``_user_home()`` 的解析链由 #10 单独覆盖。
            home = cfg_module._user_home()
            expected_model_path = f"{home}/models/"
            expected_resource_dir = f"{home}/assets"

            # [2] default_base_paths 返回 <HOME>/...
            ok = (
                offdevice_paths["model_path"] == expected_model_path
                and offdevice_paths["resource_dir"] == expected_resource_dir
            )
            results.append(_check(
                "[2] default_base_paths() off-device -> <HOME>/{models/, assets}",
                ok,
                detail=json.dumps(offdevice_paths),
            ))

            # [3] _seed_defaults 落盘
            print("  [3] off-device 时 _seed_defaults 落盘")
            with tempfile.TemporaryDirectory() as td:
                cfg_path = Path(td) / "config.jsonc"
                cfg_module._store = None
                store = cfg_module.ConfigStore.__new__(cfg_module.ConfigStore)
                store._path = cfg_path
                store._data = {}
                store._lock = __import__("threading").RLock()
                store.reload()
                from app.core.compute.jsonc import parse_jsonc
                written = parse_jsonc(cfg_path.read_text())
                base = written.get("base", {})
                ok = (
                    base.get("model_path") == expected_model_path
                    and base.get("resource_dir") == expected_resource_dir
                )
                results.append(_check(
                    "config.jsonc['base'].{model_path, resource_dir} == <HOME>/{models/, assets}",
                    ok,
                    detail=json.dumps({
                        "model_path": base.get("model_path"),
                        "resource_dir": base.get("resource_dir"),
                    }),
                ))

            # [4] _BASE_DEFAULTS 同步
            print("  [4] off-device 时 _BASE_DEFAULTS 同步")
            api_cfg = importlib.import_module("app.system.api.config")
            bd = api_cfg._BASE_DEFAULTS
            ok = (
                bd.get("model_path") == expected_model_path
                and bd.get("resource_dir") == expected_resource_dir
            )
            results.append(_check(
                "_BASE_DEFAULTS.{model_path, resource_dir} == <HOME>/{models/, assets}",
                ok,
                detail=json.dumps({
                    "model_path": bd.get("model_path"),
                    "resource_dir": bd.get("resource_dir"),
                }),
            ))
    finally:
        # [5] reload 后 on-device 路径恢复
        importlib.reload(cfg_module)
        api_cfg = importlib.reload(
            importlib.import_module("app.system.api.config")
        )
        print("  [5] reload 后 on-device 路径恢复")
        ok = (
            cfg_module.default_base_paths()["resource_dir"].endswith("/assets")
            and api_cfg._BASE_DEFAULTS.get("resource_dir", "").endswith("/assets")
            and Path(cfg_module.default_base_paths()["model_path"].rstrip("/")).is_dir()
        )
        results.append(_check(
            "reload 后 default_base_paths() / _BASE_DEFAULTS 都回到 on-device 路径",
            ok,
            detail=json.dumps({
                "default_base_paths": cfg_module.default_base_paths(),
                "_BASE_DEFAULTS": {
                    k: api_cfg._BASE_DEFAULTS.get(k)
                    for k in ("model_path", "resource_dir")
                },
            }),
        ))

    # 6) default_models_root 三段优先级 ───────────────────────────────────
    print("[6] default_models_root 三段优先级")
    cfg_module = importlib.import_module("app.core.config")
    original_is_dir = Path.is_dir
    try:
                # 每个 scenario 给一个"假装存在的候选路径集合"。不在集合里的
        # 候选一律报不存在(走 fallback);真存在但不在集合里的也压成 False。
        scenarios = [
            ("仅 /userdata/models 存在 → 选 /userdata/models",
             {"/userdata/models"},
             "/userdata/models"),
            ("仅 /mnt/oem/models 存在 → 选 /mnt/oem/models",
             {"/mnt/oem/models"},
             "/mnt/oem/models"),
            ("两个都不存在 → 选 <HOME>/models",
             set(),
             None),
        ]
        for label, pretend_exist, expected in scenarios:
            with mock.patch.object(Path, "is_dir", autospec=True) as mocked_is_dir:
                # 候选路径之外的路径,统统走真实 is_dir;候选路径本身:
                # 在 pretend_exist 里 → 报 True,不在 → 报 False。
                # 这样能独立控制每个 candidate 的存在性。
                def make_fake(pretend):
                    def is_dir_fake(self, *args, **kwargs):
                        s = str(self)
                        if s in ("/userdata/models", "/mnt/oem/models"):
                            return s in pretend
                        return original_is_dir(self, *args, **kwargs)
                    return is_dir_fake
                mocked_is_dir.side_effect = make_fake(pretend_exist)
                importlib.reload(cfg_module)
                importlib.reload(importlib.import_module("app.system.api.config"))
                if expected is None:
                    # 兜底是「操作员家目录」下的 models/，不是 $HOME。
                    expected_path = cfg_module._user_home() / "models"
                else:
                    expected_path = Path(expected)
                ok = cfg_module.default_models_root() == expected_path
                results.append(_check(
                    label,
                    ok,
                    detail=str(cfg_module.default_models_root()),
                ))
    finally:
        importlib.reload(cfg_module)
        importlib.reload(importlib.import_module("app.system.api.config"))

    # 7) default_resource_root 自动 mkdir ────────────────────────────────
    print("[7] default_resource_root() 自动 mkdir")
    cfg_module = importlib.import_module("app.core.config")
    original_is_dir = Path.is_dir
    try:
        with tempfile.TemporaryDirectory() as fake_home_str:
            fake_home = Path(fake_home_str)
            # 确保 fake_home/assets 不预先存在
            test_dir = fake_home / "assets"
            if test_dir.exists():
                shutil.rmtree(test_dir)
            # mock _user_home -> fake_home + /userdata 视为不存在
            #
            # ⚠️ mock 的是 ``_user_home()`` 而不是 ``Path.home()``：
            # 资源根走的是 ``_user_home()``（操作员家目录），不是 ``$HOME``。
            # 早先 mock 的是 ``Path.home()`` —— 对当前实现完全无效，于是
            # 这一条会去 mkdir **真实的** /home/rockchip/assets（污染真文件系统），
            # 而且断言拿临时目录去比对真实路径，恒失败。
            with mock.patch.object(Path, "is_dir", autospec=True) as mocked_is_dir, \
                 mock.patch.object(cfg_module, "_user_home", return_value=fake_home):
                def is_dir_fake(self, *args, **kwargs):
                    if str(self) == "/userdata":
                        return False
                    return original_is_dir(self, *args, **kwargs)
                mocked_is_dir.side_effect = is_dir_fake

                root = cfg_module.default_resource_root()
                ok = root == fake_home / "assets"
                results.append(_check(
                    "off-device + mock <HOME>: root = <HOME>/assets",
                    ok,
                    detail=str(root),
                ))
                ok = test_dir.is_dir()
                results.append(_check(
                    "mkdir 副作用: <HOME>/assets 真的被创建出来",
                    ok,
                    detail=str(test_dir),
                ))

                # 再调一次幂等性(应该 exist_ok=True 不抛)
                try:
                    cfg_module.default_resource_root()
                    idempotent = True
                except Exception:
                    idempotent = False
                results.append(_check(
                    "第二次调用幂等(exist_ok=True 不抛)",
                    idempotent,
                ))
    finally:
        # 还原 module 状态
        importlib.reload(cfg_module)
        importlib.reload(importlib.import_module("app.system.api.config"))

    # 8) 静态源码审计 ─────────────────────────────────────────────────────
    print("[8] 静态源码审计: app/**/*.py 不得有硬编码 /userdata 运行时字面量")
    violations = _audit_source_for_hardcoded_userdata(repo_root)
    if violations:
        for py_path, line_no, line in violations:
            rel = py_path.relative_to(repo_root)
            print(f"  [{FAIL}] {rel}:{line_no}: {line.strip()}")
    else:
        print(f"  [{PASS}] 全部 .py 文件(除 app/core/config.py 外)无硬编码 /userdata 字面量")
    results.append(_check(
        "audit 通过 (0 violations)",
        len(violations) == 0,
        detail=f"{len(violations)} 处违规" if violations else "",
    ))

    # 9) TTS 输出路径:不应落到 emmc 上的 app/uploads/tts/ ─────────────
    # TTS 每跑一次合成就落一个 WAV,如果写到 emmc 上的 repo 内,
    # 频繁写会把 emmc 寿命踩爆。`get_tts_output_root` 应该解析到
    # <resource_dir>/tts,且这个路径不能在 app/uploads/ 下。
    print("[9] TTS 输出路径不落 emmc:app/uploads/<card>")
    from app.core.uploads import get_tts_output_root, get_card_uploads_root
    tts_out = str(get_tts_output_root())
    tts_old = str(get_card_uploads_root("tts"))
    ok = not tts_out.startswith(str(repo_root / "app" / "uploads"))
    results.append(_check(
        f"get_tts_output_root() = {tts_out} (不落 repo 内)",
        ok,
        detail=f"对照旧路径 {tts_old}",
    ))
    ok = tts_out.endswith("/tts")
    results.append(_check(
        f"get_tts_output_root() 路径以 /tts 结尾(per-app 子目录)",
        ok,
    ))
    # 也确认 service 层 _default_output_path 走的就是新 helper
    from wdnapps_audio_chat.cards.tts.service import TtsSession
    sample_path = TtsSession._default_output_path(None)
    ok = str(sample_path).startswith(tts_out)
    results.append(_check(
        f"TtsSession._default_output_path() 落到新 helper 目录下",
        ok,
        detail=str(sample_path),
    ))

    # 10) sudo 上下文: <HOME> 应是 sudo 前的,而不是 /root ─────────────
    # 当 `sudo -H` 把 HOME 改成 /root 时,off-device fallback 路径
    # 会错误指向 /root/models、/root/assets。`_user_home()` 应该读
    # SUDO_USER env,从 NSS 找回原始用户的 home dir。
    print("[10] _user_home() 在 sudo 上下文中返回 sudo 前的 HOME")
    import importlib as _importlib
    cfg_mod = _importlib.import_module("app.core.config")
    real_home = str(Path.home())
    original_is_dir = Path.is_dir
    with mock.patch.object(Path, "is_dir", autospec=True) as mocked_is_dir:
        # 让 /userdata、/mnt/oem/models 都报不存在,逼出 <HOME> 分支
        def offdev_is_dir(self, *args, **kwargs):
            if str(self) in ("/userdata", "/userdata/models", "/mnt/oem/models"):
                return False
            return original_is_dir(self, *args, **kwargs)
        mocked_is_dir.side_effect = offdev_is_dir

        # [10a] no-sudo 上下文: HOME=/home/rockchip, SUDO_USER 未设
        with mock.patch.dict("os.environ", {"HOME": "/home/rockchip"}, clear=True):
            home = cfg_mod._user_home()
            ok = str(home) == "/home/rockchip"
            results.append(_check(
                "no-sudo: HOME=/home/rockchip,无 SUDO_USER -> _user_home() == /home/rockchip",
                ok, detail=str(home),
            ))

        # [10b] sudo -H 上下文: HOME=/root, SUDO_USER=rockchip
        # _user_home 应回到 /home/rockchip,不是 /root
        with mock.patch.dict("os.environ",
                             {"SUDO_USER": "rockchip", "HOME": "/root"},
                             clear=False):
            home = cfg_mod._user_home()
            ok = str(home) == "/home/rockchip"
            results.append(_check(
                "sudo: HOME=/root, SUDO_USER=rockchip -> _user_home() == /home/rockchip (不是 /root)",
                ok, detail=str(home),
            ))
            # 同时验证 default_models_root 走同一条路
            mr = cfg_mod.default_models_root()
            ok = str(mr) == "/home/rockchip/models"
            results.append(_check(
                "sudo: default_models_root() == /home/rockchip/models (不是 /root/models)",
                ok, detail=str(mr),
            ))
            # default_resource_root 也走同一条路
            with tempfile.TemporaryDirectory() as tmp:
                # mock Path.home() 也指过去,避免 mkdir 真污染 /home/rockchip/assets
                with mock.patch.object(Path, "home", return_value=Path(tmp)):
                    rr = cfg_mod.default_resource_root()
                ok = str(rr) == "/home/rockchip/assets"
                results.append(_check(
                    "sudo: default_resource_root() == /home/rockchip/assets (不是 /root/assets)",
                    ok, detail=str(rr),
                ))

        # [10c] SUDO_USER 不在 /etc/passwd(罕见): 链条**继续往下走**，
        # 而不是立刻掉回 Path.home()。下一站是「服务进程 cwd 的属主」——
        # systemd 场景下 cwd 归操作员所有，那才是模型该落的地方。
        with mock.patch.dict("os.environ",
                             {"SUDO_USER": "ghost_user_no_such_account",
                              "HOME": "/root"},
                             clear=False):
            home = cfg_mod._user_home()
            # 期望值独立推导：按 cwd 属主查 pwd，**不调用被测函数**。
            cwd_uid = os.stat(os.getcwd()).st_uid
            if cwd_uid != 0:
                expected_home = Path(pwd.getpwuid(cwd_uid).pw_dir)
            else:
                expected_home = Path.home()
            ok = str(home) == str(expected_home)
            results.append(_check(
                "SUDO_USER 在 NSS 查不到 -> 继续走链条: 落 cwd 属主的家目录",
                ok, detail=f"{home} (期望 {expected_home})",
            ))
            # 关键性质：链条没有停在 Path.home()(=/root)。
            # 只有在 cwd 也归 root、且系统里定位不到唯一操作员时，
            # 才允许退到 Path.home()。
            ok = str(home) != "/root" or cwd_uid == 0
            results.append(_check(
                "SUDO_USER 查不到时也不会掉回 /root",
                ok, detail=str(home),
            ))

    total = len(results)
    passed = sum(results)
    print(f"\n[{passed}/{total}] {'OK' if passed == total else 'FAIL'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())