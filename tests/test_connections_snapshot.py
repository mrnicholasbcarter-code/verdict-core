"""Restricted gateway keys use fresh credential-free local inventory evidence."""

import hashlib
import json
import stat
import sys
from datetime import datetime, timedelta, timezone

import pytest

from verdict.orchestration import run
from verdict.orchestration.contracts import OrchestrationError


def snapshot(connections, *, age=0):
    payload = {
        "schema_version": 1,
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat(),
        "connections": connections,
    }
    payload["sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def safe_rows():
    return run.sanitize_connections(
        [
            {
                "provider": "claude",
                "authType": "oauth",
                "isActive": True,
                "testStatus": "success",
                "id": "private-account",
                "backoffLevel": 0,
            }
        ]
    )


def test_snapshot_command_is_private_and_sanitized(tmp_path, monkeypatch):
    from verdict.cli import main

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "local-admin")
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(tmp_path / "unused"))
    raw = [
        {
            "provider": "claude",
            "authType": "oauth",
            "isActive": True,
            "testStatus": "success",
            "backoffLevel": 0,
            "id": "raw-connection",
            "account_id": "raw-account",
            "pool_id": "raw-pool",
            "scope_type": "account",
            "scope_id": "raw-scope",
            "name": "Personal Name",
            "email": "person@example.com",
            "apiKey": "sk-secret",
            "lastError": "429 private error person@example.com",
            "quota_window": "private window",
            "providerSpecificData": {"plan": "Personal Name person@example.com"},
        }
    ]
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return {"connections": raw}

    monkeypatch.setattr(run, "_get_json", get)
    out = tmp_path / "connections.json"
    monkeypatch.setattr(
        sys, "argv", ["verdict", "gateway", "connections-snapshot", "--out", str(out)]
    )
    main()
    assert calls == [
        ("http://127.0.0.1:20128/api/providers", {"api_key": "local-admin", "timeout": 30})
    ]
    data = json.loads(out.read_text())
    assert data["connections"] == run.sanitize_connections(raw)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert data["captured_at"]
    digest = data.pop("sha256")
    assert (
        digest
        == hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    for private in (
        "raw-connection",
        "raw-account",
        "raw-pool",
        "raw-scope",
        "Personal Name",
        "person@example.com",
        "sk-secret",
        "private error",
        "private window",
    ):
        assert private not in out.read_text()


def test_snapshot_bypasses_http(tmp_path, monkeypatch):
    rows = safe_rows()
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(snapshot(rows)))
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(path))
    monkeypatch.setattr(run, "_get_json", lambda *_a, **_k: pytest.fail("HTTP forbidden"))
    assert run.fetch_connections("https://restricted.invalid", api_key="inference") == rows


@pytest.mark.parametrize(
    "kind", ["stale", "future", "json", "digest", "schema", "row", "extra", "timestamp", "missing"]
)
def test_snapshot_rejected_without_http(kind, tmp_path, monkeypatch):
    data = snapshot(safe_rows(), age=7 if kind == "stale" else -1 if kind == "future" else 0)
    if kind in {"row", "extra"}:
        rows = safe_rows()
        rows[0]["provider" if kind == "row" else "apiKey"] = "private secret"
        data = snapshot(rows)
    if kind == "digest":
        data["sha256"] = "0" * 64
    if kind == "schema":
        data["schema_version"] = 2
    if kind == "timestamp":
        data["captured_at"] = "not-a-time"
    path = tmp_path / "connections.json"
    if kind != "missing":
        path.write_text("not json" if kind == "json" else json.dumps(data))
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(path))
    monkeypatch.setattr(run, "_get_json", lambda *_a, **_k: pytest.fail("HTTP forbidden"))
    with pytest.raises(OrchestrationError, match="connections snapshot"):
        run.fetch_connections("https://restricted.invalid", api_key="inference")


@pytest.mark.parametrize(
    "gateway",
    ["https://public-tunnel.invalid", "http://localhost@public.invalid", "ftp://127.0.0.1"],
)
def test_capture_never_sends_admin_key_to_tunnel(gateway, tmp_path, monkeypatch):
    from verdict.cli import main

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "local-admin")
    monkeypatch.setattr(run, "_get_json", lambda *_a, **_k: pytest.fail("HTTP forbidden"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verdict",
            "gateway",
            "connections-snapshot",
            "--gateway",
            gateway,
            "--out",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_without_snapshot_fetches_local_providers(monkeypatch):
    monkeypatch.delenv("VERDICT_CONNECTIONS_SNAPSHOT", raising=False)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return [{"provider": "claude", "authType": "oauth", "isActive": True}]

    monkeypatch.setattr(run, "_get_json", get)
    assert run.fetch_connections(
        "http://localhost:20128/", api_key="local-admin"
    ) == run.sanitize_connections(get("ignored"))
    assert calls[0] == (
        "http://localhost:20128/api/providers",
        {"api_key": "local-admin", "timeout": 30},
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "raw-id"),
        ("scope_id", "raw-account"),
        ("isActive", "true"),
        ("backoffLevel", -1),
        ("quota_percent", 101),
        ("authType", "private plan"),
        ("rateLimitedUntil", "2026-01-01"),
        ("plan_label", "Personal Max"),
        ("rate_limited_until", {"model email@example.com": "2026-10-10T00:00:00Z"}),
    ],
)
def test_snapshot_schema_rejects_untyped_or_raw_evidence(field, value, tmp_path, monkeypatch):
    rows = safe_rows()
    rows[0][field] = value
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(snapshot(rows)))
    monkeypatch.setenv("VERDICT_CONNECTIONS_SNAPSHOT", str(path))
    monkeypatch.setattr(run, "_get_json", lambda *_a, **_k: pytest.fail("HTTP forbidden"))
    with pytest.raises(OrchestrationError):
        run.fetch_connections("https://restricted.invalid", api_key="inference")


@pytest.mark.parametrize(
    "status",
    [
        "expired",
        "unauthorized",
        "unavailable",
        "disabled",
        "exhausted",
        "quota_exhausted",
        "banned",
    ],
)
def test_sanitizer_preserves_negative_admission_status(status):
    raw = {"provider": "claude", "authType": "oauth", "isActive": True, "testStatus": status}
    assert run.sanitize_connections([raw])[0]["testStatus"] == status


@pytest.mark.parametrize("status", ["FAILED", " expired "])
def test_sanitizer_keeps_case_insensitive_negative_status(status):
    assert (
        run.sanitize_connections([{"provider": "claude", "testStatus": status}])[0]["testStatus"]
        == status.strip().lower()
    )


def test_opaque_pool_cooldown_matches_raw_inventory_marker():
    from verdict.admission import admit

    now = datetime.now(timezone.utc)
    row = {"id": "cc/sonnet", "owned_by": "claude", "pool_id": "private-pool"}
    connections = run.sanitize_connections(
        [
            {
                "provider": "claude",
                "isActive": True,
                "authType": "oauth",
                "testStatus": "active",
                "pool_id": "private-pool",
                "scope_type": "pool",
                "scope_id": "private-pool",
                "rateLimitedUntil": (now + timedelta(hours=1)).isoformat(),
            }
        ]
    )
    assert not admit([row], connections, None, now=now, require_runtime=False).ids


@pytest.mark.parametrize("scope", [None, "user", "", "account", "pool"])
def test_unknown_scope_id_is_opaque_in_writer_and_reader(tmp_path, scope):
    from verdict.orchestration.connections_snapshot import read_connections_snapshot

    rows = run.sanitize_connections(
        [
            {
                "provider": "claude",
                "authType": "oauth",
                "isActive": True,
                "testStatus": "active",
                "scope_type": scope,
                "scope_id": "nick.smith-acct",
            }
        ]
    )
    assert rows[0]["scope_id"].startswith("conn:")
    assert "nick.smith-acct" not in json.dumps(rows)
    rows[0]["scope_id"] = "nick.smith-acct"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(snapshot(rows)))
    with pytest.raises(OrchestrationError):
        read_connections_snapshot(path)


def test_mixed_and_naive_cooldowns_do_not_disappear():
    rows = run.sanitize_connections(
        [
            {
                "provider": "codex",
                "authType": "oauth",
                "isActive": True,
                "testStatus": "active",
                "rateLimitedUntil": "2099-01-01T00:00:00",
                "providerSpecificData": {
                    "codexScopeRateLimitedUntil": {
                        "gpt-5": "2099-01-01T00:00:00Z",
                        "other": None,
                        "gpt-6": "2099-01-01T00:00:00",
                        "bad": "unparseable",
                    }
                },
            }
        ]
    )
    row = rows[0]
    assert row["rateLimitedUntil"] == "2099-01-01T00:00:00+00:00"
    assert row["rate_limited_until"]["gpt-5"] == "2099-01-01T00:00:00Z"
    assert row["rate_limited_until"]["gpt-6"] == "2099-01-01T00:00:00+00:00"
    assert row["rate_limited_until"]["bad"].startswith("9999-")
    assert row["rate_limited_until"]["other"].startswith("9999-")


@pytest.mark.parametrize(
    "field", ["email", "apiKey", "accessToken", "refreshToken", "name", "providerSpecificData"]
)
def test_reader_rejects_forbidden_fields_even_with_a_valid_digest(tmp_path, field):
    from verdict.orchestration.connections_snapshot import read_connections_snapshot

    rows = safe_rows()
    rows[0][field] = "sensitive-canary"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(snapshot(rows)))
    with pytest.raises(OrchestrationError):
        read_connections_snapshot(path)


@pytest.mark.parametrize("proxy", ["http_proxy", "HTTPS_PROXY", "ALL_PROXY"])
def test_admin_capture_never_sends_key_to_a_proxy(tmp_path, monkeypatch, proxy):
    from verdict.cli import main

    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "admin-canary")
    monkeypatch.setenv(proxy, "http://proxy.invalid:8080")
    monkeypatch.setattr(run, "_get_json", lambda *_a, **_k: pytest.fail("HTTP forbidden"))
    monkeypatch.setattr(
        sys, "argv", ["verdict", "gateway", "connections-snapshot", "--out", str(tmp_path / "out")]
    )
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not (tmp_path / "out").exists()


def test_snapshot_action_registration_kind() -> None:
    """Verify the gateway.connections-snapshot action is registered correctly as mutation."""
    from verdict.actions.registry import get_action

    action = get_action("gateway.connections-snapshot")
    assert action is not None, "Action must be registered"
    spec, _fn = action
    assert spec.kind == "mutation", "Action kind must be mutation"
    assert spec.family == "gateway/runtime"


def test_snapshot_cli_dispatches_to_action(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """CLI uses the mutation action and retains argparse's error/exit-2 contract."""
    from verdict.actions.base import ActionResult
    from verdict.cli import main

    called = []

    def fake_run_action(name, params, sink=None):
        called.append((name, params))
        return ActionResult(data={"error": "mock error"}, ok=False, exit_code=2)

    monkeypatch.setattr("verdict.actions.registry.run_action", fake_run_action)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verdict",
            "gateway",
            "connections-snapshot",
            "--gateway",
            "http://bad.invalid",
            "--out",
            "foo.json",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert called == [
        ("gateway.connections-snapshot", {"gateway": "http://bad.invalid", "out": "foo.json"})
    ]
    assert "error: mock error" in capsys.readouterr().err
