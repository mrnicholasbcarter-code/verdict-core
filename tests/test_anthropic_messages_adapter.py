"""Targeted BOD-102 adapter tests — thin Anthropic Messages boundary."""


from verdict.anthropic_messages_adapter import (
    build_messages_url,
    is_messages_qualified,
    messages_request_digest,
    SUPPORTED_SUBSET,
)


def test_build_url():
    assert build_messages_url("http://localhost:20128/v1") == "http://localhost:20128/v1/messages"


def test_qualification_rejects_chat_only():
    chat_passport = {"capabilities": {"chat": True}}
    assert not is_messages_qualified(chat_passport)


def test_qualification_accepts_messages():
    assert is_messages_qualified({"capabilities": {"anthropic.messages": True}})
    assert is_messages_qualified({"confirmed": {"messages": True}})


def test_digest_no_secrets():
    body = {"model": "claude-3-opus-20240229", "messages": [{"role": "user"}], "stream": True}
    digest = messages_request_digest(body)
    assert "claude-3-opus-20240229" in digest
    assert "user" not in digest  # content stripped by design for receipt identity
    assert "stream" in digest


def test_supported_subset():
    assert SUPPORTED_SUBSET["messages"] is True
    assert "unsupported_feature" not in SUPPORTED_SUBSET
