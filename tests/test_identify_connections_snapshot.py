"""Restricted-key identify: the sanitized connections snapshot replaces management reads."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError

import pytest

from verdict import identify
from verdict import omniroute_evidence as module
from verdict.orchestration.contracts import OrchestrationError
from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.run import sanitize_connections

pytestmark = pytest.mark.usefixtures("no_gateway_network")

SECRET = "restricted-key-sentinel"
MANAGEMENT = ("/api/providers", "/api/resilience", "/api/usage", "/api/providers/health")
V1_MODELS = {
    "data": [
        {"id": "kc/free-model", "owned_by": "kilocode", "root": "free-model"},
        {"id": "cl/sub-model", "owned_by": "claude", "root": "sub-model"},
    ]
}
CANDIDATES = {
    "candidates": [
        {
            "provider": provider,
            "connectionId": "c-" + provider,
            "model": route,
            "modelStr": route,
            "excluded": False,
            "reachable": True,
            "breakerState": "CLOSED",
            "connectionCooldown": False,
            "modelLocked": False,
            "freeAccessExclusion": None,
        }
        for route, provider in (("kc/free-model", "kilocode"), ("cl/sub-model", "claude"))
    ]
}


def _rows():
    return sanitize_connections(
        [
            {
                "provider": "kilocode",
                "authType": "apikey",
                "isActive": True,
                "testStatus": "active",
                "backoffLevel": 0,
                "id": "raw-kilo-connection",
                "providerSpecificData": {"importFreeModelsOnly": True},
            },
            {
                "provider": "claude",
                "authType": "oauth",
                "isActive": True,
                "testStatus": "active",
                "backoffLevel": 0,
                "id": "raw-claude-connection",
            },
        ]
    )


def _write(path, rows, *, age_hours=0.0, tamper=False):
    payload = {
        "schema_version": 1,
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "connections": rows,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if tamper:
        payload["connections"] = []
    path.write_text(json.dumps(payload))
    return path


@pytest.fixture
def restricted(monkeypatch):
    """A self:usage key: inference reads answer, every management read is 401."""
    seen = []

    def transport(request, *, timeout):
        path = request.full_url.removeprefix("http://gateway")
        seen.append(path)
        if path == "/v1/models":
            return BytesIO(json.dumps(V1_MODELS).encode())
        if path == "/v1/auto-combo/auto/candidates":
            return BytesIO(json.dumps(CANDIDATES).encode())
        raise HTTPError(request.full_url, 401, "Invalid management token", {}, None)

    monkeypatch.setattr(module, "open_no_redirect", transport)
    monkeypatch.setattr(module, "resolve_api_key", lambda: SECRET)
    return seen


def _fetch():
    return module.fetch_omniroute_evidence("http://gateway/", api_key=SECRET)


def _screen(evidence, tmp_path):
    from tests.test_identify_screen import passing

    return identify.screen_models(
        evidence,
        metadata=None,
        cache=HealthCache(tmp_path / "health.json"),
        transport=passing([]),
        allow_live_probe=True,
        max_per_provider=2,
        max_routes=5,
        capacities=("free",),
    )


def test_valid_snapshot_gives_free_routes_without_management_reads(
    restricted, tmp_path, monkeypatch
):
    """Catches the preflight checking 0 routes when the key cannot read /api/*."""
    snap = _write(tmp_path / "snap.json", _rows())
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(snap))
    evidence = _fetch()
    assert not any(path.startswith(MANAGEMENT) for path in restricted)
    assert evidence.connections_source == "snapshot"
    assert evidence.route_capacities["kc/free-model"] == "free"
    assert evidence.route_capacity_evidence["kc/free-model"] == "import_free_only"
    assert evidence.route_capacities["cl/sub-model"] == "subscription"
    assert {"providers", "resilience", "limits", "cooldowns"}.isdisjoint(evidence.source_errors)
    report = _screen(evidence, tmp_path)
    assert report["routes_checked"] > 0
    assert [r["route_id"] for r in report["routes"] if r["turns_sent"]] == ["kc/free-model"]


def test_no_snapshot_restricted_key_still_fails_closed(restricted, tmp_path, monkeypatch):
    """Catches a change that grants capacity without snapshot or management evidence."""
    monkeypatch.delenv("VERDICT_CONNECTIONS_SNAPSHOT", raising=False)
    evidence = _fetch()
    assert set(evidence.route_capacities.values()) == {"unknown"}
    assert evidence.source_error_kinds["providers"] == "http_401"
    assert _screen(evidence, tmp_path)["routes_checked"] == 0


@pytest.mark.parametrize("case", ["stale", "tampered", "missing"])
def test_invalid_snapshot_is_a_named_error_not_a_fallback(restricted, tmp_path, monkeypatch, case):
    """Catches an invalid snapshot silently falling back to management reads or 'free'."""
    path = tmp_path / "snap.json"
    if case != "missing":
        _write(path, _rows(), age_hours=7 if case == "stale" else 0, tamper=case == "tampered")
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(path))
    with pytest.raises(OrchestrationError, match="Invalid connections snapshot"):
        _fetch()
    assert restricted == []


def test_snapshot_rate_limit_and_inactive_rows_are_not_usable(restricted, tmp_path, monkeypatch):
    """Catches snapshot rows bypassing the usability rules (inactive, cooling down)."""
    rows = _rows()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    rows[0]["rateLimitedUntil"] = future
    rows[1]["isActive"] = False
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(_write(tmp_path / "s.json", rows)))
    evidence = _fetch()
    assert {c.reason for c in evidence.connections} == {"cooling_down", "inactive"}
    assert set(evidence.route_capacities.values()) == {"unknown"}


def test_cli_invalid_snapshot_exits_nonzero_with_named_error(tmp_path, monkeypatch, restricted):
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(tmp_path / "absent.json"))
    monkeypatch.setattr(identify, "resolve_gateway_binding", lambda: ("http://gateway", SECRET))
    with pytest.raises(SystemExit) as error:
        identify.cmd_identify(output_json=True)
    assert "Invalid connections snapshot" in str(error.value)
    assert SECRET not in str(error.value)


def test_snapshot_connection_health_uses_test_status_not_fabricated():
    """Catches setting connectionStatus: 'healthy' for every row regardless of testStatus.

    A snapshot-derived live row must reflect the row's own testStatus; it must
    never label a connection healthy when testStatus is absent or unknown.
    Rows that pass testStatus should map to healthy; rows that do not should not.
    """
    from datetime import timezone

    from verdict.omniroute_evidence import _snapshot_sources

    now = datetime.now(timezone.utc)

    # Row with testStatus "active": maps to "healthy".
    _, resilience, _, _ = _snapshot_sources(
        [{"id": "c-active", "provider": "p", "isActive": True, "testStatus": "active"}], now
    )
    live_by_id = {r["id"]: r for r in resilience["connections"]}
    assert live_by_id["c-active"]["connectionStatus"] == "healthy"

    # Row with no testStatus: health is unknown — must NOT be "healthy".
    _, resilience, _, _ = _snapshot_sources(
        [{"id": "c-unknown", "provider": "p", "isActive": True}], now
    )
    live_by_id = {r["id"]: r for r in resilience["connections"]}
    assert live_by_id["c-unknown"]["connectionStatus"] != "healthy"


def test_snapshot_rate_limited_row_uses_passthrough_not_invented_ms_cooldown():
    """Catches int(bool)=1 inventing a 1 ms cooldown for rate-limited connections.

    A rate-limited snapshot row must pass the actual rate-limit timestamp
    through to the live row via rateLimitedUntil; cooldownRemainingMs must not
    be set to int(True)=1.
    """
    from datetime import timezone

    from verdict.omniroute_evidence import _snapshot_sources

    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    rate_limited_row = {
        "id": "c1",
        "provider": "p",
        "isActive": True,
        "testStatus": "active",
        "rateLimitedUntil": future,
    }
    _, resilience, _, _ = _snapshot_sources([rate_limited_row], now)
    live_row = resilience["connections"][0]

    # Must NOT invent a millisecond count (int(True) = 1).
    assert live_row.get("cooldownRemainingMs") != 1
    # Must pass through the actual rate-limit timestamp so _connection_reason
    # returns "cooling_down" from a real window, not a fabricated ms value.
    assert "rateLimitedUntil" in live_row
