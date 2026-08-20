"""ExecutionEnvelope cross-runtime parity suite (NOD-002, ADR-025).

Runs the shared invalid-envelope fixtures from ``test_fixtures/envelopes/``
through ``ExecutionEnvelope.from_dict`` and asserts the documented verdict and
error for each. The same fixture files are validated by the TypeScript side in
``verdict-node/tests/contract-parity.test.ts`` against
``@bodanglin/verdict-contracts``; the verdict-node ``contract-parity`` CI job
fails if the runtimes ever disagree.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from verdict.contracts import ContractValidationError, ExecutionEnvelope, TaskSpec

_MANIFEST_PATH = Path(__file__).resolve().parent / "fixtures" / "invalid_envelopes.py"
_spec = importlib.util.spec_from_file_location("invalid_envelopes_manifest", _MANIFEST_PATH)
assert _spec is not None and _spec.loader is not None
_manifest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_manifest)

ACCEPTED_EMPTY_ARRAY_FIXTURES = _manifest.ACCEPTED_EMPTY_ARRAY_FIXTURES
INVALID_ENVELOPE_FIXTURES = _manifest.INVALID_ENVELOPE_FIXTURES
VALID_FIXTURE = _manifest.VALID_FIXTURE

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "test_fixtures" / "envelopes"


def _load(name: str) -> dict[str, object]:
    payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_fixture_manifest_matches_directory() -> None:
    """Every JSON fixture on disk is covered by the manifest, and vice versa."""
    on_disk = {path.name for path in FIXTURES_DIR.glob("*.json")}
    documented = (
        set(INVALID_ENVELOPE_FIXTURES) | set(ACCEPTED_EMPTY_ARRAY_FIXTURES) | {VALID_FIXTURE}
    )
    assert on_disk == documented


def test_valid_envelope_fixture_accepted() -> None:
    envelope = ExecutionEnvelope.from_dict(_load(VALID_FIXTURE))
    assert isinstance(envelope.task_spec, TaskSpec)
    assert envelope.schema_version == "1"


@pytest.mark.parametrize("name", sorted(INVALID_ENVELOPE_FIXTURES))
def test_invalid_envelope_fixture_rejected(name: str) -> None:
    _invariant, expected_error = INVALID_ENVELOPE_FIXTURES[name]
    with pytest.raises(ContractValidationError) as excinfo:
        ExecutionEnvelope.from_dict(_load(name))
    assert expected_error in str(excinfo.value)


@pytest.mark.parametrize("name", sorted(ACCEPTED_EMPTY_ARRAY_FIXTURES))
def test_empty_array_fixture_accepted(name: str) -> None:
    envelope = ExecutionEnvelope.from_dict(_load(name))
    assert envelope.schema_version == "1"
