"""Tests for the M0 headline metric report (issue #1213).

Covers:
- extend_run_metrics: per-role splits, frontier tokens, first-attempt pass rate.
- Fixture exclusion: fixture runs (test-* run_id) are never counted in the live aggregate.
- aggregate_extended_metrics: empty and multi-run cases.
- compute_headline_report: via monkeypatched load_receipts_since.
- _action_headline_metric: via run_action.
- format_headline_report: smoke test.

All receipts used here are in-memory or from tests/fixtures/metrics/; no live
provider calls are made.  Fixture runs are labelled as fixtures per AGENTS.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.receipt import compute_run_metrics, extend_run_metrics

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
            "tasks_per_dollar": "undefined",
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
            "frontier_tokens": {"input": 0, "output": 0, "total": 0, "per_accepted_task": None},
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

    def test_action_missing_runs_dir(self) -> None:
        from verdict.actions.registry import run_action

        result = run_action("headline.metric", {"runs_dir": "/tmp/does-not-exist-verdict-headline"})
        assert not result.ok
        assert "not found" in result.data.get("error", "")

    def test_action_invalid_since_date(self) -> None:
        from verdict.actions.registry import run_action

        result = run_action(
            "headline.metric", {"runs_dir": str(FIXTURES_DIR), "since": "not-a-date"}
        )
        assert not result.ok
        assert "invalid" in result.data.get("error", "").lower()
