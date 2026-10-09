"""文字转语音 worker。

模型身份声明在同目录 ``manifest.json``（名字、swcap、显存预估等）；
这里只负责把该模型装配成一个 ``wdnai`` ModelSpec，然后交给共享运行时
``app.core.compute.workers._base.run_worker``。

本文件是 ``--model qwen3-tts`` 的进程入口，也是 registry spawn 时按
``/proc/<pid>/cmdline`` 反查身份所匹配的文件名，所以它必须叫
``compute_worker.py``。
"""
import os
import sys

# This file is normally launched by the registry, which injects the repo
# root via PYTHONPATH. Bootstrap it here too so the worker can also be
# started by hand (``python .../compute_worker.py --model ... --npu 0``)
# from any cwd — the __main__-style manual run is a supported path.
_here = os.path.dirname(os.path.abspath(__file__))
while _here != os.path.dirname(_here):
    if os.path.isfile(os.path.join(_here, "app", "__init__.py")):
        sys.path.insert(0, _here)
        break
    _here = os.path.dirname(_here)
else:  # pragma: no cover
    raise SystemExit(
        "compute_worker: repo root (containing app/) not found above "
        f"{os.path.abspath(__file__)}")

from app.core.compute.workers._base import run_worker, set_spec_factory


def build_spec(mod):
    """Qwen3-TTS — 文字转语音（kind="tts"）。"""
    return mod["SystemLanguageModel"].qwen3_tts()


def main() -> int:
    set_spec_factory(build_spec)
    # Pass our own folder explicitly: _base.py lives in workers/ and
    # cannot infer which worker it is serving.
    return run_worker(worker_dir=os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    sys.exit(main())
