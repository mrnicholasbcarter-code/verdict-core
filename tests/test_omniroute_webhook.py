from __future__ import annotations

import hashlib
import hmac
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from verdict import omniroute_webhook as wh

NOW = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)
SECRET = "whsec_" + "ab" * 24
RECEIVER = "https://verdict.example.test/v1/omniroute/webhook"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    return tmp_path / ".verdict"


def _configure(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / wh.CONFIG_FILENAME).write_text(
        json.dumps({"webhook_id": "wh-1", "url": RECEIVER, "secret": SECRET}), encoding="utf-8"
    )


def _delivery(
    event: str = "request.completed",
    *,
    data: dict[str, object] | None = None,
    at: datetime = NOW - timedelta(seconds=5),
    secret: str = SECRET,
) -> tuple[bytes, dict[str, str]]:
    payload = {
        "event": event,
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "data": data
        if data is not None
        else {"combo": "c", "provider": "openai", "model": "gpt-x", "latencyMs": 812},
    }
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Webhook-Event": event, "X-Webhook-Signature": signature}


def test_valid_signature_is_stored(_home: Path) -> None:
    _configure(_home)
    raw, headers = _delivery()

    assert wh.accept_delivery(raw, headers, now=NOW) == (
        200,
        {"accepted": True, "reason": "stored"},
    )

    [event] = wh.read_events()
    assert (event.event, event.provider, event.model, event.latency_ms) == (
        "request.completed",
        "openai",
        "gpt-x",
        812.0,
    )
    assert event.digest == hashlib.sha256(raw).hexdigest()
    assert stat.S_IMODE((_home / wh.EVENTS_FILENAME).stat().st_mode) == 0o600


@pytest.mark.parametrize("configured", [True, False])
def test_bad_or_missing_signature_is_401(_home: Path, configured: bool) -> None:
    if configured:
        _configure(_home)
    raw, headers = _delivery(secret="wrong")

    assert wh.accept_delivery(raw, headers, now=NOW)[0] == 401
    assert wh.accept_delivery(raw, {}, now=NOW)[0] == 401
    assert wh.read_events() == []


def test_duplicate_delivery_is_not_stored_twice(_home: Path) -> None:
    _configure(_home)
    raw, headers = _delivery()
    wh.accept_delivery(raw, headers, now=NOW)

    assert wh.accept_delivery(raw, headers, now=NOW) == (
        200,
        {"accepted": False, "reason": "duplicate"},
    )
    assert len(wh.read_events()) == 1


def test_unattributable_failure_is_stored_without_provider(_home: Path) -> None:
    _configure(_home)
    raw, headers = _delivery(
        "request.failed", data={"combo": "c", "reason": "all failed", "latencyMs": 40}
    )

    assert wh.accept_delivery(raw, headers, now=NOW)[1]["accepted"] is True
    [event] = wh.read_events()
    assert (event.event, event.provider, event.model) == ("request.failed", None, None)


def test_ping_is_accepted_but_not_stored_and_bad_json_is_400(_home: Path) -> None:
    _configure(_home)
    raw, headers = _delivery("test.ping", data={})
    assert wh.accept_delivery(raw, headers, now=NOW) == (
        200,
        {"accepted": True, "reason": "not_stored"},
    )
    bad = b"{not json"
    signature = "sha256=" + hmac.new(SECRET.encode(), bad, hashlib.sha256).hexdigest()
    assert wh.accept_delivery(bad, {"X-Webhook-Signature": signature}, now=NOW)[0] == 400
    assert wh.read_events() == []


def test_future_timestamp_is_rejected(_home: Path) -> None:
    _configure(_home)
    raw, headers = _delivery(at=NOW + timedelta(seconds=301))

    assert wh.accept_delivery(raw, headers, now=NOW)[1] == {
        "accepted": False,
        "reason": "future_timestamp",
    }
    assert wh.read_events() == []


def test_event_log_keeps_newest_lines(_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(_home)
    monkeypatch.setattr(wh, "MAX_EVENT_LINES", 3)
    for offset in range(5):
        raw, headers = _delivery(at=NOW - timedelta(minutes=10 - offset))
        wh.accept_delivery(raw, headers, now=NOW)

    events = wh.read_events()
    assert [event.timestamp for event in events] == [
        NOW - timedelta(minutes=8),
        NOW - timedelta(minutes=7),
        NOW - timedelta(minutes=6),
    ]


def test_read_events_since_skips_older_and_malformed(_home: Path) -> None:
    _configure(_home)
    for minutes in (30, 5):
        raw, headers = _delivery(at=NOW - timedelta(minutes=minutes))
        wh.accept_delivery(raw, headers, now=NOW)
    with (_home / wh.EVENTS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write("garbage\n{}\n")

    assert len(wh.read_events()) == 2
    [recent] = wh.read_events(since=NOW - timedelta(minutes=10))
    assert recent.timestamp == NOW - timedelta(minutes=5)
    status = wh.webhook_status(now=NOW)
    assert status["configured"] is True
    assert status["events_total"] == 2
    assert status["receiver_path"] == wh.RECEIVER_PATH


class _FakeOmniRoute:
    def __init__(self, *, valid: dict[str, object], webhooks: list[dict[str, object]]) -> None:
        self.valid = valid
        self.webhooks = webhooks
        self.created: list[dict[str, object]] = []
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer mgmt"
        if request.url.path == "/api/webhooks/validate-url":
            return httpx.Response(200, json=self.valid)
        if request.method == "GET" and request.url.path == "/api/webhooks":
            return httpx.Response(200, json={"webhooks": self.webhooks})
        if request.method == "POST" and request.url.path == "/api/webhooks":
            body = json.loads(request.content)
            self.created.append(body)
            return httpx.Response(201, json={"webhook": {"id": "wh-new", "url": body["url"]}})
        return httpx.Response(404)

    def register(self, *, dry_run: bool = False) -> dict[str, object]:
        return wh.register_webhook(
            origin="http://omniroute.test/v1",
            management_token="mgmt",
            url=RECEIVER,
            dry_run=dry_run,
            transport=httpx.MockTransport(self),
        )


def test_register_blocked_private_writes_no_secret_and_creates_nothing(_home: Path) -> None:
    fake = _FakeOmniRoute(valid={"valid": False, "reason": "blocked_private"}, webhooks=[])

    result = fake.register()

    assert (result["status"], result["reason"]) == ("blocked", "blocked_private")
    assert ("POST", "/api/webhooks") not in fake.calls
    stored = json.loads((_home / wh.CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert "secret" not in stored
    assert stored["registration"]["reason"] == "blocked_private"
    assert wh.webhook_status()["configured"] is False


def test_register_success_persists_id_and_private_secret(_home: Path) -> None:
    fake = _FakeOmniRoute(valid={"valid": True}, webhooks=[])

    result = fake.register()

    assert result["status"] == "registered"
    assert "secret" not in json.dumps(result)
    [created] = fake.created
    assert created["events"] == ["request.completed", "request.failed"]
    path = _home / wh.CONFIG_FILENAME
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["webhook_id"] == "wh-new"
    assert stored["secret"] == created["secret"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert wh.webhook_status()["configured"] is True


def test_register_existing_url_without_local_secret_is_reported(_home: Path) -> None:
    fake = _FakeOmniRoute(
        valid={"valid": True},
        webhooks=[{"id": "wh-other", "url": RECEIVER, "secret": "whsec_ab..."}],
    )

    assert fake.register()["status"] == "exists_unknown_secret"
    assert fake.created == []


def test_register_dry_run_creates_and_writes_nothing(_home: Path) -> None:
    fake = _FakeOmniRoute(valid={"valid": True}, webhooks=[])

    assert fake.register(dry_run=True)["status"] == "would_register"
    assert fake.created == []
    assert not (_home / wh.CONFIG_FILENAME).exists()


def test_api_route_accepts_signed_delivery_without_bearer(
    _home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "server-token")
    _configure(_home)
    raw, headers = _delivery(at=datetime.now(timezone.utc) - timedelta(seconds=1))

    with TestClient(api.app) as client:
        response = client.post(wh.RECEIVER_PATH, content=raw, headers=headers)
        unsigned = client.post(wh.RECEIVER_PATH, content=raw)

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": "stored"}
    assert unsigned.status_code == 401
    assert len(wh.read_events()) == 1
