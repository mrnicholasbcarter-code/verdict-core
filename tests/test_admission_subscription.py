"""Canonical admission tests for subscription capacity evidence."""

from datetime import datetime, timezone

from verdict.admission import AdmissionStage, RuntimeEvidence, RuntimeObservation, admit
from verdict.capacity_models import (
    CapacityFailureClass,
    CapacityObservationError,
    CapacityPool,
    CapacitySnapshot,
    ConnectionIdentity,
    EvidenceAuthority,
)
from verdict.cost_ledger import _subscription_reserved, subscription_budgets

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(
    *pools: CapacityPool,
    errors: tuple[CapacityObservationError, ...] = (),
    observed_at: datetime = NOW,
    fresh_until: datetime | None = None,
) -> CapacitySnapshot:
    return CapacitySnapshot(
        identity=ConnectionIdentity(provider_id="openai", account_id="acct-123", adapter_id="test"),
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
        [{"provider": "openai", "isActive": True, "testStatus": "ok"}],
    )


def test_legacy_subscription_exhaustion_hard_drop() -> None:
    key = "openai/acct-123/subscription"
    subscription_budgets[key] = 100
    _subscription_reserved[key] = 100
    result = admit(
        [{"id": "openai/acct-123/subscription/gpt-4", "owned_by": "openai/acct-123/subscription"}],
        [{"provider": "openai", "account_id": "acct-123", "isActive": True, "testStatus": "ok"}],
        None,
        now=NOW,
    )
    record = result.record_for("openai/acct-123/subscription/gpt-4")
    assert record is not None and not record.admitted
    assert record.first_failed_stage == AdmissionStage.AVAILABLE
    assert record.reason == "exhausted"


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
    assert record.reason == "exhausted"


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
