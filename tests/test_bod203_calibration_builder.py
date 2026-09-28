"""Tests for scripts/build_calibration_records.py (BOD-203).

Builds small synthetic runs in tmp_path and verifies CalibrationRecord JSONL
output is correct and consumable by calibration.load_records().
"""

from __future__ import annotations

# Import the builder module
import importlib.util
import json
from pathlib import Path
from typing import Any

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_calibration_records.py"
_spec = importlib.util.spec_from_file_location("build_calibration_records", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

build_record = _mod.build_record
process_runs_dir = _mod.process_runs_dir
main = _mod.main

from verdict.decision_signals.calibration import CalibrationRecord, load_records  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal_set(
    *,
    frontier_worthy: float = 0.7,
    security_sensitive: float = 0.1,
    confidence: float = 0.85,
    latency_ms: int = 120,
) -> dict[str, Any]:
    """Minimal DecisionSignalSetV1-shaped dict."""
    return {
        "schema_version": "decision-signals/v1",
        "provider": "openjev",
        "model": "test-model",
        "version": "v1",
        "request_id": "req-001",
        "purpose": "route_selection",
        "signals": {
            "frontier_worthy": frontier_worthy,
            "security_sensitive": security_sensitive,
            "complexity": 0.5,
            "decomposability": 0.3,
            "ambiguity": 0.2,
            "verification_strength": 0.8,
            "context_need": 0.4,
        },
        "confidence": confidence,
        "latency_ms": latency_ms,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "input_digest": "a" * 64,
        "observed_at": "2026-09-28T00:00:00Z",
        "failure_class": None,
        "mode": "SHADOW",
    }


def _attempt(
    num: int = 1, route: str = "kr/claude-sonnet-5", outcome: str = "success"
) -> dict[str, Any]:
    return {
        "attempt": num,
        "route_id": route,
        "provider": "kr",
        "capacity_class": "standard",
        "outcome": outcome,
    }


def _make_receipt(
    *,
    run_id: str = "run-001",
    topology: str = "SOLO",
    outcome: str = "COMPLETE",
    nodes: list[dict[str, Any]] | None = None,
    decision_signals: list[dict[str, Any]] | None = None,
    started_at: str = "2026-09-28T00:00:00Z",
    finished_at: str = "2026-09-28T00:05:00Z",
    reassignments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal valid receipt dict."""
    if nodes is None:
        nodes = [
            {
                "node_id": "impl-1",
                "kind": "implement",
                "final_state": "VALIDATED",
                "attempts": [_attempt()],
                "validated_by": [{"command": "pytest", "exit_code": 0, "seq": 10}],
            }
        ]
    return {
        "schema": "verdict.run-receipt/v1",
        "run_id": run_id,
        "goal": "test goal",
        "topology": topology,
        "nodes": nodes,
        "outcome": outcome,
        "reason": "all good",
        "started_at": started_at,
        "finished_at": finished_at,
        "decision_signals": decision_signals,
        "reassignments": reassignments or [],
        "cooldowns": [],
        "review": {"status": "PASS", "reviewer": "test", "route_id": "kr/x", "blocking": 0},
        "integration": {"ok": True, "barriers": [], "missing": []},
        "event_count": 12,
        "events_digest": "sha256:" + "b" * 64,
        "graph_digest": "sha256:" + "c" * 64,
    }


def _ds_entry(sig: dict[str, Any]) -> dict[str, Any]:
    return {"seq": 5, "at": "2026-09-28T00:01:00Z", "node_id": "", "signals": sig, "mode": "SHADOW"}


def _write_run(runs_dir: Path, receipt: dict[str, Any]) -> Path:
    """Write a receipt to a run directory and return the run dir."""
    run_dir = runs_dir / receipt["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return run_dir


# ---------------------------------------------------------------------------
# Tests: build_record
# ---------------------------------------------------------------------------


class TestBuildRecord:
    def test_complete_solo_no_signals(self) -> None:
        receipt = _make_receipt(topology="SOLO", outcome="COMPLETE")
        record, reason = build_record("run-001", receipt)
        assert record is not None
        assert reason is None
        assert record["task_id"] == "run-001"
        assert record["task_class"] == "bounded_implementation"
        assert record["frontier_needed"] is False
        assert record["planner_was_frontier"] is False
        assert record["verified"] is True
        assert record["first_pass"] is True
        assert record["retries"] == 0
        assert record["escalations"] == 0
        assert record["frontier_worthy"] is None
        assert record["confidence"] is None
        assert record["security_sensitive"] is None
        assert record["signal_latency_ms"] is None

    def test_complete_frontier_with_signals(self) -> None:
        sig = _make_signal_set(frontier_worthy=0.9, security_sensitive=0.8, confidence=0.95)
        receipt = _make_receipt(
            topology="WORKER_CRITIC", outcome="COMPLETE", decision_signals=[_ds_entry(sig)]
        )
        record, reason = build_record("run-002", receipt)
        assert record is not None
        assert reason is None
        assert record["frontier_needed"] is True
        assert record["planner_was_frontier"] is True
        assert record["task_class"] == "multi_file_implementation"
        assert record["frontier_worthy"] == 0.9
        assert record["security_sensitive"] == 0.8
        assert record["confidence"] == 0.95
        assert record["signal_latency_ms"] == 120
        assert record["security_relevant"] is True

    def test_parallel_topology(self) -> None:
        receipt = _make_receipt(topology="PARALLEL_WORK_UNITS", outcome="COMPLETE")
        record, _ = build_record("run-003", receipt)
        assert record is not None
        assert record["task_class"] == "decomposable_parallel"
        assert record["planner_was_frontier"] is True
        assert record["frontier_needed"] is True

    def test_retries_counted(self) -> None:
        nodes = [
            {
                "node_id": "impl-1",
                "kind": "implement",
                "final_state": "VALIDATED",
                "attempts": [_attempt(1, "a", "failure"), _attempt(2, "b", "success")],
                "validated_by": [{"command": "pytest", "exit_code": 0, "seq": 10}],
            }
        ]
        receipt = _make_receipt(nodes=nodes, outcome="COMPLETE")
        record, _ = build_record("run-retry", receipt)
        assert record is not None
        assert record["retries"] == 1
        assert record["first_pass"] is False

    def test_escalations_counted(self) -> None:
        reassignments = [
            {"seq": 3, "at": "2026-09-28T00:02:00Z", "node_id": "impl-1"},
            {"seq": 6, "at": "2026-09-28T00:03:00Z", "node_id": "impl-1"},
        ]
        receipt = _make_receipt(reassignments=reassignments)
        record, _ = build_record("run-esc", receipt)
        assert record is not None
        assert record["escalations"] == 2

    def test_blocked_run_not_verified(self) -> None:
        nodes = [
            {
                "node_id": "impl-1",
                "kind": "implement",
                "final_state": "TERMINAL_FAILURE",
                "attempts": [_attempt(1, "a", "failure")],
                "validated_by": [],
            }
        ]
        receipt = _make_receipt(nodes=nodes, outcome="BLOCKED")
        record, _ = build_record("run-fail", receipt)
        assert record is not None
        assert record["verified"] is False
        assert record["first_pass"] is False
        assert record["time_to_green_s"] is None

    def test_time_to_green(self) -> None:
        receipt = _make_receipt(
            started_at="2026-09-28T00:00:00Z",
            finished_at="2026-09-28T00:10:00Z",
            outcome="COMPLETE",
        )
        record, _ = build_record("run-time", receipt)
        assert record is not None
        assert record["time_to_green_s"] == 600.0

    def test_missing_nodes_skipped(self) -> None:
        receipt = _make_receipt()
        receipt["nodes"] = []
        record, reason = build_record("run-empty", receipt)
        assert record is None
        assert reason is not None

    def test_missing_topology_skipped(self) -> None:
        receipt = _make_receipt()
        receipt["topology"] = ""
        record, reason = build_record("run-notop", receipt)
        assert record is None
        assert reason is not None

    def test_role_emitted_as_optional(self) -> None:
        receipt = _make_receipt()
        record, _ = build_record("run-role", receipt)
        assert record is not None
        assert record.get("role") == "implementation_worker"

    def test_role_review_node(self) -> None:
        nodes = [
            {
                "node_id": "review-1",
                "kind": "review",
                "final_state": "VALIDATED",
                "attempts": [_attempt(1, "a", "success")],
                "validated_by": [],
            }
        ]
        receipt = _make_receipt(nodes=nodes, outcome="COMPLETE")
        record, _ = build_record("run-rev", receipt)
        assert record is not None
        assert record.get("role") == "independent_reviewer"

    def test_role_research_node(self) -> None:
        nodes = [
            {
                "node_id": "research-1",
                "kind": "research",
                "final_state": "VALIDATED",
                "attempts": [_attempt(1, "a", "success")],
                "validated_by": [],
            }
        ]
        receipt = _make_receipt(nodes=nodes, outcome="COMPLETE")
        record, _ = build_record("run-res", receipt)
        assert record is not None
        assert record.get("role") == "research_test_worker"

    def test_frontier_needed_false_for_incomplete_solo(self) -> None:
        """SOLO run that failed: we don't guess frontier was needed."""
        nodes = [
            {
                "node_id": "impl-1",
                "kind": "implement",
                "final_state": "TERMINAL_FAILURE",
                "attempts": [_attempt(1, "a", "failure")],
                "validated_by": [],
            }
        ]
        receipt = _make_receipt(nodes=nodes, topology="SOLO", outcome="BLOCKED")
        record, _ = build_record("run-solo-fail", receipt)
        assert record is not None
        assert record["frontier_needed"] is False


# ---------------------------------------------------------------------------
# Tests: process_runs_dir + JSONL output
# ---------------------------------------------------------------------------


class TestProcessRunsDir:
    def test_produces_valid_jsonl(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_run(runs, _make_receipt(run_id="run-a"))
        _write_run(runs, _make_receipt(run_id="run-b", topology="WORKER_CRITIC"))

        records, skips = process_runs_dir(runs)
        assert len(records) == 2
        assert not skips

    def test_skips_missing_receipt(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        (runs / "run-no-receipt").mkdir()

        records, skips = process_runs_dir(runs)
        assert len(records) == 0
        assert skips.get("missing receipt.json") == 1

    def test_skips_corrupt_receipt(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        bad = runs / "run-bad"
        bad.mkdir()
        (bad / "receipt.json").write_text("not json!", encoding="utf-8")

        records, skips = process_runs_dir(runs)
        assert len(records) == 0
        assert any("unreadable" in k for k in skips)

    def test_end_to_end_load_records(self, tmp_path: Path) -> None:
        """JSONL from builder is consumable by calibration.load_records()."""
        runs = tmp_path / "runs"
        runs.mkdir()
        sig = _make_signal_set()
        _write_run(
            runs,
            _make_receipt(
                run_id="run-e2e",
                topology="WORKER_CRITIC",
                outcome="COMPLETE",
                decision_signals=[_ds_entry(sig)],
            ),
        )

        records, _ = process_runs_dir(runs)
        assert len(records) == 1

        # Write JSONL and load via calibration.load_records
        jsonl_path = tmp_path / "calibration.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

        loaded = load_records(jsonl_path)
        assert len(loaded) == 1
        cr = loaded[0]
        assert isinstance(cr, CalibrationRecord)
        assert cr.task_id == "run-e2e"
        assert cr.frontier_worthy == 0.7
        assert cr.confidence == 0.85
        assert cr.frontier_needed is True
        assert cr.verified is True
        assert cr.planner_was_frontier is True

    def test_end_to_end_cli(self, tmp_path: Path) -> None:
        """CLI --runs-dir produces valid JSONL on stdout."""
        import contextlib
        import io

        runs = tmp_path / "runs"
        runs.mkdir()
        _write_run(runs, _make_receipt(run_id="run-cli"))

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            exit_code = main(["--runs-dir", str(runs)])

        assert exit_code == 0
        lines = [line for line in stdout_buf.getvalue().strip().split("\n") if line.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["task_id"] == "run-cli"

        # stderr has summary
        assert "1 records" in stderr_buf.getvalue()

    def test_mixed_valid_and_invalid(self, tmp_path: Path) -> None:
        """Valid runs produce records; invalid runs are skipped."""
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_run(runs, _make_receipt(run_id="run-ok"))

        bad = runs / "run-broken"
        bad.mkdir()
        (bad / "receipt.json").write_text(json.dumps({"not": "a receipt"}), encoding="utf-8")

        records, skips = process_runs_dir(runs)
        assert len(records) == 1
        assert records[0]["task_id"] == "run-ok"
        assert sum(skips.values()) >= 1

    def test_security_relevant_from_signal(self, tmp_path: Path) -> None:
        """security_relevant derived from signals, not guessed."""
        runs = tmp_path / "runs"
        runs.mkdir()
        sig = _make_signal_set(security_sensitive=0.9)
        _write_run(runs, _make_receipt(run_id="run-sec", decision_signals=[_ds_entry(sig)]))

        records, _ = process_runs_dir(runs)
        assert records[0]["security_relevant"] is True

    def test_no_signals_security_relevant_false(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_run(runs, _make_receipt(run_id="run-nosec"))

        records, _ = process_runs_dir(runs)
        assert records[0]["security_relevant"] is False

    def test_role_field_tolerated_by_load_records(self, tmp_path: Path) -> None:
        """CalibrationRecord.from_dict ignores unknown fields like role."""
        runs = tmp_path / "runs"
        runs.mkdir()
        _write_run(runs, _make_receipt(run_id="run-role-compat"))

        records, _ = process_runs_dir(runs)
        assert "role" in records[0]

        jsonl_path = tmp_path / "calibration.jsonl"
        with jsonl_path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")

        # load_records should not crash even with the extra 'role' field
        loaded = load_records(jsonl_path)
        assert len(loaded) == 1
