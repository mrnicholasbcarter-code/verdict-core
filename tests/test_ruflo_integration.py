"""
Tests for Ruflo integration scenarios and completion evidence (Issue #42 / Slice 32.5).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from verdict.ruflo_integration import (
    RufloHarness,
    RufloHarnessConfig,
    ScenarioType,
    TaskStatus,
    run_integration_suite,
)


class TestRufloHarnessConfig:
    """Tests for RufloHarnessConfig."""

    def test_default_config(self):
        config = RufloHarnessConfig()
        assert config.scenario_timeout_seconds == 30
        assert config.default_budget_usd == 10.0
        assert config.max_concurrency == 3
        assert config.max_replans == 2
        assert config.risk_floor == 0.1
        assert config.output_dir == Path("evidence")
        assert config.produce_evidence is True

    def test_custom_config(self):
        config = RufloHarnessConfig(
            scenario_timeout_seconds=60,
            default_budget_usd=50.0,
            max_concurrency=10,
            max_replans=5,
            risk_floor=0.5,
            output_dir=Path("/tmp/test"),
            produce_evidence=False,
        )
        assert config.scenario_timeout_seconds == 60
        assert config.default_budget_usd == 50.0
        assert config.max_concurrency == 10
        assert config.max_replans == 5
        assert config.risk_floor == 0.5
        assert config.output_dir == Path("/tmp/test")
        assert config.produce_evidence is False


class TestRufloHarness:
    """Tests for RufloHarness."""

    def test_harness_initialization(self):
        harness = RufloHarness()
        assert harness.config is not None
        assert harness.results == []
        assert harness.current_scenario is None

    def test_harness_creates_output_dir(self):
            with tempfile.TemporaryDirectory() as tmpdir:
                config = RufloHarnessConfig(output_dir=Path(tmpdir) / "test_evidence")
                _ = RufloHarness(config)
                assert (Path(tmpdir) / "test_evidence").exists()

    def test_run_happy_path(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.HAPPY_PATH)

        assert result.scenario_type == ScenarioType.HAPPY_PATH
        assert result.status == TaskStatus.COMPLETED
        assert len(result.steps_completed) == 6
        assert len(result.steps_failed) == 0
        assert result.replan_attempts == 0
        assert result.completed_at is not None
        assert result.duration_ms > 0

    def test_run_approval_required(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.APPROVAL_REQUIRED)

        assert result.scenario_type == ScenarioType.APPROVAL_REQUIRED
        assert result.status == TaskStatus.COMPLETED
        assert any("approval" in step.lower() for step in result.steps_completed)
        assert any(v.get("requires_approval") for v in result.verification_results)

    def test_run_pause_resume(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.PAUSE_RESUME)

        assert result.scenario_type == ScenarioType.PAUSE_RESUME
        assert result.status == TaskStatus.COMPLETED
        assert "pause" in result.steps_completed
        assert "resume" in result.steps_completed

    def test_run_cancellation(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.CANCELLATION)

        assert result.scenario_type == ScenarioType.CANCELLATION
        assert result.status == TaskStatus.CANCELLED
        assert "cancel" in result.steps_completed

    def test_run_timeout(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.TIMEOUT)

        assert result.scenario_type == ScenarioType.TIMEOUT
        assert result.status == TaskStatus.TIMED_OUT
        assert "timeout" in result.steps_completed

    def test_run_partial_failure(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.PARTIAL_FAILURE)

        assert result.scenario_type == ScenarioType.PARTIAL_FAILURE
        assert result.status == TaskStatus.COMPLETED
        assert len(result.steps_failed) == 1
        assert any("retry" in step for step in result.steps_completed)

    def test_run_retry(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.RETRY)

        assert result.scenario_type == ScenarioType.RETRY
        assert result.status == TaskStatus.COMPLETED
        assert len(result.steps_failed) >= 2  # Multiple failed attempts
        assert any("backoff" in step for step in result.steps_completed)

    def test_run_verification_failure(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.VERIFICATION_FAILURE)

        assert result.scenario_type == ScenarioType.VERIFICATION_FAILURE
        assert result.status == TaskStatus.COMPLETED
        assert result.replan_attempts == 1
        assert any("replan" in step for step in result.steps_completed)
        assert any(v.get("outcome") == "fail" for v in result.verification_results)

    def test_run_replan_exhaustion(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.REPLAN_EXHAUSTION)

        assert result.scenario_type == ScenarioType.REPLAN_EXHAUSTION
        assert result.status == TaskStatus.FAILED
        assert result.replan_attempts == harness.config.max_replans + 1
        assert result.error is not None
        assert "Max replans" in result.error

    def test_run_quota_denial(self):
        harness = RufloHarness()
        result = harness.run_scenario(ScenarioType.QUOTA_DENIAL)

        assert result.scenario_type == ScenarioType.QUOTA_DENIAL
        assert result.status == TaskStatus.COMPLETED  # Completes but with quota failure
        assert any("quota" in v.get("classification", "") for v in result.verification_results)

    def test_run_rufl_o_unavailable(self):
        harness = RufloHarness()
        _ = harness.run_scenario(ScenarioType.RUFLO_UNAVAILABLE)

    def test_run_all_scenarios(self):
        harness = RufloHarness()
        results = harness.run_all_scenarios()

        assert len(results) == len(ScenarioType)
        assert all(r.completed_at is not None for r in results)
        assert all(r.duration_ms > 0 for r in results)

    def test_evidence_bundle_generated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = RufloHarnessConfig(
                output_dir=Path(tmpdir) / "evidence",
                produce_evidence=True,
            )
            harness = RufloHarness(config)
            result = harness.run_scenario(ScenarioType.HAPPY_PATH)

            assert result.evidence_bundle is not None
            assert "scenario" in result.evidence_bundle
            assert "commit" in result.evidence_bundle
            assert "adapter_version" in result.evidence_bundle
            assert "schema_version" in result.evidence_bundle
            assert "policy_version" in result.evidence_bundle

            # Check file was written
            evidence_files = list((Path(tmpdir) / "evidence").glob("*.json"))
            assert len(evidence_files) == 1

    def test_no_evidence_when_disabled(self):
        config = RufloHarnessConfig(produce_evidence=False)
        harness = RufloHarness(config)
        result = harness.run_scenario(ScenarioType.HAPPY_PATH)

        assert result.evidence_bundle == {}


class TestIntegrationSuite:
    """Tests for the integration suite runner."""

    def test_run_integration_suite(self):
        config = RufloHarnessConfig(
            output_dir=Path("evidence/test_suite"),
            produce_evidence=True,
        )
        results, summary = run_integration_suite(config)

        assert len(results) == len(ScenarioType)
        assert isinstance(summary, str)
        assert "Ruflo Integration Scenario Summary" in summary
        assert "Scenario Results:" in summary
        assert "Summary:" in summary

    def test_summary_includes_all_scenarios(self):
        config = RufloHarnessConfig(produce_evidence=False)
        _, summary = run_integration_suite(config)

        for scenario_type in ScenarioType:
            assert scenario_type.value in summary


class TestEvidenceBundle:
    """Tests for evidence bundle structure."""

    def test_evidence_bundle_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = RufloHarnessConfig(
                output_dir=Path(tmpdir) / "evidence",
                produce_evidence=True,
            )
            harness = RufloHarness(config)
            result = harness.run_scenario(ScenarioType.HAPPY_PATH)

            bundle = result.evidence_bundle
            assert bundle["scenario"] == "happy_path"
            assert "started_at" in bundle
            assert "completed_at" in bundle
            assert "duration_ms" in bundle
            assert "status" in bundle
            assert "steps_completed" in bundle
            assert "verification_results" in bundle
            assert "replan_attempts" in bundle
            assert "generated_at" in bundle


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
