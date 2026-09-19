"""Tests for capability bootstrap staged installer DX (BOD-124)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict.capability_bootstrap import (
    BootstrapMode,
    BootstrapScope,
    DiscoveredProvider,
    ProviderKind,
    ProviderLifecycle,
    run_bootstrap,
)
from verdict.setup_plan import build_setup_plan


def _provider(
    provider_id: str,
    kind: ProviderKind,
    capabilities: frozenset[str],
    *,
    lifecycle: ProviderLifecycle = ProviderLifecycle.NOT_INSTALLED,
    health_state: str = "unknown",
    auth_state: str = "unknown",
    qualification_state: str = "unqualified",
    install_source: str = "",
    path_or_endpoint: str | None = None,
    failure_reason: str | None = None,
    managed_by_verdict: bool = False,
) -> DiscoveredProvider:
    return DiscoveredProvider(
        provider_id=provider_id,
        provider_kind=kind,
        capabilities=capabilities,
        lifecycle=lifecycle,
        health_state=health_state,
        auth_state=auth_state,
        qualification_state=qualification_state,
        install_source=install_source,
        path_or_endpoint=path_or_endpoint,
        failure_reason=failure_reason,
        managed_by_verdict=managed_by_verdict,
    )


def _clean_machine() -> tuple[DiscoveredProvider, ...]:
    """Fresh VPS: only Verdict-native structural provider present."""
    return (
        _provider(
            "native.verdict.ast",
            ProviderKind.INTELLIGENCE,
            frozenset({"code.symbols", "code.graph"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="not_required",
            qualification_state="qualified",
        ),
    )


def _full_reuse_machine() -> tuple[DiscoveredProvider, ...]:
    return (
        _provider(
            "harness.claude_code",
            ProviderKind.HARNESS,
            frozenset({"harness.execute"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="authenticated",
            qualification_state="qualified",
            path_or_endpoint="/usr/bin/claude",
        ),
        _provider(
            "harness.codex",
            ProviderKind.HARNESS,
            frozenset({"harness.execute"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="authenticated",
            qualification_state="qualified",
            path_or_endpoint="/usr/bin/codex",
        ),
        _provider(
            "gateway.omniroute",
            ProviderKind.GATEWAY,
            frozenset({"gateway.inventory", "gateway.execute"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="authenticated",
            qualification_state="qualified",
            path_or_endpoint="http://127.0.0.1:20128/v1",
        ),
        _provider(
            "adapter.serena_lsp",
            ProviderKind.INTELLIGENCE,
            frozenset({"code.symbols", "code.definitions", "code.references"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="not_required",
            qualification_state="qualified",
        ),
        _provider(
            "native.verdict.ast",
            ProviderKind.INTELLIGENCE,
            frozenset({"code.symbols", "code.graph"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="not_required",
            qualification_state="qualified",
        ),
    )


def _unhealthy_gateway_machine() -> tuple[DiscoveredProvider, ...]:
    return (
        _provider(
            "gateway.omniroute",
            ProviderKind.GATEWAY,
            frozenset({"gateway.inventory", "gateway.execute"}),
            lifecycle=ProviderLifecycle.INSTALLED,
            health_state="unhealthy",
            auth_state="unknown",
            qualification_state="unqualified",
            path_or_endpoint="http://127.0.0.1:20128/v1",
            failure_reason="health probe failed",
        ),
        _provider(
            "native.verdict.ast",
            ProviderKind.INTELLIGENCE,
            frozenset({"code.symbols", "code.graph"}),
            lifecycle=ProviderLifecycle.HEALTHY,
            health_state="healthy",
            auth_state="not_required",
            qualification_state="qualified",
        ),
    )


def test_presence_is_not_healthy_or_qualified() -> None:
    installed = _provider(
        "harness.hermes",
        ProviderKind.HARNESS,
        frozenset({"harness.execute"}),
        lifecycle=ProviderLifecycle.INSTALLED,
        health_state="unknown",
        auth_state="unknown",
        qualification_state="unqualified",
        path_or_endpoint="/usr/local/bin/hermes",
    )
    report = run_bootstrap(
        providers=installed, mode=BootstrapMode.PLAN, non_interactive=True
    ).to_dict()
    provider = next(item for item in report["providers"] if item["provider_id"] == "harness.hermes")
    assert provider["lifecycle"] == "installed"
    assert provider["health_state"] != "healthy"
    assert provider["qualification_state"] != "qualified"
    assert provider["path_or_endpoint"]


def test_clean_machine_recommends_enrichments_capabilities_first() -> None:
    report = run_bootstrap(
        providers=_clean_machine(), mode=BootstrapMode.RECOMMENDED, non_interactive=True
    ).to_dict()

    assert report["mutation_free"] is True
    assert [stage["stage"] for stage in report["stages"]][:4] == [
        "discover",
        "normalize",
        "recommend",
        "preflight",
    ]
    recs = report["recommendations"]
    assert recs, "expected capability recommendations"
    assert all("capability_id" in item for item in recs)
    # Capabilities listed before provider candidates in each recommendation.
    for item in recs:
        keys = list(item.keys())
        assert keys.index("capability_id") < keys.index("candidate_providers")
    capability_ids = {item["capability_id"] for item in recs}
    assert "gateway.inventory" in capability_ids or "gateway.execute" in capability_ids
    assert any("omniroute" in provider for item in recs for provider in item["candidate_providers"])
    # OmniRoute must never be a hard dependency.
    assert all(item["hard_dependency"] is False for item in recs)
    plan_actions = report["plan"]["actions"]
    assert any(action["requires_consent"] for action in plan_actions)


def test_full_machine_reuses_without_duplicate_install() -> None:
    report = run_bootstrap(
        providers=_full_reuse_machine(), mode=BootstrapMode.RECOMMENDED, non_interactive=True
    ).to_dict()

    healthy_ids = {
        "harness.claude_code",
        "harness.codex",
        "gateway.omniroute",
        "adapter.serena_lsp",
    }
    install_targets = {
        action["provider_id"]
        for action in report["plan"]["actions"]
        if action["kind"] == "install_provider"
    }
    assert install_targets.isdisjoint(healthy_ids)
    reused = {
        provider["provider_id"]
        for provider in report["providers"]
        if provider["lifecycle"] == "healthy"
    }
    assert healthy_ids <= reused


def test_unhealthy_gateway_is_installed_not_healthy_and_not_certified() -> None:
    cert_calls: list[Any] = []

    def _certify(providers: tuple[DiscoveredProvider, ...]) -> dict[str, object]:
        cert_calls.append(providers)
        return {
            "schema_version": "runtime-certification-seam/v1",
            "results": [
                {
                    "provider_id": item.provider_id,
                    "certified": item.health_state == "healthy",
                    "parity": "unsupported" if item.health_state != "healthy" else "supported",
                    "reason": item.failure_reason or "healthy",
                }
                for item in providers
            ],
        }

    report = run_bootstrap(
        providers=_unhealthy_gateway_machine(),
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        certifier=_certify,
    ).to_dict()

    omni = next(item for item in report["providers"] if item["provider_id"] == "gateway.omniroute")
    assert omni["lifecycle"] == "installed"
    assert omni["health_state"] == "unhealthy"
    assert "installed != healthy" in report["stages"][-1]["summary"].lower() or any(
        not result["certified"] for result in report["certification"]["results"]
    )
    assert any(not result["certified"] for result in report["certification"]["results"])
    # Recommendation should still offer alternatives / not lock onto unhealthy OmniRoute.
    gateway_recs = [
        item for item in report["recommendations"] if item["capability_id"].startswith("gateway.")
    ]
    assert gateway_recs
    assert all(item.get("selected_provider_id") != "gateway.omniroute" for item in gateway_recs)


def test_default_certifier_uses_bod92_runtime_certification() -> None:
    """Default CERTIFY seam must call BOD-92 certify_runtime (not a stub)."""

    report = run_bootstrap(
        providers=_unhealthy_gateway_machine(), mode=BootstrapMode.PLAN, non_interactive=True
    ).to_dict()

    certification = report["certification"]
    assert certification["schema_version"] == "runtime-certification/v1"
    assert certification["passport_seam"] == "runtime_certification"
    assert isinstance(certification.get("runtime_report"), dict)
    assert certification["runtime_report"]["purpose"] == "evidence"
    results = {item["provider_id"]: item for item in certification["results"]}
    omni = results["gateway.omniroute"]
    assert omni["certified"] is False
    assert omni["reason"] == "installed != healthy"
    assert omni["passport_seam"] == "runtime_certification"
    assert "state" in omni


def test_plan_noninteractive_is_deterministic_json_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    frozen = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    first = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        state_dir=tmp_path / "state",
        certify_now=frozen,
    ).to_dict()
    second = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        state_dir=tmp_path / "state",
        certify_now=frozen,
    ).to_dict()

    assert first == second
    assert first["mutation_free"] is True
    assert first["network_access"] == "disabled"
    assert not (tmp_path / "config").exists()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_apply_without_consent_or_allowlist_is_blocked(tmp_path: Path) -> None:
    installs: list[str] = []

    report = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=(),
        consent=False,
        state_dir=tmp_path / "state",
        install_runner=lambda action: installs.append(action.action_id),
    ).to_dict()

    consent_stage = next(stage for stage in report["stages"] if stage["stage"] == "consent")
    assert consent_stage["status"] == "blocked"
    assert installs == []
    assert report["apply"]["mutated"] is False


def test_decline_safe_native_only_works(tmp_path: Path) -> None:
    report = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=(),
        consent=False,
        decline_optional=True,
        state_dir=tmp_path / "state",
    ).to_dict()

    assert report["apply"]["mutated"] is False
    assert any(
        stage["stage"] == "certify" and stage["status"] == "ok" for stage in report["stages"]
    )
    covered = {
        item["capability_id"] for item in report["recommendations"] if item["status"] == "covered"
    }
    assert "code.symbols" in covered or "code.graph" in covered


def test_allowlisted_apply_is_idempotent_with_backup_and_ownership(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    installs: list[str] = []

    def _install(action: Any) -> dict[str, object]:
        installs.append(action.action_id)
        marker = state_dir / "markers" / f"{action.target}.installed"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n", encoding="utf-8")
        return {"status": "installed", "marker": str(marker)}

    first = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        install_runner=_install,
        scope=BootstrapScope.GATEWAYS,
    )
    second = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        install_runner=_install,
        scope=BootstrapScope.GATEWAYS,
    )

    assert first.to_dict()["apply"]["mutated"] is True
    assert len(installs) == 1, "second run must not reinstall"
    ownership = json.loads((state_dir / "ownership.json").read_text(encoding="utf-8"))
    assert ownership["managed_actions"]
    assert any(Path(path).exists() for path in ownership.get("backups", {}).values())
    # No-op re-run does not require re-consent theater in stage summary.
    consent_second = next(
        stage for stage in second.to_dict()["stages"] if stage["stage"] == "consent"
    )
    assert consent_second["status"] in {"ok", "skipped"}


def test_rollback_removes_verdict_owned_markers_and_ownership(tmp_path: Path) -> None:
    from verdict.capability_bootstrap import rollback_bootstrap_actions

    state_dir = tmp_path / "state"
    installs: list[str] = []

    def _install(action: Any) -> dict[str, object]:
        installs.append(action.action_id)
        marker = state_dir / "markers" / f"{action.target}.installed"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("ok\n", encoding="utf-8")
        return {"status": "installed", "marker": str(marker)}

    run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        install_runner=_install,
        scope=BootstrapScope.GATEWAYS,
    )
    marker = state_dir / "markers" / "gateway.omniroute.installed"
    assert marker.is_file()

    stage = rollback_bootstrap_actions(state_dir=state_dir)
    assert stage.status == "ok"
    assert stage.stage == "rollback"
    assert not marker.exists()
    ownership = json.loads((state_dir / "ownership.json").read_text(encoding="utf-8"))
    assert ownership["managed_actions"] == []
    assert stage.details["rolled_back"]


def test_rollback_without_runner_reports_manual_undo_for_external_install(tmp_path: Path) -> None:
    from verdict.capability_bootstrap import rollback_bootstrap_actions

    state_dir = tmp_path / "state"

    def _install(action: Any) -> dict[str, object]:
        # Marker outside state_dir — not Verdict-owned filesystem.
        return {"status": "installed", "marker": str(tmp_path / "outside" / action.target)}

    run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        install_runner=_install,
        scope=BootstrapScope.GATEWAYS,
    )
    stage = rollback_bootstrap_actions(state_dir=state_dir)
    assert stage.status == "partial"
    assert stage.details["manual_undo_required"]
    ownership = json.loads((state_dir / "ownership.json").read_text(encoding="utf-8"))
    assert ownership["managed_actions"], "ownership retained until uninstall runner succeeds"


def test_scope_filters_intelligence_gateways_harnesses() -> None:
    mixed = (
        *_full_reuse_machine(),
        _provider(
            "adapter.context7",
            ProviderKind.DOCS,
            frozenset({"docs.lookup"}),
            lifecycle=ProviderLifecycle.NOT_INSTALLED,
        ),
    )
    intel = run_bootstrap(
        providers=mixed,
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        scope=BootstrapScope.INTELLIGENCE,
    ).to_dict()
    gateways = run_bootstrap(
        providers=mixed,
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        scope=BootstrapScope.GATEWAYS,
    ).to_dict()
    harnesses = run_bootstrap(
        providers=mixed,
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        scope=BootstrapScope.HARNESSES,
    ).to_dict()

    assert all(
        item["provider_kind"] in {"intelligence", "docs", "other"} for item in intel["providers"]
    )
    assert all(item["provider_kind"] == "gateway" for item in gateways["providers"])
    assert all(item["provider_kind"] == "harness" for item in harnesses["providers"])


def test_setup_plan_includes_bootstrap_enrichment_actions_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    plan = build_setup_plan(bootstrap_providers=_clean_machine(), include_bootstrap=True).to_dict()
    assert plan["mutation_free"] is True
    action_ids = {action["action_id"] for action in plan["actions"]}
    assert "create-config" in action_ids
    assert any(action_id.startswith("bootstrap:") for action_id in action_ids)
    for action in plan["actions"]:
        for field in ("reason", "security_impact", "postcondition", "undo"):
            assert action[field]


def test_discovery_from_path_does_not_promote_binary_to_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"claude", "codex", "hermes"} else None

    report = run_bootstrap(
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        path_resolver=fake_which,
        probe_gateway=lambda _provider_id: {
            "reachable": False,
            "health_ok": False,
            "reason": "not probed in fixture",
        },
    ).to_dict()
    harnesses = [
        item
        for item in report["providers"]
        if item["provider_kind"] == "harness" and item["lifecycle"] != "not_installed"
    ]
    assert harnesses
    assert all(item["lifecycle"] == "installed" for item in harnesses)
    assert all(item["health_state"] != "healthy" for item in harnesses)
    assert all(item["qualification_state"] != "qualified" for item in harnesses)


def test_discovery_accepts_official_binary_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prime ships as `prime-agent`; Codebase Memory MCP as `codebase-memory-mcp`."""

    def fake_which(name: str) -> str | None:
        aliases = {
            "prime-agent": "/home/user/.local/bin/prime-agent",
            "codebase-memory-mcp": "/home/user/.local/bin/codebase-memory-mcp",
            "claude": "/usr/bin/claude",
        }
        return aliases.get(name)

    report = run_bootstrap(
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        path_resolver=fake_which,
        probe_gateway=lambda _provider_id: {
            "reachable": False,
            "health_ok": False,
            "reason": "not probed in fixture",
        },
    ).to_dict()
    by_id = {item["provider_id"]: item for item in report["providers"]}
    prime = by_id["harness.prime"]
    assert prime["lifecycle"] == "installed"
    assert prime["path_or_endpoint"] == "/home/user/.local/bin/prime-agent"
    cbm = by_id["adapter.codebase_memory"]
    assert cbm["lifecycle"] == "installed"
    assert cbm["path_or_endpoint"] == "/home/user/.local/bin/codebase-memory-mcp"


def test_apply_without_install_runner_is_blocked_and_not_owned(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        scope=BootstrapScope.GATEWAYS,
        install_runner=None,
    ).to_dict()
    apply_stage = next(stage for stage in first["stages"] if stage["stage"] == "apply")
    assert apply_stage["status"] == "blocked"
    assert first["apply"]["mutated"] is False
    ownership_path = state_dir / "ownership.json"
    if ownership_path.exists():
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        assert ownership.get("managed_actions", []) == []

    # Second run must still attempt (not suppressed by fake ownership).
    second = run_bootstrap(
        providers=_clean_machine(),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        scope=BootstrapScope.GATEWAYS,
        install_runner=None,
    ).to_dict()
    apply_second = next(stage for stage in second["stages"] if stage["stage"] == "apply")
    assert apply_second["status"] == "blocked"


def test_successful_apply_refreshes_provider_before_certify(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    observed: list[str] = []

    def _install(action: Any) -> dict[str, object]:
        return {"status": "installed", "provider_id": action.provider_id}

    def _certify(providers: tuple[DiscoveredProvider, ...]) -> dict[str, object]:
        for item in providers:
            observed.append(f"{item.provider_id}:{item.lifecycle.value}")
        return {
            "summary": "ok",
            "results": [
                {
                    "provider_id": item.provider_id,
                    "certified": item.lifecycle is not ProviderLifecycle.NOT_INSTALLED,
                    "reason": item.lifecycle.value,
                }
                for item in providers
            ],
        }

    report = run_bootstrap(
        providers=(
            *_clean_machine(),
            _provider(
                "gateway.omniroute",
                ProviderKind.GATEWAY,
                frozenset({"gateway.inventory", "gateway.execute"}),
                lifecycle=ProviderLifecycle.NOT_INSTALLED,
            ),
        ),
        mode=BootstrapMode.APPLY,
        non_interactive=True,
        allowlist=("gateway.omniroute",),
        consent=True,
        state_dir=state_dir,
        scope=BootstrapScope.GATEWAYS,
        install_runner=_install,
        certifier=_certify,
    )
    assert report.to_dict()["apply"]["mutated"] is True
    assert any(":installed" in entry for entry in observed), observed
    installed = [item for item in report.providers if item.lifecycle is ProviderLifecycle.INSTALLED]
    assert installed, "successful APPLY must refresh providers before certify"


def test_omniroute_env_does_not_mark_other_gateways_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    def fake_which(_name: str) -> str | None:
        return None

    report = run_bootstrap(
        mode=BootstrapMode.PLAN,
        non_interactive=True,
        path_resolver=fake_which,
        probe_gateway=lambda provider_id: {
            "reachable": False,
            "health_ok": False,
            "endpoint": "http://127.0.0.1:4000/v1"
            if provider_id != "gateway.omniroute"
            else "http://127.0.0.1:20128/v1",
            "reason": "unreachable",
        },
        scope=BootstrapScope.GATEWAYS,
    ).to_dict()
    by_id = {item["provider_id"]: item for item in report["providers"]}
    assert by_id["gateway.omniroute"]["lifecycle"] in {"configured", "installed", "unhealthy"} or (
        by_id["gateway.omniroute"]["lifecycle"] != "not_installed"
    )
    for other in ("gateway.litellm", "gateway.9router"):
        if other in by_id:
            assert by_id[other]["lifecycle"] == "not_installed"


def test_scoped_setup_plan_actions_stay_in_gateway_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    plan = build_setup_plan(
        bootstrap_providers=(
            *_clean_machine(),
            _provider("gateway.omniroute", ProviderKind.GATEWAY, frozenset({"gateway.inventory"})),
            _provider("harness.claude_code", ProviderKind.HARNESS, frozenset({"harness.execute"})),
        ),
        include_bootstrap=True,
        bootstrap_scope="gateways",
    ).to_dict()
    bootstrap_actions = [
        action for action in plan["actions"] if str(action["action_id"]).startswith("bootstrap:")
    ]
    assert bootstrap_actions
    assert all("harness" not in action["action_id"] for action in bootstrap_actions)
