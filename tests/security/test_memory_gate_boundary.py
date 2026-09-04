"""PII/secret boundary tests for `verdict.memory_gate` (T017).

`MemoryGate.write()` redacts via `_redact_value` before persisting to its
backing `MemoryPlane` (`_record_for`), and the same redaction applies to the
durable audit projection returned by `get_write_history()`. These tests
assert that guarantee empirically, against a real `MemoryGate`/`MemoryPlane`
pair — not a mock — including bearer/basic credentials and generic PII under
ordinary key names.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.security.fixtures import (
    BASIC_AUTH_HEADER,
    BEARER_AUTH_HEADER,
    INNOCUOUS_VALUE,
    PII_SHAPED_BARE_VALUES,
    PROMPT_KEYED_VALUES,
    SECRET_KEYED_VALUES,
    SECRET_SHAPED_BARE_VALUES,
)
from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane


@pytest.fixture
def gate() -> MemoryGate:
    with tempfile.TemporaryDirectory() as tmp:
        plane = MemoryPlane(path=str(Path(tmp) / "boundary.db"))
        yield MemoryGate(plane=plane)


def _write(gate: MemoryGate, key: str, value: object, scope: str = "test-scope"):
    request = MemoryWriteRequest(
        namespace="patterns",
        key=key,
        value=value,
        authority="agent",
        confidence=1.0,
        provenance={"source": "test_memory_gate_boundary"},
        scope=scope,
    )
    return gate.write(request)


def test_secret_keyed_values_are_redacted(gate: MemoryGate) -> None:
    for secret_key, secret_value in SECRET_KEYED_VALUES.items():
        result = _write(gate, f"k-{secret_key}", {secret_key: secret_value})
        assert result.allowed is True
        assert secret_value not in result.record.content
        assert "[REDACTED]" in result.record.content


def test_prompt_keyed_values_are_redacted(gate: MemoryGate) -> None:
    for prompt_key, prompt_value in PROMPT_KEYED_VALUES.items():
        result = _write(gate, f"k-{prompt_key}", {prompt_key: prompt_value})
        assert result.allowed is True
        assert prompt_value not in result.record.content
        assert "[REDACTED]" in result.record.content


def test_secret_shaped_bare_values_are_redacted_regardless_of_key_name(gate: MemoryGate) -> None:
    for token in SECRET_SHAPED_BARE_VALUES:
        result = _write(gate, "note", {"note": f"see token {token}"})
        assert result.allowed is True
        assert token not in result.record.content
        assert "[REDACTED]" in result.record.content


def test_innocuous_value_is_not_redacted(gate: MemoryGate) -> None:
    result = _write(gate, "note", {"note": INNOCUOUS_VALUE})
    assert result.allowed is True
    assert INNOCUOUS_VALUE in result.record.content
    assert "[REDACTED]" not in result.record.content


def test_write_history_projection_stays_redacted(gate: MemoryGate) -> None:
    secret_key, secret_value = next(iter(SECRET_KEYED_VALUES.items()))
    _write(gate, f"history-{secret_key}", {secret_key: secret_value})
    history = gate.get_write_history()
    assert history, "expected at least one recorded write"
    # get_write_history()'s events carry a `request` dict (namespace/key/authority/
    # scope/ttl/confidence/tags/supersedes/provenance only — never `request.value`)
    # and a `result` dict (from MemoryWriteResult.to_dict(), which stores only
    # `record_id`, never `record.content`). The written secret is structurally
    # absent from both, not merely redacted — assert that guarantee directly.
    serialized = json.dumps(
        [{"request": event.request, "result": event.result} for event in history], default=str
    )
    assert secret_value not in serialized


def test_data_written_in_one_scope_is_not_returned_for_another_scope(gate: MemoryGate) -> None:
    secret_key, secret_value = next(iter(SECRET_KEYED_VALUES.items()))
    _write(gate, "scoped-key", {secret_key: secret_value}, scope="scope-a")
    other_scope_record = gate.plane.get("patterns", "scoped-key", scope="scope-b")
    assert other_scope_record is None


def test_bearer_and_basic_auth_tokens_are_redacted(gate: MemoryGate) -> None:
    for header in (BEARER_AUTH_HEADER, BASIC_AUTH_HEADER):
        result = _write(gate, "authz", {"note": header})
        token = header.split(" ", 2)[-1]
        assert token not in result.record.content


def test_pii_shaped_bare_values_are_redacted(gate: MemoryGate) -> None:
    for pii_value in PII_SHAPED_BARE_VALUES.values():
        result = _write(gate, "note", {"note": f"contact info: {pii_value}"})
        assert pii_value not in result.record.content
