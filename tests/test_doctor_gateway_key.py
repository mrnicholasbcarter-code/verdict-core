"""verdict doctor: the resolved OmniRoute key is DB-backed, never the gateway env key."""

from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

import verdict.doctor_diagnostics as dd


class _Response(io.BytesIO):
    status = 200

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _run(
    monkeypatch: pytest.MonkeyPatch, outcome: Any, token: str | None = "k", status: int = 200
) -> tuple:
    seen: list[str] = []

    def opener(request: Any, *, timeout: float) -> _Response:
        seen.append(request.full_url)
        assert request.get_header("Authorization") == f"Bearer {token}"
        if isinstance(outcome, Exception):
            raise outcome
        response = _Response(json.dumps(outcome).encode())
        response.status = status
        return response

    monkeypatch.setattr(dd, "_read_omniroute_token", lambda: token)
    monkeypatch.setattr(dd, "open_no_redirect", opener)
    diag = dd.DoctorDiagnostics()
    dd._doctor_gateway_key(diag, "http://127.0.0.1:20128/v1")
    return diag, seen


def _http(code: int) -> HTTPError:
    return HTTPError("http://x/api/v1/me/status", code, "x", {}, None)  # type: ignore[arg-type]


def test_db_backed_key_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    diag, seen = _run(monkeypatch, {"apiKey": {"id": "k1", "name": "verdict"}, "usage": {}})
    assert seen == ["http://127.0.0.1:20128/api/v1/me/status"]
    assert diag.issues == []
    assert diag.sections == [("Gateway key", "ok", "DB-backed key verdict")]


@pytest.mark.parametrize("code", [401, 403])
def test_env_key_or_refused_key_is_an_issue(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    diag, _ = _run(monkeypatch, _http(code))
    assert len(diag.issues) == 1
    assert f"HTTP {code}" in diag.issues[0] and "OMNIROUTE_API_KEY" in diag.issues[0]
    assert diag.sections[0][:2] == ("Gateway key", "failed")


def test_non_200_success_status_is_never_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    diag, _ = _run(monkeypatch, {"apiKey": {"id": "k1", "name": "verdict"}}, status=203)
    assert len(diag.issues) == 1
    assert ("Gateway key", "ok") not in [row[:2] for row in diag.sections]


def test_status_without_key_metadata_is_an_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    diag, _ = _run(monkeypatch, {"usage": {}})
    assert len(diag.issues) == 1 and "no key metadata" in diag.issues[0]


def test_gateway_without_per_key_status_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    diag, _ = _run(monkeypatch, _http(404))
    assert diag.issues == [] and diag.sections == [] and diag.warnings == []


@pytest.mark.parametrize("outcome", [URLError("down"), TimeoutError("timed out")])
def test_unverified_key_is_a_warning_never_ok(
    monkeypatch: pytest.MonkeyPatch, outcome: Exception
) -> None:
    diag, _ = _run(monkeypatch, outcome)
    assert diag.issues == [] and len(diag.warnings) == 1
    assert diag.sections[0][:2] == ("Gateway key", "warn")


def test_no_resolved_key_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    diag, seen = _run(monkeypatch, {"apiKey": {"id": "k1"}}, token=None)
    assert seen == [] and diag.issues == []
