"""Supervisor controller route-scope: --scope / VERDICT_CONTROLLER_ROUTE_PREFIXES.

AC1: scoped admission restricts to matching prefixes (CONTROLLER_SCOPE stage, named reason).
AC2: out-of-scope routes appear in the receipt with named reason.
AC3: empty scope is parity with unconstrained admission.
AC4: scope that matches nothing fails closed with controller_scope_empty.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import verdict.admission as adm
from tests.test_prime_supervisor import module
from tests.test_supervisor_admission import _yaml_only_home  # re-use helper

NOW = datetime.now(timezone.utc)

# ── minimal fake inventory and connections ──────────────────────────────────
CATALOG = [
    {"id": "cc/claude-opus", "owned_by": "cc", "context_length": 200_000},
    {"id": "kr/claude-opus", "owned_by": "kr", "context_length": 200_000},
]
CONNECTIONS = [
    {"provider": "cc", "isActive": True, "testStatus": "ok"},
    {"provider": "kr", "isActive": True, "testStatus": "ok"},
]


def _make_admitted() -> adm.AdmittedSet:
    """Build a real AdmittedSet with both cc/ and kr/ routes admitted."""
    return adm.admit(CATALOG, CONNECTIONS, None, now=NOW, require_runtime=False)


# ── AC1 + AC2: scoped admission ────────────────────────────────────────────


def test_scoped_loader_restricts_to_matching_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader with scope_prefixes=["kr/"] admits only kr/ routes; cc/ is dropped
    with stage CONTROLLER_SCOPE and reason outside_controller_route_prefix."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    loader = m._default_live_admission_loader(tmp_path / "state", scope_prefixes=["kr/"])
    admitted = loader(NOW)

    # AC1: only kr/ routes survive
    assert "kr/claude-opus" in admitted
    assert "cc/claude-opus" not in admitted

    # AC2: dropped route has named reason in receipt
    receipt = admitted.receipt()
    candidates = receipt["candidates"]
    dropped = [r for r in candidates if not r["admitted"]]
    assert any(
        r["route_id"] == "cc/claude-opus"
        and r["first_failed_stage"] == "CONTROLLER_SCOPE"
        and r["reason"] == "outside_controller_route_prefix"
        for r in dropped
    ), f"expected cc/ record with CONTROLLER_SCOPE in dropped: {dropped}"


# ── AC3: empty scope is parity ─────────────────────────────────────────────


def test_empty_scope_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty scope_prefixes leaves all admitted routes intact (parity with unscoped)."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    loader_scoped = m._default_live_admission_loader(tmp_path / "state", scope_prefixes=[])
    loader_unscoped = m._default_live_admission_loader(tmp_path / "state2")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state2").mkdir(parents=True, exist_ok=True)

    admitted_scoped = loader_scoped(NOW)
    admitted_unscoped = loader_unscoped(NOW)

    assert admitted_scoped.ids == admitted_unscoped.ids


# ── AC4: fail-closed when scope matches nothing ────────────────────────────


def test_scope_with_no_matching_route_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If scope_prefixes is non-empty but no admitted route matches, the loader
    raises AdmissionUnavailableError with reason controller_scope_empty instead
    of falling back to an unscoped result."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    loader = m._default_live_admission_loader(tmp_path / "state", scope_prefixes=["nonexistent/"])
    with pytest.raises(adm.AdmissionUnavailableError) as exc_info:
        loader(NOW)

    err = exc_info.value
    assert err.reason == "controller_scope_empty"
    assert "nonexistent/" in err.detail
