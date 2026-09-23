"""Prove-at-rest daemon for free-tier ∩ active OmniRoute identities.

Continuously (or once) proves only free∩active concrete catalog identities at
rest. Paid/frontier and inactive/unconnected identities are never probed; they
are either omitted or recorded as *skipped* with a named reason when they appear
as free-tier admit drops.

Proof results (healthy / failed / skipped) and optional ModelPassport payloads
are persisted to a JSON file Core reads on serve admit. Request-time budgeted
confirm probes run in ``verdict.admit_prove_confirm`` (not this daemon).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from verdict.free_tier_admit import (
    OmniRouteAdmitSnapshot,
    admit_free_tier_active,
    load_omniroute_admit_snapshot,
    omniroute_endpoint_from_env,
)
from verdict.model_passports import PASSPORT_TTL_SECONDS, ModelPassport
from verdict.probes import ProbeBudget, ProbeObservation, ProbePolicy, ProbeRunner, ProbeTransport

PROVE_AT_REST_SCHEMA_VERSION = "1"
STATUS_HEALTHY = "healthy"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
_VALID_STATUSES = frozenset({STATUS_HEALTHY, STATUS_FAILED, STATUS_SKIPPED})

DEFAULT_INTERVAL_SECONDS = 300.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 15.0
ENV_STATE_PATH = "VERDICT_PROVE_AT_REST_STATE"


class ProveAtRestError(ValueError):
    """Raised when prove-at-rest state or inputs violate the contract."""


def default_state_path() -> Path:
    """Return the configured or default prove-at-rest state file path."""
    configured = os.getenv(ENV_STATE_PATH)
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".verdict" / "prove-at-rest" / "state.json").resolve()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ProveAtRestError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ProveAtRestError(f"{field_name} must be a non-empty ISO-8601 string")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ProveAtRestError(f"{field_name} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ProveAtRestError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _provider_of(identity_id: str) -> str:
    if "/" in identity_id:
        return identity_id.split("/", 1)[0]
    return identity_id or "unknown"


def passport_from_probe(
    *, provider: str, identity_id: str, observation: ProbeObservation
) -> ModelPassport:
    """Build a ModelPassport from an already-run at-rest probe (no second call)."""
    ready = observation.availability_state == "ready"
    if observation.error_class == "unauthorized":
        auth_state = "unauthorized"
    elif ready:
        auth_state = "authorized"
    else:
        auth_state = "unknown"
    if ready:
        availability_state = "eligible"
        reason = None
    elif observation.availability_state == "denied" and observation.quarantine_until is not None:
        availability_state = "quarantined"
        reason = observation.error or "quarantined"
    elif observation.availability_state == "denied":
        availability_state = "denied"
        reason = observation.error or "denied"
    else:
        availability_state = "degraded"
        reason = observation.error_class or observation.error or observation.status
    qualified_at = observation.observed_at
    expires_at = qualified_at + timedelta(seconds=PASSPORT_TTL_SECONDS)
    return ModelPassport(
        provider=provider,
        model_id=identity_id,
        auth_state=auth_state,
        latency_p95=observation.latency_ms,
        last_verified_timestamp=observation.observed_at,
        availability_state=availability_state,
        availability_reason=reason,
        quarantine_until=observation.quarantine_until
        if availability_state == "quarantined"
        else None,
        quarantined_at=observation.observed_at if availability_state == "quarantined" else None,
        qualified_at=qualified_at,
        expires_at=expires_at,
    )


@dataclass(frozen=True)
class ProofResult:
    """One identity's prove-at-rest outcome."""

    identity_id: str
    provider: str
    status: str
    reason: str | None = None
    proved_at: datetime | None = None
    latency_ms: float | None = None
    http_status: int | None = None
    error_class: str | None = None
    passport: ModelPassport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.identity_id, str) or not self.identity_id.strip():
            raise ProveAtRestError("identity_id must be non-empty")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ProveAtRestError("provider must be non-empty")
        if self.status not in _VALID_STATUSES:
            raise ProveAtRestError("status must be healthy, failed, or skipped")
        if self.status == STATUS_SKIPPED and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ProveAtRestError("skipped results require a named reason")
        if self.proved_at is not None and (
            not isinstance(self.proved_at, datetime) or self.proved_at.tzinfo is None
        ):
            raise ProveAtRestError("proved_at must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "identity_id": self.identity_id,
            "provider": self.provider,
            "status": self.status,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.proved_at is not None:
            payload["proved_at"] = _format_datetime(self.proved_at)
        if self.latency_ms is not None:
            payload["latency_ms"] = self.latency_ms
        if self.http_status is not None:
            payload["http_status"] = self.http_status
        if self.error_class is not None:
            payload["error_class"] = self.error_class
        if self.passport is not None:
            payload["passport"] = self.passport.to_dict()
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProofResult:
        if not isinstance(value, Mapping):
            raise ProveAtRestError("proof result must be a mapping")
        identity_id = value.get("identity_id")
        provider = value.get("provider")
        status = value.get("status")
        if not isinstance(identity_id, str) or not isinstance(provider, str):
            raise ProveAtRestError("identity_id and provider are required")
        if not isinstance(status, str):
            raise ProveAtRestError("status is required")
        passport_raw = value.get("passport")
        passport = None
        if passport_raw is not None:
            if not isinstance(passport_raw, Mapping):
                raise ProveAtRestError("passport must be a mapping")
            passport = ModelPassport.from_dict(passport_raw)
        proved_raw = value.get("proved_at")
        proved_at = _parse_datetime(proved_raw, "proved_at") if proved_raw is not None else None
        reason = value.get("reason")
        latency = value.get("latency_ms")
        http_status = value.get("http_status")
        error_class = value.get("error_class")
        return cls(
            identity_id=identity_id,
            provider=provider,
            status=status,
            reason=str(reason) if isinstance(reason, str) else None,
            proved_at=proved_at,
            latency_ms=float(latency) if isinstance(latency, (int, float)) else None,
            http_status=int(http_status) if isinstance(http_status, int) else None,
            error_class=str(error_class) if isinstance(error_class, str) else None,
            passport=passport,
        )


@dataclass(frozen=True)
class ProveAtRestCycle:
    """One complete prove-at-rest pass over free∩active + named skips."""

    cycle_id: str
    started_at: datetime
    finished_at: datetime
    results: tuple[ProofResult, ...]
    admitted: tuple[str, ...] = ()
    active_providers: tuple[str, ...] = ()
    free_tier_providers: tuple[str, ...] = ()
    schema_version: str = PROVE_AT_REST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PROVE_AT_REST_SCHEMA_VERSION:
            raise ProveAtRestError("schema_version must be '1'")
        if not isinstance(self.cycle_id, str) or not self.cycle_id.strip():
            raise ProveAtRestError("cycle_id must be non-empty")
        for name in ("started_at", "finished_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ProveAtRestError(f"{name} must be timezone-aware")

    @property
    def summary(self) -> dict[str, int]:
        counts = {STATUS_HEALTHY: 0, STATUS_FAILED: 0, STATUS_SKIPPED: 0}
        for item in self.results:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    def healthy_identities(self) -> tuple[str, ...]:
        return tuple(item.identity_id for item in self.results if item.status == STATUS_HEALTHY)

    def passports(self) -> dict[str, ModelPassport]:
        """Passports keyed by identity_id for later admit consumption."""
        out: dict[str, ModelPassport] = {}
        for item in self.results:
            if item.passport is not None:
                out[item.identity_id] = item.passport
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cycle_id": self.cycle_id,
            "started_at": _format_datetime(self.started_at),
            "finished_at": _format_datetime(self.finished_at),
            "admitted": list(self.admitted),
            "active_providers": list(self.active_providers),
            "free_tier_providers": list(self.free_tier_providers),
            "summary": self.summary,
            "results": [item.to_dict() for item in self.results],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProveAtRestCycle:
        if not isinstance(value, Mapping):
            raise ProveAtRestError("cycle must be a mapping")
        results_raw = value.get("results")
        if not isinstance(results_raw, list):
            raise ProveAtRestError("results must be a list")
        results = tuple(ProofResult.from_dict(item) for item in results_raw)
        admitted = value.get("admitted") or []
        active = value.get("active_providers") or []
        free_tier = value.get("free_tier_providers") or []
        if (
            not isinstance(admitted, list)
            or not isinstance(active, list)
            or not isinstance(free_tier, list)
        ):
            raise ProveAtRestError("admitted/active/free_tier provider lists must be lists")
        return cls(
            cycle_id=str(value.get("cycle_id") or ""),
            started_at=_parse_datetime(value.get("started_at"), "started_at"),
            finished_at=_parse_datetime(value.get("finished_at"), "finished_at"),
            results=results,
            admitted=tuple(str(item) for item in admitted),
            active_providers=tuple(str(item) for item in active),
            free_tier_providers=tuple(str(item) for item in free_tier),
            schema_version=str(value.get("schema_version") or PROVE_AT_REST_SCHEMA_VERSION),
        )


@dataclass
class ProveAtRestStore:
    """Atomic JSON persistence for the latest prove-at-rest cycle."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser().resolve()

    def read(self) -> ProveAtRestCycle | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProveAtRestError(f"cannot read prove-at-rest state: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ProveAtRestError("prove-at-rest state must be a JSON object")
        return ProveAtRestCycle.from_dict(payload)

    def write(self, cycle: ProveAtRestCycle) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = json.dumps(cycle.to_dict(), indent=2, sort_keys=True) + "\n"
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(self.path)

    def record_confirm(
        self, identity_id: str, observation: ProbeObservation, *, now: datetime | None = None
    ) -> None:
        """Persist one budgeted confirm without replacing the rest of the cycle.

        A successful confirm refreshes that identity's passport. A failed
        confirm records the failure. Missing state is left untouched because a
        confirm must not invent a prove cycle.
        """
        cycle = self.read()
        if cycle is None:
            return
        current = now or observation.observed_at
        provider = _provider_of(identity_id)
        ready = observation.availability_state == "ready" and observation.error is None
        passport = (
            passport_from_probe(provider=provider, identity_id=identity_id, observation=observation)
            if ready
            else None
        )
        result = ProofResult(
            identity_id=identity_id,
            provider=provider,
            status=STATUS_HEALTHY if ready else STATUS_FAILED,
            reason=(
                None
                if ready
                else (observation.error_class or observation.error or observation.status)
            ),
            proved_at=observation.observed_at,
            latency_ms=observation.latency_ms,
            http_status=observation.http_status,
            error_class=observation.error_class,
            passport=passport,
        )
        replaced = False
        results: list[ProofResult] = []
        for item in cycle.results:
            if item.identity_id == identity_id and not replaced:
                results.append(result)
                replaced = True
            else:
                results.append(item)
        if not replaced:
            results.append(result)
        self.write(
            replace(
                cycle,
                finished_at=current if current >= cycle.started_at else cycle.finished_at,
                results=tuple(results),
            )
        )


SnapshotLoader = Callable[[], OmniRouteAdmitSnapshot]
CycleErrorHandler = Callable[[Exception], None]


@dataclass
class ProveAtRestDaemon:
    """Background (or one-shot) prover for free∩active OmniRoute identities."""

    store: ProveAtRestStore
    snapshot_loader: SnapshotLoader
    transport: ProbeTransport
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS
    live: bool = False
    consented: bool = False
    clock: Callable[[], datetime] = field(default=_now)
    sleep: Callable[[float], None] = field(default=time.sleep)
    issue_passports: bool = True
    on_cycle_error: CycleErrorHandler | None = None
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (int, float))
            or self.interval_seconds <= 0
        ):
            raise ProveAtRestError("interval_seconds must be positive")
        if (
            isinstance(self.probe_timeout_seconds, bool)
            or not isinstance(self.probe_timeout_seconds, (int, float))
            or self.probe_timeout_seconds <= 0
        ):
            raise ProveAtRestError("probe_timeout_seconds must be positive")

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> ProveAtRestCycle | None:
        return self.store.read()

    def run_once(self) -> ProveAtRestCycle:
        """Prove free∩active identities once and persist the cycle."""
        started = self.clock()
        snapshot = self.snapshot_loader()
        receipt = admit_free_tier_active(snapshot)
        results: list[ProofResult] = []

        # Named skips from admit — never probed (inactive, opaque, ghosts, …).
        for drop in receipt.exclusions:
            results.append(
                ProofResult(
                    identity_id=drop.model_id,
                    provider=_provider_of(drop.model_id),
                    status=STATUS_SKIPPED,
                    reason=drop.reason,
                    proved_at=started,
                )
            )

        # Only free∩active concrete identities are probed.
        if receipt.admitted:
            results.extend(self._prove_admitted(receipt.admitted, observed_at=started))

        finished = self.clock()
        cycle = ProveAtRestCycle(
            cycle_id=str(uuid4()),
            started_at=started,
            finished_at=finished,
            results=tuple(results),
            admitted=receipt.admitted,
            active_providers=receipt.active_providers,
            free_tier_providers=receipt.free_tier_providers,
        )
        self.store.write(cycle)
        return cycle

    def run_forever(self) -> ProveAtRestCycle | None:
        """Loop ``run_once`` until ``stop()``; returns the last completed cycle."""
        self._stop.clear()
        last: ProveAtRestCycle | None = None
        while not self._stop.is_set():
            try:
                last = self.run_once()
            except Exception as exc:
                # A transient inventory or probe-plane failure must not kill the
                # long-running refresher. Keep the last complete state on disk,
                # surface the named failure to the host, and retry next interval.
                if self.on_cycle_error is not None:
                    self.on_cycle_error(exc)
            remaining = float(self.interval_seconds)
            while remaining > 0 and not self._stop.is_set():
                step = min(0.25, remaining)
                self.sleep(step)
                remaining -= step
        return last

    def _prove_admitted(
        self, admitted: Sequence[str], *, observed_at: datetime
    ) -> list[ProofResult]:
        n = max(len(admitted), 1)
        duration = max(60.0, float(n) * float(self.probe_timeout_seconds))
        runner = ProbeRunner(
            ProbePolicy(
                max_models_per_run=n,
                timeout_seconds=float(self.probe_timeout_seconds),
                max_duration_seconds=duration,
            )
        )
        observations = runner.run(
            list(admitted),
            self.transport,
            now=observed_at,
            live=self.live,
            consented=self.consented,
            provider="prove_at_rest",
            budget=ProbeBudget(
                provider="prove_at_rest",
                max_requests=n,
                max_tokens=n,
                max_duration_seconds=duration,
            ),
        )
        by_id = {item.model_id: item for item in observations}
        out: list[ProofResult] = []
        for identity_id in admitted:
            observation = by_id.get(identity_id)
            provider = _provider_of(identity_id)
            if observation is None:
                out.append(
                    ProofResult(
                        identity_id=identity_id,
                        provider=provider,
                        status=STATUS_FAILED,
                        reason="probe_missing",
                        proved_at=observed_at,
                    )
                )
                continue
            ready = observation.availability_state == "ready"
            passport = None
            if ready and self.issue_passports:
                passport = passport_from_probe(
                    provider=provider, identity_id=identity_id, observation=observation
                )
            if ready:
                out.append(
                    ProofResult(
                        identity_id=identity_id,
                        provider=provider,
                        status=STATUS_HEALTHY,
                        proved_at=observation.observed_at,
                        latency_ms=observation.latency_ms,
                        http_status=observation.http_status,
                        passport=passport,
                    )
                )
            else:
                out.append(
                    ProofResult(
                        identity_id=identity_id,
                        provider=provider,
                        status=STATUS_FAILED,
                        reason=observation.error_class or observation.error or observation.status,
                        proved_at=observation.observed_at,
                        latency_ms=observation.latency_ms,
                        http_status=observation.http_status,
                        error_class=observation.error_class,
                    )
                )
        return out


def build_live_daemon(
    *,
    state_path: Path | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    probe_timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    allow_live_probe: bool = False,
    transport: ProbeTransport | None = None,
) -> ProveAtRestDaemon:
    """Construct a daemon wired to OmniRoute env / explicit endpoint."""
    from verdict.free_tier_admit import normalize_omniroute_origin
    from verdict.probes import openai_probe_transport

    if base_url and base_url.strip():
        endpoint: tuple[str, str | None] = (base_url.strip(), api_key)
    else:
        found = omniroute_endpoint_from_env()
        if found is None:
            raise ProveAtRestError(
                "OmniRoute endpoint required; set OMNIROUTE_BASE_URL or pass --base-url"
            )
        endpoint = found
    origin, key = endpoint
    if api_key is not None:
        key = api_key
    origin = normalize_omniroute_origin(origin)

    def _loader() -> OmniRouteAdmitSnapshot:
        return load_omniroute_admit_snapshot(origin, api_key=key)

    if transport is None:
        transport = openai_probe_transport(f"{origin}/v1", api_key=key)
    return ProveAtRestDaemon(
        store=ProveAtRestStore(path=state_path or default_state_path()),
        snapshot_loader=_loader,
        transport=transport,
        interval_seconds=interval_seconds,
        probe_timeout_seconds=probe_timeout_seconds,
        live=True,
        consented=allow_live_probe,
    )


def load_healthy_passports(path: Path | None = None) -> dict[str, ModelPassport]:
    """Read persisted healthy passports for later admit (empty if missing)."""
    store = ProveAtRestStore(path=path or default_state_path())
    cycle = store.read()
    if cycle is None:
        return {}
    return {
        identity_id: passport
        for identity_id, passport in cycle.passports().items()
        if any(
            item.identity_id == identity_id and item.status == STATUS_HEALTHY
            for item in cycle.results
        )
    }


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "ENV_STATE_PATH",
    "PROVE_AT_REST_SCHEMA_VERSION",
    "STATUS_FAILED",
    "STATUS_HEALTHY",
    "STATUS_SKIPPED",
    "ProofResult",
    "ProveAtRestCycle",
    "ProveAtRestDaemon",
    "ProveAtRestError",
    "ProveAtRestStore",
    "build_live_daemon",
    "default_state_path",
    "load_healthy_passports",
    "passport_from_probe",
]
