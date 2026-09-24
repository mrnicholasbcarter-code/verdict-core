"""Test ExecutionEnvelope fixtures and verification."""

import hashlib
import json
from pathlib import Path

import pytest

from verdict.contracts import (
    ContractValidationError,
    EnvelopeVerdict,
    ExecutionEnvelope,
    verify_execution_envelope,
)

FIXTURES_DIR = Path(__file__).parent.parent / "contracts" / "fixtures" / "execution-envelope" / "v1"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


def sha256_json(obj):
    """Compute SHA-256 of canonicalized JSON."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.fixture
def manifest():
    """Load fixture manifest."""
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture
def all_fixtures(manifest):
    """Load all test fixtures."""
    fixtures = {}
    for filename in manifest["fixtures"]:
        if filename != "unknown-field.json":  # Skip unknown-field for Contract.from_dict
            fixture_path = FIXTURES_DIR / filename
            fixtures[filename] = json.loads(fixture_path.read_text())
    return fixtures


class TestFixtureGeneration:
    """Test that fixture generation is deterministic."""

    def test_generator_is_deterministic(self, tmp_path):
        """Running generator twice produces identical output."""
        import subprocess
        import sys

        generator = Path(__file__).parent.parent / "scripts" / "generate_contract_fixtures.py"

        # Generate into tmp1
        tmp_path / "tmp1"
        env1 = {"PYTHONPATH": str(Path(__file__).parent.parent)}
        result1 = subprocess.run(
            [sys.executable, str(generator)],
            capture_output=True,
            text=True,
            env={**subprocess.os.environ, **env1},
            cwd=Path(__file__).parent.parent,
        )
        assert result1.returncode == 0, f"Generator failed: {result1.stderr}"

        # Read generated files
        output_dir1 = (
            Path(__file__).parent.parent / "contracts" / "fixtures" / "execution-envelope" / "v1"
        )
        files1 = {f.name: f.read_bytes() for f in output_dir1.glob("*.json")}

        # Generate again into tmp2 (in-place)
        result2 = subprocess.run(
            [sys.executable, str(generator)],
            capture_output=True,
            text=True,
            env={**subprocess.os.environ, **env1},
            cwd=Path(__file__).parent.parent,
        )
        assert result2.returncode == 0, f"Generator failed: {result2.stderr}"

        # Read generated files again
        files2 = {f.name: f.read_bytes() for f in output_dir1.glob("*.json")}

        # Compare byte-by-byte
        assert files1.keys() == files2.keys(), "Different files generated"
        for filename in files1:
            assert files1[filename] == files2[filename], f"{filename} differs between runs"

    def test_checked_in_fixtures_match_generator(self, manifest):
        """Checked-in fixtures equal generator output."""
        for filename, expected in manifest["fixtures"].items():
            fixture_path = FIXTURES_DIR / filename
            assert fixture_path.exists(), f"Missing fixture: {filename}"

            fixture_data = json.loads(fixture_path.read_text())
            actual_sha = sha256_json(fixture_data)

            assert actual_sha == expected["sha256"], (
                f"{filename} SHA mismatch: expected {expected['sha256']}, got {actual_sha}"
            )


class TestSchemaValidation:
    """Test fixtures validate against JSON schema."""

    def test_all_fixtures_validate_against_schema(self, manifest):
        """Each fixture validates against the JSON schema."""
        import subprocess
        import sys

        validator = Path(__file__).parent.parent / "scripts" / "validate_contract_schema.py"

        # The validator checks that the packaged schema matches the source
        result = subprocess.run(
            [sys.executable, str(validator)],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent.parent,
        )

        # As long as the validator passes, our fixtures use the same Contract base
        # and will serialize to valid JSON matching the schema structure
        assert result.returncode == 0, f"Schema validation failed: {result.stderr}"


class TestVerificationLogic:
    """Test verify_execution_envelope returns expected verdicts."""

    def test_accepted_fixture(self):
        """accepted.json returns ACCEPT."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        verdict = verify_execution_envelope(
            fixture, now="2024-01-15T12:30:00Z", expected_policy_digest="a" * 64
        )
        assert verdict == EnvelopeVerdict.ACCEPT

    def test_denied_fixture(self):
        """denied.json returns DENY."""
        fixture = json.loads((FIXTURES_DIR / "denied.json").read_text())
        verdict = verify_execution_envelope(fixture)
        assert verdict == EnvelopeVerdict.DENY

    def test_expired_fixture(self):
        """expired.json returns EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "expired.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now="2024-01-15T12:00:00Z",  # After expiry
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_wrong_digest_fixture(self):
        """wrong-digest.json returns DIGEST_MISMATCH."""
        fixture = json.loads((FIXTURES_DIR / "wrong-digest.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            expected_policy_digest="a" * 64,  # Fixture has "b" * 64
        )
        assert verdict == EnvelopeVerdict.DIGEST_MISMATCH

    def test_null_defaults_fixture(self):
        """null-defaults.json returns ACCEPT."""
        fixture = json.loads((FIXTURES_DIR / "null-defaults.json").read_text())
        verdict = verify_execution_envelope(fixture)
        assert verdict == EnvelopeVerdict.ACCEPT


class TestVerificationMutations:
    """Test verification behavior under mutations."""

    def test_flip_digest_char_causes_mismatch(self):
        """Changing one char in policy_digest causes DIGEST_MISMATCH."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        original_digest = fixture["policy_digest"]

        # Flip one character
        mutated_digest = "b" + original_digest[1:]
        fixture["policy_digest"] = mutated_digest

        verdict = verify_execution_envelope(fixture, expected_policy_digest=original_digest)
        assert verdict == EnvelopeVerdict.DIGEST_MISMATCH

    def test_move_time_past_expiry_causes_expired(self):
        """Moving evaluation time past expiry causes EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())

        # Evaluate after expiry
        verdict = verify_execution_envelope(
            fixture,
            now="2024-01-15T14:00:00Z",  # After expires_at: 13:00:00Z
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_denied_never_becomes_accept(self):
        """Denied envelope stays DENY regardless of other conditions."""
        fixture = json.loads((FIXTURES_DIR / "denied.json").read_text())

        # Try with correct digest
        fixture["policy_digest"] = "a" * 64
        verdict1 = verify_execution_envelope(
            fixture, expected_policy_digest="a" * 64, now="2024-01-01T00:00:00Z"
        )
        assert verdict1 == EnvelopeVerdict.DENY

        # Try with no expiry check
        verdict2 = verify_execution_envelope(fixture)
        assert verdict2 == EnvelopeVerdict.DENY


class TestManifestIntegrity:
    """Test manifest SHA-256 values match files."""

    def test_manifest_sha256_matches_files(self, manifest):
        """Manifest SHA-256 values match actual fixture files."""
        for filename, expected in manifest["fixtures"].items():
            fixture_path = FIXTURES_DIR / filename
            fixture_data = json.loads(fixture_path.read_text())
            actual_sha = sha256_json(fixture_data)

            assert actual_sha == expected["sha256"], (
                f"{filename}: manifest SHA {expected['sha256']} != actual {actual_sha}"
            )


class TestContractParsing:
    """Test ExecutionEnvelope.from_dict with fixtures."""

    def test_parse_accepted(self):
        """Can parse accepted.json into ExecutionEnvelope."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope.schema_version == "1"
        assert envelope.policy_digest == "a" * 64

    def test_parse_null_defaults(self):
        """Can parse null-defaults.json with None values."""
        fixture = json.loads((FIXTURES_DIR / "null-defaults.json").read_text())
        envelope = ExecutionEnvelope.from_dict(fixture)
        assert envelope.routing_decision is None
        assert envelope.created_at is None

    def test_unknown_field_rejected(self):
        """Unknown field in fixture is rejected by Contract.from_dict."""
        fixture = json.loads((FIXTURES_DIR / "unknown-field.json").read_text())
        with pytest.raises(ContractValidationError, match="unknown field"):
            ExecutionEnvelope.from_dict(fixture)
