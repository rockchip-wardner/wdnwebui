"""LLM subsystem: wdnai-backed detector base class.

``app.core.llm.detector_base.WdnaiDetector`` is the generic single-VLM-session
frame-loop base class. Cards subclass it with their own schema binding and
required capability (see apps/video_detection/detector.py etc.).
"""
from __future__ import annotations
