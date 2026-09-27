"""Canonical admission tests for subscription capacity evidence."""

from datetime import datetime, timedelta, timezone

import pytest

from verdict.admission import (
    AdmissionBypassError,
    AdmissionStage,
    RuntimeEvidence,
    RuntimeObservation,
    admit,
)
from verdict.capacity_models import (
    CapacityFailureClass,
    CapacityObservationError,
    CapacityPool,
    CapacitySnapshot,
    ConnectionIdentity,
    EvidenceAuthority,
)
from verdict.subscription_headroom import DEFAULT_SUBSCRIPTION_FRESHNESS_TTL_SECONDS

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(
    *pools: CapacityPool,
    errors: tuple[CapacityObservationError, ...] = (),
    observed_at: datetime = NOW,
    fresh_until: datetime | None = None,
    account_id: str = "acct-123",
) -> CapacitySnapshot:
    return CapacitySnapshot(
        identity=ConnectionIdentity(provider_id="openai", account_id=account_id, adapter_id="test"),
        source_kind="direct_provider",
        authority=EvidenceAuthority.PROVIDER_API,
        observed_at=observed_at,
        fresh_until=fresh_until,
        pools=pools,
        errors=errors,
    )


def _inventory() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    return (
        [{"id": "openai/gpt-4", "owned_by": "openai", "capabilities": {}}],
        [{"provider": "openai", "account_id": "acct-123", "isActive": True, "testStatus": "ok"}],
    )


def test_explicit_subscription_exhaustion_hard_drop() -> None:
    inventory = [
        {"id": "openai/acct-123/subscription/gpt-4", "owned_by": "openai/acct-123/subscription"}
    ]
    connections = [
        {"provider": "openai", "account_id": "acct-123", "isActive": True, "testStatus": "ok"}
    ]
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[_snapshot(CapacityPool("subscription", status="exhausted"))],
    )
    record = result.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None and not record.admitted
    assert record.first_failed_stage == AdmissionStage.AVAILABLE
    assert record.reason == "subscription_exhaustion"


def test_snapshot_exhaustion_is_canonical_hard_drop() -> None:
    inventory, connections = _inventory()
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[_snapshot(CapacityPool("rolling", status="exhausted"))],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None and not record.admitted
    assert record.first_failed_stage == AdmissionStage.AVAILABLE
    assert record.reason == "subscription_exhaustion"


def test_stale_snapshot_does_not_overwrite_fresh_route_evidence() -> None:
    inventory, connections = _inventory()
    stale_time = NOW.replace(year=2025, month=12, day=1)
    stale = _snapshot(
        CapacityPool("rolling", status="exhausted"), observed_at=stale_time, fresh_until=stale_time
    )
    runtime = RuntimeEvidence(
        (RuntimeObservation("route:openai/gpt-4", "healthy", "probe", "probe", NOW.isoformat()),),
        ("probe",),
    )
    result = admit(inventory, connections, runtime, now=NOW, subscription_snapshots=[stale])
    record = result.record_for("openai/gpt-4")
    assert record is not None and record.admitted and record.health == "healthy"


def test_distinct_pool_windows_keep_identity_and_reset_metadata() -> None:
    from verdict.subscription_headroom import subscription_observations

    reset_a, reset_b = NOW.replace(hour=2), NOW.replace(hour=3)
    observations, _ = subscription_observations(
        [
            _snapshot(
                CapacityPool("daily", status="constrained", reset_at=reset_a),
                CapacityPool("monthly", status="available", reset_at=reset_b),
            )
        ],
        now=NOW,
    )
    assert [row["pool_id"] for row in observations] == ["daily", "monthly"]
    assert [row["reset_at"] for row in observations] == [reset_a.isoformat(), reset_b.isoformat()]


def test_subscription_snapshot_lockout_precedes_availability() -> None:
    inventory, connections = _inventory()
    snapshot = _snapshot(
        CapacityPool("daily", status="available"),
        errors=(CapacityObservationError(CapacityFailureClass.PERMISSION_DENIED, "denied"),),
    )
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[snapshot],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None and not record.admitted
    assert record.first_failed_stage == AdmissionStage.ENTITLED
    assert record.reason == "lockout"


def test_exhausted_account_does_not_drop_route_for_active_sibling_account() -> None:
    """F1 / M1: acct-a exhausted, acct-b available and both active -> acct-b stays admitted."""
    inventory = [{"id": "openai/gpt-4", "owned_by": "openai", "capabilities": {}}]
    connections = [
        {"provider": "openai", "account_id": "acct-a", "isActive": True, "testStatus": "ok"},
        {"provider": "openai", "account_id": "acct-b", "isActive": True, "testStatus": "ok"},
    ]
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[
            _snapshot(CapacityPool("s", status="exhausted"), account_id="acct-a"),
            _snapshot(CapacityPool("s", status="available"), account_id="acct-b"),
        ],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None
    assert record.admitted, record.to_dict()


def test_connection_without_account_id_rejects_foreign_account_evidence() -> None:
    """F1 / M1: a connection without account_id must not inherit foreign-account evidence."""
    inventory, _ = _inventory()
    connections = [{"provider": "openai", "isActive": True, "testStatus": "ok"}]
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[
            _snapshot(CapacityPool("s", status="exhausted"), account_id="acct-a")
        ],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None
    assert record.admitted, record.to_dict()


def test_workspace_scope_isolates_foreign_workspace_exhaustion() -> None:
    """M2: exhausted evidence for a foreign workspace must not drop the active workspace."""
    inventory = [{"id": "openai/gpt-4", "owned_by": "openai", "capabilities": {}}]
    connections = [
        {
            "provider": "openai",
            "account_id": "acct-123",
            "workspace_id": "ws-active",
            "isActive": True,
            "testStatus": "ok",
        }
    ]

    def _ws_snapshot(status: str, workspace_id: str) -> CapacitySnapshot:
        return CapacitySnapshot(
            identity=ConnectionIdentity(
                provider_id="openai",
                account_id="acct-123",
                adapter_id="test",
                workspace_id=workspace_id,
            ),
            source_kind="direct_provider",
            authority=EvidenceAuthority.PROVIDER_API,
            observed_at=NOW,
            fresh_until=NOW + timedelta(minutes=5),
            pools=(CapacityPool("shared", status=status),),
        )

    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[
            _ws_snapshot("exhausted", "ws-foreign"),
            _ws_snapshot("available", "ws-active"),
        ],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None
    assert record.admitted, record.to_dict()


def test_default_freshness_ttl_rejects_snapshot_without_fresh_until() -> None:
    """M3: a snapshot with no fresh_until must expire after the default TTL, not forever."""
    inventory, connections = _inventory()
    stale = _snapshot(
        CapacityPool("rolling", status="exhausted"),
        observed_at=NOW - timedelta(seconds=DEFAULT_SUBSCRIPTION_FRESHNESS_TTL_SECONDS + 1),
        fresh_until=None,
    )
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[stale],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None
    assert record.admitted, record.to_dict()
    assert record.reason == "admitted_unverified"


def test_default_freshness_ttl_still_applies_snapshot_within_window() -> None:
    """M3 (direction check): a snapshot inside the default TTL must still apply."""
    inventory, connections = _inventory()
    fresh_enough = _snapshot(
        CapacityPool("rolling", status="exhausted"),
        observed_at=NOW - timedelta(seconds=60),
        fresh_until=None,
    )
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[fresh_enough],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None and not record.admitted
    assert record.reason == "subscription_exhaustion"


def test_unknown_headroom_admitted_but_not_launchable_without_confirmation() -> None:
    """M4: an unknown-headroom snapshot must be gated even with a separate healthy route probe."""
    inventory, connections = _inventory()
    runtime = RuntimeEvidence(
        (RuntimeObservation("route:openai/gpt-4", "healthy", "probe", "probe", NOW.isoformat()),),
        ("probe",),
    )
    result = admit(
        inventory,
        connections,
        runtime,
        now=NOW,
        subscription_snapshots=[_snapshot(CapacityPool("s", status="unknown"))],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None
    assert record.admitted
    assert record.reason == "subscription_unknown"
    assert record.health == "unknown"
    assert not result.launchable("openai/gpt-4")
    with pytest.raises(AdmissionBypassError):
        result.require_launchable("openai/gpt-4", surface="test")


def test_launchable_blocks_unknown_headroom_until_confirmed() -> None:
    """M5: launchable() must reject subscription_unknown before, and allow it after, confirm."""
    inventory, connections = _inventory()
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[_snapshot(CapacityPool("s", status="unknown"))],
    )
    assert not result.launchable("openai/gpt-4")
    confirmed = result.record_confirmation(
        "openai/gpt-4", healthy=True, source="bounded-confirm", observed_at=NOW.isoformat()
    )
    assert confirmed.launchable("openai/gpt-4")
    assert confirmed.require_launchable("openai/gpt-4", surface="test").route_id == "openai/gpt-4"


def test_reset_at_is_preserved_in_admission_record_dict() -> None:
    """M6: reset_at must survive into the canonical AdmissionRecord.to_dict()."""
    inventory, connections = _inventory()
    reset_at = NOW + timedelta(hours=1)
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[
            _snapshot(
                CapacityPool("s", status="cooldown", reset_at=reset_at, retry_after_seconds=60)
            )
        ],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None
    as_dict = record.to_dict()
    assert as_dict["reset_at"] == reset_at.isoformat(), as_dict


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (CapacityFailureClass.CONCURRENCY_LIMIT, "concurrency_limit"),
        (CapacityFailureClass.PROVIDER_OVERLOAD, "provider_overload"),
    ],
)
def test_distinct_capacity_failure_classes_hard_drop(
    failure: CapacityFailureClass, expected_reason: str
) -> None:
    """M7 / M8: concurrency and overload errors must classify and hard-drop distinctly."""
    inventory, connections = _inventory()
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[
            _snapshot(errors=(CapacityObservationError(failure, failure.value),))
        ],
    )
    record = result.record_for("openai/gpt-4")
    assert record is not None and not record.admitted
    assert record.first_failed_stage == AdmissionStage.AVAILABLE
    assert record.reason == expected_reason


def test_launchable_guard_blocks_unknown_reason_even_if_health_marked_healthy() -> None:
    """M5: launchable() must not fall through to the health=="healthy" check for
    subscription_unknown/constrained reasons, even if health is (incorrectly)
    healthy. This guards derived states -- narrow()/record_confirmation() reuse
    the same _derive() path -- from ever bypassing the unknown-headroom gate.
    """
    from dataclasses import replace

    inventory, connections = _inventory()
    result = admit(
        inventory,
        connections,
        RuntimeEvidence(),
        now=NOW,
        require_runtime=False,
        subscription_snapshots=[_snapshot(CapacityPool("s", status="unknown"))],
    )
    forced_records = tuple(
        replace(r, health="healthy") if r.route_id == "openai/gpt-4" else r for r in result.records
    )
    forced = result._derive(forced_records, "TEST:force-healthy-unknown")
    forced_record = forced.record_for("openai/gpt-4")
    assert forced_record is not None
    assert forced_record.reason == "subscription_unknown"
    assert forced_record.health == "healthy"
    assert not forced.launchable("openai/gpt-4")
