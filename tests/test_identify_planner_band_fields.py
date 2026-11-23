"""identify_report returns rating_coverage and planner_band_families (ADR-051 Decision 9)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import verdict.planner_band as _pb_mod
from verdict import identify
from verdict.omniroute_evidence import parse_omniroute_evidence

FIXTURE = Path(__file__).parent / "fixtures/omniroute_evidence/snapshot.json"


def _evidence() -> Any:
    data = json.loads(FIXTURE.read_text())
    from datetime import datetime, timezone

    return parse_omniroute_evidence(**data, now=datetime(2026, 11, 23, 12, tzinfo=timezone.utc))


def _patch_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identify, "resolve_gateway_binding", lambda: ("http://gw", "k"))
    monkeypatch.setattr(identify, "fetch_omniroute_evidence", lambda *a, **kw: _evidence())
    monkeypatch.setattr(identify, "resolve_management_token", lambda: None)


def _patch_band(
    monkeypatch: pytest.MonkeyPatch, *, coverage: dict[str, Any] | None, families: list[str] | None
) -> None:
    """Patch band functions to return controlled values."""
    band_mock = MagicMock()
    monkeypatch.setattr(_pb_mod, "load_planner_band_config", lambda: MagicMock())
    monkeypatch.setattr(_pb_mod, "compute_planner_bands", lambda *a, **kw: band_mock)
    monkeypatch.setattr(_pb_mod, "rating_coverage_summary", lambda *a, **kw: coverage)
    monkeypatch.setattr(_pb_mod, "planner_band_families", lambda *a, **kw: families)


def test_identify_report_includes_rating_coverage_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """rating_coverage is present and matches what rating_coverage_summary returns."""
    monkeypatch.setattr(identify, "default_cache_path", lambda: tmp_path / "hc.json")
    _patch_gateway(monkeypatch)
    expected_coverage = {
        "rated_models": 5,
        "inventory_routes": 10,
        "coverage_pct": 50.0,
        "frontier_model_count": 2,
        "fallback_model_count": 1,
        "cutoff_frontier": 85.0,
        "cutoff_fallback": 70.0,
    }
    _patch_band(monkeypatch, coverage=expected_coverage, families=["claude", "gpt-4"])
    report = identify.identify_report()
    assert "rating_coverage" in report
    assert report["rating_coverage"] == expected_coverage


def test_identify_report_includes_planner_band_families_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """planner_band_families is present and matches what planner_band_families returns."""
    monkeypatch.setattr(identify, "default_cache_path", lambda: tmp_path / "hc.json")
    _patch_gateway(monkeypatch)
    expected_families = ["claude", "gpt-4o"]
    _patch_band(monkeypatch, coverage={}, families=expected_families)
    report = identify.identify_report()
    assert "planner_band_families" in report
    assert report["planner_band_families"] == expected_families


def test_identify_report_band_fields_none_when_compute_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both fields are None when compute_planner_bands raises; the report still returns."""
    monkeypatch.setattr(identify, "default_cache_path", lambda: tmp_path / "hc.json")
    _patch_gateway(monkeypatch)
    monkeypatch.setattr(_pb_mod, "load_planner_band_config", lambda: MagicMock())

    def _raise(*a: Any, **kw: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(_pb_mod, "compute_planner_bands", _raise)
    report = identify.identify_report()
    assert report["rating_coverage"] is None
    assert report["planner_band_families"] is None


def test_identify_report_band_fields_none_when_rating_coverage_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both fields are None when rating_coverage_summary raises (whole block is guarded)."""
    monkeypatch.setattr(identify, "default_cache_path", lambda: tmp_path / "hc.json")
    _patch_gateway(monkeypatch)
    band_mock = MagicMock()
    monkeypatch.setattr(_pb_mod, "load_planner_band_config", lambda: MagicMock())
    monkeypatch.setattr(_pb_mod, "compute_planner_bands", lambda *a, **kw: band_mock)

    def _raise(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("bad")

    monkeypatch.setattr(_pb_mod, "rating_coverage_summary", _raise)
    report = identify.identify_report()
    assert report["rating_coverage"] is None
    assert report["planner_band_families"] is None
