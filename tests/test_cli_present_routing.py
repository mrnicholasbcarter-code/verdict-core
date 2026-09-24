"""Smoke tests for BOD-187 routing-family human output (present.* helpers)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import verdict.cli as cli


@pytest.fixture()
def decisions_log(tmp_path: Path) -> Path:
    log = tmp_path / "decisions.jsonl"
    log.write_text(
        json.dumps({"decision": {"tier": 0, "model": "frontier", "latency_ms": 10.0}}) + "\n"
        + json.dumps({"decision": {"tier": 3, "model": "cheap", "latency_ms": 20.0}}) + "\n"
    )
    return log


def test_cmd_stats_present_header(decisions_log: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.cmd_stats(str(decisions_log))
    out = capsys.readouterr().out
    assert "Routing stats" in out
    assert "Tier Distribution" in out
    assert "Total Requests" in out
    assert "frontier" in out
    assert "cheap" in out


def test_cmd_stats_missing_uses_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.cmd_stats(str(tmp_path / "missing.jsonl"))
    out = capsys.readouterr().out
    assert "Routing stats" in out
    assert "No log file found" in out


def test_cmd_cost_report_present_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    cli.cmd_cost_report()
    out = capsys.readouterr().out
    assert "Cost and Usage Report" in out
    assert "No routing telemetry found" in out


def test_cmd_simulate_present_header(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli.cmd_simulate("hello world task", "medium")
    out = capsys.readouterr().out
    assert "Verdict pre-execution simulation" in out
    assert "Risk score" in out


def test_cmd_route_verbose_present_header(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_route("format docs", "low", terse=False, allow_legacy_selector=True)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Routing Decision" in out
    assert "format docs" in out
    assert '\"transport_outcome\": \"error\"' in out


def test_cmd_failover_proof_present_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.cmd_failover_proof(str(tmp_path / "memory.db"), output_json=False)
    out = capsys.readouterr().out
    assert "Failover proof" in out
    assert "proof" in out.lower()
