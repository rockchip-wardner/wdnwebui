"""Compute subsystem: registry, worker plugins, RPC assembly, jsonc.

Cards obtain LLMSession handles via ``app.core.compute.registry.get_registry()``.
Those handles are ``wdndds.LLMRPCSession`` instances — the model lives in a
worker subprocess, but the object is a real ``wdnai.LLMSession``, so calling
code never branches on transport. Assembly lives in ``app.core.compute.proxy``;
config parsing is in ``app.core.compute.jsonc``.

The transport itself is not here either. ``wdndds.ai_session`` holds both
halves: ``serve_local_session`` on the worker, ``LLMRPCSession`` on the client,
including the ``/dev/shm`` attachment and long-reply routing that keeps big
payloads under the cshim per-message ceiling.

Workers are plugins: one folder per model under
``app/core/compute/workers/<model_id>/``, each holding a ``manifest.json``
(declarative identity — label, swcaps, memory estimate) plus a
``compute_worker.py`` entry point that knows how to build that model's spec.
The shared, model-agnostic runtime is ``workers/_base.py``; discovery and
manifest validation live in ``workers/__init__.py``. The registry spawns a
worker's entry script on demand.

The one piece of session knowledge a worker still lends out is exact token
counting (``count_tokens``): history trimming is caller-side policy, but only
the process holding the tokenizer can count accurately.
"""
from __future__ import annotations
