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


def test_header_injection_token_never_leaks_through_the_real_opener(
    store: CredentialsStore,
) -> None:
    secret = "oma_secret\r\nX-Injected: 1"
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", secret)
    diag = dd.DoctorDiagnostics()
    dd._doctor_management_token(diag, "http://127.0.0.1:9")  # real opener rejects the header
    assert len(diag.warnings) == 1
    assert "oma_secret" not in _rendered(diag)


@pytest.mark.parametrize(
    "outcome", [{"connections": []}, _http(401), _http(403), _http(500), URLError("down")]
)
def test_token_value_never_appears_in_doctor_text_or_json(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch, outcome: Any
) -> None:
    from verdict.actions.registry import run_action

    store.set("OMNIROUTE_MANAGEMENT_TOKEN", SECRET)

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
    result = run_action("doctor", {"fix": False, "preflight_timeout": 1.0})
    rendered = json.dumps(result.data, default=str)
    assert any(s["label"] == "Management token" for s in result.data["sections"])
    assert SECRET not in rendered and "doctor-management-sentinel" not in rendered
    assert result.ok is (not isinstance(outcome, HTTPError) or outcome.code not in (401, 403))
