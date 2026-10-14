"""Tests for receipt_store key matching cache and privacy preservation (Task 3)."""

from __future__ import annotations

from pathlib import Path

from verdict.receipt_store import (
    _RAW_KEY_PARTS,
    _SENSITIVE_KEY_PARTS,
    ReceiptStore,
    _key_matches_parts,
    redact_sensitive_dict,
)
from verdict.routing_receipt import build_routing_receipt, persist_routing_receipt


def test_key_matches_parts_exact_semantics_and_caching() -> None:
    # Sensitive keys
    assert _key_matches_parts("api_key", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("token", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("user_token", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("tokens", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("client_secret", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("authorization", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("auth", _SENSITIVE_KEY_PARTS) is True
    assert _key_matches_parts("authority", _SENSITIVE_KEY_PARTS) is False

    # Raw keys
    assert _key_matches_parts("prompt", _RAW_KEY_PARTS) is True
    assert _key_matches_parts("messages", _RAW_KEY_PARTS) is True
    assert _key_matches_parts("arguments", _RAW_KEY_PARTS) is True
    assert _key_matches_parts("output", _RAW_KEY_PARTS) is True
    assert _key_matches_parts("unrelated_key", _RAW_KEY_PARTS) is False

    # Verify cache info if lru_cache is applied
    assert hasattr(_key_matches_parts, "cache_info")
    if True:
        info = _key_matches_parts.cache_info()
        assert info.hits > 0 or info.misses > 0


def test_redaction_preserves_sensitive_and_raw_key_protection() -> None:
    payload = {
        "api_key": "secret_value_123",
        "authorization": "Bearer xyz",
        "nested": {
            "token": "tok_abc",
            "password": "pass",
            "prompt": "my sensitive prompt",
            "safe_field": "ok",
        },
        "url_field": "https://user:password@example.com/api?secret_token=123",
        "safe_number": 42,
    }

    redacted = redact_sensitive_dict(payload)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["prompt"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "ok"
    assert redacted["safe_number"] == 42
    assert "password" not in redacted["url_field"]
    assert "secret_token" not in redacted["url_field"]


def test_persist_routing_receipt_preserves_privacy_in_store(tmp_path: Path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db")
    receipt = build_routing_receipt(
        story_id="test-story", work_unit_id="unit-1", attempt_id="attempt-1", state="in_progress"
    )

    # Inject sensitive data into receipt extensions
    receipt = receipt.with_updates(
        extensions={"api_key": "leaked_key_value", "safe_meta": "allowed"}
    )

    record = persist_routing_receipt(store, receipt, scope="test-scope")
    assert record is not None

    stored = store.get_receipt(record.receipt_id, scope="test-scope")
    assert stored is not None
    # Verify the stored payload in SQLite is redacted!
    assert stored.payload["extensions"]["api_key"] == "[REDACTED]"
    assert stored.payload["extensions"]["safe_meta"] == "allowed"
