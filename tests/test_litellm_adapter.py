"""Targeted BOD-9 adapter tests — optional LiteLLM adapter, not authority."""

import pytest
from verdict.litellm_adapter import (
    ADAPTER_ID,
    is_litellm_route_qualified,
    adapter_digest,
    SUPPORTED_CAPABILITIES,
)


def test_qualification_requires_litellm():
    # Chat-only passport is insufficient (same gate as BOD-102)
    chat_only = {"capabilities": {"chat": True}}
    assert not is_litellm_route_qualified(chat_only)


def test_qualification_accepts_litellm():
    assert is_litellm_route_qualified({"capabilities": {"litellm_adapter": True}})


def test_digest_has_adapter_identity():
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user"}]}
    d = adapter_digest(body)
    assert ADAPTER_ID in d
    assert "gpt-4o-mini" in d
    assert "secret" not in d  # never include secrets


def test_adapter_does_not_claim_authority():
    # Adapter identity is not routing authority (preserves BOD-104)
    assert SUPPORTED_CAPABILITIES["anthropic_messages"] is False
    assert "openai_chat_completions" in SUPPORTED_CAPABILITIES
