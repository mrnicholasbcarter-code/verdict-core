"""Canonical live route admission.

Every controller and worker candidate set starts here. ``admit`` turns live
inventory, provider connections and normalized runtime evidence into an
``AdmittedSet``. Downstream code (ranking, probing, bridges, replacement,
relay alternatives) may only narrow that set. It can never rebuild or widen it:
the only public constructor is ``admit`` and the only derivations are
``narrow`` / ``exclude``.

Catalog presence and an active provider connection are descriptive inputs.
They are never launch authority on their own: a route with live evidence of
exhaustion, cooldown, auth failure or unavailability is dropped before ranking,
and a route with no runtime evidence stays explicitly ``unknown`` (admitted for
a bounded probe, never recorded as proven healthy).

A runtime source that was not found (``<source>:absent``) was not consulted.
With no consulted runtime source, every admitted route stays ``unknown``.

Launch gate: an admitted route may be launched only when its record is
``proven_healthy`` or a bounded live confirmation for that exact route has
succeeded (``record_confirmation``). A failed confirmation drops the route at
``HEALTHY``. ``require_launchable`` is the asserted precondition at every launch
site; it raises ``AdmissionBypassError``.

Construction guard: ``AdmittedSet`` requires a private module token. Building
one with ``object.__new__``, or by reading the private token, is unsupported
and bypasses every guarantee in this module.

Quota rows are evidence only (exhausted -> drop, unknown -> explicit).
Headroom windows and bounded confirmation are handled elsewhere.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import InitVar, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

GATEWAY_PREFIX = "omniroute/"
ACTIVE_CONTROLLER_ENV = "VERDICT_ACTIVE_CONTROLLER_ROUTE"
CONTROLLER_IDENTITY_UNKNOWN = "unknown"
_OPAQUE_PREFIXES = ("auto/", "combo/", "router/", "virtual/")
_OPAQUE_IDS = frozenset({"auto", "default", "best"})
_BAD_TEST_STATUS = frozenset(
    {
        "error",
        "failed",
        "expired",
        "unauthorized",
        "invalid",
        "unavailable",
        "disabled",
        "exhausted",
        "quota_exhausted",
        "rate_limited",
        "banned",
    }
)
_AUTH_CATEGORIES = frozenset({"authentication", "permission", "payment_required", "unauthorized"})
_EXHAUSTED_CATEGORIES = frozenset({"quota_exhausted", "exhausted", "rate_limited"})


class AdmissionStage(str, Enum):
    """Ordered admission stages. The first failed stage is recorded per route."""

    DISCOVERED = "DISCOVERED"  # present in live inventory, concrete (not opaque)
    ENTITLED = "ENTITLED"  # active, authorized provider connection
    HEALTHY = "HEALTHY"  # no fresh live evidence of unhealthiness
    AVAILABLE = "AVAILABLE"  # no cooldown / quota exhaustion / rate-limit window
    CAPABILITY = "CAPABILITY"  # required capabilities and context fit
    POLICY = "POLICY"  # deny policy
    WORKER_SCOPE = "WORKER_SCOPE"  # worker-only route-prefix / family scope
    CONTROLLER_EXCLUDED = "CONTROLLER_EXCLUDED"  # active controller identity
    DOWNSTREAM = "DOWNSTREAM"  # any later narrowing (ranking input filters)


class AdmissionUnavailableError(RuntimeError):
    """Authoritative live admission was required but its evidence is missing."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class AdmissionBypassError(RuntimeError):
    """A downstream stage tried to use a route outside the admitted set."""

    def __init__(self, surface: str, route_ids: Iterable[str]) -> None:
        self.surface = surface
        self.route_ids = tuple(sorted(set(route_ids)))
        super().__init__(
            f"{surface}: routes outside the canonical admitted set: {', '.join(self.route_ids)}"
        )


def canonical_route_id(route_id: str) -> str:
    """Gateway-neutral route id: ``omniroute/gc/x`` and ``gc/x`` compare equal."""
    text = str(route_id or "").strip()
    if text.lower().startswith(GATEWAY_PREFIX):
        text = text[len(GATEWAY_PREFIX) :]
    return text


def is_opaque(route_id: str) -> bool:
    lowered = canonical_route_id(route_id).lower()
    return lowered in _OPAQUE_IDS or lowered.startswith(_OPAQUE_PREFIXES)


def route_provider_prefix(route_id: str) -> str:
    text = canonical_route_id(route_id)
    return text.split("/", 1)[0].lower() if "/" in text else ""


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RuntimeObservation:
    """One normalized piece of live runtime evidence for a route or provider.

    ``state`` is one of ``healthy``, ``unhealthy``, ``unauthorized``,
    ``exhausted``, ``cooldown`` or ``unknown``.
    """

    key: str  # "route:<canonical id>" or "provider:<name>"
    state: str
    category: str
    source: str
    observed_at: str | None = None
    until: str | None = None


@dataclass(frozen=True)
class RuntimeEvidence:
    """Normalized runtime evidence. ``sources`` lists every source consulted."""

    observations: tuple[RuntimeObservation, ...] = ()
    sources: tuple[str, ...] = ()

    def for_key(self, key: str) -> tuple[RuntimeObservation, ...]:
        return tuple(o for o in self.observations if o.key == key)

    @property
    def consulted(self) -> tuple[str, ...]:
        """Runtime sources that were actually read. ``:absent`` sources do not count."""
        return tuple(s for s in self.sources if not s.endswith(":absent"))

    def merged(self, other: RuntimeEvidence) -> RuntimeEvidence:
        return RuntimeEvidence(
            self.observations + other.observations,
            tuple(dict.fromkeys(self.sources + other.sources)),
        )


def evidence_from_ladder_state(
    path: Path, *, now: datetime, healthy_ttl_seconds: float = 300.0
) -> RuntimeEvidence:
    """Health and cooldowns persisted by the orchestration ladder."""
    source = f"ladder_state:{path.name}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RuntimeEvidence((), (source + ":absent",))
    out: list[RuntimeObservation] = []
    health = raw.get("health") if isinstance(raw, dict) else None
    for route_id, entry in (health or {}).items() if isinstance(health, dict) else ():
        if not isinstance(entry, dict):
            continue
        checked = _parse_iso(entry.get("checked_at"))
        if checked is None or (now - checked).total_seconds() > healthy_ttl_seconds:
            continue  # stale evidence proves nothing either way
        category = str(entry.get("category", "") or "")
        state = "healthy" if entry.get("healthy") is True else _state_for_category(category)
        out.append(
            RuntimeObservation(
                f"route:{canonical_route_id(route_id)}",
                state,
                category or state,
                source,
                _iso(checked),
            )
        )
    cooldowns = raw.get("cooldowns") if isinstance(raw, dict) else None
    for key, entry in (cooldowns or {}).items() if isinstance(cooldowns, dict) else ():
        if not isinstance(entry, dict):
            continue
        until = _parse_iso(entry.get("until"))
        if until is None or until <= now:
            continue
        scope, _, name = str(key).partition(":")
        if scope not in {"route", "provider"} or not name:
            continue
        norm = canonical_route_id(name) if scope == "route" else name.lower()
        category = str(entry.get("category", "cooldown") or "cooldown")
        out.append(
            RuntimeObservation(f"{scope}:{norm}", "cooldown", category, source, None, _iso(until))
        )
    return RuntimeEvidence(tuple(out), (source,))


def evidence_from_health_cache(path: Path, *, now: datetime) -> RuntimeEvidence:
    """Unexpired worker probe / runtime outcomes from the worker HealthCache."""
    source = f"health_cache:{path.name}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RuntimeEvidence((), (source + ":absent",))
    out: list[RuntimeObservation] = []
    for key, entry in raw.items() if isinstance(raw, dict) else ():
        if not isinstance(entry, dict):
            continue
        expires = _parse_iso(entry.get("expires_at"))
        if expires is None or expires <= now:
            continue
        category = str(entry.get("category", "unknown") or "unknown")
        healthy = entry.get("healthy") is True
        if str(key).startswith("provider:"):
            if healthy:
                continue  # provider-level healthy is not route proof
            out.append(
                RuntimeObservation(
                    str(key).lower(),
                    "cooldown",
                    category,
                    source,
                    entry.get("observed_at"),
                    _iso(expires),
                )
            )
            continue
        state = "healthy" if healthy else _state_for_category(category)
        out.append(
            RuntimeObservation(
                f"route:{canonical_route_id(str(key))}",
                state,
                category,
                source,
                entry.get("observed_at"),
                None if healthy else _iso(expires),
            )
        )
    return RuntimeEvidence(tuple(out), (source,))


def evidence_from_quota_rows(
    rows: Iterable[Mapping[str, Any]], *, source: str = "quota"
) -> RuntimeEvidence:
    """Pass-through quota evidence: exhausted drops, anything else is explicit unknown.

    Row shape: ``{"route_id"| "provider": str, "exhausted": bool | None,
    "observed_at": str, "until": str | None, "source": str}``.
    """
    out: list[RuntimeObservation] = []
    for row in rows:
        route_id = row.get("route_id")
        provider = row.get("provider")
        if isinstance(route_id, str) and route_id.strip():
            key = f"route:{canonical_route_id(route_id)}"
        elif isinstance(provider, str) and provider.strip():
            key = f"provider:{provider.strip().lower()}"
        else:
            continue
        exhausted = row.get("exhausted")
        state = "exhausted" if exhausted is True else "unknown"
        out.append(
            RuntimeObservation(
                key,
                state,
                "quota_exhausted" if exhausted is True else "quota_unknown",
                str(row.get("source") or source),
                str(row.get("observed_at")) if row.get("observed_at") else None,
                str(row.get("until")) if row.get("until") else None,
            )
        )
    return RuntimeEvidence(tuple(out), (source,))


def _state_for_category(category: str) -> str:
    lowered = category.strip().lower()
    if lowered in _AUTH_CATEGORIES:
        return "unauthorized"
    if lowered in _EXHAUSTED_CATEGORIES:
        return "exhausted"
    if lowered in {"", "unknown", "unprobed"}:
        return "unknown"
    return "unhealthy"


@dataclass(frozen=True)
class AdmissionRecord:
    """Per-candidate admission trace. ``first_failed_stage`` is None when admitted."""

    route_id: str
    provider: str
    admitted: bool
    first_failed_stage: AdmissionStage | None
    reason: str
    source: str
    observed_at: str | None
    health: str  # healthy | unknown | <failure state>
    until: str | None = None
    # Bounded live confirmation for this exact route, recorded before launch.
    confirmation_source: str | None = None
    confirmed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "provider": self.provider,
            "admitted": self.admitted,
            "first_failed_stage": (
                self.first_failed_stage.value if self.first_failed_stage else None
            ),
            "reason": self.reason,
            "source": self.source,
            "observed_at": self.observed_at,
            "health": self.health,
            "until": self.until,
            "confirmation_source": self.confirmation_source,
            "confirmed_at": self.confirmed_at,
        }


class _MintToken:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<admission mint token>"


# Capability, not a flag: only code holding this object can build a set.
_MINT = _MintToken()


def _build_admitted_set(token: object, **kwargs: Any) -> AdmittedSet:
    if token is not _MINT:
        raise TypeError("AdmittedSet can only be produced by verdict.admission.admit()")
    return AdmittedSet(**kwargs, _mint=token)


@dataclass(frozen=True)
class AdmittedSet:
    """Canonical admitted routes. Construct only through ``admit``; narrow only."""

    records: tuple[AdmissionRecord, ...]
    generated_at: str
    sources: tuple[str, ...]
    controller_identity: str = CONTROLLER_IDENTITY_UNKNOWN
    narrowing: tuple[str, ...] = ()
    runtime_consulted: tuple[str, ...] = ()
    _mint: InitVar[object] = None

    def __post_init__(self, _mint: object) -> None:
        # Direct construction and dataclasses.replace() both land here without
        # the private token (replace() does not carry InitVars), so neither can
        # mint or widen an admitted set.
        if _mint is not _MINT:
            raise TypeError("AdmittedSet can only be produced by verdict.admission.admit()")

    # ---------------------------------------------------------------- queries
    @property
    def ids(self) -> frozenset[str]:
        return frozenset(r.route_id for r in self.records if r.admitted)

    def __contains__(self, route_id: object) -> bool:
        return isinstance(route_id, str) and canonical_route_id(route_id) in self.ids

    def __len__(self) -> int:
        return len(self.ids)

    def record_for(self, route_id: str) -> AdmissionRecord | None:
        key = canonical_route_id(route_id)
        for record in self.records:
            if record.route_id == key:
                return record
        return None

    def proven_healthy(self, route_id: str) -> bool:
        record = self.record_for(route_id)
        return bool(record and record.admitted and record.health == "healthy")

    def launchable(self, route_id: str) -> bool:
        """Launch gate: proven healthy, or confirmed live for this exact route."""
        record = self.record_for(route_id)
        if record is None or not record.admitted:
            return False
        return record.health == "healthy" or record.confirmation_source is not None

    def require_launchable(self, route_id: str, *, surface: str) -> AdmissionRecord:
        """Asserted launch precondition. Raises ``AdmissionBypassError`` when violated."""
        if not self.launchable(route_id):
            raise AdmissionBypassError(surface, [canonical_route_id(route_id)])
        record = self.record_for(route_id)
        assert record is not None
        return record

    def launch_authority(self, route_id: str) -> dict[str, Any]:
        """Why ``route_id`` may (or may not) launch, with source and time."""
        record = self.first_failure(route_id)
        if record.admitted and record.health == "healthy":
            basis = "proven_healthy"
            source, observed_at = record.source, record.observed_at
        elif record.admitted and record.confirmation_source is not None:
            basis = "live_confirmation"
            source, observed_at = record.confirmation_source, record.confirmed_at
        else:
            basis = "none"
            source, observed_at = record.source, record.observed_at
        stage = record.first_failed_stage.value if record.first_failed_stage else None
        return {
            "route_id": record.route_id,
            "basis": basis,
            "source": source,
            "observed_at": observed_at,
            "health": record.health,
            "first_failed_stage": stage,
            "reason": record.reason,
        }

    def record_confirmation(
        self, route_id: str, *, healthy: bool, source: str, observed_at: str, category: str = ""
    ) -> AdmittedSet:
        """Record a bounded live confirmation for one exact route.

        Success marks the admitted record as confirmed (``health`` is left as
        observed). Failure drops the route at ``HEALTHY`` with the probe's
        category. A route that is not admitted is never re-admitted.
        """
        key = canonical_route_id(route_id)
        records: list[AdmissionRecord] = []
        for r in self.records:
            if r.route_id != key or not r.admitted:
                records.append(r)
            elif healthy:
                records.append(replace(r, confirmation_source=source, confirmed_at=observed_at))
            else:
                records.append(
                    replace(
                        r,
                        admitted=False,
                        first_failed_stage=AdmissionStage.HEALTHY,
                        reason=category or "confirmation_failed",
                        source=source,
                        observed_at=observed_at,
                        health=_state_for_category(category or "unhealthy"),
                    )
                )
        outcome = "confirmed" if healthy else f"failed:{category or 'unhealthy'}"
        return self._derive(tuple(records), f"CONFIRMATION:{key}:{outcome}@{source}")

    def first_failure(self, route_id: str) -> AdmissionRecord:
        """The record that explains why ``route_id`` is not admitted."""
        record = self.record_for(route_id)
        if record is not None:
            return record
        return AdmissionRecord(
            canonical_route_id(route_id),
            route_provider_prefix(route_id),
            False,
            AdmissionStage.DISCOVERED,
            "absent_from_live_inventory",
            "admission",
            self.generated_at,
            "unknown",
        )

    def require_subset(self, route_ids: Iterable[str], *, surface: str) -> None:
        outside = [r for r in route_ids if r not in self]
        if outside:
            raise AdmissionBypassError(surface, outside)

    # ------------------------------------------------------------- narrowing
    def narrow(
        self, stage: AdmissionStage, reason: str, keep: Callable[[str], bool], *, source: str = ""
    ) -> AdmittedSet:
        """Drop admitted routes for which ``keep`` is False. Never re-admits."""
        records = tuple(
            replace(
                r,
                admitted=False,
                first_failed_stage=stage,
                reason=reason,
                source=source or r.source,
            )
            if r.admitted and not keep(r.route_id)
            else r
            for r in self.records
        )
        return self._derive(records, f"{stage.value}:{reason}")

    def exclude(
        self, route_ids: Iterable[str], stage: AdmissionStage, reason: str, *, source: str = ""
    ) -> AdmittedSet:
        drop = {canonical_route_id(r) for r in route_ids if r}
        return self.narrow(stage, reason, lambda rid: rid not in drop, source=source)

    def restrict_prefixes(self, prefixes: Sequence[str]) -> AdmittedSet:
        """Worker route-prefix scope. An extra narrowing, never a replacement."""
        cleaned = tuple(p.strip() for p in prefixes if p and p.strip())
        if not cleaned:
            return self
        # A bare family ("kr") matches on the family boundary ("kr/..."), so it
        # never admits "krypton/...". A prefix with "/" narrows within a family.
        canon = tuple(
            c if "/" in c else c + "/" for c in (canonical_route_id(p) for p in cleaned) if c
        )
        return self.narrow(
            AdmissionStage.WORKER_SCOPE,
            "outside_worker_route_prefix",
            lambda rid: rid.startswith(canon),
            source="worker_scope",
        )

    def restrict_families(self, families: Sequence[str]) -> AdmittedSet:
        wanted = {f.strip().lower() for f in families if f and f.strip()}
        if not wanted:
            return self
        return self.narrow(
            AdmissionStage.WORKER_SCOPE,
            "outside_provider_family",
            lambda rid: route_provider_prefix(rid) in wanted,
            source="worker_scope",
        )

    def exclude_controller(self, identity: str | None) -> AdmittedSet:
        """Hard-exclude the active controller from worker admission.

        An unknown identity is recorded as ``unknown``; nothing is guessed.
        """
        if not identity or not identity.strip():
            return self._derive(
                self.records,
                "CONTROLLER_EXCLUDED:identity_unknown",
                controller_identity=CONTROLLER_IDENTITY_UNKNOWN,
            )
        canon = canonical_route_id(identity)
        narrowed = self.exclude(
            [canon],
            AdmissionStage.CONTROLLER_EXCLUDED,
            "active_controller_identity",
            source="controller_identity",
        )
        return narrowed._derive(narrowed.records, None, controller_identity=canon)

    def _derive(
        self,
        records: tuple[AdmissionRecord, ...],
        step: str | None,
        *,
        controller_identity: str | None = None,
    ) -> AdmittedSet:
        # Structural guard: a derivation can only keep or drop, never admit.
        before = self.ids
        after = frozenset(r.route_id for r in records if r.admitted)
        if not after <= before:
            raise AdmissionBypassError("AdmittedSet._derive", after - before)
        return _build_admitted_set(
            _MINT,
            records=records,
            generated_at=self.generated_at,
            sources=self.sources,
            controller_identity=controller_identity or self.controller_identity,
            narrowing=self.narrowing + ((step,) if step else ()),
            runtime_consulted=self.runtime_consulted,
        )

    # --------------------------------------------------------------- receipt
    def receipt(self) -> dict[str, Any]:
        body = {
            "schema": "verdict.admission/v1",
            "generated_at": self.generated_at,
            "sources": list(self.sources),
            "runtime_consulted": list(self.runtime_consulted),
            "controller_identity": self.controller_identity,
            "narrowing": list(self.narrowing),
            "admitted": sorted(self.ids),
            "candidates": [r.to_dict() for r in self.records],
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {**body, "digest": f"sha256:{digest}"}

    def write_receipt(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.receipt(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path


def _connections_for(
    connections: Sequence[Mapping[str, Any]], names: Iterable[str]
) -> list[Mapping[str, Any]]:
    wanted = {n.lower() for n in names if n}
    return [c for c in connections if str(c.get("provider", "")).lower() in wanted]


def _judge(
    route_id: str,
    row: Mapping[str, Any],
    connections: Sequence[Mapping[str, Any]],
    runtime: RuntimeEvidence,
    *,
    now: datetime,
    connections_source: str,
    required_capabilities: frozenset[str],
    min_context_tokens: int,
    deny: Callable[[str], str | None] | None,
) -> AdmissionRecord:
    owned = str(row.get("owned_by", "") or "").lower()
    prefix = route_provider_prefix(route_id)
    provider = owned or prefix
    stamp = _iso(now)

    def drop(stage: AdmissionStage, reason: str, source: str, **kw: Any) -> AdmissionRecord:
        return AdmissionRecord(
            route_id,
            provider,
            False,
            stage,
            reason,
            source,
            kw.get("observed_at", stamp),
            kw.get("health", "unknown"),
            kw.get("until"),
        )

    if is_opaque(route_id) or owned == "combo":
        return drop(AdmissionStage.DISCOVERED, "opaque_route", "inventory")

    conns = _connections_for(connections, (owned, prefix))
    active = [c for c in conns if c.get("isActive") is True]
    if not active:
        reason = "no_connection_evidence" if not conns else "no_active_account"
        return drop(AdmissionStage.ENTITLED, reason, connections_source)
    statuses = {str(c.get("testStatus", "") or "").strip().lower() for c in active}
    if statuses and statuses <= _BAD_TEST_STATUS:
        status = sorted(statuses)[0]
        return drop(
            AdmissionStage.ENTITLED,
            f"connection_status:{status}",
            connections_source,
            health=_state_for_category(status),
        )
    for conn in active:
        windows = conn.get("rate_limited_until") or {}
        if not isinstance(windows, Mapping):
            continue
        for key, value in windows.items():
            if "/" in str(key) and canonical_route_id(str(key)) != route_id:
                continue
            until = _parse_iso(value)
            if until is not None and until > now:
                return drop(
                    AdmissionStage.AVAILABLE,
                    "provider_rate_limited",
                    connections_source,
                    health="exhausted",
                    until=_iso(until),
                )

    route_obs = runtime.for_key(f"route:{route_id}")
    provider_obs = tuple(
        o for name in {owned, prefix} if name for o in runtime.for_key(f"provider:{name}")
    )
    for obs in route_obs + provider_obs:
        if obs.state in {"unauthorized"}:
            return drop(
                AdmissionStage.ENTITLED,
                obs.category,
                obs.source,
                observed_at=obs.observed_at,
                health=obs.state,
                until=obs.until,
            )
    for obs in route_obs:
        if obs.state == "unhealthy":
            return drop(
                AdmissionStage.HEALTHY,
                obs.category,
                obs.source,
                observed_at=obs.observed_at,
                health=obs.state,
                until=obs.until,
            )
    for obs in route_obs + provider_obs:
        if obs.state in {"exhausted", "cooldown"}:
            return drop(
                AdmissionStage.AVAILABLE,
                f"{obs.state}:{obs.category}",
                obs.source,
                observed_at=obs.observed_at,
                health=obs.state,
                until=obs.until,
            )

    if required_capabilities or min_context_tokens:
        caps = row.get("capabilities") or {}
        caps = caps if isinstance(caps, Mapping) else {}
        for needed in sorted(required_capabilities):
            key = {"tools": "tool_calling"}.get(needed, needed)
            if caps.get(key) is not True:
                return drop(AdmissionStage.CAPABILITY, f"missing_capability:{needed}", "inventory")
        context = int(row.get("max_input_tokens") or row.get("context_length") or 0)
        if context < min_context_tokens:
            return drop(AdmissionStage.CAPABILITY, "insufficient_context", "inventory")

    if deny is not None:
        denied = deny(route_id)
        if denied:
            return drop(AdmissionStage.POLICY, denied, "policy")

    healthy = [o for o in route_obs if o.state == "healthy"]
    if healthy:
        latest = healthy[-1]
        return AdmissionRecord(
            route_id, provider, True, None, "admitted", latest.source, latest.observed_at, "healthy"
        )
    # No runtime truth: admitted for a bounded probe, explicitly unknown.
    return AdmissionRecord(
        route_id, provider, True, None, "admitted_unverified", connections_source, stamp, "unknown"
    )


def admit(
    inventory_rows: Sequence[Mapping[str, Any]] | None,
    connections: Sequence[Mapping[str, Any]] | None,
    runtime: RuntimeEvidence | None,
    *,
    now: datetime,
    require_runtime: bool = True,
    inventory_source: str = "omniroute:/v1/models",
    connections_source: str = "omniroute:/api/providers",
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
    deny: Callable[[str], str | None] | None = None,
) -> AdmittedSet:
    """Build the canonical admitted set from live evidence.

    Raises ``AdmissionUnavailableError`` when inventory or connections are
    missing, or when ``require_runtime`` is set and no runtime evidence object
    was supplied. There is no catalog-only fallback.

    Runtime sources marked ``:absent`` were not consulted. When none was
    consulted, every admitted route is ``admitted_unverified`` / ``unknown``
    and ``runtime_consulted`` is empty: nothing is launchable until a bounded
    live confirmation for that exact route succeeds.
    """
    if inventory_rows is None:
        raise AdmissionUnavailableError("live_inventory_unavailable", inventory_source)
    if connections is None:
        raise AdmissionUnavailableError("connection_evidence_unavailable", connections_source)
    if runtime is None and require_runtime:
        raise AdmissionUnavailableError(
            "runtime_evidence_unavailable", "no runtime health/cooldown/quota source"
        )
    evidence = runtime or RuntimeEvidence()
    rows: dict[str, Mapping[str, Any]] = {}
    for row in inventory_rows:
        rid = row.get("id")
        if isinstance(rid, str) and rid.strip():
            rows.setdefault(canonical_route_id(rid), row)
    records = tuple(
        _judge(
            rid,
            rows[rid],
            list(connections),
            evidence,
            now=now,
            connections_source=connections_source,
            required_capabilities=frozenset(required_capabilities),
            min_context_tokens=min_context_tokens,
            deny=deny,
        )
        for rid in sorted(rows)
    )
    return _build_admitted_set(
        _MINT,
        records=records,
        generated_at=_iso(now),
        sources=tuple(dict.fromkeys((inventory_source, connections_source, *evidence.sources))),
        runtime_consulted=evidence.consulted,
    )


def active_controller_route(env: Mapping[str, str] | None = None) -> str | None:
    """Active controller identity exported by the supervisor, or None if unknown."""
    value = (env if env is not None else os.environ).get(ACTIVE_CONTROLLER_ENV, "")
    value = value.strip()
    return value or None


def default_runtime_evidence(
    *, now: datetime, state_dir: Path | None = None, ladder_state: Path | None = None
) -> RuntimeEvidence:
    """Local persisted runtime evidence: ladder health/cooldowns + worker HealthCache.

    ``ladder_state`` names the ladder state file the caller actually uses
    (for example a ``--state-file`` or a per-run chaos file). It defaults to
    ``orchestration-health.json`` in ``state_dir``.
    """
    base = state_dir or Path(os.environ.get("VERDICT_HOME", Path.home() / ".verdict"))
    ladder = ladder_state or (base / "orchestration-health.json")
    return evidence_from_ladder_state(ladder, now=now).merged(
        evidence_from_health_cache(base / "subagent-health.json", now=now)
    )


def load_live_admission(
    gateway: str,
    *,
    now: datetime,
    api_key: str | None = None,
    state_dir: Path | None = None,
    inventory_rows: Sequence[Mapping[str, Any]] | None = None,
    timeout: float = 30,
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
) -> AdmittedSet:
    """Read-only live admission against an OmniRoute gateway (GET only).

    When the caller knows the task requirements, pass them so CAPABILITY drops
    appear in the canonical receipt.

    Any fetch failure is ``AdmissionUnavailableError``: authoritative callers
    fail closed instead of falling back to catalog truth.
    """
    from verdict.orchestration.run import fetch_connections, fetch_inventory

    base = gateway.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        rows = (
            list(inventory_rows)
            if inventory_rows is not None
            else fetch_inventory(base, api_key=api_key, timeout=timeout)
        )
    except Exception as exc:
        raise AdmissionUnavailableError("live_inventory_unavailable", str(exc)[:200]) from exc
    try:
        connections = fetch_connections(base, api_key=api_key, timeout=timeout)
    except Exception as exc:
        raise AdmissionUnavailableError("connection_evidence_unavailable", str(exc)[:200]) from exc
    return admit(
        rows,
        connections,
        default_runtime_evidence(now=now, state_dir=state_dir),
        now=now,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
    )


__all__ = [
    "ACTIVE_CONTROLLER_ENV",
    "CONTROLLER_IDENTITY_UNKNOWN",
    "AdmissionBypassError",
    "AdmissionRecord",
    "AdmissionStage",
    "AdmissionUnavailableError",
    "AdmittedSet",
    "RuntimeEvidence",
    "RuntimeObservation",
    "active_controller_route",
    "admit",
    "canonical_route_id",
    "default_runtime_evidence",
    "evidence_from_health_cache",
    "evidence_from_ladder_state",
    "evidence_from_quota_rows",
    "is_opaque",
    "load_live_admission",
]
