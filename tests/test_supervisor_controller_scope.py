"""Supervisor controller route-scope: --scope / VERDICT_CONTROLLER_ROUTE_PREFIXES.

AC1: scoped admission restricts to matching prefixes (CONTROLLER_SCOPE stage, named reason).
AC2: out-of-scope routes appear in the receipt with named reason, receipt written even when empty.
AC3: empty scope is parity with unconstrained admission.
AC4: scope that matches nothing fails closed with controller_scope_empty; receipt written first.
AC5: --scope wins over VERDICT_CONTROLLER_ROUTE_PREFIXES; env var alone works.
AC5b: --scope "" (explicit empty) never reads env var; tokenless scope exits 2.
AC5c: injected admission loader is also wrapped with the scope boundary.
AC6: canonicalisation — "cc" == "cc/", "omniroute/cc/" matches cc/ routes; no re-admission.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import verdict.admission as adm
from tests.test_prime_supervisor import _git_init, module
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


def _fake_factory_for_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, m: Any, *, extra_argv: list[str] | None = None
) -> dict[str, Any]:
    """Set up a minimal main()-runnable supervisor with a capturing fake factory.

    Returns a dict that will be populated with the factory kwargs once main() runs.
    """
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()

    captured: dict[str, Any] = {}

    def fake_factory(**kwargs: Any) -> Any:
        import types

        captured.update(kwargs)
        return types.SimpleNamespace(
            hooks=None, artifacts=types.SimpleNamespace(last_compiled=None)
        )

    def fake_resolve(**kwargs: Any) -> Any:
        raise m.ControllerLaunchError("no_eligible_route", "test factory done")

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", fake_factory)
    monkeypatch.setattr(m, "resolve_controller_decision", fake_resolve)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")

    argv = [
        "prime_supervisor.py",
        "--repo",
        str(repo),
        "--state-dir",
        str(state),
        "--skip-identity-verify",
        "--max-restarts",
        "0",
    ] + (extra_argv or [])
    monkeypatch.setattr(sys, "argv", argv)
    captured["_repo"] = repo
    captured["_state"] = state
    return captured


# ── AC1 + AC2: scoped admission via _wrap_admission_with_scope ────────────


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
    receipt_path = state / "controller-admission-latest.json"
    assert receipt_path.exists(), "receipt must be written even when routes are scoped out"
    receipt = json.loads(receipt_path.read_text())
    disk_dropped = [r for r in receipt["candidates"] if not r["admitted"]]
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

    # Receipt is written before the raise
    receipt_path = state / "controller-admission-latest.json"
    assert receipt_path.exists(), "receipt must be written even when scope matches nothing"
    receipt = json.loads(receipt_path.read_text())
    for cand in receipt["candidates"]:
        if not cand["admitted"]:
            assert cand["first_failed_stage"] == "CONTROLLER_SCOPE", cand
            assert cand["reason"] == "outside_controller_route_prefix", cand
    dropped_ids = {r["route_id"] for r in receipt["candidates"] if not r["admitted"]}
    assert "cc/claude-opus" in dropped_ids
    assert "kr/claude-opus" in dropped_ids


# ── AC5: flag precedence drives real wiring (main()) ──────────────────────


def test_scope_flag_wins_over_env_var_via_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--scope kr/ is passed to the factory even when VERDICT_CONTROLLER_ROUTE_PREFIXES=cc/."""
    m = module()
    captured = _fake_factory_for_main(tmp_path, monkeypatch, m, extra_argv=["--scope", "kr/"])
    monkeypatch.setenv("VERDICT_CONTROLLER_ROUTE_PREFIXES", "cc/")

    # main() returns non-zero (2) when ControllerLaunchError is raised inside attempt()
    result = m.main()
    assert result != 0, f"main should fail closed; got {result}"

    assert captured.get("controller_scope_prefixes") == ("kr/",), (
        f"--scope must win over env var; got {captured.get('controller_scope_prefixes')}"
    )


def test_explicit_empty_scope_flag_ignores_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--scope '' means no scope; VERDICT_CONTROLLER_ROUTE_PREFIXES must NOT be read."""
    m = module()
    captured = _fake_factory_for_main(tmp_path, monkeypatch, m, extra_argv=["--scope", ""])
    monkeypatch.setenv("VERDICT_CONTROLLER_ROUTE_PREFIXES", "cc/")

    result = m.main()
    assert result != 0

    assert captured.get("controller_scope_prefixes") == (), (
        f"explicit empty --scope must produce empty prefixes; "
        f"got {captured.get('controller_scope_prefixes')}"
    )


def test_env_var_alone_via_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VERDICT_CONTROLLER_ROUTE_PREFIXES alone restricts controller scope via main()."""
    m = module()
    captured = _fake_factory_for_main(tmp_path, monkeypatch, m)
    monkeypatch.setenv("VERDICT_CONTROLLER_ROUTE_PREFIXES", "kr/")

    result = m.main()
    assert result != 0

    assert captured.get("controller_scope_prefixes") == ("kr/",), (
        f"env var must set prefixes; got {captured.get('controller_scope_prefixes')}"
    )


def test_tokenless_scope_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--scope ' , ' tokenizes to nothing → argparse error (exit 2)."""
    m = module()
    _fake_factory_for_main(tmp_path, monkeypatch, m, extra_argv=["--scope", " , "])
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 2, f"expected exit 2 for tokenless scope, got {exc.value.code}"


# ── AC5c: injected admission loader is also wrapped ──────────────────────


def test_injected_admission_loader_is_wrapped_with_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a caller injects an admission= kwarg, the scope still applies."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()
    state = tmp_path / "state"
    state.mkdir()
    calls: list[int] = []

    def injected_loader(when: Any) -> adm.AdmittedSet:
        calls.append(1)
        return full

    import types

    def fake_cs_bundle(**kwargs: Any) -> Any:
        return types.SimpleNamespace(
            hooks=None, artifacts=types.SimpleNamespace(last_compiled=None)
        )

    monkeypatch.setattr(m.CS, "build_production_controller_selection_bundle", fake_cs_bundle)
    service = types.SimpleNamespace(require_execution_path_authority=True)

    # Call build_production_controller_selection_bundle with an injected
    # admission and a non-empty scope; the result must be scoped.
    captured_admission: list[Any] = []

    def capturing_bundle(**kwargs: Any) -> Any:
        captured_admission.append(kwargs.get("admission"))
        return types.SimpleNamespace(
            hooks=None, artifacts=types.SimpleNamespace(last_compiled=None)
        )

    monkeypatch.setattr(m.CS, "build_production_controller_selection_bundle", capturing_bundle)

    m.build_production_controller_selection_bundle(
        repo=tmp_path,
        state_dir=state,
        intelligence_service=service,
        bind_prime_target=lambda route: None,
        controller_scope_prefixes=("kr/",),
        admission=injected_loader,
    )

    assert captured_admission, "admission kwarg must be forwarded to inner bundle"
    wrapped = captured_admission[0]
    assert callable(wrapped)
    # Calling the wrapped loader must return only kr/ routes
    admitted = wrapped(NOW)
    assert "kr/claude-opus" in admitted
    assert "cc/claude-opus" not in admitted
    assert calls, "injected loader must have been called"


# ── AC6: canonicalisation ──────────────────────────────────────────────────


def test_canonicalisation_bare_family_matches_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restrict_controller_scope("cc") and ("cc/") both match cc/... routes."""
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

    # "cc" (no slash) should match cc/claude-opus
    admitted_bare = m._default_live_admission_loader(state1, scope_prefixes=["cc"])
    result_bare = admitted_bare(NOW)
    assert "cc/claude-opus" in result_bare
    assert "kr/claude-opus" not in result_bare

    # "cc/" (with slash) is the canonical form — should behave identically
    admitted_slash = m._default_live_admission_loader(state2, scope_prefixes=["cc/"])
    result_slash = admitted_slash(NOW)
    assert result_bare.ids == result_slash.ids


def test_canonicalisation_omniroute_prefix_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope prefix "omniroute/cc/" must match a route whose canonical id is "cc/..."."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()
    full = _make_admitted()
    state = tmp_path / "state"
    state.mkdir()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return full

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    # canonical_route_id strips "omniroute/" so "omniroute/cc/" → "cc/"
    admitted = m._default_live_admission_loader(state, scope_prefixes=["omniroute/cc/"])
    result = admitted(NOW)
    assert "cc/claude-opus" in result, (
        "omniroute/ prefix should be stripped by canonicalisation, matching cc/ routes"
    )
    assert "kr/claude-opus" not in result


def test_narrowing_never_readmits_previously_excluded_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """restrict_controller_scope on an already-restricted AdmittedSet never re-admits."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    # Start with a set that already excluded cc/
    full = _make_admitted()
    already_restricted = full.restrict_controller_scope(["kr/"])
    assert "cc/claude-opus" not in already_restricted
    assert "kr/claude-opus" in already_restricted

    state = tmp_path / "state"
    state.mkdir()

    def fake_load(*a: Any, **k: Any) -> adm.AdmittedSet:
        return already_restricted

    monkeypatch.setattr(adm, "load_live_admission", fake_load)

    # Scoping again to cc/ must NOT re-admit cc/ — widen would bypass the prior exclusion
    loader = m._default_live_admission_loader(state, scope_prefixes=["cc/"])
    with pytest.raises(adm.AdmissionUnavailableError) as exc:
        loader(NOW)
    # cc/ was excluded earlier; scope ["cc/"] narrows to empty → fail closed
    assert exc.value.reason == "controller_scope_empty"
    # kr/ is still dropped because the new scope is cc/, not kr/
    receipt_path = state / "controller-admission-latest.json"
    assert receipt_path.exists()


# ── Fix-1 extra tests: whitespace-only prefixes in wrapper ────────────────


def test_wrap_with_whitespace_only_prefix_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_wrap_admission_with_scope((' ',)) and (('', ' ')) raise ValueError immediately —
    a non-empty but all-whitespace input must not silently become unscoped."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()
    state = tmp_path / "state"
    state.mkdir()

    def dummy_loader(when: Any) -> Any:  # pragma: no cover
        raise AssertionError("should never be called")

    with pytest.raises(ValueError, match="no usable prefixes"):
        m._wrap_admission_with_scope(dummy_loader, state, (" ",))

    with pytest.raises(ValueError, match="no usable prefixes"):
        m._wrap_admission_with_scope(dummy_loader, state, ("", " "))


# ── Fix-2: env-var tokenless exits 2 via main() ───────────────────────────


def test_env_var_tokenless_scope_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No --scope flag, VERDICT_CONTROLLER_ROUTE_PREFIXES=' , ' → exit 2."""
    m = module()
    _fake_factory_for_main(tmp_path, monkeypatch, m)  # no extra_argv → no --scope
    monkeypatch.setenv("VERDICT_CONTROLLER_ROUTE_PREFIXES", " , ")
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 2, f"tokenless env-var scope must exit 2; got {exc.value.code}"


# ── Fix-3a: injected loader fail-closed + receipt ─────────────────────────


def test_injected_loader_fail_closed_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wrapped injected loader: all routes out of scope → controller_scope_empty,
    receipt written with CONTROLLER_SCOPE drops before the raise."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()

    full = _make_admitted()
    state = tmp_path / "state"
    state.mkdir()

    def injected_loader(when: Any) -> adm.AdmittedSet:
        return full

    wrapped = m._wrap_admission_with_scope(injected_loader, state, ["nonexistent/"])

    with pytest.raises(adm.AdmissionUnavailableError) as exc_info:
        wrapped(NOW)

    err = exc_info.value
    assert err.reason == "controller_scope_empty"
    assert "nonexistent/" in err.detail

    receipt_path = state / "controller-admission-latest.json"
    assert receipt_path.exists(), "receipt must be written before raise"
    receipt = json.loads(receipt_path.read_text())
    dropped = [r for r in receipt["candidates"] if not r["admitted"]]
    assert all(r["first_failed_stage"] == "CONTROLLER_SCOPE" for r in dropped), dropped
    assert all(r["reason"] == "outside_controller_route_prefix" for r in dropped), dropped


# ── Fix-3b: scoped no-service/no-loader branch raises ValueError ──────────


def test_scoped_no_service_no_loader_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_production_controller_selection_bundle with controller_scope_prefixes set
    but no service and no injected admission raises ValueError immediately.

    Pass prepare_execution_request to suppress service auto-build (the function
    skips _build_intelligence_service_from_config when that kwarg is present).
    """
    m = module()

    def fake_cs_bundle(**kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("inner bundle should not be called")

    monkeypatch.setattr(m.CS, "build_production_controller_selection_bundle", fake_cs_bundle)

    # The ValueError is caught internally and re-wrapped as ControllerLaunchError.
    with pytest.raises(m.ControllerLaunchError) as exc_info:
        m.build_production_controller_selection_bundle(
            repo=tmp_path,
            state_dir=tmp_path / "state",
            # intelligence_service omitted → None; prepare_execution_request
            # present so the auto-build is skipped, keeping service=None
            prepare_execution_request=lambda *a, **k: None,
            bind_prime_target=lambda route: None,
            controller_scope_prefixes=("kr/",),
            # no admission= kwarg
        )
    assert "no IntelligenceService" in str(exc_info.value)
