"""Thin Anthropic Messages adapter for BOD-102.

Delegates the Messages wire protocol to a configured upstream gateway
while preserving Verdict's admission, receipt, and context contracts.
Does NOT implement a second full provider protocol inside Core.
"""

from __future__ import annotations

import json
from typing import Any


SUPPORTED_SUBSET = {
    "messages": True,
    "streaming": True,
    "buffered_json": True,
    "client_tool_definitions": True,
    "tool_use_result_blocks": True,
}

# Minimal interface for the governed relay. Actual routing uses the
# same auth/admission/evidence lifecycle as chat/completions.


def build_messages_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1/messages"):
        return root
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return f"{root}/v1/messages"


def is_messages_qualified(passport: dict[str, Any] | None) -> bool:
    """Protocol-bound qualification: a chat-only passport is insufficient."""
    if not passport:
        return False
    # Must include messages-specific capability confirmation, not only chat.
    capabilities = passport.get("capabilities") or passport.get("confirmed") or {}
    messages_confirmed = capabilities.get("anthropic.messages") or capabilities.get("messages")
    return bool(messages_confirmed)


def messages_request_digest(request_body: dict[str, Any]) -> str:
    # Preserve only the structure needed for receipt identity (no secrets).
    payload = {
        "model_preference": request_body.get("model"),
        "message_count": len(request_body.get("messages", [])),
        "max_tokens": request_body.get("max_tokens"),
        "stream": request_body.get("stream"),
    }
    return json.dumps(payload, sort_keys=True)
