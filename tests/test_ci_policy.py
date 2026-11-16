"""Tests for verdict.ci_policy - CI classification and bounded recovery policy."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from verdict.ci_policy import INFRA_PATTERNS, ci_recovery_action, classify_ci, classify_merge_state


class TestInfraPatterns:
    def test_infra_patterns_tuple_contains_expected_values(self) -> None:
        assert "runner" in INFRA_PATTERNS
        assert "availability" in INFRA_PATTERNS
        assert "infrastructure" in INFRA_PATTERNS
        assert "network" in INFRA_PATTERNS
        assert "quota" in INFRA_PATTERNS
        assert "rate-limit" in INFRA_PATTERNS
        assert "timeout" in INFRA_PATTERNS
        assert "dependency-download" in INFRA_PATTERNS
        assert "artifact-upload" in INFRA_PATTERNS


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

    def test_infra_failure_when_failed_check_matches_infra_pattern(self) -> None:
        infra_failure = {
            "name": "runner-availability",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "required": True,
        }
        assert classify_ci([infra_failure]) == "INFRA_FAILURE"
        # Also test other patterns
        for pattern in INFRA_PATTERNS:
            check = {
                "name": f"check-{pattern}",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "required": True,
            }
            assert classify_ci([check]) == "INFRA_FAILURE", f"pattern {pattern} should match"

    def test_cancelled_when_any_required_cancelled(self) -> None:
        cancelled = {
            "name": "test",
            "status": "COMPLETED",
            "conclusion": "CANCELLED",
            "required": True,
        }
        assert classify_ci([cancelled]) == "CANCELLED"

    def test_unknown_conclusion_is_pending(self) -> None:
        unknown = {
            "name": "test",
            "status": "COMPLETED",
            "conclusion": "ACTION_REQUIRED",
            "required": True,
        }
        assert classify_ci([unknown]) == "PENDING"

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
