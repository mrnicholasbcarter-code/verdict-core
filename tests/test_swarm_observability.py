"""
Tests for swarm observability, explainability, and completion metrics (Issue #47 / Slice 37.5).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from verdict.swarm_observability import (
    CompletionMetrics,
    SwarmMetricsCollector,
    SwarmTelemetryEvent,
    SwarmTelemetrySink,
    explain_assignment,
    generate_baseline_report,
)


class TestSwarmTelemetryEvent:
    """Tests for SwarmTelemetryEvent."""

    def test_event_serialization(self):
        """Event serializes to valid JSONL."""
        event = SwarmTelemetryEvent(
            correlation_id="corr-123",
            event_type="task_completed",
            task_id="task-1",
            model_id="model-a",
            data={"latency_ms": 150, "cost_usd": 0.01},
        )
        jsonl = event.to_jsonl()
        parsed = json.loads(jsonl)
        assert parsed["correlation_id"] == "corr-123"
        assert parsed["event_type"] == "task_completed"
        assert parsed["data"]["latency_ms"] == 150

    def test_event_has_unique_id(self):
        """Each event gets a unique ID."""
        e1 = SwarmTelemetryEvent(correlation_id="c1", event_type="test")
        e2 = SwarmTelemetryEvent(correlation_id="c1", event_type="test")
        assert e1.event_id != e2.event_id


class TestSwarmTelemetrySink:
    """Tests for SwarmTelemetrySink."""

    def test_sink_writes_jsonl(self):
        """Sink appends events as JSONL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "telemetry.jsonl"
            sink = SwarmTelemetrySink(path)

            event = SwarmTelemetryEvent(
                correlation_id="corr-1",
                event_type="task_assigned",
                task_id="task-1",
            )
            sink.emit(event)

            content = path.read_text()
            lines = content.strip().split("\n")
            assert len(lines) == 1
            parsed = json.loads(lines[0])
            assert parsed["correlation_id"] == "corr-1"
            assert parsed["event_type"] == "task_assigned"


class TestSwarmExplainer:
    """Tests for explain_assignment."""

    def test_explain_assignment(self):
        """Explainer generates reasons for assignment."""
        result = explain_assignment(
            task_id="task-1",
            candidate_id="model-b",
            eligible=True,
            reasons=[],
            candidate_cost_usd=0.02,
            budget_usd=0.10,
        )

        assert result["decision"] == "assigned"
        assert result["candidate_id"] == "model-b"
        assert result["cost_analysis"]["within_budget"] is True


class TestSwarmMetricsCollector:
    """Tests for SwarmMetricsCollector."""

    def test_task_lifecycle_events(self):
        """Collector records task lifecycle events."""
        collector = SwarmMetricsCollector(run_id="run-1")

        collector.record_task_event("task-1", "planned")
        collector.record_task_event("task-1", "assigned")
        collector.record_task_event("task-1", "started")
        collector.record_task_event("task-1", "completed")
        collector.record_task_event("task-1", "verified")

        # Also record a passing verification to count as verified
        collector.record_verification(
            task_id="task-1",
            attempt_id="attempt-1",
            passed=True,
            latency_ms=100.0,
            retry_count=0,
            cost_usd=0.01,
        )

        metrics = collector.compute_metrics()

        assert metrics.planned == 1
        assert metrics.assigned == 1
        assert metrics.completed == 1
        assert metrics.verified == 1

    def test_verification_metrics(self):
        """Collector tracks verification outcomes."""
        collector = SwarmMetricsCollector(run_id="run-1")

        collector.record_task_event("task-1", "planned")
        collector.record_task_event("task-1", "assigned")
        collector.record_task_event("task-1", "started")
        collector.record_task_event("task-1", "completed")

        # First attempt fails
        collector.record_verification(
            task_id="task-1",
            attempt_id="attempt-1",
            passed=False,
            latency_ms=100.0,
            retry_count=0,
            cost_usd=0.01,
        )
        # Second attempt succeeds
        collector.record_verification(
            task_id="task-1",
            attempt_id="attempt-2",
            passed=True,
            latency_ms=150.0,
            retry_count=1,
            cost_usd=0.02,
        )

        metrics = collector.compute_metrics()

        assert metrics.first_pass_verification_rate == 0.0  # First attempt failed
        assert metrics.rework_rate == 1.0  # One rework
        assert metrics.avg_retry_count == 0.5  # Average of (0+1)/2 = 0.5

    def test_latency_percentiles(self):
        """Collector computes latency percentiles."""
        collector = SwarmMetricsCollector(run_id="run-1")

        collector.record_task_event("task-1", "planned")
        collector.record_task_event("task-1", "assigned")
        collector.record_task_event("task-1", "completed")

        for latency in [100, 200, 300, 400, 500]:
            collector.record_verification(
                task_id=f"task-{latency}",
                attempt_id="attempt-1",
                passed=True,
                latency_ms=float(latency),
                retry_count=0,
                cost_usd=0.01,
            )

        metrics = collector.compute_metrics()

        assert metrics.p50_latency_ms == 300.0  # median
        assert metrics.p95_latency_ms == 500.0

    def test_cost_metrics(self):
        """Collector tracks cost metrics."""
        collector = SwarmMetricsCollector(run_id="run-1")

        collector.record_task_event("task-1", "planned")
        collector.record_task_event("task-1", "assigned")
        collector.record_task_event("task-1", "completed")

        collector.record_verification(
            task_id="task-1",
            attempt_id="attempt-1",
            passed=True,
            latency_ms=100.0,
            retry_count=0,
            cost_usd=0.05,
        )
        collector.record_task_event("task-2", "planned")
        collector.record_task_event("task-2", "assigned")
        collector.record_task_event("task-2", "completed")

        collector.record_verification(
            task_id="task-2",
            attempt_id="attempt-1",
            passed=True,
            latency_ms=100.0,
            retry_count=0,
            cost_usd=0.10,
        )

        metrics = collector.compute_metrics()

        # Use approximate equality for floating point
        assert abs(metrics.cost_per_verified_task - 0.075) < 0.001

    def test_fallback_rate(self):
        """Collector tracks model fallback rate."""
        collector = SwarmMetricsCollector(run_id="run-1")

        # Need completed tasks for metrics to compute
        for i in range(10):
            collector.record_task_event(f"task-{i}", "planned")
            collector.record_task_event(f"task-{i}", "assigned")
            collector.record_task_event(f"task-{i}", "completed")

        for _ in range(8):
            collector.record_model_call(fallback=False)
        for _ in range(2):
            collector.record_model_call(fallback=True)

        metrics = collector.compute_metrics()

        assert metrics.fallback_rate == 0.2  # 2/10
        assert metrics.model_availability_rate == 0.8

    def test_concurrency_utilization(self):
        """Collector computes concurrency utilization."""
        collector = SwarmMetricsCollector(run_id="run-1")

        # Plan 10 tasks
        for i in range(10):
            collector.record_task_event(f"task-{i}", "planned")

        # Assign and start 5
        for i in range(5):
            collector.record_task_event(f"task-{i}", "assigned")
            collector.record_task_event(f"task-{i}", "started")
            collector.record_task_event(f"task-{i}", "completed")

        metrics = collector.compute_metrics()

        # 5 concurrent out of 10 planned = 50%
        assert metrics.concurrency_utilization == 0.5

    def test_queue_wait_percentiles(self):
        """Collector computes queue wait percentiles."""
        collector = SwarmMetricsCollector(run_id="run-1")

        # Need completed tasks for metrics to compute
        for i in range(10):
            collector.record_task_event(f"task-{i}", "planned")
            collector.record_task_event(f"task-{i}", "assigned")
            collector.record_task_event(f"task-{i}", "completed")

        for wait in [10, 20, 30, 40, 50]:
            collector.record_queue_wait(float(wait))

        metrics = collector.compute_metrics()

        assert metrics.queue_wait_p50_ms == 30.0
        assert metrics.queue_wait_p95_ms == 50.0


class TestBaselineReport:
    """Tests for baseline report generation."""

    def test_report_generation(self):
        """Report generates deterministic output."""
        metrics = CompletionMetrics(
            run_id="run-1",
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T01:00:00Z",
            total_tasks=10,
            planned=10,
            assigned=8,
            completed=8,
            verified=7,
            blocked=1,
            failed=0,
            first_pass_verification_rate=0.85,
            rework_rate=0.15,
            avg_retry_count=0.3,
            p50_latency_ms=200.0,
            p95_latency_ms=500.0,
            cost_per_verified_task=0.05,
            total_budget_usd=50.0,
            budget_utilization=0.5,
            queue_wait_p50_ms=50.0,
            queue_wait_p95_ms=200.0,
            concurrency_utilization=0.8,
            model_availability_rate=0.95,
            fallback_rate=0.05,
            escaped_defects=0,
        )

        report = generate_baseline_report(metrics)

        assert "Swarm Completion Metrics Report" in report
        assert "run-1" in report
        assert "First-pass rate:" in report
        assert "85.0%" in report
        assert "p50: 200.0" in report
        assert "Cost per verified: $0.0500" in report


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
