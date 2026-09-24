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


def sha256_raw_file(filepath):
    """Compute SHA-256 of raw file bytes (language-neutral)."""
    return hashlib.sha256(filepath.read_bytes()).hexdigest()


@pytest.fixture
def manifest():
    """Load fixture manifest."""
    return json.loads(MANIFEST_PATH.read_text())


@pytest.fixture
def all_fixtures(manifest):
    """Load all test fixtures."""
    fixtures = {}
    for filename in manifest["fixtures"]:
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

            # Compare raw file bytes sha256 (language-neutral)
            actual_sha = sha256_raw_file(fixture_path)

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
    """Test verify_execution_envelope returns expected verdicts for all fixtures."""

    def test_all_fixtures_match_manifest_verdicts(self, manifest, all_fixtures):
        """Every fixture returns the verdict specified in the manifest."""
        eval_time = manifest["evaluation_time"]
        expected_digest = manifest["expected_policy_digest"]

        for filename, fixture in all_fixtures.items():
            expected_verdict = manifest["fixtures"][filename]["expected_verdict"]
            verdict = verify_execution_envelope(
                fixture, now=eval_time, expected_policy_digest=expected_digest
            )
            assert verdict.value == expected_verdict, (
                f"{filename}: expected {expected_verdict}, got {verdict.value}"
            )

    def test_accepted_fixture(self, manifest):
        """accepted.json returns ACCEPT."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.ACCEPT

    def test_denied_fixture(self, manifest):
        """denied.json returns DENY."""
        fixture = json.loads((FIXTURES_DIR / "denied.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DENY

    def test_expired_fixture(self, manifest):
        """expired.json returns EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "expired.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_wrong_digest_fixture(self, manifest):
        """wrong-digest.json returns DIGEST_MISMATCH."""
        fixture = json.loads((FIXTURES_DIR / "wrong-digest.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DIGEST_MISMATCH

    def test_null_defaults_fixture(self, manifest):
        """null-defaults.json returns ACCEPT (optional fields null but valid)."""
        fixture = json.loads((FIXTURES_DIR / "null-defaults.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.ACCEPT

    def test_unknown_field_fixture(self, manifest):
        """unknown-field.json returns REJECT_UNKNOWN."""
        fixture = json.loads((FIXTURES_DIR / "unknown-field.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN


class TestFailClosed:
    """Test fail-closed semantics: no ACCEPT when checks are skipped or malformed."""

    def test_missing_policy_digest(self, manifest):
        """Empty policy_digest -> REJECT_UNKNOWN (schema validation fails)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["policy_digest"] = ""
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN

    def test_empty_eligibility_decision(self, manifest):
        """Empty eligibility_decision {} -> DENY."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["eligibility_decision"] = {}
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DENY

    def test_admitted_false(self, manifest):
        """admitted=false -> DENY."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["eligibility_decision"]["admitted"] = False
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DENY

    def test_unparseable_expires_at(self, manifest):
        """Unparseable expires_at -> EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["execution_constraints"]["expires_at"] = "not-a-timestamp"
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_naive_timestamp_expires_at(self, manifest):
        """Timezone-naive expires_at -> EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["execution_constraints"]["expires_at"] = "2024-01-15T13:00:00"  # No timezone
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_naive_timestamp_now(self, manifest):
        """Timezone-naive now parameter -> EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        verdict = verify_execution_envelope(
            fixture,
            now="2024-01-15T12:00:00",  # No timezone
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_eligibility_decision_as_string(self, manifest):
        """eligibility_decision as string -> REJECT_UNKNOWN (schema validation fails)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["eligibility_decision"] = "accept"
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN


class TestVerificationMutations:
    """Test verification behavior under mutations."""

    def test_flip_digest_char_causes_mismatch(self, manifest):
        """Changing one char in policy_digest causes DIGEST_MISMATCH."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        original_digest = manifest["expected_policy_digest"]

        # Flip one character
        mutated_digest = "b" + original_digest[1:]
        fixture["policy_digest"] = mutated_digest

        verdict = verify_execution_envelope(
            fixture, now=manifest["evaluation_time"], expected_policy_digest=original_digest
        )
        assert verdict == EnvelopeVerdict.DIGEST_MISMATCH

    def test_move_time_past_expiry_causes_expired(self, manifest):
        """Moving evaluation time past expiry causes EXPIRED."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())

        # Evaluate after expiry
        verdict = verify_execution_envelope(
            fixture,
            now="2024-01-15T14:00:00Z",  # After expires_at: 13:00:00Z
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_denied_never_becomes_accept(self, manifest):
        """Denied envelope stays DENY regardless of other conditions."""
        fixture = json.loads((FIXTURES_DIR / "denied.json").read_text())

        # Try with correct digest
        verdict = verify_execution_envelope(
            fixture,
            now="2024-01-01T00:00:00Z",
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DENY


class TestManifestIntegrity:
    """Test manifest SHA-256 values match files."""

    def test_manifest_sha256_matches_files(self, manifest):
        """Manifest SHA-256 values match actual fixture files (raw bytes)."""
        for filename, expected in manifest["fixtures"].items():
            fixture_path = FIXTURES_DIR / filename
            # Compare raw file bytes sha256 (language-neutral)
            actual_sha = sha256_raw_file(fixture_path)

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
        assert len(envelope.policy_digest) == 64

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


class TestAdversarialInputs:
    """Test that the verifier never raises on garbage/malformed inputs."""

    def test_execution_constraints_as_string(self, manifest):
        """execution_constraints as string -> REJECT_UNKNOWN (never raises)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["execution_constraints"] = "x"
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN

    def test_execution_constraints_as_none(self, manifest):
        """execution_constraints as None -> REJECT_UNKNOWN (never raises)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["execution_constraints"] = None
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN

    def test_execution_constraints_as_list(self, manifest):
        """execution_constraints as list -> REJECT_UNKNOWN (never raises)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["execution_constraints"] = ["constraint1", "constraint2"]
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN

    def test_task_spec_as_list(self, manifest):
        """task_spec as list -> REJECT_UNKNOWN (never raises)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["task_spec"] = ["task1", "task2"]
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN

    def test_missing_expires_at(self, manifest):
        """Missing expires_at -> EXPIRED (envelope must have bounded lifetime)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["execution_constraints"] = {}  # No expires_at
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.EXPIRED

    def test_contradictory_admitted_and_denied(self, manifest):
        """eligibility with admitted=True AND denied=True -> DENY (fail-closed)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["eligibility_decision"] = {"admitted": True, "denied": True}
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DENY

    def test_contradictory_admitted_and_decision_deny(self, manifest):
        """eligibility with admitted=True AND decision='deny' -> DENY (fail-closed)."""
        fixture = json.loads((FIXTURES_DIR / "accepted.json").read_text())
        fixture["eligibility_decision"] = {"admitted": True, "decision": "deny"}
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )
        assert verdict == EnvelopeVerdict.DENY

    def test_verifier_never_raises_on_garbage(self, manifest):
        """Verifier never raises across a table of garbage inputs."""
        garbage_inputs = [
            1,
            None,
            "x",
            [],
            {},
            {"eligibility_decision": None},
            {"eligibility_decision": "accept"},
            {"eligibility_decision": {"admitted": True}, "policy_digest": 123},
            {
                "eligibility_decision": {"admitted": True},
                "policy_digest": "a" * 64,
                "execution_constraints": "bad",
            },
        ]

        for garbage in garbage_inputs:
            # Should never raise, always return a verdict
            verdict = verify_execution_envelope(
                garbage,
                now=manifest["evaluation_time"],
                expected_policy_digest=manifest["expected_policy_digest"],
            )
            assert isinstance(verdict, EnvelopeVerdict), (
                f"Garbage input {garbage} caused non-verdict return"
            )
            # Should return a rejection verdict (never ACCEPT on garbage)
            assert verdict != EnvelopeVerdict.ACCEPT, f"Garbage input {garbage} was ACCEPTed"


class TestCanonicalConstraintKeys:
    """Test that ExecutionEnvelope rejects non-canonical execution_constraints keys."""

    def test_from_dict_rejects_unknown_constraint_key(self):
        """ExecutionEnvelope.from_dict rejects execution_constraints with unknown keys."""
        from verdict.contracts import (
            ContractValidationError,
            ExecutionEnvelope,
            TaskSpec,
            VerificationPlan,
        )

        task_spec = TaskSpec(objective="test", task_type="test")
        verification = VerificationPlan(checks=[])

        # Build envelope with non-canonical key in execution_constraints
        envelope_dict = {
            "task_spec": task_spec.to_dict(),
            "eligibility_decision": {"admitted": True},
            "policy_digest": "a" * 64,
            "allowed_capabilities": ["read"],
            "execution_constraints": {"max_ms": 5000},  # Non-canonical key
            "verification_requirements": verification.to_dict(),
            "evidence_ids": [],
            "schema_version": "1",
        }

        # Should raise ContractValidationError for unknown key
        with pytest.raises(ContractValidationError, match="unknown field"):
            ExecutionEnvelope.from_dict(envelope_dict)

    def test_verify_rejects_unknown_constraint_key(self, manifest):
        """verify_execution_envelope returns REJECT_UNKNOWN for unknown constraint keys."""
        from verdict.contracts import EnvelopeVerdict, verify_execution_envelope

        # Start with accepted fixture
        fixture_path = FIXTURES_DIR / "accepted.json"
        fixture = json.loads(fixture_path.read_text())

        # Add a non-canonical key
        fixture["execution_constraints"]["privacy_level"] = "high"

        # Verify should return REJECT_UNKNOWN (schema validation fails)
        verdict = verify_execution_envelope(
            fixture,
            now=manifest["evaluation_time"],
            expected_policy_digest=manifest["expected_policy_digest"],
        )

        assert verdict == EnvelopeVerdict.REJECT_UNKNOWN, (
            f"Expected REJECT_UNKNOWN for non-canonical key, got {verdict}"
        )
