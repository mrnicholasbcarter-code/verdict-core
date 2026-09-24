"""Test ExecutionEnvelope fixture parity across Python, JSON Schema, and Zod."""

import json
from pathlib import Path

import pytest

from verdict.contracts import ContractValidationError, ExecutionEnvelope

FIXTURES_DIR = Path(__file__).parent.parent / "contracts" / "fixtures" / "execution-envelope" / "v1"


def load_fixture(name: str) -> dict:
    """Load a fixture JSON file."""
    return json.loads((FIXTURES_DIR / name).read_text())


class TestExecutionEnvelopeFixtures:
    """Test that Python accepts/rejects the same fixtures as TypeScript/Zod."""

    def test_accepted_fixture(self):
        """Accepted fixture should parse successfully."""
        fixture = load_fixture("accepted.json")
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope is not None
        assert envelope.execution_constraints["expires_at"] == "2024-01-15T13:00:00Z"

    def test_denied_fixture(self):
        """Denied fixture should parse successfully (schema valid, just denied)."""
        fixture = load_fixture("denied.json")
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope is not None

    def test_expired_fixture(self):
        """Expired fixture should parse successfully (schema valid, just expired)."""
        fixture = load_fixture("expired.json")
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope is not None

    def test_wrong_digest_fixture(self):
        """Wrong digest fixture should parse successfully (schema valid, wrong digest)."""
        fixture = load_fixture("wrong-digest.json")
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope is not None

    def test_null_defaults_fixture(self):
        """Null defaults fixture should parse successfully."""
        fixture = load_fixture("null-defaults.json")
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope is not None

    def test_unknown_field_fixture(self):
        """Unknown field fixture should be REJECTED (strict schema)."""
        fixture = load_fixture("unknown-field.json")
        with pytest.raises(ContractValidationError, match="unknown field"):
            ExecutionEnvelope.from_dict(fixture)
