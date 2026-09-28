"""Tests for SONA outcome records (BOD-90 / BOD-203).

Covers:
- build from live4_calibration receipt fixture (alpha success, beta 2 attempts + 1 reroute)
- synthetic cancelled and fail-closed runs → negative records
- unknown cost → None
- schema parity between dataclass and JSON schema
- receipt digest still verifies after sona-outcomes.jsonl sidecar is written
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from verdict.sona import SONA_FILE, SONAOutcomeRecord, build_sona_records, write_sona_outcomes

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "live4_calibration"
SCHEMA_PATH = Path(__file__).parents[1] / "verdict" / "schemas" / "sona-outcome.v1.json"


@pytest.fixture()
def live_receipt() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "receipt.json").read_text())


@pytest.fixture()
def sona_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


# ---------------------------------------------------------------- live fixture


class TestLiveReceipt:
    """Build SONA records from the live4_calibration receipt."""

    def test_record_count(self, live_receipt: dict[str, Any]) -> None:
        records = build_sona_records(live_receipt)
        assert len(records) == 2  # alpha + beta

    def test_alpha_record(self, live_receipt: dict[str, Any]) -> None:
        records = build_sona_records(live_receipt)
        alpha = next(r for r in records if r.node_id == "alpha")
        assert alpha.outcome == "accepted"
        assert alpha.role == "implement"
        assert alpha.chosen_models == ("kr/claude-haiku-4.5",)
        assert alpha.retries == 0
        assert alpha.reroutes == 0
        assert alpha.repairs == 0
        assert alpha.proof_pass is True
        assert alpha.input_tokens == 9393
        assert alpha.output_tokens == 44
        assert len(alpha.verified_commands) == 1

    def test_beta_record(self, live_receipt: dict[str, Any]) -> None:
        records = build_sona_records(live_receipt)
        beta = next(r for r in records if r.node_id == "beta")
        assert beta.outcome == "accepted"
        assert beta.role == "implement"
        assert beta.chosen_models == ("kr/claude-haiku-4.5", "kr/claude-sonnet-4")
        assert beta.retries == 1
        assert beta.reroutes == 1
        assert beta.repairs == 1
        assert beta.proof_pass is True
        assert beta.input_tokens == 9656 + 9302
        assert beta.output_tokens == 25 + 102
        assert beta.latency_seconds is not None
        assert beta.latency_seconds == pytest.approx(12.39 + 13.83)

    def test_beta_cost_zero_from_provider(self, live_receipt: dict[str, Any]) -> None:
        """Fixture reports cost_usd=0.0 from provider — accepted as real zero."""
        records = build_sona_records(live_receipt)
        beta = next(r for r in records if r.node_id == "beta")
        # Both attempts report cost_usd=0.0 — that is a real provider value
        assert beta.cost_usd == 0.0

    def test_run_id_propagated(self, live_receipt: dict[str, Any]) -> None:
        records = build_sona_records(live_receipt)
        for r in records:
            assert r.run_id == live_receipt["run_id"]


# ---------------------------------------------------------------- synthetic negative records


def _minimal_receipt(
    nodes: list[dict[str, Any]],
    *,
    reassignments: list[dict[str, Any]] | None = None,
    claimed_outcome: str = "",
) -> dict[str, Any]:
    return {
        "run_id": "test-run",
        "nodes": nodes,
        "reassignments": reassignments or [],
        "claimed_outcome": claimed_outcome,
    }


class TestNegativeRecords:
    """Cancelled, timed-out, and fail-closed nodes → negative records."""

    def test_cancelled_node(self) -> None:
        receipt = _minimal_receipt(
            [{"node_id": "n1", "kind": "implement", "final_state": "PLANNED", "attempts": []}]
        )
        records = build_sona_records(receipt)
        assert len(records) == 1
        assert records[0].outcome == "cancelled"
        assert records[0].proof_pass is None

    def test_timeout_node(self) -> None:
        receipt = _minimal_receipt(
            [
                {
                    "node_id": "n1",
                    "kind": "implement",
                    "final_state": "RUNNING",
                    "attempts": [
                        {
                            "attempt": 1,
                            "route_id": "m/a",
                            "outcome": "running",
                            "usage": {"input_tokens": 100, "output_tokens": 50},
                        }
                    ],
                }
            ]
        )
        records = build_sona_records(receipt)
        assert len(records) == 1
        assert records[0].outcome == "timeout"
        assert records[0].input_tokens == 100

    def test_fail_closed_node(self) -> None:
        receipt = _minimal_receipt(
            [
                {
                    "node_id": "n1",
                    "kind": "implement",
                    "final_state": "BLOCKED",
                    "attempts": [
                        {
                            "attempt": 1,
                            "route_id": "m/a",
                            "outcome": "failure",
                            "duration_seconds": 5.0,
                            "usage": {"input_tokens": 200, "output_tokens": 30},
                        }
                    ],
                }
            ]
        )
        records = build_sona_records(receipt)
        assert len(records) == 1
        assert records[0].outcome == "rejected"
        assert records[0].proof_pass is False
        assert records[0].latency_seconds == 5.0
        assert records[0].repairs == 1

    def test_rejected_node(self) -> None:
        receipt = _minimal_receipt(
            [
                {
                    "node_id": "n1",
                    "kind": "review",
                    "final_state": "REJECTED",
                    "attempts": [{"attempt": 1, "route_id": "m/a", "outcome": "failure"}],
                }
            ]
        )
        records = build_sona_records(receipt)
        assert records[0].outcome == "rejected"
        assert records[0].role == "review"


# ---------------------------------------------------------------- cost handling


class TestCostHandling:
    """cost_usd is None unless every attempt reported a numeric cost."""

    def test_missing_cost_returns_none(self) -> None:
        receipt = _minimal_receipt(
            [
                {
                    "node_id": "n1",
                    "kind": "implement",
                    "final_state": "VALIDATED",
                    "attempts": [
                        {
                            "attempt": 1,
                            "route_id": "m/a",
                            "outcome": "success",
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        }
                    ],
                }
            ]
        )
        records = build_sona_records(receipt)
        assert records[0].cost_usd is None

    def test_partial_cost_returns_none(self) -> None:
        receipt = _minimal_receipt(
            [
                {
                    "node_id": "n1",
                    "kind": "implement",
                    "final_state": "VALIDATED",
                    "attempts": [
                        {
                            "attempt": 1,
                            "route_id": "m/a",
                            "outcome": "failure",
                            "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
                        },
                        {
                            "attempt": 2,
                            "route_id": "m/b",
                            "outcome": "success",
                            "usage": {"input_tokens": 20, "output_tokens": 10},
                        },
                    ],
                }
            ]
        )
        records = build_sona_records(receipt)
        assert records[0].cost_usd is None

    def test_all_costs_present_returns_sum(self) -> None:
        receipt = _minimal_receipt(
            [
                {
                    "node_id": "n1",
                    "kind": "implement",
                    "final_state": "VALIDATED",
                    "attempts": [
                        {
                            "attempt": 1,
                            "route_id": "m/a",
                            "outcome": "failure",
                            "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
                        },
                        {
                            "attempt": 2,
                            "route_id": "m/b",
                            "outcome": "success",
                            "usage": {"input_tokens": 20, "output_tokens": 10, "cost_usd": 0.02},
                        },
                    ],
                }
            ]
        )
        records = build_sona_records(receipt)
        assert records[0].cost_usd == pytest.approx(0.03)

    def test_no_attempts_cost_none(self) -> None:
        receipt = _minimal_receipt(
            [{"node_id": "n1", "kind": "implement", "final_state": "PLANNED", "attempts": []}]
        )
        records = build_sona_records(receipt)
        assert records[0].cost_usd is None


# ---------------------------------------------------------------- schema parity


class TestSchemaParity:
    """JSON schema matches SONAOutcomeRecord dataclass fields."""

    def test_schema_valid(self, sona_schema: dict[str, Any]) -> None:
        Draft202012Validator.check_schema(sona_schema)

    def test_dataclass_fields_match_schema(self, sona_schema: dict[str, Any]) -> None:
        import dataclasses

        dc_fields = {f.name for f in dataclasses.fields(SONAOutcomeRecord)}
        schema_props = set(sona_schema["properties"].keys())
        assert dc_fields == schema_props, (
            f"mismatch: dataclass-only={dc_fields - schema_props}, "
            f"schema-only={schema_props - dc_fields}"
        )

    def test_record_validates_against_schema(
        self, live_receipt: dict[str, Any], sona_schema: dict[str, Any]
    ) -> None:
        validator = Draft202012Validator(sona_schema)
        records = build_sona_records(live_receipt)
        for record in records:
            errors = list(validator.iter_errors(record.to_dict()))
            assert not errors, f"{record.node_id}: {errors}"

    def test_negative_record_validates(self, sona_schema: dict[str, Any]) -> None:
        receipt = _minimal_receipt(
            [{"node_id": "n1", "kind": "implement", "final_state": "PLANNED", "attempts": []}]
        )
        records = build_sona_records(receipt)
        validator = Draft202012Validator(sona_schema)
        errors = list(validator.iter_errors(records[0].to_dict()))
        assert not errors, errors


# ---------------------------------------------------------------- receipt digest verification


class TestReceiptDigestIntegrity:
    """The receipt digest still verifies after the sidecar file is written."""

    def test_sidecar_does_not_corrupt_receipt(self, tmp_path: Path) -> None:
        from verdict.orchestration.contracts import WorkGraph, WorkNode
        from verdict.orchestration.receipt import EventLog, verify_run_receipt, write_run_receipt

        # Build a minimal run directory with events + graph
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        graph = WorkGraph(
            goal="test sona sidecar",
            nodes=(
                WorkNode(
                    "a",
                    "build a",
                    owned_files=("pkg/a.py",),
                    verification_command=("pytest",),
                    barrier="int",
                ),
            ),
        )
        (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
        log = EventLog(run_dir / "events.jsonl")
        log.emit("run_started", run_id="sidecar-test", goal="test sona sidecar")
        log.emit("dispatch", node_id="a", attempt=1, route_id="m/a", capacity_class="free")
        log.emit(
            "terminal",
            node_id="a",
            attempt=1,
            ok=True,
            duration_seconds=2.0,
            route_id="m/a",
            reported_model="m/a",
            usage={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        )
        log.emit("verify", node_id="a", ok=True, command=["pytest"], exit_code=0)
        log.emit("barrier", node_id="a", name="int", ok=True)
        log.emit("run_finished", outcome="COMPLETE", reason="done")

        # write_run_receipt now also writes sona-outcomes.jsonl
        receipt_path = write_run_receipt(run_dir)
        assert receipt_path.exists()

        # Verify the receipt is still valid
        problems = verify_run_receipt(run_dir)
        assert problems == [], f"receipt verification failed: {problems}"

        # Confirm sona file was written
        sona_path = run_dir / SONA_FILE
        assert sona_path.exists()
        lines = sona_path.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["run_id"] == "sidecar-test"
        assert record["node_id"] == "a"
        assert record["outcome"] == "accepted"
        assert record["cost_usd"] == 0.01

    def test_sona_records_from_written_file(self, live_receipt: dict[str, Any]) -> None:
        """Records read back from the JSONL file match the in-memory build."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            write_sona_outcomes(run_dir, live_receipt)

            expected = build_sona_records(live_receipt)
            lines = (run_dir / SONA_FILE).read_text().strip().split("\n")
            assert len(lines) == len(expected)
            for line, exp in zip(lines, expected, strict=True):
                got = json.loads(line)
                assert got["node_id"] == exp.node_id
                assert got["outcome"] == exp.outcome
