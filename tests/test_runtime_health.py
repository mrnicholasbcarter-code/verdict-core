from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.runtime_health import (
    RuntimeHealthError,
    RuntimeHealthReport,
    RuntimeHealthStatus,
    build_runtime_health_report,
)


def plan(*, health: str = "unavailable", errors: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "operation": "runtime",
        "status": "ready" if not errors else "blocked",
        "services": [
            {
                "service_id": "fixture-mcp",
                "kind": "mcp",
                "endpoint": "http://127.0.0.1:29999/mcp",
                "health_endpoint": "http://127.0.0.1:29999/healthz",
                "status": "unavailable",
                "health": health,
                "port_state": "available",
                "owner_pid": None,
            }
        ],
        "errors": list(errors),
    }


def test_unknown_health_does_not_become_ready() -> None:
    report = build_runtime_health_report(plan(), observed_at="2026-07-31T00:00:00Z")

    assert report.status == "unknown"
    assert report.passed is True
    assert [item.status for item in report.observations] == [
        RuntimeHealthStatus.CONFIGURED,
        RuntimeHealthStatus.UNKNOWN,
    ]
    assert report.observations[1].identity_verified is False


def test_health_endpoint_is_separate_from_process_and_port_evidence() -> None:
    report = build_runtime_health_report(
        {
            **plan(health="healthy"),
            "services": [
                {
                    **plan()["services"][0],  # type: ignore[index]
                    "status": "unavailable",
                    "owner_pid": None,
                    "port_state": "occupied",
                    "health": "healthy",
                }
            ],
        },
        observed_at="2026-07-31T00:00:00Z",
    )

    assert report.status == "ready"
    assert report.observations[1].status is RuntimeHealthStatus.REACHABLE
    assert report.observations[1].identity_verified is False
    assert "protocol compatibility" in report.observations[1].limitations[0]


def test_runtime_errors_block_report_without_leaking_payloads() -> None:
    report = build_runtime_health_report(
        plan(errors=("fixture-mcp:port-collision",)), observed_at="2026-07-31T00:00:00Z"
    )

    assert report.status == "blocked"
    assert report.passed is False
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    assert "authorization" not in encoded.lower()


def test_runtime_artifact_round_trip_digest_and_schema() -> None:
    report = build_runtime_health_report(plan(health="healthy"), observed_at="2026-07-31T00:00:00Z")
    restored = RuntimeHealthReport.from_dict(report.to_dict())
    assert restored == report

    schema = json.loads(
        (Path(__file__).parents[1] / "verdict" / "schemas" / "runtime-health.v1.json").read_text()
    )
    assert list(Draft202012Validator(schema).iter_errors(report.to_dict())) == []

    tampered = report.to_dict()
    tampered["observations"][0]["evidence_digest"] = "sha256:" + "b" * 64  # type: ignore[index]
    with pytest.raises(RuntimeHealthError, match="does not match"):
        RuntimeHealthReport.from_dict(tampered)


def test_cli_runtime_explain_is_machine_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setenv("VERDICT_RUNTIME_STATE_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(sys, "argv", ["verdict", "runtime", "explain", "--json"])

    from verdict import cli

    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "1"
    assert report["status"] in {"unknown", "degraded", "ready", "blocked"}
    assert report["observations"]
