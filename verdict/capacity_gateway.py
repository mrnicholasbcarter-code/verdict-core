"""Gateway capacity adapters (BOD-129).

OmniRoute is optional. Generic gateway observations use the same
CapacitySnapshot contract as direct providers. Unsupported quota stays unknown.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from verdict.capacity_models import (
    CapacityFailureClass,
    CapacityObservationError,
    CapacityPool,
    CapacitySignal,
    CapacitySnapshot,
    ConnectionIdentity,
    DiagnoseReport,
    EvidenceAuthority,
    RefreshPolicy,
    _reject_secrets,
    _utc,
)


class GenericGatewayCapacityAdapter:
    """Gateway quota/budget/health evidence — never routing authority."""

    def __init__(
        self,
        adapter_id: str = "gateway.generic",
        *,
        gateway_id: str = "generic",
        identities: Sequence[ConnectionIdentity] = (),
        fixtures: Mapping[str, Mapping[str, Any]] | None = None,
        quota_supported: bool = True,
    ) -> None:
        self._adapter_id = adapter_id
        self._gateway_id = gateway_id
        self._identities = tuple(identities)
        self._fixtures = {key: dict(value) for key, value in (fixtures or {}).items()}
        for payload in self._fixtures.values():
            _reject_secrets(payload, "gateway_fixture")
        self._quota_supported = quota_supported

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def discover(self) -> Sequence[ConnectionIdentity]:
        return self._identities

    def capabilities(self) -> Mapping[CapacitySignal, bool]:
        return {
            CapacitySignal.QUOTA_WINDOWS: self._quota_supported,
            CapacitySignal.BALANCES: self._quota_supported,
            CapacitySignal.RESET_TIMES: self._quota_supported,
            CapacitySignal.COOLDOWNS: True,
            CapacitySignal.RETRY_AFTER: True,
            CapacitySignal.SHARED_POOLS: False,
            CapacitySignal.HEALTH: True,
        }

    def refresh_policy(self) -> RefreshPolicy:
        return RefreshPolicy(ttl_seconds=30, prefer_reset_at=True, honor_retry_after=True)

    def diagnose(self) -> DiagnoseReport:
        return DiagnoseReport(
            adapter_id=self._adapter_id,
            available=True,
            status="ok" if self._quota_supported else "quota_unsupported",
            details={"gateway_id": self._gateway_id, "quota_supported": self._quota_supported},
        )

    def observe_capacity(
        self,
        identity: ConnectionIdentity | None = None,
        *,
        now: datetime | None = None,
    ) -> CapacitySnapshot:
        moment = _utc(now)
        if identity is None:
            if not self._identities:
                identity = ConnectionIdentity(
                    provider_id="gateway",
                    account_id="default",
                    adapter_id=self._adapter_id,
                    gateway_id=self._gateway_id,
                    access_mode="upstream_proxy",
                )
            else:
                identity = self._identities[0]

        if not self._quota_supported:
            return CapacitySnapshot(
                identity=identity,
                source_kind="gateway",
                authority=EvidenceAuthority.GATEWAY_NATIVE,
                observed_at=moment,
                confidence=None,
                health="unknown",
                errors=(
                    CapacityObservationError(
                        failure_class=CapacityFailureClass.UNSUPPORTED,
                        message="gateway quota unsupported (not fabricated)",
                    ),
                ),
                notes=("unknown",),
            )

        payload = self._fixtures.get(identity.account_id, {})
        pools = tuple(CapacityPool.from_dict(item) for item in payload.get("pools", ()))
        return CapacitySnapshot(
            identity=identity,
            source_kind="gateway",
            authority=EvidenceAuthority.GATEWAY_NATIVE,
            observed_at=moment,
            pools=pools,
            fresh_until=moment + timedelta(seconds=30),
            confidence=payload.get("confidence", 0.8),
            health=str(payload.get("health", "ok")),
            notes=tuple(str(item) for item in payload.get("notes", ())),
        )


def omniroute_capacity_adapter_from_fixtures(
    fixtures: Mapping[str, Mapping[str, Any]],
    *,
    identities: Sequence[ConnectionIdentity] | None = None,
) -> GenericGatewayCapacityAdapter:
    """Optional OmniRoute-shaped gateway adapter (fixtures; not architectural authority)."""

    if identities is None:
        identities = tuple(
            ConnectionIdentity(
                provider_id="openai",
                account_id=account_id,
                adapter_id="gateway.omniroute",
                gateway_id="omniroute",
                access_mode="upstream_proxy",
            )
            for account_id in fixtures
        )
    return GenericGatewayCapacityAdapter(
        "gateway.omniroute",
        gateway_id="omniroute",
        identities=identities,
        fixtures=fixtures,
        quota_supported=True,
    )


__all__ = [
    "GenericGatewayCapacityAdapter",
    "omniroute_capacity_adapter_from_fixtures",
]
