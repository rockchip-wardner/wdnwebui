"""Shared chat subsystem.

Used by the text-chat card (apps/text_chat) and the WeChat-bridge card
(apps/im_bridge). Both wrap an LLMSession with an agent-loop and
message-stream serialization.
"""
from __future__ import annotations
