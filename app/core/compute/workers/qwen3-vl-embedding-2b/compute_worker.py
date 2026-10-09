"""视频内容检索嵌入 worker。

模型身份声明在同目录 ``manifest.json``（名字、swcap、显存预估等）；
这里只负责把该模型装配成一个 ``wdnai`` ModelSpec，然后交给共享运行时
``app.core.compute.workers._base.run_worker``。

本文件是 ``--model qwen3-vl-embedding-2b`` 的进程入口，也是 registry spawn 时按
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
    """Qwen3-VL-Embedding-2B — 视频内容检索的嵌入模型。

    RKNN3Lite 普通模型路径直接输出 2048 维归一化向量，不做 token 生成；
    wdnai 内部走 EmbeddingBackend 的 embed_text / embed_video_chunk。
    文件布局与 Qwen3-VL-4B 同款。
    """
    return mod["SystemLanguageModel"].qwen3_vl_embedding()


def main() -> int:
    set_spec_factory(build_spec)
    # Pass our own folder explicitly: _base.py lives in workers/ and
    # cannot infer which worker it is serving.
    return run_worker(worker_dir=os.path.dirname(os.path.abspath(__file__)))


if __name__ == "__main__":
    sys.exit(main())
