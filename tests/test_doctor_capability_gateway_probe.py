"""Doctor's capability report uses doctor's real gateway probe (no network)."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from verdict import capability_bootstrap, doctor_diagnostics
from verdict.gateway_lifecycle import GatewayHealth
from verdict.provider_bootstrap import BootstrapDiagnostic, BootstrapError

GATEWAY_URL = "http://127.0.0.1:20128/v1"


class _StopDoctorError(Exception):
    """Raised after the capability report so the rest of doctor never runs."""


def _health(reachable: bool, detail: str | None = None) -> Any:
    try:
        return GatewayHealth(reachable=reachable, responded=reachable, detail=detail)
    except TypeError:
        return types.SimpleNamespace(reachable=reachable, responded=reachable, detail=detail)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setenv("VERDICT_GATEWAY_URL", "http://127.0.0.1:20128")
    for name in ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    work = tmp_path / "work"
    work.mkdir()
    (work / "verdict.yaml").write_text("gateway_url: http://127.0.0.1:20128\n", encoding="utf-8")
    monkeypatch.chdir(work)


def _fake_gateway(monkeypatch: pytest.MonkeyPatch, health: Any) -> None:
    monkeypatch.setattr(
        doctor_diagnostics,
        "_cli_bootstrap",
        lambda: types.SimpleNamespace(gateway_url=GATEWAY_URL, providers={}),
    )
    monkeypatch.setattr(
        doctor_diagnostics, "_gateway_probe_for", lambda bootstrap: lambda url: health
    )


def _report_via_doctor(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []
    original = capability_bootstrap.doctor_capability_report

    def _wrapper(**kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("probe_gateway") is not None
        report = original(**kwargs)
        captured.append(report)
        raise _StopDoctorError

    monkeypatch.setattr(capability_bootstrap, "doctor_capability_report", _wrapper)
    with pytest.raises(_StopDoctorError):
        doctor_diagnostics._collect_doctor_diagnostics(False, interactive=False)
    assert captured
    return captured[0]


def _capability(report: dict[str, Any], name: str) -> dict[str, Any]:
    for item in report.get("capabilities", []):
        if not isinstance(item, dict):
            continue
        for key in ("capability", "capability_id", "id", "name"):
            if item.get(key) == name:
                return item
    raise AssertionError(f"capability {name} missing from report")


def test_healthy_fake_probe_marks_gateway_capabilities_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, _health(True))
    report = _report_via_doctor(monkeypatch)
    for name in ("gateway.inventory", "gateway.execute"):
        item = _capability(report, name)
        assert item["status"] == "covered"
        assert item["selected_provider_id"] == "gateway.omniroute"
        assert item["health"] == "healthy"


def test_unreachable_fake_probe_keeps_gateway_capabilities_not_covered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, _health(False, "connection refused"))
    report = _report_via_doctor(monkeypatch)
    for name in ("gateway.inventory", "gateway.execute"):
        assert _capability(report, name)["status"] != "covered"


def test_doctor_probe_reachable_maps_health_ok_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, _health(True))
    result = doctor_diagnostics._doctor_capability_gateway_probe()("gateway.omniroute")
    assert result["health_ok"] is True
    assert result["reachable"] is True
    assert "api_key" not in result
    for value in result.values():
        text = str(value).lower()
        assert "sk-" not in text
        assert "bearer" not in text


def test_doctor_probe_unreachable_uses_detail_as_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, _health(False, "connection refused"))
    result = doctor_diagnostics._doctor_capability_gateway_probe()("gateway.omniroute")
    assert result["health_ok"] is False
    assert result["reason"] == "connection refused"


def test_doctor_probe_bootstrap_error_uses_reason_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    error = BootstrapError(
        "reason_code",
        (
            BootstrapDiagnostic(
                code="reason_code",
                diagnostic_class="gateway_health",
                field="gateway_url",
                detail="probe failed",
                remediation="check gateway url",
            ),
        ),
    )

    def _raise() -> Any:
        raise error

    monkeypatch.setattr(doctor_diagnostics, "_cli_bootstrap", _raise)
    result = doctor_diagnostics._doctor_capability_gateway_probe()("gateway.omniroute")
    assert result["health_ok"] is False
    assert result["reason"] == "reason_code"


def test_doctor_probe_other_provider_not_probed() -> None:
    result = doctor_diagnostics._doctor_capability_gateway_probe()("other.provider")
    assert result == {"reachable": False, "health_ok": False, "reason": "not probed by doctor"}


def test_doctor_probe_os_error_reports_probe_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)

    def _raise() -> Any:
        raise OSError("boom")

    monkeypatch.setattr(doctor_diagnostics, "_cli_bootstrap", _raise)
    result = doctor_diagnostics._doctor_capability_gateway_probe()("gateway.omniroute")
    assert result["reason"] == "probe error: OSError"
    assert "boom" not in str(result)


def test_doctor_probe_redacts_userinfo_in_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path)
    _fake_gateway(monkeypatch, _health(False, "failed http://user:pw@gateway.invalid/v1"))
    result = doctor_diagnostics._doctor_capability_gateway_probe()("gateway.omniroute")
    assert result["health_ok"] is False
    assert "user:pw" not in result["reason"]
