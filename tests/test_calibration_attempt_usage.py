"""Tests for calibration attempt-usage extraction (BOD-203 AC6).

Verifies that build_calibration_records.py correctly sums real per-attempt
token usage from orchestration receipts into CalibrationRecord fields.
"""

from __future__ import annotations

# The script is not a package; import its internals via importlib.
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from verdict.decision_signals.calibration import CalibrationRecord, load_records

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_calibration_records.py"
_spec = importlib.util.spec_from_file_location("build_calibration_records", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
sys.modules["build_calibration_records"] = _mod
_spec.loader.exec_module(_mod)

build_record = _mod.build_record
process_runs_dir = _mod.process_runs_dir
_sum_attempt_usage = _mod._sum_attempt_usage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "live4_calibration"


def _make_receipt(
    nodes: list[dict[str, Any]], *, topology: str = "PARALLEL_WORK_UNITS", outcome: str = "COMPLETE"
) -> dict[str, Any]:
    """Minimal receipt for testing."""
    return {"run_id": "test-run", "topology": topology, "outcome": outcome, "nodes": nodes}


def _make_node(
    node_id: str, attempts: list[dict[str, Any]], kind: str = "implement"
) -> dict[str, Any]:
    return {"node_id": node_id, "kind": kind, "attempts": attempts}


def _make_attempt(
    num: int, *, usage: dict[str, Any] | None = None, outcome: str = "success"
) -> dict[str, Any]:
    att: dict[str, Any] = {"attempt": num, "outcome": outcome}
    if usage is not None:
        att["usage"] = usage
    return att


# ---------------------------------------------------------------------------
# Tests: _sum_attempt_usage
# ---------------------------------------------------------------------------


class TestSumAttemptUsage:
    """Unit tests for the usage-summation helper."""

    def test_all_attempts_with_usage(self) -> None:
        nodes = [
            _make_node(
                "a",
                [
                    _make_attempt(
                        1, usage={"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.5}
                    )
                ],
            ),
            _make_node(
                "b",
                [
                    _make_attempt(
                        1, usage={"input_tokens": 200, "output_tokens": 20, "cost_usd": 1.0}
                    ),
                    _make_attempt(
                        2, usage={"input_tokens": 300, "output_tokens": 30, "cost_usd": 1.5}
                    ),
                ],
            ),
        ]
        result = _sum_attempt_usage(nodes)
        assert result["total_input_tokens"] == 600
        assert result["total_output_tokens"] == 60
        assert result["total_cost_usd"] == pytest.approx(3.0)
        assert result["attempts_with_usage"] == 3
        assert result["attempts_without_usage"] == 0

    def test_no_usage_at_all(self) -> None:
        nodes = [
            _make_node("a", [_make_attempt(1)]),  # no usage key
            _make_node("b", [_make_attempt(1)]),
        ]
        result = _sum_attempt_usage(nodes)
        assert result["total_input_tokens"] is None
        assert result["total_output_tokens"] is None
        assert result["total_cost_usd"] is None
        assert result["attempts_with_usage"] == 0
        assert result["attempts_without_usage"] == 2

    def test_mixed_usage_and_no_usage(self) -> None:
        """Old attempts without usage are counted, never treated as zero."""
        nodes = [
            _make_node(
                "a",
                [
                    _make_attempt(
                        1, usage={"input_tokens": 500, "output_tokens": 50, "cost_usd": 2.0}
                    )
                ],
            ),
            _make_node(
                "b",
                [
                    _make_attempt(1)  # old log, no usage
                ],
            ),
        ]
        result = _sum_attempt_usage(nodes)
        assert result["total_input_tokens"] == 500
        assert result["total_output_tokens"] == 50
        assert result["attempts_with_usage"] == 1
        assert result["attempts_without_usage"] == 1

    def test_cost_none_when_any_missing(self) -> None:
        """If any attempt with usage lacks cost_usd, total_cost_usd is None."""
        nodes = [
            _make_node(
                "a",
                [
                    _make_attempt(
                        1, usage={"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.5}
                    ),
                    _make_attempt(2, usage={"input_tokens": 200, "output_tokens": 20}),  # no cost
                ],
            )
        ]
        result = _sum_attempt_usage(nodes)
        assert result["total_input_tokens"] == 300
        assert result["total_cost_usd"] is None
        assert result["attempts_with_usage"] == 2

    def test_kr_subscription_zero_cost(self) -> None:
        """kr/* routes report cost_usd=0.0; tokens are the real signal."""
        nodes = [
            _make_node(
                "a",
                [
                    _make_attempt(
                        1, usage={"input_tokens": 9393, "output_tokens": 44, "cost_usd": 0.0}
                    )
                ],
            )
        ]
        result = _sum_attempt_usage(nodes)
        assert result["total_input_tokens"] == 9393
        assert result["total_output_tokens"] == 44
        # cost_usd 0.0 is a valid reported value, not missing
        assert result["total_cost_usd"] == pytest.approx(0.0)
        assert result["attempts_with_usage"] == 1

    def test_empty_usage_dict_counts_as_missing(self) -> None:
        nodes = [_make_node("a", [_make_attempt(1, usage={})])]
        result = _sum_attempt_usage(nodes)
        assert result["attempts_without_usage"] == 1
        assert result["total_input_tokens"] is None


# ---------------------------------------------------------------------------
# Tests: build_record with live4 fixture
# ---------------------------------------------------------------------------


class TestBuildRecordLive4:
    """Integration: build a record from the live4 receipt fixture."""

    @pytest.fixture()
    def receipt(self) -> dict[str, Any]:
        return json.loads((FIXTURE_DIR / "receipt.json").read_text())

    def test_beta_totals(self, receipt: dict[str, Any]) -> None:
        """Beta had 2 attempts: 9656/25 failed + 9302/102 succeeded = 18958/127."""
        record, reason = build_record("live4", receipt)
        assert record is not None, f"build_record failed: {reason}"
        # Totals across ALL nodes (alpha + beta)
        # alpha: 9393/44, beta: 9656+9302=18958 / 25+102=127
        assert record["total_input_tokens"] == 9393 + 9656 + 9302
        assert record["total_output_tokens"] == 44 + 25 + 102
        assert record["attempts_with_usage"] == 3
        assert record["attempts_without_usage"] == 0

    def test_cost_zero_for_subscription(self, receipt: dict[str, Any]) -> None:
        """All kr/* attempts report cost_usd=0.0, so total is 0.0 (valid)."""
        record, _ = build_record("live4", receipt)
        assert record is not None
        assert record["total_cost_usd"] == pytest.approx(0.0)

    def test_record_loads_via_calibration(self, receipt: dict[str, Any]) -> None:
        """Record roundtrips through CalibrationRecord.from_dict / load_records."""
        record, _ = build_record("live4", receipt)
        assert record is not None
        cr = CalibrationRecord.from_dict(record)
        assert cr.total_input_tokens == 9393 + 9656 + 9302
        assert cr.total_output_tokens == 44 + 25 + 102
        assert cr.attempts_with_usage == 3
        assert cr.attempts_without_usage == 0


# ---------------------------------------------------------------------------
# Tests: process_runs_dir with fixture
# ---------------------------------------------------------------------------


class TestProcessRunsDir:
    """End-to-end: process_runs_dir over a synthetic runs directory."""

    @pytest.fixture()
    def runs_dir(self, tmp_path: Path) -> Path:
        run = tmp_path / "run1"
        run.mkdir()
        shutil.copy(FIXTURE_DIR / "receipt.json", run / "receipt.json")
        return tmp_path

    def test_produces_record_with_usage(self, runs_dir: Path) -> None:
        records, _skips = process_runs_dir(runs_dir)
        assert len(records) == 1
        r = records[0]
        assert r["total_input_tokens"] is not None
        assert r["attempts_with_usage"] == 3

    def test_jsonl_roundtrip(self, runs_dir: Path, tmp_path: Path) -> None:
        """JSONL output loads back through calibration.load_records."""
        records, _ = process_runs_dir(runs_dir)
        jsonl_path = tmp_path / "cal.jsonl"
        jsonl_path.write_text("\n".join(json.dumps(r, sort_keys=True) for r in records))
        loaded = load_records(jsonl_path)
        assert len(loaded) == 1
        assert loaded[0].total_input_tokens == 9393 + 9656 + 9302


# ---------------------------------------------------------------------------
# Tests: edge cases for CalibrationRecord new fields
# ---------------------------------------------------------------------------


class TestCalibrationRecordNewFields:
    """CalibrationRecord handles the new optional fields correctly."""

    def test_defaults_when_absent(self) -> None:
        """Old records without token fields still load (defaults)."""
        old_record = {
            "task_id": "old",
            "task_class": "bounded_implementation",
            "frontier_worthy": None,
            "confidence": None,
            "security_sensitive": None,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
        }
        cr = CalibrationRecord.from_dict(old_record)
        assert cr.total_input_tokens is None
        assert cr.total_output_tokens is None
        assert cr.attempts_with_usage == 0
        assert cr.attempts_without_usage == 0

    def test_with_token_fields(self) -> None:
        cr = CalibrationRecord.from_dict(
            {
                "task_id": "t1",
                "task_class": "bounded_implementation",
                "frontier_worthy": 0.8,
                "confidence": 0.9,
                "security_sensitive": 0.1,
                "frontier_needed": True,
                "security_relevant": False,
                "planner_was_frontier": True,
                "verified": True,
                "first_pass": False,
                "total_input_tokens": 28351,
                "total_output_tokens": 171,
                "attempts_with_usage": 3,
                "attempts_without_usage": 0,
            }
        )
        assert cr.total_input_tokens == 28351
        assert cr.total_output_tokens == 171
        assert cr.attempts_with_usage == 3


# ---------------------------------------------------------------------------
# Tests: old events without usage -> attempts_without_usage
# ---------------------------------------------------------------------------


class TestOldEventsWithoutUsage:
    """Receipts from before usage recording produce correct counts."""

    def test_all_missing(self) -> None:
        receipt = _make_receipt(
            [_make_node("n1", [_make_attempt(1), _make_attempt(2)])],
            topology="SOLO",
            outcome="COMPLETE",
        )
        record, _ = build_record("old-run", receipt)
        assert record is not None
        assert record["total_input_tokens"] is None
        assert record["total_output_tokens"] is None
        assert record["total_cost_usd"] == 0.0  # fallback
        assert record["attempts_with_usage"] == 0
        assert record["attempts_without_usage"] == 2

    def test_mixed_cost_reporting(self) -> None:
        """One attempt has cost, one doesn't -> total_cost_usd is None (falls back to 0.0 in record)."""
        receipt = _make_receipt(
            [
                _make_node(
                    "n1",
                    [
                        _make_attempt(
                            1, usage={"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.5}
                        ),
                        _make_attempt(2, usage={"input_tokens": 200, "output_tokens": 20}),
                    ],
                )
            ],
            topology="SOLO",
            outcome="COMPLETE",
        )
        record, _ = build_record("mixed-cost", receipt)
        assert record is not None
        # total_cost_usd is None from usage sum -> record stores 0.0 (safe default)
        assert record["total_cost_usd"] == 0.0
        assert record["total_input_tokens"] == 300
        assert record["attempts_with_usage"] == 2
