"""Gateway kind: env wins, then verdict.yaml, else unset; doctor reports rankings state."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

import verdict.omniroute_rankings as rankings_mod
from verdict import doctor_diagnostics as dd
from verdict.omniroute_rankings import OmniRouteRankings, resolve_gateway_kind

_PAYLOAD = {"rankings": [{"id": "cx", "topModel": {"modelId": "gpt-6.1-sol", "score": 0.9}}]}


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("VERDICT_GATEWAY_KIND", raising=False)
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    path = tmp_path / "verdict" / "verdict.yaml"
    path.parent.mkdir(parents=True)
    return path


def test_default_is_unset_and_rankings_skip_as_generic(config_file: Path) -> None:
    assert resolve_gateway_kind() == ("", "default")
    client = OmniRouteRankings("http://127.0.0.1:20128/v1", None, fetch_json=_fail_fetch)
    assert client.fetch("coding").omission == "generic_gateway"


def test_config_key_enables_rankings(config_file: Path) -> None:
    config_file.write_text("gateway_kind: OmniRoute\n")
    assert resolve_gateway_kind() == ("omniroute", "verdict.yaml gateway_kind")
    client = OmniRouteRankings("http://127.0.0.1:20128/v1", None, fetch_json=lambda *a: _PAYLOAD)
    assert [row.id for row in client.fetch("coding").rows] == ["cx"]


def test_env_wins_over_config(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file.write_text("gateway_kind: omniroute\n")
    monkeypatch.setenv("VERDICT_GATEWAY_KIND", "generic")
    assert resolve_gateway_kind() == ("generic", "env VERDICT_GATEWAY_KIND")
    client = OmniRouteRankings("http://127.0.0.1:20128/v1", None, fetch_json=_fail_fetch)
    assert client.fetch("coding").omission == "generic_gateway"


def test_malformed_config_stays_unset(config_file: Path) -> None:
    config_file.write_text("gateway_kind: [omniroute\n")
    assert resolve_gateway_kind() == ("", "default")


def _sections(monkeypatch: pytest.MonkeyPatch, fetch: Any) -> list[tuple[str, str, str]]:
    monkeypatch.setattr(rankings_mod, "_default_fetch_json", fetch)
    diag = dd.DoctorDiagnostics()
    dd._doctor_advisory_rankings(diag)
    return [s for s in diag.sections if s[0] == "Advisory rankings"]


def _fail_fetch(*_: Any) -> Any:
    raise AssertionError("rankings must not be fetched")


def test_doctor_reports_inactive_when_unset(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [(_, state, detail)] = _sections(monkeypatch, _fail_fetch)
    assert state == "note"
    assert detail.startswith("inactive: gateway_kind=unset (default)")


def test_doctor_reports_active_with_ranked_rows(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file.write_text("gateway_kind: omniroute\n")
    [(_, state, detail)] = _sections(monkeypatch, lambda *a: _PAYLOAD)
    assert state == "ok"
    assert detail == (
        "active: gateway_kind=omniroute (verdict.yaml gateway_kind); "
        "1 ranked providers (coding) at http://127.0.0.1:20128"
    )


def test_doctor_reports_omission_when_endpoint_fails(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_GATEWAY_KIND", "omniroute")

    def unreachable(*_: Any) -> Any:
        raise urllib.error.URLError("refused")

    [(_, state, detail)] = _sections(monkeypatch, unreachable)
    assert state == "warn"
    assert detail.endswith("rankings omitted (unavailable) at http://127.0.0.1:20128")
