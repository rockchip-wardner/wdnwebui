"""The one and only worker entry point — shared by every model.

Launched by ``ComputeRegistry._spawn_compute_worker`` as::

    python -u app/core/compute/workers/compute_worker.py --model <id> [--local-only]

Which model that is comes entirely from ``workers/<id>.json``: identity,
capabilities, UI metadata, and the ``spec_factory`` naming the
``wdnai.SystemLanguageModel`` method that builds it. This file contains no
model-specific code and must not grow any — the whole point of the flat
layout is that adding a model is dropping in a JSON file, not writing a
near-duplicate of this script.

Model-specific *behaviour* does not belong here either. This process is a
transport shell: it brings up the DDS hub, registers the replier and
forwards a prompt. Anything that would make one model behave differently
at inference time (special options, a different loading strategy, an extra
backend) belongs in ``wdnai``'s ``LLMLocalSession`` / backend layer, where
every caller benefits instead of forking per worker.

The basename must stay ``compute_worker.py`` and the flag must stay
``--model``: the spawn dedup in ``registry.py`` and the process scan in
``system_monitor.py`` both match ``compute_worker.py\\x00--model\\x00<id>\\x00``
against ``/proc/<pid>/cmdline``. Renaming either one silently breaks
orphan detection and dedup.
"""
import os
import sys

# Normally launched by the registry, which injects the repo root via
# PYTHONPATH. Bootstrap it here too so the worker can also be started by
# hand from any cwd — a supported path for debugging.
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

from app.core.compute.workers._base import run_worker


def main() -> int:
    return run_worker()


if __name__ == "__main__":
    sys.exit(main())
