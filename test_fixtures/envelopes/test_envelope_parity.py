#!/usr/bin/env python3
"""Cross-repo parity test for ExecutionEnvelope invalid fixtures.

This test verifies that Python rejects the same invalid envelope fixtures
that TypeScript rejects. Run from verdict-core root.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from verdict.contracts import ContractValidationError, ExecutionEnvelope

FIXTURES_DIR = Path(__file__).parent

# Fixtures that should be REJECTED by both Python and TypeScript
INVALID_FIXTURES = [
    "invalid_missing_task_spec.json",
    "invalid_empty_policy_digest.json",
    "invalid_empty_evidence_id_item.json",
    "invalid_wrong_type_task_spec.json",
    "invalid_wrong_type_allowed_capabilities.json",
    "invalid_wrong_type_execution_constraints.json",
    "invalid_wrong_type_verification_requirements.json",
    "invalid_unknown_field.json",
    "invalid_task_spec_missing_objective.json",
    "invalid_task_spec_empty_objective.json",
    "invalid_empty_allowed_capability_item.json",
]

# Fixtures that are ACCEPTED by both (empty arrays allowed)
ALLOWED_EMPTY_ARRAY_FIXTURES = [
    "invalid_empty_allowed_capabilities.json",
    "invalid_empty_evidence_ids.json",
]

# Valid fixture
VALID_FIXTURE = "valid_envelope.json"


def test_invalid_fixtures_rejected():
    """Verify all invalid fixtures are rejected by Python."""
    failed = []
    for fixture_name in INVALID_FIXTURES:
        fixture_path = FIXTURES_DIR / fixture_name
        with open(fixture_path) as f:
            envelope_data = json.load(f)

        try:
            ExecutionEnvelope.from_dict(envelope_data)
            failed.append(f"{fixture_name}: Expected rejection but was accepted")
        except ContractValidationError:
            pass  # Expected
        except Exception as e:
            failed.append(f"{fixture_name}: Unexpected error type: {type(e).__name__}: {e}")

    if failed:
        print("FAILED: Python accepted fixtures it should reject:")
        for f in failed:
            print(f"  {f}")
        return False
    else:
        print(f"PASSED: All {len(INVALID_FIXTURES)} invalid fixtures rejected by Python")
        return True


def test_allowed_empty_arrays():
    """Verify empty array fixtures are accepted by Python."""
    failed = []
    for fixture_name in ALLOWED_EMPTY_ARRAY_FIXTURES:
        fixture_path = FIXTURES_DIR / fixture_name
        with open(fixture_path) as f:
            envelope_data = json.load(f)

        try:
            ExecutionEnvelope.from_dict(envelope_data)
        except Exception as e:
            failed.append(f"{fixture_name}: Expected acceptance but got {type(e).__name__}: {e}")

    if failed:
        print("FAILED: Python rejected fixtures with empty arrays:")
        for f in failed:
            print(f"  {f}")
        return False
    else:
        print(
            f"PASSED: All {len(ALLOWED_EMPTY_ARRAY_FIXTURES)} empty-array fixtures accepted by Python"
        )
        return True


def test_valid_fixture():
    """Verify valid fixture is accepted by Python."""
    fixture_path = FIXTURES_DIR / VALID_FIXTURE
    with open(fixture_path) as f:
        envelope_data = json.load(f)

    try:
        ExecutionEnvelope.from_dict(envelope_data)
        print("PASSED: Valid fixture accepted by Python")
        return True
    except Exception as e:
        print(f"FAILED: Valid fixture rejected: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    all_passed = True
    all_passed &= test_valid_fixture()
    all_passed &= test_invalid_fixtures_rejected()
    all_passed &= test_allowed_empty_arrays()

    if all_passed:
        print("\n✅ All cross-repo parity tests PASSED")
        sys.exit(0)
    else:
        print("\n❌ Some cross-repo parity tests FAILED")
        sys.exit(1)
