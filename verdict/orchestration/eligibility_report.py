"""Eligibility reporting and selector construction for orchestration.

Moved from verdict.orchestration.cli to avoid CLI-layer dependencies in actions.
These functions and their private helpers support eligibility evaluation and
reporting without depending on the CLI argument parsing layer.
"""

from __future__ import annotations

import json
import os
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.orchestration.contracts import OrchestrationError
from verdict.orchestration.health_cache import HealthCache, default_cache_path
from verdict.orchestration.session_evidence import SessionLedger
from verdict.paths import verdict_home

# Omitted openrouter_free_set: cache-only read from the caller-owned state dir.
# Explicit None: no FREE assertion. Explicit set: use as-is.
# Live refresh is owned by prove_at_rest / dedicated refresh (same cache path).
_OPENROUTER_FREE_SET_OMITTED = object()


def parse_provider_families(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """``["cc,kr", "gc"]`` -> ``("cc", "gc", "kr")`` (deduplicated, sorted, lowercase)."""
    found = {part.strip().lower() for value in values for part in value.split(",")}
    return tuple(sorted(f for f in found if f))


def route_prefix(route_id: str) -> str:
    """Provider family of a gateway route id: the prefix before '/' (``cc/x`` -> ``cc``)."""
    return route_id.split("/", 1)[0].lower() if "/" in route_id else route_id.lower()


def _is_omniroute_gateway(gateway: str) -> bool:
    """Check if gateway is an OmniRoute gateway (not a generic OpenAI-compatible one).

    Only OmniRoute gateways serve /api/free-provider-rankings. Generic gateways
    should be skipped with a named omission rather than probed.
    """
    gateway_lower = gateway.lower()
    # OmniRoute is always localhost-based; the rankings endpoint is OmniRoute-only.
    return (
        "omniroute" in gateway_lower or "localhost" in gateway_lower or "127.0.0.1" in gateway_lower
    )


def prime_visibility_report(live_rows: Any, *, prime_home: Path | None = None) -> dict[str, Any]:
    """Compare live concrete gateway models with Prime's registry. Never writes."""
    from verdict.harness_prime import resolve_paths
    from verdict.prime_inventory import _read, concrete_rows, sidecar_path

    paths = resolve_paths(prime_home=prime_home)
    live_concrete, excluded = concrete_rows(live_rows)
    live = {str(row["id"]) for row in live_concrete}
    visible: set[str] = set()
    registry_error = None
    try:
        data = json.loads(paths.models.read_text(encoding="utf-8"))
        models = data["providers"]["omniroute"]["models"]
        visible = {str(m["id"]) for m in models if isinstance(m, dict) and m.get("id")}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        registry_error = type(exc).__name__
    snapshot, failure = _read(sidecar_path(paths.models))
    return {
        "schema": "verdict.prime-visibility/v1",
        "registry": str(paths.models),
        "registry_error": registry_error,
        "live_count": len(live),
        "live_opaque_excluded": len(excluded),
        "prime_visible_count": len(visible),
        "live_not_visible": sorted(live - visible),
        "visible_not_live": sorted(visible - live),
        "in_sync": registry_error is None and live == visible,
        "last_refresh": snapshot.to_dict() | {"route_ids": None} if snapshot else None,
        "last_failure": failure.to_dict() if failure else None,
    }


def prime_visibility(path: Path | None = None, *, live_rows: Any = None, sync: bool = False) -> Any:
    """Return the concrete ids Prime can actually spawn, never gateway authority.

    Read-only by default (``sync=False``): this never writes Prime's registry
    on its own, even when ``live_rows`` is supplied. Per OpenSpec
    bod-293-295-harness-bootstrap-autocomplete/design.md ("Selection SHALL
    never implicitly call automatic visibility refresh"), writing Prime's
    registry from live rows is something only an explicit, operator-invoked
    surface may request -- today that is ``verdict harness prime sync-models``
    and the opt-in ``verdict eligibility --sync-visibility`` flag, both of
    which pass ``sync=True`` deliberately. Everything else (plain
    ``verdict eligibility``, ``verdict orchestrate``) reads the registry as it
    currently stands on disk and reports accordingly; a failed/skipped sync
    leaves whatever LKG sidecar already exists for audit, but never invents
    visibility. Health, capacity, entitlement and launch authority stay in
    their separate gates regardless of ``sync``.
    """
    from verdict.harness_prime import resolve_paths
    from verdict.orchestration.eligibility import HarnessVisibility

    registry = path or resolve_paths().models
    if sync and live_rows is not None:
        from verdict.harness_prime import refresh_omniroute_visibility

        rows = tuple(row for row in live_rows if isinstance(row, Mapping))
        # A successful refresh changes Prime's local registry before selection.
        # On failure its LKG remains auditable, but only registry ids below can
        # be claimed as actually spawn-visible.
        refresh_omniroute_visibility(
            prime_home=registry.parent,
            source="omniroute:/v1/models",
            fetch_rows=lambda: rows,
            force=True,
        )
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
        models = data["providers"]["omniroute"]["models"]
        visible = frozenset(str(m["id"]) for m in models if isinstance(m, dict) and m.get("id"))
    except (OSError, ValueError, KeyError, TypeError):
        return HarnessVisibility(None, source="none")
    return HarnessVisibility(visible, source="models.json")


class GatewayDiscoveryError(OrchestrationError):
    """Safe discovery failure with an explicit, conservative restart policy."""

    def __init__(self, endpoint: str, status_code: int | None) -> None:
        self.status_code = status_code
        # Authentication and permission failures cannot be repaired by an
        # unchanged process restart. Other discovery failures retain recovery.
        self.retryable = status_code not in {401, 403}
        failure = f"HTTP {status_code}" if status_code is not None else "connection error"
        super().__init__(
            f"gateway {endpoint} failed ({failure}); check --gateway and gateway credentials"
        )


def _gateway_fetch_error(endpoint: str, exc: urllib.error.URLError | OSError) -> OrchestrationError:
    """Give actionable endpoint context without exposing request headers or credentials."""
    status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    return GatewayDiscoveryError(endpoint, status)


def build_selector(
    gateway: str,
    *,
    scope: str,
    prefer: str,
    load: Any = None,
    state_file: Path | None = None,
    provider_families: tuple[str, ...] = (),
    required_capabilities: frozenset[str] = frozenset(),
    min_context_tokens: int = 0,
    capacity: tuple[str, ...] = (),
    route_ids: frozenset[str] | None = None,
    refresh_hook: Callable[[Sequence[str], datetime], Mapping[str, str] | None] | None = None,
    sync_visibility: bool = False,
    visibility_executor: str = "prime",
    health_cache: HealthCache | None = None,
    session_ledger: SessionLedger | None = None,
    openrouter_free_set: Any = _OPENROUTER_FREE_SET_OMITTED,
    now: datetime | None = None,
    no_confirm: bool = False,
    task_category: str = "general",
) -> Any:
    """Admitted eligibility ladder over the live gateway.

    ``required_capabilities`` / ``min_context_tokens`` are the floor every
    selection from this ladder needs. They go into canonical admission so
    CAPABILITY drops appear in the receipt. The ladder still applies each
    request's own requirements later.

    ``sync_visibility`` defaults to False: selection reads Prime's visibility
    registry as-is and never writes it. Pass True only from a caller that
    exists to make that write explicit (``--sync-visibility``); see
    ``prime_visibility``'s docstring for the OpenSpec requirement this keeps.

    ``now`` defaults to wall-clock UTC. Pass an explicit clock so cache TTL
    checks and admission use the same instant as the caller (tests / replay).
    """
    from verdict.admission import (
        AdmissionStage,
        active_controller_route,
        admit,
        default_runtime_evidence,
    )
    from verdict.omniroute_health import load_omniroute_health
    from verdict.omniroute_rankings import OmniRouteRankings
    from verdict.openrouter_free_set import OpenRouterFreeSet, load_openrouter_free_set
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.run import fetch_connections, fetch_inventory, resolve_api_key
    from verdict.subagent_selection import HealthResult, LaunchCandidate, openai_health_probe

    gateway = normalize_omniroute_origin(gateway)
    key = resolve_api_key()
    try:
        rows = fetch_inventory(gateway, api_key=key)
    except (urllib.error.URLError, OSError) as exc:
        raise _gateway_fetch_error("/v1/models", exc) from None
    try:
        connections = fetch_connections(gateway, api_key=key, include_quota=True)
    except (urllib.error.URLError, OSError) as exc:
        raise _gateway_fetch_error("/api/providers", exc) from None
    state_path = state_file or (verdict_home() / "orchestration-health.json")
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # BOD-418: public OpenRouter 0/0 set. Selector construction is cache-only
    # against the caller-owned state dir (same path prove_at_rest refresh writes).
    # Missing cache => no openrouter/ FREE assertion (fail closed). Never fetch
    # or write as a side effect of building a selector over gateway inventory.
    if openrouter_free_set is _OPENROUTER_FREE_SET_OMITTED:
        owned_cache = state_path.parent / "openrouter-free-set.json"
        openrouter_free_set = load_openrouter_free_set(
            now=now, cache_path=owned_cache, refresh=False
        )
    elif not isinstance(openrouter_free_set, OpenRouterFreeSet):
        openrouter_free_set = None
    # Canonical admission over the full live inventory first. Scope, provider
    # family and the active controller are extra narrowing on that set.
    runtime = default_runtime_evidence(
        now=now, state_dir=state_path.parent, ladder_state=state_path
    ).merged(load_omniroute_health(gateway, api_key=key, now=now))
    if no_confirm:
        from verdict.admission import RuntimeEvidence, RuntimeObservation
        from verdict.admit_prove_confirm import passport_confirmation

        proof = passport_confirmation([str(row.get("id", "")) for row in rows], now=now)
        fresh = set(proof["admitted"])
        runtime = runtime.merged(
            RuntimeEvidence(
                tuple(
                    RuntimeObservation(
                        key=f"route:{row['identity_id']}",
                        state="healthy",
                        category="ok",
                        source="passport_confirmation",
                        observed_at=row.get("qualified_at"),
                        until=row.get("expires_at"),
                    )
                    for row in proof["passport"]
                    if row["identity_id"] in fresh
                ),
                ("passport_confirmation",),
            )
        )
    admitted = admit(
        rows,
        connections,
        runtime,
        now=now,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
    )
    prefixes = tuple(p.strip() for p in scope.split(",") if p.strip())
    admitted = admitted.restrict_prefixes(prefixes).restrict_families(provider_families)
    admitted = admitted.exclude_controller(active_controller_route())
    if route_ids is not None:
        admitted = admitted.narrow(
            AdmissionStage.POLICY,
            "outside_route_filter",
            lambda rid: rid in route_ids,
            source="selection:route_ids",
        )
    if capacity:
        if any(c not in {"free", "subscription", "metered", "unknown"} for c in capacity):
            raise ValueError("Invalid capacity filter")
        classifier = EligibilityLadder(
            rows,
            connections,
            lambda _: HealthResult(False, "not_probed"),
            state_path,
            openrouter_free_set=openrouter_free_set,
        )
        admitted = admitted.narrow(
            AdmissionStage.POLICY,
            "outside_capacity_filter",
            lambda rid: classifier.capacity_class(rid).value in capacity,
            source="orchestrate:capacity",
        )
    receipt_path = state_path.parent / "admission-latest.json"
    if not no_confirm:
        admitted.write_receipt(receipt_path)
    # Prime visibility reads (and, only when sync_visibility=True, refreshes)
    # from the FULL live catalog. Scope and provider-family filters only
    # narrow what this ladder evaluates; they must never shrink the
    # operator's Prime registry to the scoped subset.
    catalog_rows = list(rows)
    if prefixes:
        rows = [r for r in rows if str(r.get("id", "")).startswith(prefixes)]
    if provider_families:
        rows = [r for r in rows if route_prefix(str(r.get("id", ""))) in provider_families]
    raw_probe = (
        (lambda _: HealthResult(False, "not_probed"))
        if no_confirm
        else openai_health_probe(gateway.rstrip("/") + "/v1", api_key=key, timeout_seconds=30)
    )
    # Read the same health cache the selection refresh hook writes. With an
    # explicit --state-file (or --inject chaos state) that hook writes beside
    # the state file (StorePaths.defaults(state_dir)); otherwise both use
    # default_cache_path(). VERDICT_HEALTH_CACHE still wins everywhere.
    if state_file is not None and not (os.getenv("VERDICT_HEALTH_CACHE") or "").strip():
        cache_path = state_path.parent / "health-cache.json"
    else:
        cache_path = default_cache_path()

    def probe(route_id: str) -> Any:
        return raw_probe(
            LaunchCandidate(route_id, route_id, frozenset(), 0, 0.0, 0.0, False, False, 0, 0)
        )

    from verdict.orchestration.eligibility import HarnessVisibility
    from verdict.prime_inventory import concrete_rows

    if visibility_executor == "direct-gateway":
        live_concrete, _ = concrete_rows(catalog_rows)
        harness_visible = HarnessVisibility(
            frozenset(str(r["id"]) for r in live_concrete), source="live-gateway"
        )
    else:
        harness_visible = prime_visibility(live_rows=catalog_rows, sync=sync_visibility)

    # BOD-417: optional OmniRoute advisory rankings as task-aware tie-break among
    # already-admitted routes. Rankings are provider-level; map provider+topModel
    # to route_id and rank by score within the requested task category. Never
    # mix Arena (0.4-0.98) with AA coding (0-100); unknown stays 999 (baseline).
    # Only query OmniRoute gateways - generic OpenAI-compatible gateways are
    # skipped with a named omission.
    rankings: dict[str, int] = {}
    rankings_omission: str | None = None
    rankings_category: str | None = None
    if task_category and task_category != "general":
        rankings_category = task_category
        if not _is_omniroute_gateway(gateway):
            rankings_omission = "generic_gateway"
        else:
            try:
                rankings_client = OmniRouteRankings(gateway, key)
                result = rankings_client.fetch(task_category)
                if result.rows:
                    scored = sorted(result.rows, key=lambda r: r.score, reverse=True)
                    rankings = {
                        f"{row.id}/{row.top_model_id}": rank + 1 for rank, row in enumerate(scored)
                    }
                elif result.omission:
                    rankings_omission = result.omission
            except Exception:
                # Rankings are advisory; failure preserves baseline ordering.
                rankings_omission = "unavailable"
                rankings = {}
    return EligibilityLadder(
        rows,
        connections,
        probe,
        state_path,
        prefer_providers=tuple(p.strip() for p in prefer.split(",") if p.strip()),
        load=load,
        harness_visible=harness_visible,
        admitted=admitted,
        admission_receipt=None if no_confirm else receipt_path,
        refresh_hook=refresh_hook,
        health_cache=health_cache if health_cache is not None else HealthCache(cache_path),
        session_ledger=session_ledger if session_ledger is not None else SessionLedger(),
        openrouter_free_set=openrouter_free_set,
        max_probes_per_select=0 if no_confirm else 8,
        rankings=rankings or None,
        rankings_omission=rankings_omission,
        rankings_category=rankings_category,
    )


_STAGE_ORDER = ("DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED")


def eligibility_payload(
    verdicts: Any,
    summary: dict[str, int],
    chosen: Any,
    filters: dict[str, list[str]],
    *,
    health_cache: HealthCache | None = None,
    now: datetime | None = None,
    capacity_class: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Complete, self-reconciling eligibility record set (never truncated).

    ``summary`` keeps the ladder's cumulative stage counts and adds
    ``selected`` (the chosen route id or None, equal to ``selected.route_id``)
    and ``by_reached_stage``: exclusive buckets whose sum is ``evaluated_count``.
    """
    from verdict.prove_at_rest import status_report

    records = [v.to_dict() for v in verdicts]
    buckets = dict.fromkeys(_STAGE_ORDER, 0)
    buckets["NONE"] = 0
    for record in records:
        buckets[record["reached"] or "NONE"] += 1
    full_summary: dict[str, Any] = dict(summary)
    full_summary["selected"] = chosen.route_id if chosen else None
    full_summary["by_reached_stage"] = buckets
    return {
        "health_cache": (
            status_report(health_cache, now=now, capacity_class=capacity_class)
            if health_cache is not None
            else None
        ),
        "filters": filters,
        "evaluated_count": len(records),
        "summary": full_summary,
        "selected": chosen.to_dict() if chosen else None,
        "verdicts": records,
    }
