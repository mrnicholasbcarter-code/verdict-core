"""BOD-219: `verdict doctor` validates the management token serve will resolve.

One bounded read-only GET of the management route serve reads (connections) with the
resolved token. Rows name the token's source, never its value.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

import verdict.credentials_store as credentials_store
import verdict.doctor_diagnostics as dd
from verdict.credentials_store import CredentialsStore
from verdict.doctor_presentation import repair_command

SECRET = "oma_doctor-management-sentinel"
GATEWAY = "http://127.0.0.1:20128/v1"
REPAIR = "verdict credentials set OMNIROUTE_MANAGEMENT_TOKEN --stdin"


class _Response(io.BytesIO):
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@pytest.fixture(autouse=True)
def _offline(no_gateway_network: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may open a socket; provenance starts empty for each test."""
    monkeypatch.setattr(credentials_store, "_STORE_INJECTED", {}, raising=False)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CredentialsStore:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN", raising=False)
    return CredentialsStore()


def _http(code: int) -> HTTPError:
    return HTTPError("http://x/api/resilience/connections", code, "x", {}, None)  # type: ignore[arg-type]


def _run(
    monkeypatch: pytest.MonkeyPatch,
    outcome: Any,
    *,
    status: int = 200,
    gateway: str | None = GATEWAY,
) -> tuple[dd.DoctorDiagnostics, list[tuple[str, str | None, float]]]:
    seen: list[tuple[str, str | None, float]] = []

    def opener(request: Any, *, timeout: float) -> _Response:
        seen.append((request.full_url, request.get_header("Authorization"), timeout))
        assert request.get_method() == "GET" and request.data is None
        if isinstance(outcome, Exception):
            raise outcome
        response = _Response(json.dumps(outcome).encode())
        response.status = status
        return response

    monkeypatch.setattr(dd, "open_no_redirect", opener)
    diag = dd.DoctorDiagnostics()
    dd._doctor_management_token(diag, gateway)
    return diag, seen


def _rendered(diag: dd.DoctorDiagnostics) -> str:
    return json.dumps({"sections": diag.sections, "issues": diag.issues, "warnings": diag.warnings})


def test_absent_token_is_info_health_only_and_makes_no_request(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "  ")  # blank is absent
    diag, seen = _run(monkeypatch, {"connections": []})
    assert seen == []
    assert diag.issues == [] and diag.warnings == []
    [(label, state, detail)] = diag.sections
    assert (label, state) == ("Management token", "note")
    assert "runtime sources: health only" in detail
    assert REPAIR in detail and "oma_" in detail and "manage-scope API key" in detail


def test_valid_environment_token_is_ok_with_one_bounded_get(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", "store-loses")
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", f" {SECRET} ")
    diag, seen = _run(monkeypatch, {"connections": []})
    assert seen == [("http://127.0.0.1:20128/api/resilience/connections", f"Bearer {SECRET}", 5.0)]
    assert diag.issues == [] and diag.warnings == []
    [(label, state, detail)] = diag.sections
    assert (label, state) == ("Management token", "ok")
    assert "source: environment" in detail and "connections" in detail


def test_valid_store_token_names_the_credential_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    diag, seen = _run(monkeypatch, {"connections": []})
    assert [auth for _url, auth, _timeout in seen] == [f"Bearer {SECRET}"]
    [(_label, state, detail)] = diag.sections
    assert state == "ok" and "source: credential_store" in detail


def test_store_value_copied_into_the_environment_is_still_the_credential_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI entry runs ``load_into_env`` before doctor; the source stays the store."""
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    store.load_into_env()
    diag, _ = _run(monkeypatch, {"connections": []})
    [(_label, state, detail)] = diag.sections
    assert state == "ok" and "source: credential_store" in detail


def test_exported_token_equal_to_the_stored_value_is_the_environment(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provenance is what load_into_env injected, never value equality."""
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    diag, _ = _run(monkeypatch, _http(401))
    [(_label, state, detail)] = diag.sections
    assert state == "failed" and "source: environment" in detail
    [issue] = diag.issues
    assert "from the environment" in issue and "overrides the credential store" in issue


@pytest.mark.parametrize(("code", "word"), [(401, "rejected"), (403, "forbidden")])
def test_rejected_or_forbidden_token_is_a_failure_with_source_and_repair(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, code: int, word: str
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    diag, seen = _run(monkeypatch, _http(code))
    assert len(seen) == 1
    assert diag.warnings == []
    [(label, state, detail)] = diag.sections
    assert (label, state) == ("Management token", "failed")
    assert f"HTTP {code}" in detail and word in detail and "source: credential_store" in detail
    [issue] = diag.issues
    assert f"HTTP {code}" in issue and "credential store" in issue and "fails closed" in issue
    assert REPAIR in issue and "oma_" in issue and "manage-scope API key" in issue
    # The rendered next step is the exact repair, not a generic gateway hint.
    assert repair_command(f"ISSUE failed {issue}") == REPAIR
    assert repair_command(f"{label} {state} {detail}") == REPAIR


def test_rejected_environment_token_says_the_export_overrides_the_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    diag, _ = _run(monkeypatch, _http(401))
    [(_label, _state, detail)] = diag.sections
    assert "source: environment" in detail
    [issue] = diag.issues
    assert "environment" in issue and "overrides the credential store" in issue


@pytest.mark.parametrize(
    "outcome", [URLError("down"), TimeoutError("timed out"), _http(404), _http(429), _http(500)]
)
def test_unverifiable_token_is_a_single_attempt_warning_never_ok(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, outcome: Exception
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    diag, seen = _run(monkeypatch, outcome)
    assert len(seen) == 1  # no retry
    assert diag.issues == [] and len(diag.warnings) == 1
    [(label, state, detail)] = diag.sections
    assert (label, state) == ("Management token", "warn")
    assert "not verified" in detail and "source: credential_store" in detail


@pytest.mark.parametrize(("payload", "status"), [("not-json", 200), ({"connections": []}, 203)])
def test_unexpected_success_is_never_ok(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, payload: Any, status: int
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)

    def opener(request: Any, *, timeout: float) -> _Response:
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        response = _Response(body)
        response.status = status
        return response

    monkeypatch.setattr(dd, "open_no_redirect", opener)
    diag = dd.DoctorDiagnostics()
    dd._doctor_management_token(diag, GATEWAY)
    assert diag.issues == [] and len(diag.warnings) == 1
    assert diag.sections[0][:2] == ("Management token", "warn")


def test_token_without_a_gateway_is_unverified_without_a_request(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    diag, seen = _run(monkeypatch, {"connections": []}, gateway=None)
    assert seen == [] and diag.issues == [] and len(diag.warnings) == 1


def test_header_injection_token_is_refused_by_the_real_opener_before_any_socket(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``open_no_redirect`` runs; ``no_gateway_network`` makes any connect fail loudly.

    The token is exported (the credential store's line format cannot carry CR/LF).
    http.client validates header values before it connects, so the CR/LF token is
    refused with ``ValueError`` (whose text would echo the value) and doctor reports
    only the category.
    """
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "oma_secret\r\nX-Injected: 1")
    diag = dd.DoctorDiagnostics()
    dd._doctor_management_token(diag, GATEWAY)
    [warning] = diag.warnings
    assert warning.endswith(": ValueError")
    assert diag.sections == [
        ("Management token", "warn", "not verified: ValueError (source: environment)")
    ]
    assert "oma_secret" not in _rendered(diag) and "X-Injected" not in _rendered(diag)


_OUTCOMES = {
    "absent": None,
    "ok": {"connections": []},
    "warning": _http(500),
    "unreachable": URLError("down"),
    "401": _http(401),
    "403": _http(403),
}


@pytest.mark.parametrize("case", list(_OUTCOMES))
def test_token_value_never_appears_in_doctor_text_or_json(
    store: CredentialsStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    from verdict import cli
    from verdict.actions.registry import run_action

    outcome = _OUTCOMES[case]
    if outcome is not None:
        store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)
    else:
        store.set("UNRELATED", "x")  # a store exists but holds no management token

    def opener(request: Any, *, timeout: float) -> _Response:
        if isinstance(outcome, Exception):
            raise outcome
        return _Response(json.dumps(outcome).encode())

    def collect(*_args: Any, **_kwargs: Any) -> dd.DoctorDiagnostics:
        diag = dd.DoctorDiagnostics()
        dd._doctor_management_token(diag, GATEWAY)
        return diag

    monkeypatch.setattr(dd, "open_no_redirect", opener)
    monkeypatch.setattr(dd, "_collect_doctor_diagnostics", collect)
    failing = case in {"401", "403"}
    expected_state = {"absent": "note", "ok": "ok", "401": "failed", "403": "failed"}

    result = run_action("doctor", {"fix": False, "preflight_timeout": 1.0})
    as_json = json.dumps(result.data, default=str)
    [row] = [s for s in result.data["sections"] if s["label"] == "Management token"]
    assert row["state"] == expected_state.get(case, "warn")
    assert result.ok is not failing
    assert "doctor-management-sentinel" not in as_json

    # TEXT: the same rows through TerminalUI.doctor_finding / doctor_summary and the
    # doctor_presentation repair hints, exactly as `verdict doctor` prints them.
    if failing:
        with pytest.raises(SystemExit):
            cli.cmd_doctor()
    else:
        cli.cmd_doctor()
    text = capsys.readouterr().out
    assert "Management token" in text
    assert "doctor-management-sentinel" not in text
    if failing:
        assert f"Repair: {REPAIR}" in text
