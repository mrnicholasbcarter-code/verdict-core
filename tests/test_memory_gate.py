from __future__ import annotations

import asyncio
from pathlib import Path

from verdict.memory_gate import AuthorityLevel, MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane


def request(**overrides: object) -> MemoryWriteRequest:
    values: dict[str, object] = {
        "namespace": "patterns",
        "key": "routing",
        "value": {"outcome": "safe", "token": "sk-secret-token"},
        "authority": "verdict-core",
        "provenance": {"source": "test", "prompt": "private prompt"},
        "scope": "repo",
    }
    values.update(overrides)
    return MemoryWriteRequest(**values)


def test_gate_persists_verified_redacted_write_and_restart_history(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with MemoryPlane(path) as plane:
        gate = MemoryGate(plane)
        result = gate.write(request())
        assert result.allowed is True
        assert result.record is not None
        assert result.record.authority_verified is True
        assert "sk-secret" not in result.record.content
        assert "private prompt" not in result.record.content
        assert result.event_id

    with MemoryPlane(path) as plane:
        gate = MemoryGate(plane)
        events = gate.get_write_history()
        assert len(events) == 1
        assert events[0].result["allowed"] is True
        assert "prompt" in events[0].request["provenance"]
        assert events[0].request["provenance"]["prompt"] == "[REDACTED]"


def test_gate_derives_authority_and_rejects_caller_level_escalation(tmp_path: Path) -> None:
    with MemoryPlane(tmp_path / "memory.db") as plane:
        gate = MemoryGate(plane)
        result = gate.write(request(authority="agent", authority_level=AuthorityLevel.SYSTEM))
        assert result.allowed is False
        assert result.reason == "authority_level_mismatch"


def test_gate_requires_explicit_supersession_for_contradiction(tmp_path: Path) -> None:
    with MemoryPlane(tmp_path / "memory.db") as plane:
        gate = MemoryGate(plane)
        first = gate.write(request(value={"outcome": "safe"}))
        assert first.allowed is True

        blocked = gate.write(request(value={"outcome": "unsafe"}))
        assert blocked.allowed is False
        assert blocked.contradiction_detected is True

        replacement = gate.write(
            request(value={"outcome": "unsafe"}, supersedes=first.record.record_id)
        )
        assert replacement.allowed is True
        assert replacement.record is not None
        assert replacement.record.supersedes == first.record.record_id


def test_gate_limits_ttl_confidence_and_size(tmp_path: Path) -> None:
    with MemoryPlane(tmp_path / "memory.db") as plane:
        gate = MemoryGate(plane)
        assert gate.write(request(ttl_seconds=10_000_000)).reason == "ttl_out_of_range"
        assert gate.write(request(confidence=0.1)).reason == "confidence_below_policy"
        assert gate.write(request(value={"body": "x" * 2_000_000})).reason == "value_too_large"


def test_gate_async_compatibility_uses_same_durable_plane(tmp_path: Path) -> None:
    with MemoryPlane(tmp_path / "memory.db") as plane:
        gate = MemoryGate(plane)
        result = asyncio.run(gate.execute_write(request(key="async")))
        assert result.allowed is True
        assert plane.get("patterns", "async", scope="repo") is not None
