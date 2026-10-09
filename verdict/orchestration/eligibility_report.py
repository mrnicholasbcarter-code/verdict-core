"""Eligibility reporting and selector construction for orchestration.

Moved from verdict.orchestration.cli to avoid CLI-layer dependencies in actions.
These functions and their private helpers support eligibility evaluation and
reporting without depending on the CLI argument parsing layer.
"""

from __future__ import annotations

import json
import os
import urllib.error
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.orchestration.contracts import OrchestrationError


def parse_provider_families(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """``["cc,kr", "gc"]`` -> ``("cc", "gc", "kr")`` (deduplicated, sorted, lowercase)."""
    found = {part.strip().lower() for value in values for part in value.split(",")}
    return tuple(sorted(f for f in found if f))


def _state_dir() -> Path:
    return Path(os.environ.get("VERDICT_HOME", Path.home() / ".verdict"))


def route_prefix(route_id: str) -> str:
    """Provider family of a gateway route id: the prefix before '/' (``cc/x`` -> ``cc``)."""
    return route_id.split("/", 1)[0].lower() if "/" in route_id else route_id.lower()


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


def prime_visibility(path: Path | None = None, *, live_rows: Any = None) -> Any:
    """Return the concrete ids Prime can actually spawn, never gateway authority.

    When the caller has just refreshed ``/v1/models``, it supplies those rows
    here so the local Prime registry is synchronized before selection. A failed
    sync leaves an LKG sidecar for audit but does not invent visibility: only
    the concrete ids read from Prime's registry are returned. Health, capacity,
    entitlement and launch authority stay in their separate gates.
    """
    from verdict.harness_prime import refresh_omniroute_visibility
    from verdict.orchestration.eligibility import HarnessVisibility

    registry = path or Path.home() / ".prime" / "agent" / "models.json"
    if live_rows is not None:
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


def _gateway_fetch_error(endpoint: str, exc: urllib.error.URLError | OSError) -> OrchestrationError:
    """Give actionable endpoint context without exposing request headers or credentials."""
    failure = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else "connection error"
    return OrchestrationError(
        f"gateway {endpoint} failed ({failure}); check --gateway and that the gateway is running"
    )


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
) -> Any:
    """Admitted eligibility ladder over the live gateway.

    ``required_capabilities`` / ``min_context_tokens`` are the floor every
    selection from this ladder needs. They go into canonical admission so
    CAPABILITY drops appear in the receipt. The ladder still applies each
    request's own requirements later.
    """
    from verdict.admission import active_controller_route, admit, default_runtime_evidence
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.orchestration.run import fetch_connections, fetch_inventory, resolve_api_key
    from verdict.subagent_selection import LaunchCandidate, openai_health_probe

    gateway = normalize_omniroute_origin(gateway)
    key = resolve_api_key()
    try:
        rows = fetch_inventory(gateway, api_key=key)
    except (urllib.error.URLError, OSError) as exc:
        raise _gateway_fetch_error("/v1/models", exc) from None
    try:
        connections = fetch_connections(gateway, api_key=key)
    except (urllib.error.URLError, OSError) as exc:
        raise _gateway_fetch_error("/api/providers", exc) from None
    state_path = state_file or (_state_dir() / "orchestration-health.json")
    now = datetime.now(timezone.utc)
    # Canonical admission over the full live inventory first. Scope, provider
    # family and the active controller are extra narrowing on that set.
    admitted = admit(
        rows,
        connections,
        # Evidence comes from the ladder state file this ladder reads, so the
        # admitted set and the ladder cannot disagree about cached health.
        default_runtime_evidence(now=now, state_dir=state_path.parent, ladder_state=state_path),
        now=now,
        required_capabilities=required_capabilities,
        min_context_tokens=min_context_tokens,
    )
    prefixes = tuple(p.strip() for p in scope.split(",") if p.strip())
    admitted = admitted.restrict_prefixes(prefixes).restrict_families(provider_families)
    admitted = admitted.exclude_controller(active_controller_route())
    receipt_path = state_path.parent / "admission-latest.json"
    admitted.write_receipt(receipt_path)
    # Prime visibility is refreshed from the FULL live catalog. Scope and
    # provider-family filters only narrow what this ladder evaluates; they
    # must never shrink the operator's Prime registry to the scoped subset.
    catalog_rows = list(rows)
    if prefixes:
        rows = [r for r in rows if str(r.get("id", "")).startswith(prefixes)]
    if provider_families:
        rows = [r for r in rows if route_prefix(str(r.get("id", ""))) in provider_families]
    raw_probe = openai_health_probe(gateway.rstrip("/") + "/v1", api_key=key, timeout_seconds=30)

    def probe(route_id: str) -> Any:
        return raw_probe(
            LaunchCandidate(route_id, route_id, frozenset(), 0, 0.0, 0.0, False, False, 0, 0)
        )

    return EligibilityLadder(
        rows,
        connections,
        probe,
        state_path,
        prefer_providers=tuple(p.strip() for p in prefer.split(",") if p.strip()),
        load=load,
        harness_visible=prime_visibility(live_rows=catalog_rows),
        admitted=admitted,
        admission_receipt=receipt_path,
    )


_STAGE_ORDER = ("DISCOVERED", "ENTITLED", "HEALTHY", "AVAILABLE", "TASK_ELIGIBLE", "SELECTED")


def eligibility_payload(
    verdicts: Any, summary: dict[str, int], chosen: Any, filters: dict[str, list[str]]
) -> dict[str, Any]:
    """Complete, self-reconciling eligibility record set (never truncated).

    ``summary`` keeps the ladder's cumulative stage counts and adds
    ``selected`` (the chosen route id or None, equal to ``selected.route_id``)
    and ``by_reached_stage``: exclusive buckets whose sum is ``evaluated_count``.
    """
    records = [v.to_dict() for v in verdicts]
    buckets = dict.fromkeys(_STAGE_ORDER, 0)
    buckets["NONE"] = 0
    for record in records:
        buckets[record["reached"] or "NONE"] += 1
    full_summary: dict[str, Any] = dict(summary)
    full_summary["selected"] = chosen.route_id if chosen else None
    full_summary["by_reached_stage"] = buckets
    return {
        "filters": filters,
        "evaluated_count": len(records),
        "summary": full_summary,
        "selected": chosen.to_dict() if chosen else None,
        "verdicts": records,
    }
