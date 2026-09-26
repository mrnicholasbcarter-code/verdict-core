"""live capacity evidence — offline proof fixtures (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.capacity_adapters import CapacityAdapterRegistry, default_registry
from verdict.capacity_aggregator import (
    AGGREGATOR_CONTRACT_VERSION,
    AggregatorJsonCapacityAdapter,
    validate_aggregator_document,
)
from verdict.capacity_direct import codex_direct_adapter_from_fixtures
from verdict.capacity_gateway import (
    GenericGatewayCapacityAdapter,
    omniroute_capacity_adapter_from_fixtures,
)
from verdict.capacity_models import (
    CapacityEvidenceError,
    CapacityFailureClass,
    CapacityPool,
    CapacitySignal,
    CapacitySnapshot,
    ConnectionIdentity,
    EvidenceAuthority,
)
from verdict.capacity_project import (
    project_execution_path_evidence,
    project_quota_evidence,
    project_runtime_certification_quota,
    scarcest_quota_evidence,
)
from verdict.capacity_resolve import (
    prefer_snapshot,
    resolve_capacity_snapshots,
    stale_cannot_overwrite,
    unique_shared_capacity,
)
from verdict.cost_ledger import QuotaEvidenceInput, quota_pressure_term

FIXTURES = Path(__file__).parent / "fixtures" / "capacity"
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _codex_adapter():
    fixtures = _load("codex_direct.json")
    identities = [
        ConnectionIdentity(
            provider_id="openai",
            account_id=account_id,
            adapter_id="direct.codex",
            access_mode="oauth",
        )
        for account_id in fixtures
    ]
    return codex_direct_adapter_from_fixtures(fixtures, identities=identities)


def test_registry_discover_capabilities_observe_without_brand_switch() -> None:
    direct = _codex_adapter()
    gateway = omniroute_capacity_adapter_from_fixtures(_load("gateway_omniroute.json"))
    aggregator = AggregatorJsonCapacityAdapter.from_path(FIXTURES / "aggregator_grok.json")
    registry = default_registry([direct, gateway, aggregator])

    assert set(registry.list_adapters()) == {"direct.codex", "gateway.omniroute", "aggregator.json"}
    caps = registry.capabilities("direct.codex")
    assert caps[CapacitySignal.QUOTA_WINDOWS] is True
    assert registry.discover()
    snap = registry.observe_capacity(
        "direct.codex",
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    assert snap.source_kind == "direct_provider"
    assert registry.refresh_policy("direct.codex").honor_retry_after is True
    assert registry.diagnose("gateway.omniroute").available is True


def test_omniroute_absence_does_not_disable_direct() -> None:
    registry = default_registry([_codex_adapter()])
    assert "gateway.omniroute" not in registry.list_adapters()
    snap = registry.observe_capacity(
        "direct.codex",
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    assert len(snap.pools) == 3


def test_codex_fixture_preserves_separate_windows_and_resets() -> None:
    snap = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    pool_ids = {pool.pool_id for pool in snap.pools}
    assert pool_ids == {"codex-5h", "codex-weekly", "codex-o3-extra"}
    weekly = next(pool for pool in snap.pools if pool.pool_id == "codex-weekly")
    five_h = next(pool for pool in snap.pools if pool.pool_id == "codex-5h")
    assert weekly.remaining_pct == 8.0
    assert five_h.remaining_pct == 42.0
    assert weekly.reset_at != five_h.reset_at
    assert weekly.window_seconds == 604800
    assert five_h.window_seconds == 18000
    assert snap.balances[0].kind == "reset_credits"


def test_grok_shared_pool_cannot_be_double_counted() -> None:
    adapter = AggregatorJsonCapacityAdapter.from_path(FIXTURES / "aggregator_grok.json")
    snap = adapter.observe_capacity(now=NOW)
    assert {pool.shared_pool_id for pool in snap.pools} == {"grok-super-shared"}
    unique = unique_shared_capacity((snap,))
    assert list(unique) == ["grok-super-shared"]
    cert = project_runtime_certification_quota(snap)
    assert cert is not None
    assert len(cert["pools"]) == 1
    assert cert["pools"][0]["shared_pool_id"] == "grok-super-shared"


def test_cursor_monthly_pools_separate_from_ondemand() -> None:
    adapter = AggregatorJsonCapacityAdapter.from_path(FIXTURES / "aggregator_cursor.json")
    snap = adapter.observe_capacity(now=NOW)
    ids = {pool.pool_id for pool in snap.pools}
    assert "cursor-models-monthly" in ids
    assert "cursor-other-models-monthly" in ids
    assert "cursor-ondemand" in ids
    ondemand = next(pool for pool in snap.pools if pool.pool_id == "cursor-ondemand")
    monthly = next(pool for pool in snap.pools if pool.pool_id == "cursor-models-monthly")
    assert ondemand.shared_pool_id is None
    assert monthly.shared_pool_id is None
    assert ondemand.unit == "usd"
    assert monthly.unit == "requests"


def test_two_accounts_exhaustion_is_account_scoped() -> None:
    adapter = _codex_adapter()
    exhausted = adapter.observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-exhausted",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    healthy = adapter.observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-b",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    assert exhausted.pools[0].status == "exhausted"
    assert exhausted.pools[0].remaining_pct == 0.0
    assert healthy.pools[0].status == "available"
    assert healthy.pools[0].remaining_pct == 74.0
    resolved = resolve_capacity_snapshots((exhausted, healthy), now=NOW)
    assert len(resolved.snapshots) == 2
    by_account = {snap.identity.account_id: snap for snap in resolved.snapshots}
    assert by_account["acct-codex-exhausted"].pools[0].status == "exhausted"
    assert by_account["acct-codex-b"].pools[0].status == "available"


def test_direct_vs_gateway_conflict_keeps_both_never_averages() -> None:
    direct = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    gateway = omniroute_capacity_adapter_from_fixtures(
        _load("gateway_omniroute.json")
    ).observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="gateway.omniroute",
            gateway_id="omniroute",
            access_mode="upstream_proxy",
        ),
        now=NOW,
    )
    direct_weekly = next(pool for pool in direct.pools if pool.pool_id == "codex-weekly")
    gateway_weekly = gateway.pools[0]
    assert direct_weekly.remaining_pct == 8.0
    assert gateway_weekly.remaining_pct == 43.0

    resolution = prefer_snapshot(direct, gateway, now=NOW)
    assert resolution.averaged is False
    assert resolution.to_dict()["both_retained"] is True
    # Official CLI beats gateway for same account when both fresh.
    assert resolution.preferred.authority is EvidenceAuthority.OFFICIAL_CLI
    assert resolution.preferred.pools
    preferred_weekly = next(
        pool for pool in resolution.preferred.pools if pool.pool_id == "codex-weekly"
    )
    assert preferred_weekly.remaining_pct == 8.0
    # Never averaged: (8+43)/2 would be 25.5
    assert preferred_weekly.remaining_pct != 25.5
    assert resolution.alternate.authority is EvidenceAuthority.GATEWAY_NATIVE


def test_stale_observation_cannot_overwrite_fresh() -> None:
    identity = ConnectionIdentity(
        provider_id="openai",
        account_id="acct-codex-a",
        adapter_id="direct.codex",
        access_mode="oauth",
    )
    fresh = CapacitySnapshot(
        identity=identity,
        source_kind="direct_provider",
        authority=EvidenceAuthority.OFFICIAL_CLI,
        observed_at=NOW,
        fresh_until=NOW + timedelta(minutes=5),
        pools=(CapacityPool(pool_id="codex-weekly", remaining_pct=8.0, status="constrained"),),
    )
    stale = CapacitySnapshot(
        identity=identity,
        source_kind="gateway",
        authority=EvidenceAuthority.GATEWAY_NATIVE,
        observed_at=NOW + timedelta(seconds=1),
        fresh_until=NOW - timedelta(minutes=1),
        pools=(CapacityPool(pool_id="codex-weekly", remaining_pct=43.0, status="available"),),
    )
    kept = stale_cannot_overwrite(fresh, stale, now=NOW)
    assert kept.pools[0].remaining_pct == 8.0


def test_429_with_retry_after_is_throttle_not_exhaustion() -> None:
    snap = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-429",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    assert snap.errors
    assert snap.errors[0].failure_class is CapacityFailureClass.RATE_LIMIT
    assert snap.errors[0].http_status == 429
    assert snap.errors[0].retry_after_seconds == 45
    assert snap.pools[0].status == "cooldown"
    assert snap.pools[0].retry_after_seconds == 45
    assert snap.errors[0].failure_class is not CapacityFailureClass.QUOTA_EXHAUSTED


def test_auth_expired_distinct_from_quota_exhausted() -> None:
    auth = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-auth-expired",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    exhausted = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-exhausted",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    assert auth.errors[0].failure_class is CapacityFailureClass.AUTH_EXPIRED
    assert exhausted.pools[0].status == "exhausted"
    assert (
        not exhausted.errors
        or exhausted.errors[0].failure_class is not CapacityFailureClass.AUTH_EXPIRED
    )


def test_unsupported_gateway_returns_explicit_unknown() -> None:
    adapter = GenericGatewayCapacityAdapter(
        "gateway.opaque",
        gateway_id="opaque",
        identities=(
            ConnectionIdentity(
                provider_id="unknown-provider",
                account_id="acct-1",
                adapter_id="gateway.opaque",
                gateway_id="opaque",
            ),
        ),
        quota_supported=False,
    )
    snap = adapter.observe_capacity(now=NOW)
    assert snap.errors[0].failure_class is CapacityFailureClass.UNSUPPORTED
    assert snap.pools == ()
    assert "unknown" in snap.notes
    # Discovery must not crash.
    registry = CapacityAdapterRegistry()
    registry.register(adapter)
    assert registry.diagnose("gateway.opaque").status == "quota_unsupported"


def test_aggregator_rejects_secrets_and_bad_version() -> None:
    with pytest.raises(CapacityEvidenceError, match="secret-bearing"):
        validate_aggregator_document(
            {
                "contract_version": AGGREGATOR_CONTRACT_VERSION,
                "provider_id": "xai",
                "account_id": "a1",
                "api_key": "sk-secret",
                "pools": [],
            }
        )
    with pytest.raises(CapacityEvidenceError, match="contract_version"):
        validate_aggregator_document(
            {"contract_version": "99", "provider_id": "xai", "account_id": "a1", "pools": []}
        )


def test_aggregator_from_bytes_is_bounded() -> None:
    huge = b"{" + b"x" * 300_000 + b"}"
    with pytest.raises(CapacityEvidenceError, match="size bound"):
        AggregatorJsonCapacityAdapter.from_json_bytes(huge)


def test_connection_identity_rejects_credential_material() -> None:
    with pytest.raises(CapacityEvidenceError, match="credential"):
        ConnectionIdentity(
            provider_id="openai", account_id="sk-live-abcdef", adapter_id="direct.codex"
        )


def test_unknown_remaining_stays_unknown_in_projections() -> None:
    snap = CapacitySnapshot(
        identity=ConnectionIdentity(
            provider_id="openai", account_id="acct-unknown", adapter_id="direct.codex"
        ),
        source_kind="direct_provider",
        authority=EvidenceAuthority.UNKNOWN,
        observed_at=NOW,
        pools=(CapacityPool(pool_id="mystery", status="unknown", remaining_pct=None),),
    )
    quota = project_quota_evidence(snap)
    assert quota[0]["remaining_pct"] is None
    pressure = quota_pressure_term(quota[0])
    assert pressure.status == "unknown"
    assert pressure.amount is None


def test_projects_into_bod54_quota_pressure_without_provider_branch() -> None:
    snap = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    evidence = scarcest_quota_evidence((snap,))
    assert evidence is not None
    assert evidence["remaining_pct"] == 8.0
    term = quota_pressure_term(evidence)
    assert term.status == "observed"
    assert term.kind == "quota_pressure"


def test_projects_into_bod92_and_bod104_without_provider_imports() -> None:
    direct = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    grok = AggregatorJsonCapacityAdapter.from_path(
        FIXTURES / "aggregator_grok.json"
    ).observe_capacity(now=NOW)

    cert = project_runtime_certification_quota(direct)
    assert cert is not None
    assert cert["identity"]["account_id"] == "acct-codex-a"
    assert "openai" not in str(type(cert))  # plain mapping
    assert all(isinstance(pool, dict) for pool in cert["pools"])

    bag = project_execution_path_evidence((direct, grok))
    assert bag["schema"] == "capacity_execution_evidence.v1"
    assert bag["scarcest_pool"]["remaining_pct"] == 8.0
    # No provider-specific keys required for the execution-path authority optimizer (ADR-035) consumption.
    assert "quota_evidence" in bag
    for item in bag["quota_evidence"]:
        assert set(item.keys()) <= set(QuotaEvidenceInput.__annotations__)


def test_snapshot_roundtrip_and_digest() -> None:
    snap = _codex_adapter().observe_capacity(
        ConnectionIdentity(
            provider_id="openai",
            account_id="acct-codex-a",
            adapter_id="direct.codex",
            access_mode="oauth",
        ),
        now=NOW,
    )
    restored = CapacitySnapshot.from_dict(snap.to_dict())
    assert restored.evidence_digest == snap.evidence_digest
    assert restored.pools[0].pool_id == snap.pools[0].pool_id


def test_higher_authority_stale_yields_to_fresh_lower() -> None:
    identity = ConnectionIdentity(
        provider_id="openai", account_id="acct-codex-a", adapter_id="direct.codex"
    )
    stale_official = CapacitySnapshot(
        identity=identity,
        source_kind="direct_provider",
        authority=EvidenceAuthority.OFFICIAL_CLI,
        observed_at=NOW - timedelta(hours=2),
        fresh_until=NOW - timedelta(hours=1),
        pools=(CapacityPool(pool_id="codex-weekly", remaining_pct=8.0),),
    )
    fresh_gateway = CapacitySnapshot(
        identity=identity,
        source_kind="gateway",
        authority=EvidenceAuthority.GATEWAY_NATIVE,
        observed_at=NOW,
        fresh_until=NOW + timedelta(minutes=5),
        pools=(CapacityPool(pool_id="codex-weekly", remaining_pct=43.0),),
    )
    resolution = prefer_snapshot(stale_official, fresh_gateway, now=NOW)
    assert resolution.preferred is fresh_gateway
    assert "stale_higher_authority" in resolution.reason
