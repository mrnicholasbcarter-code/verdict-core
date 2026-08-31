"""Tests for durable ReceiptStore and memory manifest ledger."""

from verdict.receipt_store import ReceiptStore, redact_sensitive_dict


def test_redact_sensitive_dict() -> None:
    data = {
        "user": "alice",
        "api_key": "secret_abc123",
        "nested": {"password": "my_password", "safe": 42},
    }
    redacted = redact_sensitive_dict(data)
    assert redacted["user"] == "alice"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["safe"] == 42


def test_authority_is_not_treated_as_a_credential() -> None:
    redacted = redact_sensitive_dict(
        {"authority": "compiled", "authorization": "Bearer secret", "auth": "x"}
    )
    assert redacted["authority"] == "compiled"
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["auth"] == "[REDACTED]"


def test_token_budget_is_kept_only_when_allowlisted() -> None:
    payload = {
        "token_budget": 4096,
        "used_tokens": 242,
        "compiled_prompt": "do the unit",
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    }
    blocked = redact_sensitive_dict(payload)
    assert blocked["token_budget"] == "[REDACTED]"
    assert blocked["used_tokens"] == "[REDACTED]"
    assert blocked["compiled_prompt"] == "[REDACTED]"
    assert blocked["usage"]["prompt_tokens"] == "[REDACTED]"

    kept = redact_sensitive_dict(
        payload,
        allowlist=(
            "token_budget",
            "used_tokens",
            "usage.prompt_tokens",
            "usage.completion_tokens",
            "usage.total_tokens",
        ),
    )
    assert kept["token_budget"] == 4096
    assert kept["used_tokens"] == 242
    assert kept["compiled_prompt"] == "[REDACTED]"
    assert kept["usage"]["prompt_tokens"] == 10
    assert kept["usage"]["completion_tokens"] == 4
    assert kept["usage"]["total_tokens"] == 14


def test_receipt_store_put_get_and_query() -> None:
    store = ReceiptStore(":memory:")

    r1 = store.put_receipt(
        receipt_type="decision",
        scope="route_evaluation",
        payload={"selected_model": "gpt-5.6-sol", "api_key": "sensitive_123"},
        sensitivity="internal",
    )

    assert r1.receipt_id is not None
    assert r1.payload["api_key"] == "[REDACTED]"
    assert r1.payload["selected_model"] == "gpt-5.6-sol"

    fetched = store.get_receipt(r1.receipt_id)
    assert fetched is not None
    assert fetched.receipt_id == r1.receipt_id
    assert fetched.content_hash == r1.content_hash

    results = store.query_receipts(receipt_type="decision")
    assert len(results) == 1
    assert results[0].receipt_id == r1.receipt_id


def test_receipt_store_export_manifest() -> None:
    store = ReceiptStore(":memory:")
    store.put_receipt("context", "scope1", {"info": "abc"})
    store.put_receipt("verification", "scope1", {"status": "passed"})

    manifest = store.export_manifest()
    assert manifest["version"] == "1.0"
    assert manifest["receipt_count"] == 2
    assert "manifest_digest" in manifest
    assert len(manifest["receipts"]) == 2
