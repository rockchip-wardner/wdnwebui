"""Compute subsystem: registry, worker subprocess, RPC proxy, jsonc.

Cards obtain LLMSession handles via ``app.core.compute.registry.get_registry()``.
The RPC client/proxy lives in ``app.core.compute.proxy``; the worker process
that actually loads RKNN models lives in ``app.core.compute.worker`` (spawned
on demand by the registry). Config parsing is in ``app.core.compute.jsonc``.
"""
from __future__ import annotations
