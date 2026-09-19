"""Direct provider capacity adapters (fixture-backed for offline TDD).

Proves Codex/OpenAI-shaped multi-window pools. Live transport is optional;
adapters accept pre-parsed mappings so tests never need network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from verdict.capacity_models import (
    Balance,
    CapacityEvidenceError,
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


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


class FixtureDirectCapacityAdapter:
    """Direct provider adapter driven by offline fixture payloads."""

    def __init__(
        self,
        adapter_id: str,
        *,
        identities: Sequence[ConnectionIdentity],
        fixtures: Mapping[str, Mapping[str, Any]],
        authority: EvidenceAuthority = EvidenceAuthority.OFFICIAL_CLI,
        available: bool = True,
        diagnose_status: str = "ok",
    ) -> None:
        self._adapter_id = adapter_id
        self._identities = tuple(identities)
        self._fixtures = {key: dict(value) for key, value in fixtures.items()}
        for payload in self._fixtures.values():
            _reject_secrets(payload, "fixture")
        self._authority = authority
        self._available = available
        self._diagnose_status = diagnose_status

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    def discover(self) -> Sequence[ConnectionIdentity]:
        if not self._available:
            return ()
        return self._identities

    def capabilities(self) -> Mapping[CapacitySignal, bool]:
        return {
            CapacitySignal.QUOTA_WINDOWS: True,
            CapacitySignal.BALANCES: True,
            CapacitySignal.RESET_TIMES: True,
            CapacitySignal.COOLDOWNS: True,
            CapacitySignal.RETRY_AFTER: True,
            CapacitySignal.SHARED_POOLS: True,
            CapacitySignal.HEALTH: True,
        }

    def refresh_policy(self) -> RefreshPolicy:
        return RefreshPolicy(ttl_seconds=60, prefer_reset_at=True, honor_retry_after=True)

    def diagnose(self) -> DiagnoseReport:
        return DiagnoseReport(
            adapter_id=self._adapter_id,
            available=self._available,
            status=self._diagnose_status,
            details={"identity_count": len(self._identities)},
        )

    def observe_capacity(
        self,
        identity: ConnectionIdentity | None = None,
        *,
        now: datetime | None = None,
    ) -> CapacitySnapshot:
        moment = _utc(now)
        if not self._available:
            target = identity or ConnectionIdentity(
                provider_id="unknown",
                account_id="unavailable",
                adapter_id=self._adapter_id,
            )
            return CapacitySnapshot(
                identity=target,
                source_kind="direct_provider",
                authority=self._authority,
                observed_at=moment,
                errors=(
                    CapacityObservationError(
                        failure_class=CapacityFailureClass.UNSUPPORTED,
                        message="adapter unavailable",
                    ),
                ),
                notes=("unknown",),
            )

        if identity is None:
            if not self._identities:
                raise CapacityEvidenceError("no identities to observe")
            identity = self._identities[0]

        key = identity.account_id
        if key not in self._fixtures:
            return CapacitySnapshot(
                identity=identity,
                source_kind="direct_provider",
                authority=self._authority,
                observed_at=moment,
                errors=(
                    CapacityObservationError(
                        failure_class=CapacityFailureClass.UNSUPPORTED,
                        message="no fixture for account",
                    ),
                ),
            )

        payload = self._fixtures[key]
        return self._parse_fixture(identity, payload, moment)

    def _parse_fixture(
        self,
        identity: ConnectionIdentity,
        payload: Mapping[str, Any],
        moment: datetime,
    ) -> CapacitySnapshot:
        # Explicit failure fixtures (429, auth-expired, etc.)
        if "error" in payload:
            err = payload["error"]
            failure = CapacityFailureClass(str(err.get("failure_class", "unknown")))
            http_status = err.get("http_status")
            # 429 alone is never quota_exhausted.
            if http_status == 429 and failure is CapacityFailureClass.QUOTA_EXHAUSTED:
                failure = CapacityFailureClass.RATE_LIMIT
            retry_after = err.get("retry_after_seconds")
            pools: tuple[CapacityPool, ...] = ()
            if retry_after is not None:
                pools = (
                    CapacityPool(
                        pool_id=str(err.get("pool_id", "throttle")),
                        status="cooldown",
                        unit="requests",
                        retry_after_seconds=int(retry_after),
                        cooldown_until=moment + timedelta(seconds=int(retry_after)),
                        evidence_id="retry-after",
                    ),
                )
            return CapacitySnapshot(
                identity=identity,
                source_kind="direct_provider",
                authority=self._authority,
                observed_at=moment,
                pools=pools,
                fresh_until=(
                    moment + timedelta(seconds=int(retry_after))
                    if retry_after is not None
                    else moment + timedelta(seconds=30)
                ),
                confidence=1.0,
                errors=(
                    CapacityObservationError(
                        failure_class=failure,
                        message=str(err.get("message", failure.value)),
                        retry_after_seconds=retry_after,
                        http_status=http_status,
                    ),
                ),
            )

        pools = tuple(CapacityPool.from_dict(item) for item in payload.get("pools", ()))
        balances = tuple(Balance.from_dict(item) for item in payload.get("balances", ()))
        fresh_until = _parse_dt(payload.get("fresh_until")) or (moment + timedelta(seconds=120))
        return CapacitySnapshot(
            identity=identity,
            source_kind="direct_provider",
            authority=self._authority,
            observed_at=_parse_dt(payload.get("observed_at")) or moment,
            pools=pools,
            balances=balances,
            fresh_until=fresh_until,
            confidence=payload.get("confidence", 0.95),
            health=None if payload.get("health") is None else str(payload["health"]),
            notes=tuple(str(item) for item in payload.get("notes", ())),
        )


def codex_direct_adapter_from_fixtures(
    fixtures: Mapping[str, Mapping[str, Any]],
    *,
    identities: Sequence[ConnectionIdentity] | None = None,
) -> FixtureDirectCapacityAdapter:
    """Codex/OpenAI subscription usage adapter (fixture-backed)."""

    if identities is None:
        identities = tuple(
            ConnectionIdentity(
                provider_id="openai",
                account_id=account_id,
                adapter_id="direct.codex",
                access_mode="oauth",
            )
            for account_id in fixtures
        )
    return FixtureDirectCapacityAdapter(
        "direct.codex",
        identities=identities,
        fixtures=fixtures,
        authority=EvidenceAuthority.OFFICIAL_CLI,
    )


__all__ = [
    "FixtureDirectCapacityAdapter",
    "codex_direct_adapter_from_fixtures",
]
