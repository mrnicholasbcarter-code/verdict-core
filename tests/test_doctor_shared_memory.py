"""Integration tests: doctor reports degraded shared memory with named reasons (BOD-80 AC 9).

Each test runs the real ``cmd_doctor --json`` entry point with a tmp HOME,
mocking only the shared-memory provider health data to exercise every
``diagnose_shared_memory`` path through the production doctor collector.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from verdict import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_healthy_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal healthy doctor fixture — everything except shared memory."""
    cfg_dir = tmp_path / ".config" / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text(
        "schema_version: 1\n"
        "primary_model: anthropic/claude-opus-5\n"
        "log_path: route-log.jsonl\n"
        "gateway_url: http://localhost:11434/v1\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1\n"
    )
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")

    verdict_dir = tmp_path / ".verdict"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    (verdict_dir / "memory.db").touch()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("OMNIROUTE_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:0")
    # Clear any real shared-memory env so discover_shared_memory_setup sees unconfigured
    monkeypatch.delenv("VERDICT_SHARED_MEMORY_URL", raising=False)
    monkeypatch.delenv("VERDICT_SHARED_MEMORY_TOKEN", raising=False)

    monkeypatch.setattr(cli, "_omniroute_api_request", lambda *a, **k: None)

    import urllib.request

    class _FakeHealthResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeHealthResp())

    import verdict.documentation_preflight as dp

    monkeypatch.setattr(dp, "discover_sources", lambda _root=None: ())

    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda address, timeout=None, source_address=None: MagicMock()
    )


def _run_doctor_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> dict[str, Any]:
    """Run ``verdict doctor --json`` and return the parsed report."""
    monkeypatch.setattr(sys, "argv", ["verdict", "doctor", "--json"])
    cli.main()
    return json.loads(capsys.readouterr().out)


def _mock_shared_memory_report(monkeypatch: pytest.MonkeyPatch, report: dict[str, Any]) -> None:
    """Replace ``doctor_shared_memory_report`` inside memory_bridge."""
    import verdict.shared_memory as sm

    monkeypatch.setattr(sm, "doctor_shared_memory_report", lambda **kw: report)


# ---------------------------------------------------------------------------
# (a) Unreachable provider
# ---------------------------------------------------------------------------


def test_doctor_shared_memory_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_healthy_fixture(tmp_path, monkeypatch)
    _mock_shared_memory_report(
        monkeypatch,
        {
            "provider_id": "mcp-memory-service",
            "configured": True,
            "installed": True,
            "endpoint": "http://dead-host:9999",
            "status": "unavailable",
            "state": "unavailable",
            "message": "connection refused",
        },
    )
    report = _run_doctor_json(monkeypatch, capsys)
    sm = report["shared_memory"]
    assert sm["diagnosis_state"] == "degraded"
    assert "unreachable" in sm["diagnosis_reason"]
    # Should appear as a warning (shared memory is advisory, not a hard issue)
    assert any("unreachable" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# (b) Auth failed provider (BOD-273 new reason)
# ---------------------------------------------------------------------------


def test_doctor_shared_memory_auth_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify auth_failed status maps to auth_failed reason (not unwritable)."""
    _base_healthy_fixture(tmp_path, monkeypatch)
    _mock_shared_memory_report(
        monkeypatch,
        {
            "provider_id": "mcp-memory-service",
            "configured": True,
            "installed": True,
            "endpoint": "http://localhost:9999",
            "status": "auth_failed",
            "state": "unavailable",
            "message": "authentication failed",
        },
    )
    report = _run_doctor_json(monkeypatch, capsys)
    sm = report["shared_memory"]
    assert sm["diagnosis_state"] == "degraded"
    assert sm["diagnosis_reason"].startswith("auth_failed:")
    assert "auth_failed" in sm["diagnosis_reason"]
    assert any("auth_failed" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# (c) Unwritable provider (real write-permission failures)
# ---------------------------------------------------------------------------


def test_doctor_shared_memory_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify degraded status maps to unwritable reason."""
    _base_healthy_fixture(tmp_path, monkeypatch)
    _mock_shared_memory_report(
        monkeypatch,
        {
            "provider_id": "mcp-memory-service",
            "configured": True,
            "installed": True,
            "endpoint": "http://localhost:9999",
            "status": "degraded",
            "state": "degraded",
            "message": "write permission denied",
        },
    )
    report = _run_doctor_json(monkeypatch, capsys)
    sm = report["shared_memory"]
    assert sm["diagnosis_state"] == "degraded"
    assert sm["diagnosis_reason"].startswith("unwritable:")
    assert "unwritable" in sm["diagnosis_reason"]
    assert any("unwritable" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# (d) Schema-incompatible provider
# ---------------------------------------------------------------------------


def test_doctor_shared_memory_schema_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_healthy_fixture(tmp_path, monkeypatch)
    _mock_shared_memory_report(
        monkeypatch,
        {
            "provider_id": "mcp-memory-service",
            "configured": True,
            "installed": True,
            "endpoint": "http://localhost:9999",
            "status": "incompatible",
            "state": "incompatible",
            "message": "protocol version mismatch",
        },
    )
    report = _run_doctor_json(monkeypatch, capsys)
    sm = report["shared_memory"]
    assert sm["diagnosis_state"] == "degraded"
    assert "schema_incompatible" in sm["diagnosis_reason"]
    assert any("schema_incompatible" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# (e) Healthy provider
# ---------------------------------------------------------------------------


def test_doctor_shared_memory_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_healthy_fixture(tmp_path, monkeypatch)
    _mock_shared_memory_report(
        monkeypatch,
        {
            "provider_id": "mcp-memory-service",
            "configured": True,
            "installed": True,
            "endpoint": "http://localhost:9999",
            "status": "available",
            "state": "healthy",
            "message": None,
        },
    )
    report = _run_doctor_json(monkeypatch, capsys)
    sm = report["shared_memory"]
    assert sm["diagnosis_state"] == "ready"
    assert sm["diagnosis_reason"] == "ok"
    # No degradation warning
    assert not any("shared memory degraded" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# (f) Not configured
# ---------------------------------------------------------------------------


def test_doctor_shared_memory_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _base_healthy_fixture(tmp_path, monkeypatch)
    _mock_shared_memory_report(
        monkeypatch,
        {
            "provider_id": "mcp-memory-service",
            "configured": False,
            "installed": False,
            "endpoint": None,
            "state": "not_installed",
        },
    )
    report = _run_doctor_json(monkeypatch, capsys)
    sm = report["shared_memory"]
    # No diagnosis fields when not configured
    assert "diagnosis_state" not in sm
    assert not any("shared memory degraded" in w for w in report["warnings"])
