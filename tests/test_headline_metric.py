"""Tests for the M0 headline metric report (issue #1213).

Covers:
- extend_run_metrics: per-role splits, frontier tokens, first-attempt pass rate.
- Fixture exclusion: fixture runs (test-* run_id) are never counted in the live aggregate.
- aggregate_extended_metrics: empty and multi-run cases.
- compute_headline_report: via monkeypatched load_receipts_since.
- _action_headline_metric: via run_action.
- format_headline_report: smoke test.
- Issue #1315: first-attempt pass counts only validated work, ``verdict metrics
  <dir>`` reads ``<dir>``, a missing runs directory is an empty report, and the
  receipt bytes that are verified are the bytes that are aggregated.

All receipts used here are in-memory or from tests/fixtures/metrics/; no live
provider calls are made.  Fixture runs are labelled as fixtures per AGENTS.md.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_runtime import NOW, Executor, Reviewer, Selector, node
from tests.test_orch_runtime import repo as repo
from verdict.orchestration.contracts import NodeKind, WorkGraph
from verdict.orchestration.receipt import (
    EventLog,
    build_run_receipt,
    compute_run_metrics,
    extend_run_metrics,
)
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy, subprocess_runner

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "metrics"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_receipt(
    run_id: str = "run-001",
    outcome: str = "COMPLETE",
    nodes: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
    planner_attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal in-memory receipt for testing extend_run_metrics."""
    if nodes is None:
        nodes = []
    base: dict[str, Any] = {
        "run_id": run_id,
        "schema": "verdict.run-receipt/v1",
        "nodes": nodes,
        "review": review or {"status": "PASS", "reviewer": "ocr", "route_id": "", "blocking": 0},
        "integration": {"ok": True, "barriers": [], "missing": []},
        "planner_attempts": planner_attempts or [],
        "claimed_outcome": outcome,
    }
    return base


def _attempt(
    route_id: str = "free/model-a",
    capacity_class: str = "free",
    outcome: str = "success",
    input_tokens: int | None = 1000,
    output_tokens: int | None = 100,
    cost_usd: float | None = 0.0,
) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    if cost_usd is not None:
        usage["cost_usd"] = cost_usd
    return {
        "attempt": 1,
        "route_id": route_id,
        "capacity_class": capacity_class,
        "outcome": outcome,
        "route_identity": "match",
        "usage": usage,
    }


def _node(
    node_id: str = "n1",
    kind: str = "implement",
    final_state: str = "VALIDATED",
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": kind,
        "final_state": final_state,
        "attempts": attempts or [_attempt()],
        "validated_by": [{"command": "pytest", "exit_code": 0, "seq": 1}],
    }


def _build_live_run(run_dir: Path, run_id: str) -> None:
    """Copy the complete-run fixture into *run_dir*, patching run_id throughout."""
    import hashlib
    import json as _json
    import shutil

    shutil.copytree(FIXTURES_DIR / "complete-run", run_dir)
    receipt_path = run_dir / "receipt.json"
    receipt = _json.loads(receipt_path.read_text())
    receipt["run_id"] = run_id
    events_path = run_dir / "events.jsonl"
    events_text = events_path.read_text()
    events_text = events_text.replace('"test-complete"', f'"{run_id}"')
    events_path.write_text(events_text)
    digest = "sha256:" + hashlib.sha256(events_path.read_bytes()).hexdigest()
    receipt["events_digest"] = digest
    receipt_path.write_text(_json.dumps(receipt))


# ---------------------------------------------------------------------------
# extend_run_metrics — per-role splits
# ---------------------------------------------------------------------------


class TestExtendRunMetrics:
    def test_single_implement_node(self) -> None:
        receipt = _make_receipt(
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free")])]
        )
        m = extend_run_metrics(receipt)
        assert m["attempts_by_role"]["implement"] == 1
        assert m["attempts_by_role"]["research"] == 0
        assert m["attempts_by_role"]["review"] == 0
        assert m["attempts_by_role"]["integrate"] == 0

    def test_accepted_nodes_by_role_complete(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node("n1", "implement"),
                _node("n2", "research", final_state="VALIDATED"),
                _node("n3", "integrate"),
            ]
        )
        m = extend_run_metrics(receipt)
        assert m["accepted_nodes_by_role"]["implement"] == 1
        assert m["accepted_nodes_by_role"]["integrate"] == 1
        # research is never "accepted" in the gated sense
        assert m["accepted_nodes_by_role"]["research"] == 0

    def test_accepted_nodes_zero_on_non_complete(self) -> None:
        # completion_verdict recomputes outcome from evidence, so we need a
        # genuinely BLOCKED run: a node with TERMINAL_FAILURE (not VALIDATED).
        receipt = _make_receipt(nodes=[_node("n1", "implement", final_state="TERMINAL_FAILURE")])
        m = extend_run_metrics(receipt)
        assert m["accepted_nodes_by_role"]["implement"] == 0
        assert m["accepted_tasks"] == 0

    def test_attempts_by_cost_and_role(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node("n1", "implement", attempts=[_attempt("sub/x", "subscription")]),
                _node("n2", "research", attempts=[_attempt("free/y", "free")]),
            ]
        )
        m = extend_run_metrics(receipt)
        assert m["attempts_by_cost_and_role"]["implement"]["subscription"] == 1
        assert m["attempts_by_cost_and_role"]["research"]["free"] == 1
        assert m["attempts_by_cost_and_role"]["implement"]["free"] == 0

    def test_local_route_excluded_from_role_counts(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[{**_attempt("local/fake", "free"), "route_id": "local/fake"}],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        assert m["attempts_by_role"]["implement"] == 0

    def test_mechanical_attempt_excluded(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[
                        {**_attempt("sub/x", "subscription"), "route_identity": "mechanical"}
                    ],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        assert m["attempts_by_role"]["implement"] == 0


# ---------------------------------------------------------------------------
# extend_run_metrics — frontier tokens
# ---------------------------------------------------------------------------


class TestExtendRunMetricsFrontierTokens:
    def test_subscription_tokens_counted(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[
                        _attempt(
                            "sub/x",
                            "subscription",
                            input_tokens=5000,
                            output_tokens=500,
                            cost_usd=0.1,
                        )
                    ],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        ft = m["frontier_tokens"]
        assert ft["input"] == 5000
        assert ft["output"] == 500
        assert ft["total"] == 5500

    def test_metered_tokens_counted(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[
                        _attempt(
                            "met/x", "metered", input_tokens=2000, output_tokens=200, cost_usd=0.05
                        )
                    ],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        assert m["frontier_tokens"]["total"] == 2200

    def test_free_tokens_not_counted_as_frontier(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[_attempt("free/x", "free", input_tokens=1000, output_tokens=100)],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        ft = m["frontier_tokens"]
        assert ft["input"] == 0
        assert ft["output"] == 0
        assert ft["total"] == 0

    def test_missing_tokens_makes_total_none(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[
                        _attempt("sub/x", "subscription", input_tokens=None, output_tokens=None)
                    ],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        assert m["frontier_tokens"]["total"] is None
        assert m["frontier_tokens"]["per_accepted_task"] is None

    def test_per_accepted_task_computed(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[
                        _attempt("sub/x", "subscription", input_tokens=10000, output_tokens=1000)
                    ],
                ),
                _node(
                    "n2",
                    "implement",
                    attempts=[
                        _attempt("sub/y", "subscription", input_tokens=10000, output_tokens=1000)
                    ],
                ),
            ]
        )
        m = extend_run_metrics(receipt)
        # 2 accepted tasks, 22000 total frontier tokens
        assert m["frontier_tokens"]["total"] == 22000
        assert m["frontier_tokens"]["per_accepted_task"] == pytest.approx(11000.0)

    def test_planner_attempts_count(self) -> None:
        receipt = _make_receipt(
            nodes=[], planner_attempts=[{"seq": 1, "at": "2026-11-23T10:00:00Z"}]
        )
        m = extend_run_metrics(receipt)
        assert m["planner_attempts_count"] == 1

    def test_planner_tokens_note_present(self) -> None:
        m = extend_run_metrics(_make_receipt(nodes=[]))
        assert "planner_tokens_note" in m
        assert "not recorded" in m["planner_tokens_note"]

    def test_reviewer_token_budget_used(self) -> None:
        receipt = _make_receipt(
            review={
                "status": "PASS",
                "reviewer": "ocr",
                "route_id": "",
                "blocking": 0,
                "token_budget_used": 4096,
            },
            nodes=[],
        )
        m = extend_run_metrics(receipt)
        assert m["reviewer_token_budget_used"] == 4096

    def test_reviewer_token_budget_missing(self) -> None:
        m = extend_run_metrics(_make_receipt(nodes=[]))
        assert m["reviewer_token_budget_used"] is None


# ---------------------------------------------------------------------------
# extend_run_metrics — first-attempt pass rate
# ---------------------------------------------------------------------------


class TestFirstAttemptPassRate:
    def test_all_pass_on_first_attempt(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node("n1", "implement", attempts=[_attempt("free/x", "free", outcome="success")]),
                _node(
                    "n2",
                    "implement",
                    attempts=[_attempt("sub/y", "subscription", outcome="success")],
                ),
            ]
        )
        m = extend_run_metrics(receipt)
        fpr = m["first_attempt_pass_rate"]
        assert fpr["by_role"]["implement"] == pytest.approx(1.0)

    def test_none_pass_on_first_attempt(self) -> None:
        # Node with TERMINAL_FAILURE makes completion_verdict return BLOCKED.
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    final_state="TERMINAL_FAILURE",
                    attempts=[_attempt("free/x", "free", outcome="failure")],
                )
            ]
        )
        m = extend_run_metrics(receipt)
        fpr = m["first_attempt_pass_rate"]
        assert fpr["by_role"]["implement"] == pytest.approx(0.0)

    def test_mixed_pass_rate(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[_attempt("sub/x", "subscription", outcome="success")],
                ),
                _node("n2", "research", attempts=[_attempt("free/y", "free", outcome="failure")]),
            ]
        )
        m = extend_run_metrics(receipt)
        fpr = m["first_attempt_pass_rate"]
        assert fpr["by_role"]["implement"] == pytest.approx(1.0)
        assert fpr["by_role"]["research"] == pytest.approx(0.0)

    def test_first_attempt_pass_rate_by_cost(self) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[_attempt("sub/x", "subscription", outcome="success")],
                ),
                _node(
                    "n2", "implement", attempts=[_attempt("met/y", "metered", outcome="failure")]
                ),
                _node("n3", "research", attempts=[_attempt("free/z", "free", outcome="success")]),
            ]
        )
        m = extend_run_metrics(receipt)
        fpr = m["first_attempt_pass_rate"]
        assert fpr["by_cost"]["subscription"] == pytest.approx(1.0)
        assert fpr["by_cost"]["metered"] == pytest.approx(0.0)
        assert fpr["by_cost"]["free"] == pytest.approx(1.0)

    def test_no_model_attempts_gives_none_rate(self) -> None:
        receipt = _make_receipt(nodes=[])
        m = extend_run_metrics(receipt)
        fpr = m["first_attempt_pass_rate"]
        # No nodes → no data → None for all roles
        for role, rate in fpr["by_role"].items():
            assert rate is None, f"{role} should be None"

    def test_research_duplication_note(self) -> None:
        m = extend_run_metrics(_make_receipt(nodes=[]))
        assert "research_duplication_note" in m
        assert "not tracked" in m["research_duplication_note"]


# ---------------------------------------------------------------------------
# extend_run_metrics embeds compute_run_metrics output
# ---------------------------------------------------------------------------


class TestExtendEmbedsCoreMetrics:
    def test_base_metrics_present(self) -> None:
        receipt_path = FIXTURES_DIR / "complete-run" / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        m = extend_run_metrics(receipt)
        # All keys from compute_run_metrics must be present.
        base = compute_run_metrics(receipt)
        for key in base:
            assert key in m, f"missing base key: {key}"

    def test_accepted_tasks_matches_base(self) -> None:
        receipt_path = FIXTURES_DIR / "complete-run" / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        m = extend_run_metrics(receipt)
        base = compute_run_metrics(receipt)
        assert m["accepted_tasks"] == base["accepted_tasks"]


# ---------------------------------------------------------------------------
# Fixture exclusion
# ---------------------------------------------------------------------------


class TestFixtureExclusion:
    def test_fixture_run_identified(self) -> None:
        from verdict.headline_metric import _is_fixture_run

        assert _is_fixture_run({"run_id": "test-complete"}) is True
        assert _is_fixture_run({"run_id": "run-20261123-abc"}) is False
        assert _is_fixture_run({}) is False

    def test_load_receipts_since_excludes_fixtures(self, tmp_path: Path) -> None:
        """Fixture runs in the runs_dir must not appear in the live aggregate."""
        import shutil

        from verdict.headline_metric import load_receipts_since

        # Copy a fixture run into a tmp runs_dir to simulate it being present.

        run_dir = tmp_path / "test-complete"
        shutil.copytree(FIXTURES_DIR / "complete-run", run_dir)
        # Rename to have a test- run_id already in the receipt.
        # The fixture already has run_id=test-complete, so it will be excluded.

        receipts, skipped_live, fixture_skipped = load_receipts_since(tmp_path)
        assert fixture_skipped == 1
        assert len(receipts) == 0
        assert skipped_live == 0

    def _build_live_run(self, run_dir: Path, run_id: str) -> None:
        _build_live_run(run_dir, run_id)

    def test_load_receipts_since_includes_live_run(self, tmp_path: Path) -> None:
        """A verifiable live run is returned with zero skips and zero fixture counts."""
        from verdict.headline_metric import load_receipts_since

        self._build_live_run(tmp_path / "run-live-001", "run-live-001")

        receipts, skipped_live, fixture_skipped = load_receipts_since(tmp_path)
        assert fixture_skipped == 0
        assert skipped_live == 0
        assert len(receipts) == 1

    def test_live_run_contributes_accepted_work(self, tmp_path: Path) -> None:
        """A live run returned by load_receipts_since contributes accepted_tasks > 0."""
        from verdict.headline_metric import compute_headline_report

        self._build_live_run(tmp_path / "run-live-002", "run-live-002")

        result = compute_headline_report(tmp_path)
        assert result["runs_processed"] == 1
        assert result["live_runs_skipped"] == 0
        assert result["fixture_runs_excluded"] == 0
        assert result["accepted_tasks"] > 0

    def test_repair_false_torn_tail_skipped_without_modifying_events(self, tmp_path: Path) -> None:
        """Torn tail is skipped and events.jsonl is never modified (repair=False)."""
        from verdict.headline_metric import load_receipts_since
        from verdict.orchestration.receipt import verify_run_receipt

        run_dir = tmp_path / "run-torn-001"
        self._build_live_run(run_dir, "run-torn-001")

        # Confirm the run is clean before adding the torn tail.
        assert verify_run_receipt(run_dir, repair=False) == []

        # Append an incomplete JSON line (torn tail — simulates mid-write crash).
        events_path = run_dir / "events.jsonl"
        with events_path.open("ab") as f:
            f.write(b'\n{"seq":999,"at":"2026-11-18T')  # intentionally incomplete

        events_bytes_before = events_path.read_bytes()

        receipts, skipped_live, fixture_skipped = load_receipts_since(tmp_path)

        # Run must be skipped — digest mismatch from torn tail.
        assert len(receipts) == 0
        assert skipped_live == 1
        assert fixture_skipped == 0

        # events.jsonl must be byte-identical — the read-only report never modifies evidence.
        assert events_path.read_bytes() == events_bytes_before

    def test_non_object_receipt_is_skipped(self, tmp_path: Path) -> None:
        """receipt.json containing null, [], or a scalar is skipped, not an AttributeError."""
        from verdict.headline_metric import load_receipts_since

        for content, label in [("null", "null"), ("[]", "array"), ('"hello"', "string")]:
            runs_dir = tmp_path / f"runs-bad-{label}"
            runs_dir.mkdir()
            run_dir = runs_dir / "run-bad"
            run_dir.mkdir()
            (run_dir / "receipt.json").write_text(content)

            receipts, skipped_live, fixture_skipped = load_receipts_since(runs_dir)
            assert len(receipts) == 0, f"expected no receipts for {content}"
            assert skipped_live == 1, f"expected skipped_live=1 for {content}"
            assert fixture_skipped == 0

    def test_aggregate_excludes_fixture_note(self, tmp_path: Path) -> None:
        """compute_headline_report reports fixture_runs_excluded correctly."""
        import shutil

        from verdict.headline_metric import compute_headline_report

        run_dir = tmp_path / "test-some-run"
        shutil.copytree(FIXTURES_DIR / "complete-run", run_dir)

        result = compute_headline_report(tmp_path)
        assert result["fixture_runs_excluded"] == 1
        assert result["runs_processed"] == 0


# ---------------------------------------------------------------------------
# aggregate_extended_metrics
# ---------------------------------------------------------------------------


class TestAggregateExtendedMetrics:
    def test_empty_returns_zero_aggregate(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        agg = aggregate_extended_metrics([])
        assert agg["accepted_tasks"] == 0
        assert agg["runs_processed"] == 0
        assert agg["paid_model_share"] == 0.0

    def test_paid_share_computed(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=0.1)]
                ),
                _node("n2", "research", attempts=[_attempt("free/y", "free", cost_usd=0.0)]),
            ]
        )
        agg = aggregate_extended_metrics([(Path("/tmp/fake"), receipt)])
        # 1 subscription, 1 free → paid_share = 0.5
        assert agg["paid_model_share"] == pytest.approx(0.5)
        assert agg["subscription_share"] == pytest.approx(0.5)

    def test_weighted_first_pass_rate_across_unequal_runs(self) -> None:
        """Aggregate weights each eligible node, not each run (finding #2 fix)."""
        from verdict.headline_metric import aggregate_extended_metrics

        # Run A: 1 eligible node, first attempt succeeded → {success:1, eligible:1}
        receipt_a = _make_receipt(
            nodes=[
                _node("n1", "implement", attempts=[_attempt("free/x", "free", outcome="success")])
            ]
        )
        # Run B: 99 eligible nodes, all first attempts failed → {success:0, eligible:99}
        receipt_b = _make_receipt(
            nodes=[
                _node(
                    f"n{i}",
                    "implement",
                    final_state="TERMINAL_FAILURE",
                    attempts=[_attempt("free/x", "free", outcome="failure")],
                )
                for i in range(99)
            ]
        )
        agg = aggregate_extended_metrics([(Path("/tmp/a"), receipt_a), (Path("/tmp/b"), receipt_b)])
        rate = agg["first_attempt_pass_rate"]["by_role"].get("implement")
        assert rate is not None
        # Correct weighted answer: 1 success / 100 eligible = 0.01
        # Wrong unweighted-mean answer: (1.0 + 0.0) / 2 = 0.5
        assert rate == pytest.approx(1 / 100)

    def test_headline_accepted_per_paid_share(self) -> None:
        import verdict.headline_metric as hm
        from verdict.headline_metric import compute_headline_report

        # Monkeypatch load_receipts_since to avoid filesystem access.

        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=0.1)]
                ),
                _node("n2", "implement", attempts=[_attempt("met/y", "metered", cost_usd=0.1)]),
            ]
        )
        # 2 accepted tasks, 2 paid of 2 total → paid_share = 1.0
        # headline = 2 / 1.0 = 2.0

        original = hm.load_receipts_since

        def fake_load(runs_dir: Path, since: Any = None) -> tuple:
            return [(runs_dir / "fake", receipt)], 0, 0

        hm.load_receipts_since = fake_load  # type: ignore[assignment]
        try:
            result = compute_headline_report(Path("/tmp/fake-runs"))
            assert result["headline_accepted_per_paid_share"] == pytest.approx(2.0)
            # Note is null when the value is available.
            assert result["headline_accepted_per_paid_share_note"] is None
        finally:
            hm.load_receipts_since = original

    def test_headline_null_when_no_paid_attempts(self) -> None:
        """When all attempts are free, headline is null (never a string sentinel)."""
        import verdict.headline_metric as hm
        from verdict.headline_metric import compute_headline_report

        receipt = _make_receipt(
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)])]
        )
        original = hm.load_receipts_since

        def fake_load(runs_dir: Path, since: Any = None) -> tuple:
            return [(runs_dir / "fake", receipt)], 0, 0

        hm.load_receipts_since = fake_load  # type: ignore[assignment]
        try:
            result = compute_headline_report(Path("/tmp/fake-runs"))
            # JSON-serialisable null, not a string sentinel.
            assert result["headline_accepted_per_paid_share"] is None
            # Companion note explains why.
            assert isinstance(result["headline_accepted_per_paid_share_note"], str)
            assert len(result["headline_accepted_per_paid_share_note"]) > 0
        finally:
            hm.load_receipts_since = original

    def test_headline_null_on_empty_runs(self) -> None:
        """With no runs at all, headline is null and note is set."""
        from verdict.headline_metric import compute_headline_report

        result = compute_headline_report(Path("/tmp/nonexistent-verdict-headline-runs"))
        assert result["headline_accepted_per_paid_share"] is None
        assert isinstance(result["headline_accepted_per_paid_share_note"], str)

    def test_no_division_by_zero(self) -> None:
        """paid_model_share of 0.0 never raises ZeroDivisionError."""
        from verdict.headline_metric import compute_headline_report

        # Empty runs dir → paid_share = 0.0 → must not raise.
        result = compute_headline_report(Path("/tmp/nonexistent-verdict-no-div-runs"))
        assert result["paid_model_share"] == 0.0
        assert result["headline_accepted_per_paid_share"] is None

    def test_headline_json_serialisable(self) -> None:
        """The returned dict is fully JSON-serialisable (null, not string sentinels)."""
        import json as json_mod

        from verdict.headline_metric import compute_headline_report

        result = compute_headline_report(Path("/tmp/nonexistent-verdict-json-runs"))
        serialised = json_mod.dumps(result)
        parsed = json_mod.loads(serialised)
        # headline is JSON null
        assert parsed["headline_accepted_per_paid_share"] is None


# ---------------------------------------------------------------------------
# format_headline_report
# ---------------------------------------------------------------------------


class TestFormatHeadlineReport:
    def test_no_runs_message(self) -> None:
        from verdict.headline_metric import compute_headline_report, format_headline_report

        result = compute_headline_report(Path("/tmp/nonexistent-runs-dir"))
        text = format_headline_report(result)
        assert "Accepted tasks:   0" in text

    def test_includes_research_duplication_note(self) -> None:
        from verdict.headline_metric import format_headline_report

        data = {
            "accepted_tasks": 0,
            "dollars": None,
            "has_unknown_cost": False,
            "tasks_per_dollar": None,
            "tasks_per_dollar_note": "no runs in selected period",
            "free_share": 0.0,
            "subscription_share": 0.0,
            "metered_share": 0.0,
            "unknown_capacity_share": 0.0,
            "paid_model_share": 0.0,
            "total_model_attempts": 0,
            "free_attempts": 0,
            "subscription_attempts": 0,
            "metered_attempts": 0,
            "unknown_capacity_attempts": 0,
            "attempts_by_role": {},
            "accepted_nodes_by_role": {},
            "frontier_tokens": {
                "input": None,
                "output": None,
                "total": None,
                "per_accepted_task": None,
            },
            "first_attempt_pass_rate": {"by_role": {}, "by_cost": {}},
            "runs_processed": 0,
            "per_run": [],
            "fixture_runs_excluded": 0,
            "live_runs_skipped": 0,
            "since": None,
            "headline_accepted_per_paid_share": None,
            "headline_accepted_per_paid_share_note": "no paid model attempts in selected period",
        }
        text = format_headline_report(data)
        assert "planner/reviewer individual call tokens" in text
        assert "not tracked" in text


# ---------------------------------------------------------------------------
# action — headline.metric
# ---------------------------------------------------------------------------


class TestActionHeadlineMetric:
    def test_action_runs_against_fixture_dir(self) -> None:
        from verdict.actions.registry import run_action

        # The fixture dir contains only test-* runs → all excluded.
        result = run_action("headline.metric", {"runs_dir": str(FIXTURES_DIR)})
        assert result.ok
        assert result.data["fixture_runs_excluded"] >= 4  # 4 fixture runs in metrics/
        assert result.data["runs_processed"] == 0

    def test_action_invalid_since_date(self) -> None:
        from verdict.actions.registry import run_action

        result = run_action(
            "headline.metric", {"runs_dir": str(FIXTURES_DIR), "since": "not-a-date"}
        )
        assert not result.ok
        assert "invalid" in result.data.get("error", "").lower()


# ---------------------------------------------------------------------------
# Missing numbers are JSON null with a reason, never a string sentinel
# ---------------------------------------------------------------------------

_SENTINEL_PREFIXES = ("undefined", "n/a", "unknown", "none", "null")


def _string_sentinels(value: Any, path: str = "$") -> list[str]:
    """Return paths of string values that stand in for a missing number.

    Only strings that *start with* a sentinel word count; legitimate prose in a
    ``*_note`` field is allowed because it explains a ``null`` and is checked
    separately.
    """
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).endswith("_note"):
                continue
            found.extend(_string_sentinels(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_string_sentinels(item, f"{path}[{index}]"))
    elif isinstance(value, str) and value.strip().lower().startswith(_SENTINEL_PREFIXES):
        found.append(f"{path}={value!r}")
    return found


class TestMissingNumbersAreNull:
    def test_empty_input_tasks_per_dollar_is_null_with_reason(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        agg = aggregate_extended_metrics([])
        assert agg["tasks_per_dollar"] is None
        assert isinstance(agg["tasks_per_dollar_note"], str)
        assert agg["tasks_per_dollar_note"]

    def test_unknown_cost_tasks_per_dollar_is_null_with_reason(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=None)]
                )
            ]
        )
        agg = aggregate_extended_metrics([(Path("/tmp/fake"), receipt)])
        assert agg["has_unknown_cost"] is True
        assert agg["dollars"] is None
        assert agg["tasks_per_dollar"] is None
        assert "unknown cost" in agg["tasks_per_dollar_note"]

    def test_zero_recorded_spend_tasks_per_dollar_is_null_not_a_division(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        receipt = _make_receipt(
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)])]
        )
        agg = aggregate_extended_metrics([(Path("/tmp/fake"), receipt)])
        assert agg["accepted_tasks"] > 0
        assert agg["dollars"] == 0.0
        assert agg["tasks_per_dollar"] is None
        assert "no spend" in agg["tasks_per_dollar_note"]

    def test_known_spend_tasks_per_dollar_is_float_and_note_is_null(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        receipt = _make_receipt(
            nodes=[
                _node("n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=0.5)])
            ]
        )
        agg = aggregate_extended_metrics([(Path("/tmp/fake"), receipt)])
        assert agg["tasks_per_dollar"] == pytest.approx(agg["accepted_tasks"] / 0.5)
        assert agg["tasks_per_dollar_note"] is None

    def test_per_run_tasks_per_dollar_is_null_not_a_string(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        free_run = _make_receipt(
            run_id="run-free",
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)])],
        )
        paid_run = _make_receipt(
            run_id="run-paid",
            nodes=[
                _node("n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=0.5)])
            ],
        )
        agg = aggregate_extended_metrics(
            [(Path("/tmp/free"), free_run), (Path("/tmp/paid"), paid_run)]
        )
        by_id = {row["run_id"]: row for row in agg["per_run"]}
        assert by_id["run-free"]["tasks_per_dollar"] is None
        assert by_id["run-free"]["tasks_per_dollar_note"]
        assert isinstance(by_id["run-paid"]["tasks_per_dollar"], float)
        assert by_id["run-paid"]["tasks_per_dollar_note"] is None

    def test_no_string_sentinel_anywhere_in_report_json(self, tmp_path: Path) -> None:
        """Empty, free-only and unknown-cost reports carry no string sentinel."""
        import json as json_mod

        import verdict.headline_metric as hm
        from verdict.headline_metric import compute_headline_report

        cases = {
            "empty": [],
            "free_only": [
                _make_receipt(
                    run_id="run-free",
                    nodes=[
                        _node(
                            "n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)]
                        )
                    ],
                )
            ],
            "unknown_cost": [
                _make_receipt(
                    run_id="run-unknown",
                    nodes=[
                        _node(
                            "n1",
                            "implement",
                            attempts=[_attempt("sub/x", "subscription", cost_usd=None)],
                        )
                    ],
                )
            ],
        }
        original = hm.load_receipts_since
        try:
            for label, receipts in cases.items():
                hm.load_receipts_since = (  # type: ignore[assignment]
                    lambda runs_dir, since=None, _r=receipts: (
                        [(tmp_path / f"r{i}", r) for i, r in enumerate(_r)],
                        0,
                        0,
                    )
                )
                result = compute_headline_report(tmp_path)
                parsed = json_mod.loads(json_mod.dumps(result))
                assert _string_sentinels(parsed) == [], f"{label}: sentinel string in report"
                assert parsed["tasks_per_dollar"] is None or isinstance(
                    parsed["tasks_per_dollar"], float
                )
        finally:
            hm.load_receipts_since = original

    def test_formatter_renders_null_with_reason_not_undefined(self) -> None:
        from verdict.headline_metric import compute_headline_report, format_headline_report

        text = format_headline_report(
            compute_headline_report(Path("/tmp/nonexistent-verdict-format-null-runs"))
        )
        assert "undefined" not in text
        assert "Accepted/dollar:  null  (" in text

    def test_dollars_reason_distinguishes_no_runs_from_unknown_cost(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        empty = aggregate_extended_metrics([])
        assert empty["dollars"] is None
        assert empty["has_unknown_cost"] is False
        assert empty["dollars_note"] == "no runs in selected period"

        unknown = aggregate_extended_metrics(
            [
                (
                    Path("/tmp/fake"),
                    _make_receipt(
                        nodes=[
                            _node(
                                "n1",
                                "implement",
                                attempts=[_attempt("sub/x", "subscription", cost_usd=None)],
                            )
                        ]
                    ),
                )
            ]
        )
        assert unknown["dollars"] is None
        assert "unknown cost" in unknown["dollars_note"]

        known = aggregate_extended_metrics(
            [
                (
                    Path("/tmp/fake"),
                    _make_receipt(
                        nodes=[
                            _node(
                                "n1",
                                "implement",
                                attempts=[_attempt("sub/x", "subscription", cost_usd=0.5)],
                            )
                        ]
                    ),
                )
            ]
        )
        assert known["dollars"] == pytest.approx(0.5)
        assert known["dollars_note"] is None

    def test_formatter_empty_report_does_not_claim_unknown_cost(self) -> None:
        from verdict.headline_metric import compute_headline_report, format_headline_report

        text = format_headline_report(
            compute_headline_report(Path("/tmp/nonexistent-verdict-format-empty-runs"))
        )
        dollars_line = next(line for line in text.splitlines() if line.startswith("Dollars spent"))
        assert "no runs in selected period" in dollars_line
        assert "unknown cost" not in dollars_line

    def test_formatter_unknown_cost_report_says_unknown_cost(self, tmp_path: Path) -> None:
        import verdict.headline_metric as hm
        from verdict.headline_metric import compute_headline_report, format_headline_report

        receipt = _make_receipt(
            run_id="run-unknown",
            nodes=[
                _node(
                    "n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=None)]
                )
            ],
        )
        original = hm.load_receipts_since
        hm.load_receipts_since = lambda runs_dir, since=None: ([(tmp_path / "r", receipt)], 0, 0)  # type: ignore[assignment]
        try:
            text = format_headline_report(compute_headline_report(tmp_path))
        finally:
            hm.load_receipts_since = original
        dollars_line = next(line for line in text.splitlines() if line.startswith("Dollars spent"))
        assert "unknown cost" in dollars_line
        assert "no runs" not in dollars_line

    def test_per_run_rows_carry_dollars_note(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        def one(run_id: str, cap: str, route: str, cost: float | None) -> dict[str, Any]:
            return _make_receipt(
                run_id=run_id,
                nodes=[_node("n1", "implement", attempts=[_attempt(route, cap, cost_usd=cost)])],
            )

        agg = aggregate_extended_metrics(
            [
                (Path("/tmp/u"), one("run-unknown", "subscription", "sub/x", None)),
                (Path("/tmp/f"), one("run-free", "free", "free/x", 0.0)),
                (Path("/tmp/p"), one("run-paid", "subscription", "sub/y", 0.5)),
            ]
        )
        rows = {row["run_id"]: row for row in agg["per_run"]}
        assert rows["run-unknown"]["dollars"] is None
        assert "unknown cost" in rows["run-unknown"]["dollars_note"]
        assert rows["run-free"]["dollars"] == 0.0
        assert rows["run-free"]["dollars_note"] is None
        assert rows["run-paid"]["dollars"] == pytest.approx(0.5)
        assert rows["run-paid"]["dollars_note"] is None
        # Every per_run row that has a null headline figure has a reason for it.
        for row in rows.values():
            for key in ("dollars", "tasks_per_dollar"):
                if row[key] is None:
                    assert row[f"{key}_note"], f"{row['run_id']}: {key} is null without a reason"

    def test_headline_note_distinguishes_empty_report_from_free_only(self) -> None:
        import verdict.headline_metric as hm
        from verdict.headline_metric import compute_headline_report

        empty = compute_headline_report(Path("/tmp/nonexistent-verdict-headline-note-runs"))
        assert empty["runs_processed"] == 0
        assert empty["headline_accepted_per_paid_share"] is None
        assert empty["headline_accepted_per_paid_share_note"] == "no runs in selected period"

        free_only = _make_receipt(
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)])]
        )
        original = hm.load_receipts_since
        hm.load_receipts_since = lambda runs_dir, since=None: ([(Path("/tmp/r"), free_only)], 0, 0)  # type: ignore[assignment]
        try:
            result = compute_headline_report(Path("/tmp/unused"))
        finally:
            hm.load_receipts_since = original
        assert result["runs_processed"] == 1
        assert result["headline_accepted_per_paid_share"] is None
        assert (
            result["headline_accepted_per_paid_share_note"]
            == "no paid model attempts in selected period"
        )

    def test_empty_report_measured_figures_are_null_not_zero(self) -> None:
        """Nothing observed means null for measured figures; counts stay 0."""
        from verdict.headline_metric import aggregate_extended_metrics

        agg = aggregate_extended_metrics([])
        assert agg["frontier_tokens"] == {
            "input": None,
            "output": None,
            "total": None,
            "per_accepted_task": None,
        }
        # Same keys as a populated report, every value null (no data), not {}.
        assert set(agg["first_attempt_pass_rate"]["by_role"]) == {r.value for r in NodeKind}
        assert set(agg["first_attempt_pass_rate"]["by_cost"]) == {
            "free",
            "subscription",
            "metered",
            "unknown",
        }
        assert all(v is None for v in agg["first_attempt_pass_rate"]["by_role"].values())
        assert all(v is None for v in agg["first_attempt_pass_rate"]["by_cost"].values())
        # Counts over zero runs are genuinely 0.
        assert agg["accepted_tasks"] == 0
        assert agg["total_model_attempts"] == 0

    def test_observed_zero_frontier_usage_stays_zero(self) -> None:
        """A run that recorded only free attempts observed 0 frontier tokens."""
        from verdict.headline_metric import aggregate_extended_metrics

        receipt = _make_receipt(
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)])]
        )
        agg = aggregate_extended_metrics([(Path("/tmp/fake"), receipt)])
        assert agg["frontier_tokens"]["input"] == 0
        assert agg["frontier_tokens"]["output"] == 0
        assert agg["frontier_tokens"]["total"] == 0

    def test_formatter_empty_report_frontier_line_is_null_with_no_runs_reason(self) -> None:
        from verdict.headline_metric import compute_headline_report, format_headline_report

        text = format_headline_report(
            compute_headline_report(Path("/tmp/nonexistent-verdict-format-frontier-runs"))
        )
        line = next(ln for ln in text.splitlines() if ln.startswith("Frontier tokens"))
        assert line == "Frontier tokens: null  (no runs in selected period)"

    def test_formatter_unknown_token_report_keeps_token_reason(self, tmp_path: Path) -> None:
        import verdict.headline_metric as hm
        from verdict.headline_metric import compute_headline_report, format_headline_report

        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    attempts=[
                        _attempt("sub/x", "subscription", input_tokens=None, output_tokens=None)
                    ],
                )
            ]
        )
        original = hm.load_receipts_since
        hm.load_receipts_since = lambda runs_dir, since=None: ([(tmp_path / "r", receipt)], 0, 0)  # type: ignore[assignment]
        try:
            text = format_headline_report(compute_headline_report(tmp_path))
        finally:
            hm.load_receipts_since = original
        line = next(ln for ln in text.splitlines() if ln.startswith("Frontier tokens"))
        assert line == "Frontier tokens: null  (some attempts lack token data)"

    def test_per_run_rows_carry_the_headline_figure(self) -> None:
        from verdict.headline_metric import aggregate_extended_metrics

        paid = _make_receipt(
            run_id="run-paid",
            nodes=[
                _node("n1", "implement", attempts=[_attempt("sub/x", "subscription", cost_usd=0.5)])
            ],
        )
        free = _make_receipt(
            run_id="run-free",
            nodes=[_node("n1", "implement", attempts=[_attempt("free/x", "free", cost_usd=0.0)])],
        )
        rows = {
            row["run_id"]: row
            for row in aggregate_extended_metrics([(Path("/tmp/p"), paid), (Path("/tmp/f"), free)])[
                "per_run"
            ]
        }
        assert rows["run-paid"]["paid_model_share"] == pytest.approx(1.0)
        assert rows["run-paid"]["headline_accepted_per_paid_share"] == pytest.approx(
            rows["run-paid"]["accepted_tasks"] / 1.0
        )
        assert rows["run-paid"]["headline_accepted_per_paid_share_note"] is None
        assert rows["run-free"]["paid_model_share"] == 0.0
        assert rows["run-free"]["headline_accepted_per_paid_share"] is None
        assert (
            rows["run-free"]["headline_accepted_per_paid_share_note"]
            == "no paid model attempts in selected period"
        )


# ---------------------------------------------------------------------------
# #1315 finding 1: a first attempt counts as a pass only when it was validated
# ---------------------------------------------------------------------------


async def _real_run_receipt(
    repo: Path,
    tmp_path: Path,
    behaviour: dict[tuple[str, str], str],
    routes: list[str],
    *,
    explode_on_verify: bool = False,
) -> dict[str, Any]:
    """Drive the real DagRuntime (fake executor, real EventLog) and build its receipt."""
    graph = WorkGraph("g", (node("a"),))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))

    async def runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        # The node's verification command is the only one that greps for FAIL;
        # git and every other command go through the real runner.
        if explode_on_verify and any("grep -q FAIL" in part for part in argv):
            raise RuntimeError("verifier exploded before it could report")
        return await subprocess_runner(argv, cwd, timeout)

    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=graph,
        selector=Selector(routes),
        executor=Executor(behaviour, delay=0),
        classifier=FailureIntelligence(),
        events=EventLog(run_dir / "events.jsonl"),
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=RuntimePolicy(),
        now=lambda: NOW,
        runner=runner,
    )
    await runtime.run()
    return build_run_receipt(run_dir)


class TestFirstAttemptCountsOnlyValidatedWork:
    async def test_worker_success_then_failed_validation_is_not_a_pass(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Finding 1 as reported: the worker returns ok, validation then fails.

        Traced, not a defect: the runtime emits a ``failure`` event for the same
        attempt after ``_validate`` rejects it, and the receipt turns that into
        ``outcome == "failure"``.  This pins the whole chain (runtime events ->
        receipt attempt row -> metric) so the trace cannot silently stop holding.
        """
        receipt = await _real_run_receipt(
            repo, tmp_path, {("a", "cc/s"): "badcode"}, ["cc/s", "cx/g"]
        )
        node_row = receipt["nodes"][0]
        first = node_row["attempts"][0]
        assert first["route_id"] == "cc/s"
        assert first["failure_category"] == "verification_failed"
        assert first["outcome"] == "failure"
        # The node did get there in the end, so this is not a "never finished" case.
        assert node_row["final_state"] == "VALIDATED"
        counts = extend_run_metrics(receipt)["first_attempt_pass_counts"]
        assert counts["by_role"]["implement"] == {"success": 0, "eligible": 1}

    async def test_worker_success_then_validation_never_finishes_is_not_a_pass(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The worker returns ok and the validation step dies: no failure event exists.

        The runtime blocks the node ("driver crash"); the attempt row keeps
        ``outcome == "success"`` because nothing ever rejected it.  Work that was
        never validated is not accepted work.
        """
        receipt = await _real_run_receipt(
            repo, tmp_path, {}, ["cc/s", "cx/g"], explode_on_verify=True
        )
        node_row = receipt["nodes"][0]
        first = node_row["attempts"][0]
        # Precondition: the receipt really does carry a success-looking attempt on
        # a node that never validated (otherwise this test would prove nothing).
        assert first["outcome"] == "success"
        assert node_row["final_state"] != "VALIDATED"
        assert node_row["validated_by"] == []
        counts = extend_run_metrics(receipt)["first_attempt_pass_counts"]
        assert counts["by_role"]["implement"] == {"success": 0, "eligible": 1}

    @pytest.mark.parametrize("state", ["BLOCKED", "TERMINAL_SUCCESS", "REJECTED", "RUNNING"])
    def test_success_attempt_on_unvalidated_node_is_not_a_pass(self, state: str) -> None:
        receipt = _make_receipt(
            nodes=[
                _node(
                    "n1",
                    "implement",
                    final_state=state,
                    attempts=[_attempt("sub/x", "subscription")],
                )
            ]
        )
        counts = extend_run_metrics(receipt)["first_attempt_pass_counts"]
        assert counts["by_role"]["implement"] == {"success": 0, "eligible": 1}
        assert counts["by_cost"]["subscription"] == {"success": 0, "eligible": 1}

    def test_success_attempt_on_validated_node_still_passes(self) -> None:
        receipt = _make_receipt(
            nodes=[_node("n1", "implement", attempts=[_attempt("sub/x", "subscription")])]
        )
        counts = extend_run_metrics(receipt)["first_attempt_pass_counts"]
        assert counts["by_role"]["implement"] == {"success": 1, "eligible": 1}


# ---------------------------------------------------------------------------
# #1315 finding 2: `verdict metrics <path>` reads <path>
# ---------------------------------------------------------------------------


def _parse_metrics(*argv: str) -> argparse.Namespace:
    from verdict.orchestration.cli import add_parsers

    parser = argparse.ArgumentParser(prog="verdict")
    add_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(["metrics", *argv])


def _run_metrics_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    from verdict.orchestration.cli import _metrics

    capsys.readouterr()
    rc = _metrics(_parse_metrics(*argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _two_run_dirs(tmp_path: Path) -> tuple[Path, Path]:
    dir_a, dir_b = tmp_path / "runs-a", tmp_path / "runs-b"
    _build_live_run(dir_a / "run-a", "run-a")
    _build_live_run(dir_b / "run-b", "run-b")
    return dir_a, dir_b


def _reported_run_ids(stdout: str) -> list[str]:
    return sorted(row["run_id"] for row in json.loads(stdout)["per_run"])


class TestMetricsPathArgument:
    def test_positional_dir_is_read_not_runs_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dir_a, dir_b = _two_run_dirs(tmp_path)
        rc, out, _ = _run_metrics_cli(
            capsys, str(dir_a), "--since", "2026-01-01", "--runs-dir", str(dir_b), "--json"
        )
        assert rc == 0
        assert _reported_run_ids(out) == ["run-a"]
        # And the other way round, so the answer cannot come from a fixed default.
        rc, out, _ = _run_metrics_cli(
            capsys, str(dir_b), "--since", "2026-01-01", "--runs-dir", str(dir_a), "--json"
        )
        assert rc == 0
        assert _reported_run_ids(out) == ["run-b"]

    def test_positional_dir_is_read_with_daily(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dir_a, dir_b = _two_run_dirs(tmp_path)
        rc, out, _ = _run_metrics_cli(
            capsys, str(dir_a), "--daily", "--runs-dir", str(dir_b), "--json"
        )
        assert rc == 0
        assert _reported_run_ids(out) == ["run-a"]

    def test_positional_run_id_resolves_under_runs_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dir_a, _ = _two_run_dirs(tmp_path)
        _build_live_run(dir_a / "run-a2", "run-a2")
        rc, out, _ = _run_metrics_cli(
            capsys, "run-a2", "--since", "2026-01-01", "--runs-dir", str(dir_a), "--json"
        )
        assert rc == 0
        assert _reported_run_ids(out) == ["run-a2"]

    def test_no_positional_uses_runs_dir(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dir_a, dir_b = _two_run_dirs(tmp_path)
        rc, out, _ = _run_metrics_cli(capsys, "--daily", "--runs-dir", str(dir_b), "--json")
        assert rc == 0
        assert _reported_run_ids(out) == ["run-b"]
        assert dir_a.exists()

    def test_positional_path_that_is_not_a_directory_is_rejected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dir_a, _ = _two_run_dirs(tmp_path)
        missing = tmp_path / "typo-runs"
        rc, out, err = _run_metrics_cli(
            capsys, str(missing), "--since", "2026-01-01", "--runs-dir", str(dir_a), "--json"
        )
        assert rc == 2
        assert out == ""
        assert str(missing) in err
        assert "not a directory" in err


# ---------------------------------------------------------------------------
# #1315 finding 3: no runs directory is an empty report, exit 0
# ---------------------------------------------------------------------------


class TestFreshInstallIsAnEmptyReport:
    def test_action_missing_runs_dir_is_an_empty_report(self, tmp_path: Path) -> None:
        from verdict.actions.registry import run_action

        result = run_action("headline.metric", {"runs_dir": str(tmp_path / "no-such-runs")})
        assert result.ok
        assert result.exit_code == 0
        assert result.data["runs_processed"] == 0
        assert result.data["accepted_tasks"] == 0
        assert result.data["headline_accepted_per_paid_share"] is None
        assert result.data["headline_accepted_per_paid_share_note"] == "no runs in selected period"
        assert result.data["runs_dir_found"] is False

    def test_cli_daily_text_exits_zero_and_says_zero_runs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "fresh-install" / "runs"
        rc, out, err = _run_metrics_cli(capsys, "--daily", "--runs-dir", str(missing))
        assert rc == 0
        assert err == ""
        assert "Runs processed:   0" in out
        assert "directory not found" in out
        assert str(missing) in out

    def test_cli_since_json_exits_zero_with_null_headline(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "fresh-install" / "runs"
        rc, out, _ = _run_metrics_cli(
            capsys, "--since", "2026-01-01", "--runs-dir", str(missing), "--json"
        )
        assert rc == 0
        data = json.loads(out)
        assert data["runs_processed"] == 0
        assert data["headline_accepted_per_paid_share"] is None
        assert data["runs_dir"] == str(missing)
        assert data["runs_dir_found"] is False

    def test_existing_empty_runs_dir_is_found(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "runs"
        empty.mkdir()
        rc, out, _ = _run_metrics_cli(capsys, "--daily", "--runs-dir", str(empty), "--json")
        assert rc == 0
        data = json.loads(out)
        assert data["runs_dir_found"] is True
        assert data["runs_processed"] == 0
        assert "directory not found" not in json.dumps(data)

    def test_runs_dir_that_is_a_file_is_an_empty_report_not_a_crash(self, tmp_path: Path) -> None:
        from verdict.headline_metric import compute_headline_report

        not_a_dir = tmp_path / "runs.txt"
        not_a_dir.write_text("x")
        data = compute_headline_report(not_a_dir)
        assert data["runs_processed"] == 0
        assert data["runs_dir_found"] is False


# ---------------------------------------------------------------------------
# #1315 finding 4: the receipt bytes that are verified are the bytes aggregated
# ---------------------------------------------------------------------------


class TestVerifiedBytesAreAggregatedBytes:
    def test_receipt_changed_between_reads_is_never_aggregated_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disk holds a tampered receipt at the first read and a valid one afterwards.

        Two reads (aggregate, then verify) would aggregate the tampered dict
        while verifying the valid file.  One read means the tampered bytes are
        the ones verified, so the run is skipped.
        """
        from verdict.headline_metric import load_receipts_since
        from verdict.orchestration import receipt as receipt_mod

        run_dir = tmp_path / "run-race-001"
        _build_live_run(run_dir, "run-race-001")
        receipt_path = run_dir / "receipt.json"
        valid_bytes = receipt_path.read_bytes()
        tampered = json.loads(valid_bytes)
        tampered["goal"] = "a goal the events never recorded"
        receipt_path.write_text(json.dumps(tampered))

        reads: list[str] = []

        def _after_first_read() -> None:
            if not reads:
                receipt_path.write_bytes(valid_bytes)

        orig_read_text = Path.read_text
        orig_nofollow = receipt_mod._read_bytes_nofollow

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            text = orig_read_text(self, *args, **kwargs)
            if self == receipt_path:
                _after_first_read()
                reads.append("read_text")
            return text

        def read_nofollow(path: Path) -> bytes:
            raw = orig_nofollow(path)
            if Path(path) == receipt_path:
                _after_first_read()
                reads.append("nofollow")
            return raw

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(receipt_mod, "_read_bytes_nofollow", read_nofollow)

        receipts, skipped_live, fixture_skipped = load_receipts_since(tmp_path)

        assert reads, "the hook never saw a read of receipt.json: this test proves nothing"
        assert receipts == [], "a receipt that was never verified was aggregated"
        assert skipped_live == 1
        assert fixture_skipped == 0

    def test_receipt_json_is_read_once_per_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict.headline_metric import load_receipts_since
        from verdict.orchestration import receipt as receipt_mod

        run_dir = tmp_path / "run-once-001"
        _build_live_run(run_dir, "run-once-001")
        receipt_path = run_dir / "receipt.json"
        reads: list[str] = []
        orig_read_text = Path.read_text
        orig_nofollow = receipt_mod._read_bytes_nofollow

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == receipt_path:
                reads.append("read_text")
            return orig_read_text(self, *args, **kwargs)

        def read_nofollow(path: Path) -> bytes:
            if Path(path) == receipt_path:
                reads.append("nofollow")
            return orig_nofollow(path)

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(receipt_mod, "_read_bytes_nofollow", read_nofollow)

        receipts, skipped_live, _ = load_receipts_since(tmp_path)
        assert len(receipts) == 1
        assert skipped_live == 0
        assert len(reads) == 1, f"receipt.json was read {len(reads)} times: {reads}"

    def test_receipt_symlink_is_skipped_not_followed(self, tmp_path: Path) -> None:
        """Same O_NOFOLLOW rule as the runs API: a symlinked receipt is never trusted."""
        from verdict.headline_metric import load_receipts_since

        real = tmp_path / "elsewhere"
        _build_live_run(real, "run-link-001")
        runs = tmp_path / "runs"
        (runs / "run-link-001").mkdir(parents=True)
        for name in ("events.jsonl", "graph.json", "review.json"):
            (runs / "run-link-001" / name).write_bytes((real / name).read_bytes())
        (runs / "run-link-001" / "receipt.json").symlink_to(real / "receipt.json")

        receipts, skipped_live, _ = load_receipts_since(runs)
        assert receipts == []
        assert skipped_live == 1
