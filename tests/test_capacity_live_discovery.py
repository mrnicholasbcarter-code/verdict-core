"""BOD-129 live local discovery — offline probes (no network, no secrets)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from verdict.capacity_adapters import default_registry, discover_local_adapters
from verdict.capacity_live import probe_local_capacity_sources
from verdict.capacity_models import CapacityFailureClass, CapacitySignal


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")


def test_default_registry_stays_empty_without_include_local() -> None:
    assert default_registry().list_adapters() == ()


def test_discover_local_adapters_from_fake_home(tmp_path: Path) -> None:
    _write(
        tmp_path / ".codex" / "auth.json",
        {"auth_mode": "chatgpt", "tokens": {"account_id": "acct-test-123", "access_token": "tok"}},
    )
    _write(
        tmp_path / ".cursor" / "cli-config.json",
        {"authInfo": {"userId": "user-abc", "authId": "auth-1"}},
    )
    _write(tmp_path / ".hermes" / "auth.json", {"credential_pool": {"openrouter": [{"id": "1"}]}})
    _write(tmp_path / ".hermes" / ".env", "OPENROUTER_API_KEY=sk-test-not-printed\n")
    _write(
        tmp_path / ".config" / "verdict" / "capacity.json",
        {
            "contract_version": "1",
            "provider_id": "xai",
            "account_id": "grok-test",
            "pools": [
                {
                    "pool_id": "grok-shared",
                    "status": "available",
                    "unit": "credits",
                    "remaining_pct": 70.0,
                    "shared_pool_id": "grok-super-shared",
                }
            ],
            "balances": [],
        },
    )

    bins = {
        "codex": "/usr/bin/codex",
        "claude": "/usr/bin/claude",
        "cursor-agent": "/usr/bin/cursor-agent",
        "hermes": "/usr/bin/hermes",
    }

    def fake_which(name: str) -> str | None:
        return bins.get(name)

    def fake_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 3 and argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                argv, 0, '{"loggedIn": false, "authMethod": "none"}', ""
            )
        if len(argv) >= 2 and argv[1] == "status":
            return subprocess.CompletedProcess(argv, 0, "Logged in", "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    adapters = discover_local_adapters(
        home=tmp_path, env={}, path_resolver=fake_which, runner=fake_runner, include_omniroute=False
    )
    ids = {adapter.adapter_id for adapter in adapters}
    assert "direct.codex.live" in ids
    assert "direct.claude.live" in ids
    assert "direct.cursor.live" in ids
    assert "harness.hermes.live" in ids
    assert "gateway.openrouter.live" in ids
    assert "aggregator.json.live" in ids

    by_id = {adapter.adapter_id: adapter for adapter in adapters}
    codex = by_id["direct.codex.live"]
    assert len(codex.discover()) == 1
    assert codex.discover()[0].account_id.startswith("sha12-")
    assert "tok" not in json.dumps(codex.diagnose().to_dict())
    assert "sk-test" not in json.dumps(codex.diagnose().to_dict())

    claude = by_id["direct.claude.live"]
    assert claude.diagnose().status == "needs_owner"
    assert claude.discover() == ()

    cursor = by_id["direct.cursor.live"]
    assert cursor.discover()
    assert cursor.diagnose().details["auth"] == "yes"

    openrouter = by_id["gateway.openrouter.live"]
    assert openrouter.diagnose().details["auth"] == "yes"
    assert openrouter.discover()

    snap = by_id["aggregator.json.live"].observe_capacity()
    assert snap.pools
    assert snap.pools[0].shared_pool_id == "grok-super-shared"

    unsupported = codex.observe_capacity()
    assert unsupported.errors
    assert unsupported.errors[0].failure_class is CapacityFailureClass.UNSUPPORTED
    assert unsupported.pools == ()


def test_include_local_registry_registers_discovered(tmp_path: Path) -> None:
    _write(
        tmp_path / ".codex" / "auth.json",
        {"auth_mode": "chatgpt", "tokens": {"account_id": "acct-z", "access_token": "x"}},
    )

    def fake_which(name: str) -> str | None:
        return "/bin/codex" if name == "codex" else None

    adapters = discover_local_adapters(
        home=tmp_path, env={}, path_resolver=fake_which, include_omniroute=False
    )
    registry = default_registry(adapters)
    assert "direct.codex.live" in registry.list_adapters()
    assert registry.capabilities("direct.codex.live")[CapacitySignal.HEALTH] is True


def test_probe_matrix_marks_absent_litellm(tmp_path: Path) -> None:
    probes = probe_local_capacity_sources(
        home=tmp_path, env={}, path_resolver=lambda _name: None, include_omniroute=False
    )
    by_id = {row.adapter_id: row for row in probes}
    assert by_id["gateway.litellm.live"].qualification in {"ABSENT", "UNSUPPORTED"}
    assert by_id["gateway.litellm.live"].installed is False
    # Matrix serialization must stay secret-free / JSON-safe.
    assert all("sk-" not in json.dumps(row.to_dict()) for row in probes)


def test_omniroute_optional_gateway(tmp_path: Path) -> None:
    (tmp_path / ".omniroute").mkdir()

    def fake_runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "status" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                '{"gateway":"healthy","providers":{"configured":2,"active":2,"healthy":2}}',
                "",
            )
        return subprocess.CompletedProcess(argv, 0, '{"error":"No quota data"}', "")

    adapters = discover_local_adapters(
        home=tmp_path,
        env={},
        path_resolver=lambda name: "/bin/omniroute" if name == "omniroute" else None,
        runner=fake_runner,
        include_omniroute=True,
    )
    omni = next(adapter for adapter in adapters if adapter.adapter_id == "gateway.omniroute.live")
    report = omni.diagnose()
    assert report.details["gateway"] == "healthy"
    assert report.details["note"] == "evidence_only_not_routing"
    snap = omni.observe_capacity()
    assert snap.errors
    assert snap.errors[0].failure_class is CapacityFailureClass.UNSUPPORTED
    assert snap.health == "healthy"
    assert snap.pools == ()
