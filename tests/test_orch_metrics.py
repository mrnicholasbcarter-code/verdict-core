"""Tests for efficiency metrics computation from receipts (BOD-311)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.orchestration.receipt import compute_run_metrics, verify_run_receipt

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "metrics"


def test_compute_metrics_complete_run():
    """Test metrics computation for a COMPLETE run with known costs."""
    receipt_path = FIXTURES_DIR / "complete-run" / "receipt.json"
    receipt = json.loads(receipt_path.read_text())

    metrics = compute_run_metrics(receipt)

    assert metrics["accepted_tasks"] == 3
    assert metrics["dollars"] == pytest.approx(0.15)
    assert metrics["has_unknown_cost"] is False
    assert metrics["tasks_per_dollar"] == pytest.approx(20.0)
    assert metrics["free_share"] == pytest.approx(1 / 3)
    assert metrics["subscription_share"] == pytest.approx(1 / 3)
    assert metrics["metered_share"] == pytest.approx(1 / 3)
    assert metrics["unknown_capacity_share"] == 0.0
    assert metrics["total_model_attempts"] == 3
    assert metrics["outcome"] == "COMPLETE"


def test_compute_metrics_unknown_cost():
    """Test metrics computation for a COMPLETE run with unknown costs."""
    receipt_path = FIXTURES_DIR / "unknown-cost-run" / "receipt.json"
    receipt = json.loads(receipt_path.read_text())

    metrics = compute_run_metrics(receipt)

    assert metrics["accepted_tasks"] == 3
    assert metrics["dollars"] is None
    assert metrics["has_unknown_cost"] is True
    assert metrics["tasks_per_dollar"] == "undefined"
    # Shares still computed from capacity_class
    assert metrics["free_share"] == pytest.approx(1 / 3)
    assert metrics["subscription_share"] == pytest.approx(1 / 3)
    assert metrics["metered_share"] == pytest.approx(1 / 3)
    assert metrics["total_model_attempts"] == 3
    assert metrics["outcome"] == "COMPLETE"


def test_compute_metrics_blocked_run():
    """Test metrics computation for a BLOCKED run (0 accepted tasks)."""
    receipt_path = FIXTURES_DIR / "blocked-run" / "receipt.json"
    receipt = json.loads(receipt_path.read_text())

    metrics = compute_run_metrics(receipt)

    assert metrics["accepted_tasks"] == 0
    assert metrics["dollars"] == 0.0
    assert metrics["has_unknown_cost"] is False
    assert metrics["tasks_per_dollar"] == "undefined"
    assert metrics["free_share"] == 1.0
    assert metrics["subscription_share"] == 0.0
    assert metrics["metered_share"] == 0.0
    assert metrics["total_model_attempts"] == 1
    assert metrics["outcome"] == "BLOCKED"


def test_verify_receipt_tampered():
    """Test that tampered receipt is detected."""
    run_dir = FIXTURES_DIR / "tampered-run"
    problems = verify_run_receipt(run_dir)

    assert len(problems) > 0
    assert any("events_digest mismatch" in p for p in problems)


def test_verify_receipt_valid():
    """Test that valid receipts pass verification."""
    for run_name in ["complete-run", "unknown-cost-run", "blocked-run"]:
        run_dir = FIXTURES_DIR / run_name
        problems = verify_run_receipt(run_dir)
        assert len(problems) == 0, f"{run_name} should verify cleanly: {problems}"


def test_action_metrics_single_run():
    """Test the metrics action with a single run."""
    from verdict.actions.registry import run_action

    result = run_action(
        "metrics",
        {
            "run_dir": str(FIXTURES_DIR / "complete-run"),
            "runs_dir": str(FIXTURES_DIR),
            "json": True,
        },
    )

    assert result.ok
    data = result.data
    assert data["accepted_tasks"] == 3
    assert data["dollars"] == pytest.approx(0.15)
    assert data["has_unknown_cost"] is False
    assert data["runs_processed"] == 1
    assert data["runs_skipped"] == 0


def test_action_metrics_unknown_cost_run():
    """Test the metrics action with unknown cost run."""
    from verdict.actions.registry import run_action

    result = run_action(
        "metrics",
        {
            "run_dir": str(FIXTURES_DIR / "unknown-cost-run"),
            "runs_dir": str(FIXTURES_DIR),
            "json": True,
        },
    )

    assert result.ok
    data = result.data
    assert data["accepted_tasks"] == 3
    assert data["dollars"] is None
    assert data["has_unknown_cost"] is True
    assert data["tasks_per_dollar"] == "undefined"


def test_action_metrics_tampered_run():
    """Test that tampered run is skipped."""
    from verdict.actions.registry import run_action

    result = run_action(
        "metrics",
        {
            "run_dir": str(FIXTURES_DIR / "tampered-run"),
            "runs_dir": str(FIXTURES_DIR),
            "json": True,
        },
    )

    assert not result.ok  # No runs processed
    data = result.data
    assert data["runs_processed"] == 0
    assert data["runs_skipped"] == 1
    assert "verification failed" in data["skipped_runs"][0]["reason"]


def test_action_metrics_directory_scan():
    """Test the metrics action scanning a directory."""
    from verdict.actions.registry import run_action

    result = run_action("metrics", {"runs_dir": str(FIXTURES_DIR), "json": True})

    assert result.ok
    data = result.data
    # 3 runs processed (complete, unknown-cost, blocked), 1 skipped (tampered)
    assert data["runs_processed"] == 3
    assert data["runs_skipped"] == 1
    assert data["accepted_tasks"] == 6  # 3 + 3 + 0
    assert data["has_unknown_cost"] is True  # unknown-cost run has unknown cost
    assert data["dollars"] is None
    assert data["tasks_per_dollar"] == "undefined"
    # Total attempts: 3 + 3 + 1 = 7
    assert data["total_model_attempts"] == 7
    # Free: 1 + 1 + 1 = 3, Sub: 1 + 1 + 0 = 2, Metered: 1 + 1 + 0 = 2
    assert data["free_share"] == pytest.approx(3 / 7)
    assert data["subscription_share"] == pytest.approx(2 / 7)
    assert data["metered_share"] == pytest.approx(2 / 7)


def test_fixture_runs_are_fixtures():
    """Verify that fixture runs are labelled as fixtures (AGENTS.md compliance)."""
    # This test documents that our fixture data is explicitly fixture data
    # and never satisfies live proof requirements.
    for run_name in ["complete-run", "unknown-cost-run", "blocked-run", "tampered-run"]:
        receipt_path = FIXTURES_DIR / run_name / "receipt.json"
        assert receipt_path.exists(), f"Fixture {run_name} should exist"
        receipt = json.loads(receipt_path.read_text())
        # Fixture runs have test-* run_ids
        assert receipt["run_id"].startswith("test-"), f"{run_name} should have test run_id"
