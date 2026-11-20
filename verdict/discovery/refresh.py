"""verdict.discovery.refresh — CLI action for ``verdict discovery refresh``.

Loads OmniRoute routes already classified FREE or SUBSCRIPTION by a fact,
probes them at agent size, and writes results to the discovery registry.

Route source: OmniRoute catalog via ``fetch_omniroute_evidence`` and
``verdict.discovery.sources.omniroute.rows_from_evidence``.  Non-OmniRoute
catalogs (openrouter, kilo, models.dev, community) are not probeable through
the gateway until a later story registers them.

Callers receive a plain dict suitable for ``json.dumps``; the shape matches
the DiscoveryRegistry schema.  Render with ``render_refresh_plain`` for human
output.
"""

from __future__ import annotations

from typing import Any

from verdict.discovery.model import Route
from verdict.discovery.registry import (
    REGISTRY_SCHEMA,
    DiscoveryRegistry,
    load_registry,
    run_refresh,
)

# Capacity class strings that may be probed (all others are skipped)
_PROBEABLE = frozenset({"free", "subscription"})


def _load_omniroute_routes(env: Any = None) -> list[tuple[Route, str, str]]:
    """Load routes from OmniRoute via the sources/omniroute adapter.

    Uses ``fetch_omniroute_evidence`` as the single fetcher and
    ``rows_from_evidence`` for opaque-route filtering and row normalisation.
    Capacity is read from ``evidence.route_capacities`` / ``route_capacity_evidence``
    (pre-computed by ``parse_omniroute_evidence`` from connection facts).

    Returns an empty list when the OmniRoute key is absent or the fetch fails.
    Never raises.
    """
    import os

    try:
        from verdict.discovery.sources.omniroute import rows_from_evidence
        from verdict.omniroute_evidence import fetch_omniroute_evidence
    except ImportError:
        return []

    _env = env if env is not None else os.environ
    _key_vars = (
        "OMNIROUTE_API_KEY",
        "LLMGATE_API_KEY",
        "VERDICT_OMNIROUTE_API_KEY",
        "OMNIROUTE_ACCESS_TOKEN",
    )
    base = _env.get("OMNIROUTE_BASE_URL", "http://localhost:20128")
    api_key = next((v for name in _key_vars if (v := _env.get(name, ""))), "")
    if not api_key:
        return []
    auth_var = next((name for name in _key_vars if _env.get(name, "")), _key_vars[0])
    mgmt_token = _env.get("OMNIROUTE_MANAGEMENT_TOKEN") or None

    try:
        evidence = fetch_omniroute_evidence(base, api_key=api_key, management_token=mgmt_token)
        disc_rows = rows_from_evidence(evidence, base_url=base)
    except Exception:
        return []

    result: list[tuple[Route, str, str]] = []
    for row in disc_rows:
        cap_class = evidence.route_capacities.get(row.id, "unknown")
        cap_fact = evidence.route_capacity_evidence.get(row.id, "unknown")
        if cap_class not in _PROBEABLE:
            continue
        route = Route(
            provider="omniroute",
            model=row.id,
            base_url=(base.rstrip("/") + "/v1"),
            auth=f"env:{auth_var}",
            source="omniroute_catalog",
            upstream=row.provider or None,
            billing=cap_class,
        )
        result.append((route, cap_class, cap_fact))

    return result


def load_probeable_routes(env: Any = None) -> list[tuple[Route, str, str]]:
    """Return routes ready to probe: FREE or SUBSCRIPTION from OmniRoute.

    Unknown and priced routes are never returned.  Returns an empty list
    when the OmniRoute key is absent or the catalog cannot be fetched.
    """
    return _load_omniroute_routes(env)


def run_refresh_action(
    *,
    dry_run: bool = False,
    force: bool = False,
    context_tokens: int = 18_000,
    wall_budget_s: float = 300.0,
    env: Any = None,
) -> dict[str, Any]:
    """Probe routes and return a dict suitable for json.dumps.

    ``dry_run=True`` returns an empty result immediately with no network calls
    and no registry write; source is ``"dry_run_requires_catalog"`` because the
    OmniRoute catalog requires a live fetch.
    ``force=True`` re-runs even when a fresh registry already exists.
    """
    # Skip if a fresh registry already exists (unless forced or dry-run)
    if not dry_run and not force:
        existing = load_registry(env)
        if existing is not None:
            return _registry_to_summary(existing, dry_run=False, total_input=len(existing.entries))

    if dry_run:
        # Never make network calls in dry-run; the OmniRoute catalog requires a
        # live fetch, so there is no offline route list to report.
        routes: list[tuple[Route, str, str]] = []
        source = "dry_run_requires_catalog"
    else:
        routes = load_probeable_routes(env)
        source = "omniroute"

    registry = run_refresh(
        routes, context_tokens=context_tokens, wall_budget_s=wall_budget_s, env=env, dry_run=dry_run
    )
    return _registry_to_summary(registry, dry_run=dry_run, total_input=len(routes), source=source)


def _registry_to_summary(
    registry: DiscoveryRegistry, *, dry_run: bool, total_input: int, source: str = "omniroute"
) -> dict[str, Any]:
    """Convert a DiscoveryRegistry to a human/machine-readable summary dict."""
    entries = registry.entries
    by_provider: dict[str, dict[str, int]] = {}
    for e in entries:
        provider = e.route_key.split("/", 1)[0]
        counts = by_provider.setdefault(
            provider, {"total": 0, "answered": 0, "tool_ok": 0, "rate_limited": 0}
        )
        counts["total"] += 1
        if e.answered:
            counts["answered"] += 1
        if e.tool_ok:
            counts["tool_ok"] += 1
        if e.breaker_state == "open":
            counts["rate_limited"] += 1

    return {
        "schema": REGISTRY_SCHEMA,
        "written_at": registry.written_at,
        "ttl_s": registry.ttl_s,
        "dry_run": dry_run,
        "source": source,
        "counts": {
            "input": total_input,
            "total": len(entries),
            "answered": sum(1 for e in entries if e.answered),
            "tool_ok": sum(1 for e in entries if e.tool_ok),
            "rate_limited": sum(1 for e in entries if e.breaker_state == "open"),
        },
        "by_provider": by_provider,
        "entries": [e.to_dict() for e in entries],
    }


def render_refresh_plain(result: dict[str, Any]) -> str:
    """Render a human-readable plain-text summary of a refresh result."""
    lines: list[str] = []
    dry = " (dry-run)" if result.get("dry_run") else ""
    lines.append(f"verdict discovery refresh{dry}  {result.get('written_at', '')}")
    counts = result.get("counts", {})
    lines.append(
        f"  input {counts.get('input', 0)}  "
        f"answered {counts.get('answered', 0)}  "
        f"tool_ok {counts.get('tool_ok', 0)}  "
        f"rate_limited {counts.get('rate_limited', 0)}"
    )
    by_prov = result.get("by_provider", {})
    if by_prov:
        lines.append("")
        lines.append("  provider            total  answered  tool_ok")
        for provider, c in sorted(by_prov.items()):
            lines.append(
                f"  {provider:<20s}  {c['total']:>5}  {c['answered']:>8}  {c['tool_ok']:>7}"
            )
    if result.get("dry_run") and result.get("entries"):
        lines.append("")
        lines.append("  routes that would be probed:")
        for e in result["entries"][:20]:
            lines.append(f"    {e['route_key']}  ({e['capacity_class']} / {e['capacity_fact']})")
        if len(result["entries"]) > 20:
            lines.append(f"    … and {len(result['entries']) - 20} more")
    return "\n".join(lines)
