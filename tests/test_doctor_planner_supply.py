"""Doctor planner_supply diagnostic (ADR-051 Decision 9)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import verdict.credentials_store as _creds_mod
import verdict.free_tier_admit as _fta_mod
import verdict.metadata.store as _meta_mod
import verdict.omniroute_evidence as _oe_mod
import verdict.planner_band as _pb_mod
from verdict import doctor_diagnostics as dd


def _diag() -> dd.DoctorDiagnostics:
    return dd.DoctorDiagnostics()


def _band(frontier: frozenset[str], fallback: frozenset[str]) -> Any:
    return _pb_mod.BandComputation(
        frontier_model_keys=frontier,
        fallback_model_keys=fallback,
        cutoff_frontier=80.0,
        cutoff_fallback=70.0,
        rated_models=len(frontier | fallback),
        fetched_at="2026-11-23T12:00:00Z",
        model_scores={k: 85.0 for k in frontier} | {k: 75.0 for k in fallback},
        model_ranks={k: i + 1 for i, k in enumerate(sorted(frontier | fallback))},
    )


def _patch_all(monkeypatch: pytest.MonkeyPatch, band: Any, inventory: list[str]) -> None:
    """Patch every external call _doctor_planner_supply makes."""
    monkeypatch.setattr(_meta_mod, "load_store", lambda: MagicMock())
    monkeypatch.setattr(_creds_mod, "resolve_gateway_binding", lambda: ("http://gateway", "key"))
    monkeypatch.setattr(_fta_mod, "normalize_omniroute_origin", lambda x: x)
    evidence_mock = MagicMock()
    evidence_mock.inventory_route_ids = inventory
    evidence_mock.route_capacities = {}  # all routes → CapacityClass.UNKNOWN (non-metered)
    monkeypatch.setattr(_oe_mod, "fetch_omniroute_evidence", lambda *a, **kw: evidence_mock)
    monkeypatch.setattr(_pb_mod, "compute_planner_bands", lambda *a, **kw: band)
    monkeypatch.setattr(_pb_mod, "load_planner_band_config", lambda: MagicMock())


def test_planner_supply_ok_when_frontier_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frontier band has routes → ok row, no warnings."""
    b = _band(frozenset({"cc/claude-sonnet", "oa/gpt-4o"}), frozenset())
    _patch_all(monkeypatch, b, ["cc/claude-sonnet", "oa/gpt-4o"])
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    assert "Planner supply" in rows
    state, detail = rows["Planner supply"]
    assert state == "ok"
    assert "2" in detail
    assert diag.warnings == []


def test_planner_supply_ok_no_warnings_when_frontier_and_fallback_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ok row produced and no warnings when frontier has routes even if fallback also has some."""
    b = _band(frozenset({"cc/claude-sonnet"}), frozenset({"oa/gpt-4o"}))
    _patch_all(monkeypatch, b, ["cc/claude-sonnet", "oa/gpt-4o"])
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    state, detail = rows["Planner supply"]
    assert state == "ok"
    assert detail  # non-empty detail present
    assert diag.warnings == []


def test_planner_supply_warns_when_only_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frontier empty, fallback non-empty → warn row, warning appended."""
    b = _band(frozenset(), frozenset({"cc/claude-haiku"}))
    _patch_all(monkeypatch, b, ["cc/claude-haiku"])
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    state, detail = rows["Planner supply"]
    assert state == "warn"
    assert "fallback" in detail
    assert len(diag.warnings) == 1
    assert "fallback" in diag.warnings[0].lower()


def test_planner_supply_warns_when_both_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both bands empty → warn row, warning appended."""
    b = _band(frozenset(), frozenset())
    _patch_all(monkeypatch, b, [])
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    state, detail = rows["Planner supply"]
    assert state == "warn"
    assert "empty" in detail
    assert len(diag.warnings) == 1
    assert "empty" in diag.warnings[0].lower()


def test_planner_supply_note_when_metadata_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """metadata store raises → note row, no warnings."""

    def _raise() -> Any:
        raise OSError("not found")

    monkeypatch.setattr(_meta_mod, "load_store", _raise)
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    state, detail = rows["Planner supply"]
    assert state == "note"
    assert "metadata" in detail.lower() or "unavailable" in detail.lower()
    assert diag.warnings == []


def test_planner_supply_note_when_gateway_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway call raises → note row, no warnings."""
    monkeypatch.setattr(_meta_mod, "load_store", lambda: MagicMock())

    def _raise() -> Any:
        raise OSError("unreachable")

    monkeypatch.setattr(_creds_mod, "resolve_gateway_binding", _raise)
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    state, detail = rows["Planner supply"]
    assert state == "note"
    assert "OSError" in detail
    assert diag.warnings == []


def test_planner_supply_warn_when_band_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """compute_planner_bands raises → warn row with 'band error' and a warning appended."""
    inventory = ["cc/claude-sonnet"]
    monkeypatch.setattr(_meta_mod, "load_store", lambda: MagicMock())
    monkeypatch.setattr(_creds_mod, "resolve_gateway_binding", lambda: ("http://gw", "k"))
    monkeypatch.setattr(_fta_mod, "normalize_omniroute_origin", lambda x: x)
    evidence_mock = MagicMock()
    evidence_mock.inventory_route_ids = inventory
    evidence_mock.route_capacities = {}  # all routes → CapacityClass.UNKNOWN (non-metered)
    monkeypatch.setattr(_oe_mod, "fetch_omniroute_evidence", lambda *a, **kw: evidence_mock)
    monkeypatch.setattr(_pb_mod, "load_planner_band_config", lambda: MagicMock())

    def _raise(*a: Any, **kw: Any) -> Any:
        raise ValueError("bad data")

    monkeypatch.setattr(_pb_mod, "compute_planner_bands", _raise)
    diag = _diag()
    dd._doctor_planner_supply(diag)
    rows = {name: (state, detail) for name, state, detail in diag.sections}
    state, detail = rows["Planner supply"]
    assert state == "warn"
    assert "band error" in detail
    assert len(diag.warnings) == 1
    assert "band error" in diag.warnings[0]
