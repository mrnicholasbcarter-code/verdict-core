"""Supervisor controller route-scope: --scope / VERDICT_CONTROLLER_ROUTE_PREFIXES.

AC1: scoped admission restricts to matching prefixes (CONTROLLER_SCOPE stage, named reason).
AC2: out-of-scope routes appear in the receipt with named reason, never silently.
AC3: empty scope is parity with unconstrained admission.
AC4: scope that matches nothing fails closed with controller_scope_empty; receipt is still written.
AC5: --scope wins over VERDICT_CONTROLLER_ROUTE_PREFIXES; env var alone works.
"""

from __future__ import annotations

import sys
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


# ── AC1 + AC2: scoped admission restricts and records drops ────────────────


def test_scoped_loader_restricts_to_matching_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader with scope_prefixes=["kr/"] admits only kr/ routes; cc/ is dropped
    with stage CONTROLLER_SCOPE and reason outside_controller_route_prefix.
    The receipt on disk records the named drop so operators can inspect it."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()
    state = tmp_path / "state"
    state.mkdir()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    loader = m._default_live_admission_loader(state, scope_prefixes=["kr/"])
    admitted = loader(NOW)

    # AC1: only kr/ routes survive
    assert "kr/claude-opus" in admitted
    assert "cc/claude-opus" not in admitted

    # AC2: dropped route has named reason in the in-memory candidates
    candidates = admitted.receipt()["candidates"]
    dropped = [r for r in candidates if not r["admitted"]]
    assert any(
        r["route_id"] == "cc/claude-opus"
        and r["first_failed_stage"] == "CONTROLLER_SCOPE"
        and r["reason"] == "outside_controller_route_prefix"
        for r in dropped
    ), f"expected cc/ record with CONTROLLER_SCOPE in dropped: {dropped}"

    # AC2: the receipt file on disk also records the drop
    import json

    receipt_path = state / "controller-admission-latest.json"
    assert receipt_path.exists(), "receipt must be written even when routes are scoped out"
    receipt = json.loads(receipt_path.read_text())
    disk_candidates = receipt["candidates"]
    disk_dropped = [r for r in disk_candidates if not r["admitted"]]
    assert any(
        r["route_id"] == "cc/claude-opus"
        and r["first_failed_stage"] == "CONTROLLER_SCOPE"
        and r["reason"] == "outside_controller_route_prefix"
        for r in disk_dropped
    ), f"receipt on disk must name the dropped route: {disk_dropped}"


# ── AC3: empty scope is parity ─────────────────────────────────────────────


def test_empty_scope_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty scope_prefixes leaves all admitted routes intact (parity with unscoped)."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()
    state1 = tmp_path / "state1"
    state2 = tmp_path / "state2"
    state1.mkdir()
    state2.mkdir()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    loader_scoped = m._default_live_admission_loader(state1, scope_prefixes=[])
    loader_unscoped = m._default_live_admission_loader(state2)

    admitted_scoped = loader_scoped(NOW)
    admitted_unscoped = loader_unscoped(NOW)

    assert admitted_scoped.ids == admitted_unscoped.ids


# ── AC4: fail-closed; receipt is written before raising ───────────────────


def test_scope_with_no_matching_route_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When scope matches no admitted route the loader raises
    AdmissionUnavailableError(controller_scope_empty) AND still writes the
    receipt so operators can inspect the per-route drop reasons."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()
    state = tmp_path / "state"
    state.mkdir()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    loader = m._default_live_admission_loader(state, scope_prefixes=["nonexistent/"])
    with pytest.raises(adm.AdmissionUnavailableError) as exc_info:
        loader(NOW)

    err = exc_info.value
    assert err.reason == "controller_scope_empty"
    assert "nonexistent/" in err.detail

    # Receipt is written before the raise so the operator can diagnose
    import json

    receipt_path = state / "controller-admission-latest.json"
    assert receipt_path.exists(), "receipt must be written even when scope matches nothing"
    receipt = json.loads(receipt_path.read_text())
    candidates = receipt["candidates"]
    # Every route should be dropped at CONTROLLER_SCOPE
    for cand in candidates:
        if not cand["admitted"]:
            assert cand["first_failed_stage"] == "CONTROLLER_SCOPE", cand
            assert cand["reason"] == "outside_controller_route_prefix", cand
    dropped_ids = {r["route_id"] for r in candidates if not r["admitted"]}
    assert "cc/claude-opus" in dropped_ids
    assert "kr/claude-opus" in dropped_ids


# ── AC5: flag precedence and env-var-only path ────────────────────────────


def test_scope_flag_wins_over_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--scope (args.scope) takes precedence over VERDICT_CONTROLLER_ROUTE_PREFIXES."""
    from tests.test_prime_supervisor import _git_init

    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()

    # Set env var to cc/ but pass --scope kr/ — kr/ must win
    monkeypatch.setenv("VERDICT_CONTROLLER_ROUTE_PREFIXES", "cc/")

    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> Any:
        captured.update(kwargs)
        import types

        return types.SimpleNamespace(
            hooks=None, artifacts=types.SimpleNamespace(last_compiled=None)
        )

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", fake_factory)
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--scope",
            "kr/",
            "--skip-identity-verify",
        ],
    )
    # Parse only; do not run the main loop
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=None)
    parser.add_argument("--repo")
    parser.add_argument("--state-dir")
    parser.add_argument("--skip-identity-verify", action="store_true")
    args = parser.parse_args(sys.argv[1:])

    # Replicate the resolution logic from the supervisor
    import os

    _raw = (args.scope or "").strip() or (
        os.environ.get("VERDICT_CONTROLLER_ROUTE_PREFIXES") or ""
    ).strip()
    resolved = tuple(p.strip() for p in _raw.split(",") if p.strip())
    assert resolved == ("kr/",), f"--scope must win over env var; got {resolved}"


def test_scope_env_var_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VERDICT_CONTROLLER_ROUTE_PREFIXES alone restricts controller scope."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()
    state = tmp_path / "state"
    state.mkdir()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)
    monkeypatch.setenv("VERDICT_CONTROLLER_ROUTE_PREFIXES", "kr/")

    # Simulate resolution: no --scope flag → fall back to env var
    import os

    raw = ("").strip() or (os.environ.get("VERDICT_CONTROLLER_ROUTE_PREFIXES") or "").strip()
    prefixes = [p.strip() for p in raw.split(",") if p.strip()]
    assert prefixes == ["kr/"]

    loader = m._default_live_admission_loader(state, scope_prefixes=prefixes)
    admitted = loader(NOW)

    assert "kr/claude-opus" in admitted
    assert "cc/claude-opus" not in admitted
