"""Shared video decoding session.

``app.core.video.session.VideoSession`` is a singleton accessed via
``get_session()`` and shared by all cards that consume the live video
stream (vd / vtd / vtl / vfa). Each card registers a frame-sink callback
on the session; the session's decode thread invokes all sinks per frame.
"""
from __future__ import annotations
