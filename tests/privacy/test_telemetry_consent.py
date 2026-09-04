"""Telemetry opt-in/opt-out verification for the launch gate (T026)."""

from __future__ import annotations

import json
from pathlib import Path

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.swarm_observability import SwarmTelemetryEvent, SwarmTelemetrySink


def test_telemetry_is_not_emitted_without_explicit_consent(tmp_path: Path) -> None:
    output = tmp_path / "telemetry.jsonl"
    sink = SwarmTelemetrySink(output)
    sink.emit(SwarmTelemetryEvent(correlation_id="synthetic-opt-out", event_type="test"))

    assert not output.exists()


def test_explicit_telemetry_consent_emits_only_redacted_operational_data(tmp_path: Path) -> None:
    output = tmp_path / "telemetry.jsonl"
    secret = SECRET_KEYED_VALUES["api_key"]
    sink = SwarmTelemetrySink(output, consent_given=True)
    sink.emit(
        SwarmTelemetryEvent(
            correlation_id="synthetic-opt-in",
            event_type="verification",
            data={"status": "pass", "api_key": secret, "latency_ms": 4},
        )
    )

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    emitted = json.loads(lines[0])
    assert emitted["correlation_id"] == "synthetic-opt-in"
    assert emitted["data"]["status"] == "pass"
    assert emitted["data"]["latency_ms"] == 4
    assert secret not in lines[0]
    assert "[REDACTED" in lines[0]
