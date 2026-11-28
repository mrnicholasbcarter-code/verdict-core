"""Tests for OpenJev calibration metrics (BOD-203)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from verdict.decision_signals.calibration import (
    TASK_CLASSES,
    CalibrationRecord,
    CalibrationReport,
    _mean,
    _median,
    _rate,
    _usable,
    evaluate,
    load_records,
    recommend,
    render_markdown,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


FIXTURES = Path(__file__).parent / "fixtures" / "openjev" / "calibration"


@pytest.fixture
def sample_records() -> list[CalibrationRecord]:
    """Load synthetic calibration records from fixtures."""
    return load_records(FIXTURES / "replay_sample.jsonl")


@pytest.fixture
def tiny_set() -> list[CalibrationRecord]:
    """A small hand-built set of records with and without usable signals."""
    return [
        CalibrationRecord(
            task_id="t1",
            task_class="bounded_implementation",
            frontier_worthy=0.8,
            confidence=0.9,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=True,
            verified=True,
            first_pass=True,
        ),
        CalibrationRecord(
            task_id="t2",
            task_class="trivial_local_edit",
            frontier_worthy=0.2,
            confidence=0.9,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        ),
        CalibrationRecord(
            task_id="t3",
            task_class="bounded_implementation",
            frontier_worthy=0.8,
            confidence=0.9,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        ),
        CalibrationRecord(
            task_id="t4",
            task_class="bounded_implementation",
            frontier_worthy=0.3,
            confidence=0.9,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=True,
            verified=True,
            first_pass=True,
        ),
    ]


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for internal helper functions."""

    def test_rate_with_zero_denominator(self) -> None:
        """_rate returns None when denominator is zero."""
        assert _rate(0, 0) is None
        assert _rate(5, 0) is None

    def test_rate_with_valid_denominator(self) -> None:
        """_rate returns correct rate."""
        assert _rate(1, 4) == 0.25
        assert _rate(2, 4) == 0.5
        assert _rate(4, 4) == 1.0

    def test_mean_empty(self) -> None:
        """_mean returns None for empty sequence."""
        assert _mean([]) is None

    def test_mean_valid(self) -> None:
        """_mean returns correct mean."""
        assert _mean([1, 2, 3]) == 2.0
        assert _mean([10, 20]) == 15.0

    def test_median_empty(self) -> None:
        """_median returns None for empty sequence."""
        assert _median([]) is None

    def test_median_odd(self) -> None:
        """_median returns middle value for odd count."""
        assert _median([1, 2, 3]) == 2.0
        assert _median([3, 1, 2]) == 2.0  # Unsorted

    def test_median_even(self) -> None:
        """_median returns average of middle two for even count."""
        assert _median([1, 2, 3, 4]) == 2.5
        assert _median([4, 1, 2, 3]) == 2.5  # Unsorted

    def test_usable_with_valid_signal(self) -> None:
        """_usable returns True when both values are present and above threshold."""
        record = CalibrationRecord(
            task_id="t1",
            task_class="bounded_implementation",
            frontier_worthy=0.5,
            confidence=0.7,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        )
        assert _usable(record, min_confidence=0.6) is True
        assert _usable(record, min_confidence=0.8) is False

    def test_usable_missing_frontier_worthy(self) -> None:
        """_usable returns False when frontier_worthy is None."""
        record = CalibrationRecord(
            task_id="t1",
            task_class="bounded_implementation",
            frontier_worthy=None,
            confidence=0.9,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        )
        assert _usable(record, min_confidence=0.6) is False

    def test_usable_missing_confidence(self) -> None:
        """_usable returns False when confidence is None."""
        record = CalibrationRecord(
            task_id="t1",
            task_class="bounded_implementation",
            frontier_worthy=0.5,
            confidence=None,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        )
        assert _usable(record, min_confidence=0.6) is False

    def test_usable_below_threshold(self) -> None:
        """_usable returns False when confidence is below threshold."""
        record = CalibrationRecord(
            task_id="t1",
            task_class="bounded_implementation",
            frontier_worthy=0.5,
            confidence=0.4,
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        )
        assert _usable(record, min_confidence=0.6) is False


# ---------------------------------------------------------------------------
# reliability_buckets tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# evaluate tests
# ---------------------------------------------------------------------------


class TestEvaluate:
    """Tests for evaluate function."""

    def test_records_without_usable_signal_not_counted_as_avoided(
        self, tiny_set: list[CalibrationRecord]
    ) -> None:
        """Records without usable signal (None or below min_confidence) never count as planner calls avoided."""
        # Add a record with missing confidence (below threshold)
        no_confidence = CalibrationRecord(
            task_id="t5",
            task_class="bounded_implementation",
            frontier_worthy=0.1,  # Would avoid frontier
            confidence=None,  # No usable signal
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=True,  # But planner used frontier
            verified=True,
            first_pass=True,
        )
        with_usable = CalibrationRecord(
            task_id="t6",
            task_class="bounded_implementation",
            frontier_worthy=0.1,  # Would avoid frontier
            confidence=0.9,  # Usable
            security_sensitive=0.1,
            security_relevant=False,
            planner_was_frontier=True,  # But planner used frontier
            verified=True,
            first_pass=True,
        )
        extended_set = [*tiny_set, no_confidence, with_usable]
        report = evaluate(extended_set, threshold=0.5, min_confidence=0.6)
        # t4 is avoided (1 count from tiny_set)
        # t6 is avoided (2nd count from added records)
        # t5 has no usable signal, so can't count as avoided
        assert report.planner_calls_avoided == 2

    def test_security_false_negative_detection(self, tiny_set: list[CalibrationRecord]) -> None:
        """evaluate correctly identifies security false negatives."""
        # Add a security FN: security_sensitive score low but security_relevant=True
        sec_fn = CalibrationRecord(
            task_id="t5",
            task_class="security_sensitive",
            frontier_worthy=0.5,
            confidence=0.9,
            security_sensitive=0.1,  # Low score = predicted NOT security sensitive
            security_relevant=True,  # But IS security relevant
            planner_was_frontier=False,
            verified=True,
            first_pass=True,
        )
        report = evaluate(
            [*tiny_set, sec_fn], threshold=0.5, min_confidence=0.6, security_threshold=0.5
        )
        assert report.security_fn_rate == 1.0  # 1 FN out of 1 security-relevant record

    def test_per_class_counts(self, sample_records: list[CalibrationRecord]) -> None:
        """evaluate correctly counts records per class."""
        report = evaluate(sample_records, threshold=0.5, min_confidence=0.6)
        assert set(report.per_class.keys()) == set(TASK_CLASSES)
        total = sum(report.per_class.values())
        assert total == report.records


# ---------------------------------------------------------------------------
# recommend tests
# ---------------------------------------------------------------------------


class TestRecommend:
    """Tests for recommend function."""

    def _report(self, usable: int) -> CalibrationReport:
        return CalibrationReport(
            policy_threshold=0.5,
            min_confidence=0.6,
            records=usable,
            usable_signals=usable,
            per_class={c: usable for c in TASK_CLASSES},
            security_fn_rate=0.0,
        )

    def test_shadows_when_usable_less_than_50(self) -> None:
        """recommend names the sample size when usable < MIN_RECORDS_FOR_ADVISORY (50)."""
        state, reasons = recommend(self._report(10))
        assert state == "SHADOW"
        assert any("only 10 usable signals" in r for r in reasons)

    def test_never_promotes_without_a_verified_outcome_label(self) -> None:
        """Enough records still gives SHADOW: no label scores frontier_worthy (OJ1)."""
        state, reasons = recommend(self._report(500))
        assert state == "SHADOW"
        assert any("no verified-outcome label" in r for r in reasons)


# ---------------------------------------------------------------------------
# load_records tests
# ---------------------------------------------------------------------------


class TestLoadRecords:
    """Tests for load_records function."""

    def test_loads_valid_jsonl(self, sample_records: list[CalibrationRecord]) -> None:
        """load_records successfully reads valid JSONL file."""
        # Every non-blank fixture line is one record; none may be dropped silently.
        lines = [
            line
            for line in (FIXTURES / "replay_sample.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert len(sample_records) == len(lines) >= 60
        assert all(isinstance(r, CalibrationRecord) for r in sample_records)

    def test_skips_empty_lines(self) -> None:
        """load_records skips empty lines in JSONL file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                '{"task_id": "t1", "task_class": "bounded_implementation", "frontier_worthy": 0.5, '
            )
            f.write('"confidence": 0.9, "security_sensitive": 0.1, ')
            f.write('"security_relevant": false, "planner_was_frontier": false, "verified": true, ')
            f.write('"first_pass": true}\n')
            f.write("\n")  # Empty line
            f.write(
                '{"task_id": "t2", "task_class": "bounded_implementation", "frontier_worthy": 0.5, '
            )
            f.write('"confidence": 0.9, "security_sensitive": 0.1, ')
            f.write('"security_relevant": false, "planner_was_frontier": false, "verified": true, ')
            f.write('"first_pass": true}\n')
            path = Path(f.name)

        try:
            records = load_records(path)
            assert len(records) == 2
        finally:
            path.unlink()

    def test_raises_on_malformed_line(self) -> None:
        """load_records raises ValueError on malformed line with file:line context."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                '{"task_id": "t1", "task_class": "bounded_implementation", "frontier_worthy": 0.5, '
            )
            f.write('"confidence": 0.9, "security_sensitive": 0.1, ')
            f.write('"security_relevant": false, "planner_was_frontier": false, "verified": true, ')
            f.write('"first_pass": true}\n')
            f.write("this is not valid JSON\n")  # Malformed
            f.write(
                '{"task_id": "t3", "task_class": "bounded_implementation", "frontier_worthy": 0.5, '
            )
            f.write('"confidence": 0.9, "security_sensitive": 0.1, ')
            f.write('"security_relevant": false, "planner_was_frontier": false, "verified": true, ')
            f.write('"first_pass": true}\n')
            path = Path(f.name)

        try:
            with pytest.raises(ValueError) as exc_info:
                load_records(path)
            assert "invalid calibration record" in str(exc_info.value).lower()
            assert ":2:" in str(exc_info.value)  # Line 2
        finally:
            path.unlink()

    def test_handles_missing_optional_fields(self) -> None:
        """load_records handles records with missing optional fields."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                '{"task_id": "t1", "task_class": "bounded_implementation", "frontier_worthy": 0.5, '
            )
            f.write('"confidence": 0.9, "security_sensitive": 0.1, ')
            f.write('"security_relevant": false, "planner_was_frontier": false, "verified": true, ')
            f.write('"first_pass": true}\n')
            path = Path(f.name)

        try:
            records = load_records(path)
            assert len(records) == 1
            r = records[0]
            # Check optional fields have defaults
            assert r.retries == 0
            assert r.escalations == 0
            assert r.total_cost_usd is None
            assert r.time_to_green_s is None
            assert r.signal_latency_ms is None
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# render_markdown tests
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    """Tests for render_markdown function."""

    def test_contains_recommended_state(self, sample_records: list[CalibrationRecord]) -> None:
        """render_markdown contains the recommended state."""
        report = evaluate(sample_records, threshold=0.5, min_confidence=0.6)
        md = render_markdown(report)
        assert f"recommended state: **{report.recommended_state}**" in md

    def test_contains_basic_metrics(self, sample_records: list[CalibrationRecord]) -> None:
        """render_markdown contains key metrics."""
        report = evaluate(sample_records, threshold=0.5, min_confidence=0.6)
        md = render_markdown(report)
        assert f"records: {report.records}" in md
        assert "usable signals:" in md
        assert "threshold:" in md
        assert "frontier false-negative rate:" not in md
        assert "security false-negative rate:" in md

    def test_contains_recommendation_reasons(self, sample_records: list[CalibrationRecord]) -> None:
        """render_markdown contains recommendation reasons."""
        report = evaluate(sample_records, threshold=0.5, min_confidence=0.6)
        md = render_markdown(report)
        # At least one reason should be present
        assert "##" in md  # Has sections
        # Check for reasons list
        for reason in report.recommendation_reasons:
            assert reason in md
