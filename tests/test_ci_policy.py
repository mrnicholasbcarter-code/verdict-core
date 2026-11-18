"""Tests for verdict.ci_policy - CI classification and bounded recovery policy."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from verdict.ci_policy import INFRA_PATTERNS, ci_recovery_action, classify_ci, classify_merge_state


def required(name: str, conclusion: str) -> dict[str, object]:
    return {"name": name, "status": "COMPLETED", "conclusion": conclusion, "required": True}


class TestClassifyCI:
    def test_empty_checks_returns_empty(self) -> None:
        assert classify_ci([]) == "EMPTY"

    def test_no_required_checks_returns_missing_required(self) -> None:
        checks = [
            {"name": "optional", "status": "COMPLETED", "conclusion": "SUCCESS", "required": False}
        ]
        assert classify_ci(checks) == "MISSING_REQUIRED"

    def test_green_when_all_required_pass(self) -> None:
        ok = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
        assert classify_ci([ok]) == "GREEN"
        assert classify_ci([ok, ok]) == "GREEN"

    def test_pending_when_required_not_concluded(self) -> None:
        pending = {"name": "test", "status": "IN_PROGRESS", "conclusion": None, "required": True}
        assert classify_ci([pending]) == "PENDING"
        # mixed: one completed, one in progress
        ok = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
        assert classify_ci([ok, pending]) == "PENDING"

    def test_code_failure_when_required_fails_without_infra_pattern(self) -> None:
        ok = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
        failure = {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE", "required": True}
        assert classify_ci([ok, failure]) == "CODE_FAILURE"

    @pytest.mark.parametrize("pattern", INFRA_PATTERNS)
    def test_failed_check_named_for_an_infra_pattern_is_infra_failure(self, pattern: str) -> None:
        """Catches a pattern being dropped from the list, or matched case-sensitively."""
        assert classify_ci([required(f"check-{pattern}", "FAILURE")]) == "INFRA_FAILURE"
        assert classify_ci([required(f"Check {pattern.upper()}", "FAILURE")]) == "INFRA_FAILURE"

    def test_one_code_failure_among_infra_failures_is_a_code_failure(self) -> None:
        """Catches a real code failure being retried away as infrastructure."""
        checks = [required("runner-availability", "FAILURE"), required("lint", "FAILURE")]
        assert classify_ci(checks) == "CODE_FAILURE"
        assert classify_ci(list(reversed(checks))) == "CODE_FAILURE"

    @pytest.mark.parametrize(
        "name",
        [
            "test (3.11)",
            "Analyze (python)",
            "osv-security / osv-scan",
            "install-smoke (3.10)",
            "lint",
            "proof-full",
        ],
    )
    def test_real_check_names_fail_as_code_cancel_as_cancelled_and_pass_as_green(
        self, name: str
    ) -> None:
        """Catches a real check name accidentally matching an infra pattern."""
        assert classify_ci([required(name, "FAILURE")]) == "CODE_FAILURE"
        assert classify_ci([required(name, "CANCELLED")]) == "CANCELLED"
        assert classify_ci([required(name, "SUCCESS")]) == "GREEN"

    def test_unfinished_required_check_outranks_a_failure(self) -> None:
        """Catches a verdict being reached while a required check is still running."""
        checks = [
            required("lint", "FAILURE"),
            {"name": "proof-full", "status": "IN_PROGRESS", "conclusion": None, "required": True},
        ]
        assert classify_ci(checks) == "PENDING"

    def test_failure_outranks_cancelled(self) -> None:
        """Catches a code failure being hidden behind a cancelled sibling check."""
        checks = [required("proof-full", "CANCELLED"), required("lint", "FAILURE")]
        assert classify_ci(checks) == "CODE_FAILURE"

    def test_optional_failure_does_not_stop_green(self) -> None:
        """Catches an optional check deciding the result."""
        optional = {
            "name": "lint",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "required": False,
        }
        assert classify_ci([required("proof-full", "SUCCESS"), optional]) == "GREEN"

    def test_cancelled_when_any_required_cancelled(self) -> None:
        cancelled = {
            "name": "test",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "required": True,
        }
        assert classify_ci([cancelled]) == "CANCELLED"

    def test_unrecognised_conclusion_is_pending(self) -> None:
        """Catches a conclusion nobody has classified being read as green or as a failure."""
        assert classify_ci([required("lint", "SOMETHING_NEW")]) == "PENDING"

    @pytest.mark.parametrize("conclusion", ["TIMED_OUT", "STARTUP_FAILURE"])
    def test_timed_out_and_startup_failure_are_infrastructure(self, conclusion: str) -> None:
        """Catches a job the runner never finished waiting at PENDING for ever."""
        assert classify_ci([required("test (3.11)", conclusion)]) == "INFRA_FAILURE"
        assert classify_ci([required("lint", "SUCCESS"), required("proof-full", conclusion)]) == (
            "INFRA_FAILURE"
        )
        assert ci_recovery_action(classify_ci([required("lint", conclusion)]), 0, 1)["action"] == (
            "RETRY_CI"
        )

    @pytest.mark.parametrize("conclusion", ["ACTION_REQUIRED", "STALE"])
    def test_action_required_and_stale_need_repair_not_a_rerun(self, conclusion: str) -> None:
        """Catches a check that needs a person being waited on, or retried, for ever."""
        assert classify_ci([required("lint", conclusion)]) == "CODE_FAILURE"
        assert classify_ci([required("lint", conclusion), required("proof-full", "TIMED_OUT")]) == (
            "CODE_FAILURE"
        )
        assert classify_ci([required("lint", conclusion), required("proof-full", "CANCELLED")]) == (
            "CODE_FAILURE"
        )

    @pytest.mark.parametrize("conclusion", ["TIMED_OUT", "STARTUP_FAILURE"])
    def test_code_failure_outranks_an_infrastructure_conclusion(self, conclusion: str) -> None:
        """Catches a real code failure being retried away because a sibling job timed out."""
        checks = [required("proof-full", conclusion), required("lint", "FAILURE")]
        assert classify_ci(checks) == "CODE_FAILURE"

    def test_unfinished_check_outranks_the_new_conclusions(self) -> None:
        """Catches a verdict on TIMED_OUT or STALE while another required check still runs."""
        running = {"name": "lint", "status": "IN_PROGRESS", "conclusion": None, "required": True}
        assert classify_ci([required("proof-full", "TIMED_OUT"), running]) == "PENDING"
        assert classify_ci([required("proof-full", "STALE"), running]) == "PENDING"

    def test_skipped_and_neutral_are_not_failures(self) -> None:
        ok = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
        skipped = {
            "name": "skipped",
            "status": "COMPLETED",
            "conclusion": "SKIPPED",
            "required": True,
        }
        neutral = {
            "name": "neutral",
            "status": "COMPLETED",
            "conclusion": "NEUTRAL",
            "required": True,
        }
        assert classify_ci([ok, skipped, neutral]) == "GREEN"


class TestClassifyMergeState:
    def test_mergeable_and_clean_returns_mergeable(self) -> None:
        assert classify_merge_state("MERGEABLE", "CLEAN") == "MERGEABLE"

    def test_conflicting_or_dirty_returns_conflict(self) -> None:
        assert classify_merge_state("CONFLICTING", "CLEAN") == "CONFLICT"
        assert classify_merge_state("MERGEABLE", "DIRTY") == "CONFLICT"
        assert classify_merge_state("CONFLICTING", "DIRTY") == "CONFLICT"

    def test_behind_returns_behind(self) -> None:
        assert classify_merge_state("MERGEABLE", "BEHIND") == "BEHIND"
        assert classify_merge_state("CONFLICTING", "BEHIND") == "CONFLICT"

    def test_unknown_returns_unknown(self) -> None:
        assert classify_merge_state("UNKNOWN", "UNKNOWN") == "UNKNOWN"
        assert classify_merge_state("MERGEABLE", "UNKNOWN") == "UNKNOWN"
        assert classify_merge_state("UNKNOWN", "CLEAN") == "UNKNOWN"


class TestCIRecoveryAction:
    def test_green_returns_proceed_merge(self) -> None:
        result = ci_recovery_action("GREEN", 0, 2)
        assert result["action"] == "PROCEED_MERGE"
        assert result["state"] == "CI_GREEN"
        assert result["reason"] == "all required checks passed"

    def test_pending_returns_wait_ci(self) -> None:
        result = ci_recovery_action("PENDING", 0, 2)
        assert result["action"] == "WAIT_CI"
        assert result["state"] == "PR_OPEN"
        assert result["reason"] == "required checks not concluded"

    def test_infra_failure_with_attempts_remaining_returns_retry(self) -> None:
        result = ci_recovery_action("INFRA_FAILURE", 0, 2)
        assert result["action"] == "RETRY_CI"
        assert result["state"] == "PR_OPEN"
        assert result["reason"] == "transient infrastructure failure"
        result = ci_recovery_action("INFRA_FAILURE", 1, 2)
        assert result["action"] == "RETRY_CI"

    def test_infra_failure_exhausted_returns_blocked(self) -> None:
        result = ci_recovery_action("INFRA_FAILURE", 2, 2)
        assert result["action"] == "BLOCKED"
        assert result["state"] == "PR_OPEN"
        assert result["reason"] == "infrastructure retries exhausted"

    def test_cancelled_same_as_infra_failure(self) -> None:
        result = ci_recovery_action("CANCELLED", 0, 2)
        assert result["action"] == "RETRY_CI"
        result = ci_recovery_action("CANCELLED", 2, 2)
        assert result["action"] == "BLOCKED"

    def test_conflict_returns_rebase_and_reproof(self) -> None:
        result = ci_recovery_action("CONFLICT", 0, 2)
        assert result["action"] == "REBASE_AND_REPROOF"
        assert result["state"] == "IMPLEMENTING"
        assert result["reason"] == "base drift or merge conflict"

    def test_code_failure_with_attempts_returns_corrective_implementation(self) -> None:
        result = ci_recovery_action("CODE_FAILURE", 0, 2)
        assert result["action"] == "CORRECTIVE_IMPLEMENTATION"
        assert result["state"] == "IMPLEMENTING"
        assert result["reason"] == "required check failed on the PR head"

    def test_code_failure_exhausted_returns_blocked(self) -> None:
        result = ci_recovery_action("CODE_FAILURE", 2, 2)
        assert result["action"] == "BLOCKED"
        assert result["state"] == "PR_OPEN"
        assert result["reason"] == "corrective attempts exhausted"

    def test_unclassifiable_returns_blocked(self) -> None:
        result = ci_recovery_action("UNKNOWN_CLASSIFICATION", 0, 2)
        assert result["action"] == "BLOCKED"
        assert result["state"] == "PR_OPEN"
        assert "unclassifiable" in result["reason"]


class TestScriptsPrimeStateCompatibility:
    """Verify that scripts/prime_state.py re-exports work identically."""

    def test_scripts_prime_state_exports_same_symbols(self) -> None:
        import importlib.util
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        path = repo_root / "scripts/prime_state.py"
        spec = importlib.util.spec_from_file_location("prime_state", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        # These should be re-exported from verdict.ci_policy
        assert mod.INFRA_PATTERNS == INFRA_PATTERNS
        assert mod.classify_ci is classify_ci
        assert mod.classify_merge_state is classify_merge_state
        assert mod.ci_recovery_action is ci_recovery_action

    def test_script_loads_without_site_packages_from_a_foreign_cwd(self, tmp_path: Path) -> None:
        """Catches the re-export depending on an installed verdict or on the cwd.

        ``python -S`` drops site-packages, and the temp cwd is not the repository,
        so the script can only find ``verdict.ci_policy`` through its own path setup.
        """
        script = Path(__file__).resolve().parents[1] / "scripts" / "prime_state.py"
        loader = (
            "import importlib.util, sys\n"
            "spec = importlib.util.spec_from_file_location('prime_state', sys.argv[1])\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "print(mod.classify_ci([]), mod.classify_ci.__module__)\n"
        )
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        done = subprocess.run(
            [sys.executable, "-S", "-c", loader, str(script)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.split() == ["EMPTY", "verdict.ci_policy"]

    def test_scripts_prime_state_behavior_unchanged(self) -> None:
        import importlib.util
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[1]
        path = repo_root / "scripts/prime_state.py"
        spec = importlib.util.spec_from_file_location("prime_state", path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)

        ok = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
        assert mod.classify_ci([ok]) == "GREEN"
        assert mod.classify_ci([]) == "EMPTY"
        assert (
            mod.classify_ci(
                [{"name": "test", "status": "IN_PROGRESS", "conclusion": None, "required": True}]
            )
            == "PENDING"
        )
        assert (
            mod.classify_ci(
                [
                    ok,
                    {
                        "name": "lint",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "required": True,
                    },
                ]
            )
            == "CODE_FAILURE"
        )
        assert (
            mod.classify_ci(
                [
                    {
                        "name": "runner-availability",
                        "status": "COMPLETED",
                        "conclusion": "FAILURE",
                        "required": True,
                    }
                ]
            )
            == "INFRA_FAILURE"
        )
        assert (
            mod.classify_ci(
                [
                    {
                        "name": "test",
                        "status": "COMPLETED",
                        "conclusion": "CANCELLED",
                        "required": True,
                    }
                ]
            )
            == "CANCELLED"
        )
        assert (
            mod.classify_ci(
                [
                    {
                        "name": "optional",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                        "required": False,
                    }
                ]
            )
            == "MISSING_REQUIRED"
        )
        assert (
            mod.classify_ci(
                [{"name": "test", "status": "COMPLETED", "conclusion": None, "required": True}]
            )
            == "PENDING"
        )
        assert mod.classify_merge_state("MERGEABLE", "CLEAN") == "MERGEABLE"
        assert mod.classify_merge_state("CONFLICTING", "DIRTY") == "CONFLICT"
        assert mod.classify_merge_state("UNKNOWN", "UNKNOWN") == "UNKNOWN"

        plan = mod.ci_recovery_action(classification="CODE_FAILURE", attempts=0, max_attempts=2)
        assert plan["action"] == "CORRECTIVE_IMPLEMENTATION" and plan["state"] == "IMPLEMENTING"
        assert mod.ci_recovery_action("INFRA_FAILURE", 0, 2)["action"] == "RETRY_CI"
        assert mod.ci_recovery_action("CONFLICT", 0, 2)["action"] == "REBASE_AND_REPROOF"
        assert mod.ci_recovery_action("CODE_FAILURE", 2, 2)["action"] == "BLOCKED"
        assert mod.ci_recovery_action("GREEN", 0, 2)["action"] == "PROCEED_MERGE"
