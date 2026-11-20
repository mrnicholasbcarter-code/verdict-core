"""Read-only model-discovery funnel report.

Reads the catalog, capacity classifier, health cache and admission evidence
that Verdict has already written to ~/.verdict.  Makes no model calls, no
probes and no writes.

Each stage returns a *count* (int) or ``None`` (displayed as "unknown").
"Unknown" is never 0: 0 means evidence says zero, None means evidence is
absent.  Unknown is never healthy (AGENTS.md invariant).

Caps are reported by their module-level constant names and values so the
output stays honest when the code changes.

Stage order (from plan 20261119-model-discovery S0):
  listed          → all routes in the OmniRoute catalog
  free_by_fact    → routes whose capacity classifier returns FREE
  probed          → routes present in the health cache (ever screened)
  answered        → probed routes that received an HTTP response
  tool_ok         → answered routes whose agentic_capability is qualified
  admitted        → routes admitted by the admission ladder
  considered      → cap name + value only (admission x selection cap)
  selected        → unknown (requires a live selection; not in cached state)
"""

from __future__ import annotations

import inspect
import json
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.capacity import CapacityClass
from verdict.census_passports import DEFAULT_MAX_PER_PROVIDER as CENSUS_MAX_PER_PROVIDER
from verdict.identify_service import SCREEN_MAX_PER_PROVIDER, SCREEN_MAX_ROUTES
from verdict.omniroute_catalog import MAX_CATALOG_PROBE_SAMPLE
from verdict.orchestration.health_cache import (
    HealthCache,
    HealthCacheError,
    agentic_capability,
    default_cache_path,
)
from verdict.orchestration.verified_refresh import DEFAULT_MAX_ROUTES as VERIFIED_REFRESH_MAX_ROUTES
from verdict.paths import verdict_home
from verdict.prove_at_rest import DEFAULT_AGENTIC_MAX_ROUTES as PROVE_AT_REST_MAX_ROUTES
from verdict.prove_at_rest import DEFAULT_AGENTIC_POOL_CONCURRENCY as PROVE_AT_REST_POOL_CONCURRENCY


# max_probes_per_select is a default kwarg, not a module constant; derive it.
def _eligibility_max_probes_per_select() -> int:
    from verdict.orchestration.eligibility import EligibilityLadder

    param = inspect.signature(EligibilityLadder.__init__).parameters.get("max_probes_per_select")
    return (
        int(param.default)
        if param is not None and param.default is not inspect.Parameter.empty
        else 8
    )


# Stable schema identifier for --json output.
FUNNEL_SCHEMA = "verdict.discovery.funnel/v1"

# Caps named and exposed in the output (constant name → value).
CAPS: dict[str, int] = {
    "SCREEN_MAX_ROUTES": SCREEN_MAX_ROUTES,
    "SCREEN_MAX_PER_PROVIDER": SCREEN_MAX_PER_PROVIDER,
    "MAX_CATALOG_PROBE_SAMPLE": MAX_CATALOG_PROBE_SAMPLE,
    "VERIFIED_REFRESH_MAX_ROUTES": VERIFIED_REFRESH_MAX_ROUTES,
    "PROVE_AT_REST_MAX_ROUTES": PROVE_AT_REST_MAX_ROUTES,
    "PROVE_AT_REST_POOL_CONCURRENCY": PROVE_AT_REST_POOL_CONCURRENCY,
    "CENSUS_MAX_PER_PROVIDER": CENSUS_MAX_PER_PROVIDER,
}


# ---------------------------------------------------------------------------
# Data-loading helpers (all read-only)
# ---------------------------------------------------------------------------


def _load_identify_report(path: Path) -> dict[str, Any] | None:
    """Return the identify-latest.json report or None when absent/invalid."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _load_admission(path: Path) -> dict[str, Any] | None:
    """Return the admission-latest.json report or None when absent/invalid."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _load_health_cache(path: Path) -> HealthCache | None:
    """Return a read-only HealthCache instance or None when unavailable."""
    try:
        return HealthCache(path)
    except (HealthCacheError, OSError):
        return None


# ---------------------------------------------------------------------------
# Stage builders (set-based, intersecting forward)
# ---------------------------------------------------------------------------


def _stage(
    count: int | None,
    *,
    cap: str | None = None,
    drops: dict[str, int] | None = None,
    raw: int | None = None,
) -> dict[str, Any]:
    return {"count": count, "cap": cap, "drops": drops or {}, "raw": raw}


def build_funnel(
    *,
    identify_report: dict[str, Any] | None,
    health_cache: HealthCache | None,
    admission: dict[str, Any] | None,
    now: datetime,
    max_probes_per_select: int | None = None,
    registry: Any | None = None,
) -> dict[str, Any]:
    """Compute the funnel from already-loaded evidence.

    Every *count* is an int or None (unknown).  Drops record why routes
    were lost between stages and name the responsible constant.

    Parameters
    ----------
    identify_report:
        Contents of identify-latest.json.  Used for the catalog inventory
        and the capacity-classifier results (route_capacities via evidence).
    health_cache:
        A read-only HealthCache instance for probed/answered/tool_ok.
    admission:
        Contents of admission-latest.json for the admitted set.
    now:
        Reference time (UTC) for freshness checks.
    max_probes_per_select:
        Inject for tests; defaults to EligibilityLadder's default kwarg.
    """
    if max_probes_per_select is None:
        max_probes_per_select = _eligibility_max_probes_per_select()

    sources: list[str] = []

    # ------------------------------------------------------------------
    # Stage 1: listed
    # ------------------------------------------------------------------
    listed_count: int | None = None
    if identify_report is not None:
        listed_count = identify_report.get("inventory_route_count")
        if isinstance(listed_count, int):
            sources.append("identify-latest.json (inventory_route_count)")
        else:
            listed_count = None

    # ------------------------------------------------------------------
    # Stage 2: free_by_fact
    # ------------------------------------------------------------------
    # Derive from OmniRoute evidence if the identify report has capacity data.
    # The report does not store per-route capacity in a top-level field, but
    # the task_pools carry eligible candidates with capacity_evidence.
    # For S0 we use the admission candidates as a richer source because they
    # run through the same classify_route_capacity path.
    free_routes: set[str] | None = None
    free_by_rule: Counter[str] = Counter()

    if admission is not None:
        candidates = admission.get("candidates", [])
        if isinstance(candidates, list):
            # Admission candidates carry source but not capacity directly.
            # Use health_cache capacity_evidence as the per-route fact source.
            pass

    # Pull capacity facts from the health cache (each entry stores
    # capacity_evidence recorded during the screen probe).
    hc_free: set[str] | None = None
    _free_from_pools: set[str] | None = None  # sentinel: pool_free was the source
    if health_cache is not None:
        hc_routes = health_cache.routes()
        sources.append("health-cache.json (capacity_evidence)")
        # Build the free set from health cache entries that carry a capacity.
        # This under-counts (only screened routes have entries), so we
        # supplement with the task_pools data below when available.
        hc_free = set()
        for route_id, entry in hc_routes.items():
            ce = entry.capacity_evidence
            if ce and ce not in ("", "unknown"):
                # capacity_evidence is the classifier's reason string.
                # CapacityClass.FREE routes have reasons like plan_free,
                # provider_wide_free, free_tier_summary, zero_price, free_suffix,
                # import_free_only, importFreeModelsOnly, etc.
                # We include free_suffix here (it is a real classifier output for
                # S0; S1 will remove it as a valid free signal).
                is_free = ce in _FREE_EVIDENCE_VALUES
                if is_free:
                    hc_free.add(route_id)
                    free_by_rule[ce] += 1

    # Try to get a richer free count from the identify report's task_pools,
    # which contain eligible routes with capacity + capacity_evidence.
    if identify_report is not None:
        pools = identify_report.get("task_pools", {})
        if isinstance(pools, Mapping):
            seen: set[str] = set()
            pool_free: set[str] = set()
            pool_free_by_rule: Counter[str] = Counter()
            for _pool_name, pool_data in pools.items():
                if not isinstance(pool_data, Mapping):
                    continue
                for item in pool_data.get("eligible", []):
                    if not isinstance(item, Mapping):
                        continue
                    r_id = item.get("route_id")
                    if not isinstance(r_id, str) or r_id in seen:
                        continue
                    seen.add(r_id)
                    cap = item.get("capacity", "")
                    ce = item.get("capacity_evidence", "")
                    if cap == CapacityClass.FREE.value or cap == "free":
                        pool_free.add(r_id)
                        pool_free_by_rule[str(ce)] += 1
                for item in pool_data.get("fallback", []):
                    if not isinstance(item, Mapping):
                        continue
                    r_id = item.get("route_id")
                    if not isinstance(r_id, str) or r_id in seen:
                        continue
                    seen.add(r_id)
                    cap = item.get("capacity", "")
                    ce = item.get("capacity_evidence", "")
                    if cap == CapacityClass.FREE.value or cap == "free":
                        pool_free.add(r_id)
                        pool_free_by_rule[str(ce)] += 1
            if pool_free:
                sources.append("identify-latest.json (task_pools capacity)")
                # pool_free is a subset of the catalog free routes (only
                # pool-eligible routes appear); report it with this note.
                free_routes = pool_free
                free_by_rule = pool_free_by_rule
                _free_from_pools = pool_free  # record sentinel

    # Fallback: use the health-cache free set when task_pools produced no data.
    # hc_free is None only when health_cache was absent (evidence missing →
    # unknown, never 0); hc_free is an empty set when the cache had routes but
    # none carried FREE evidence (a valid lower-bound 0 from real evidence).
    if free_routes is None and hc_free is not None:
        free_routes = hc_free
        # free_by_rule was already populated from health cache entries above.

    # Note: for a full count we would need OmniRoute evidence (all routes with
    # capacity classification).  task_pools and hc_free are both lower bounds.
    free_count: int | None = len(free_routes) if free_routes is not None else None
    if free_routes is None:
        free_note: str | None = None
    elif free_routes is _free_from_pools:  # pool_free was the source
        free_note = (
            "lower bound: task_pools only cover pool-eligible routes;"
            " full count requires a live fetch"
        )
    else:
        # hc_free was the fallback source (health-cache screened routes only)
        free_note = (
            "lower bound: health-cache only covers screened routes;"
            " full count requires a live fetch"
        )

    # Drops from listed → free_by_fact (approximate)
    free_drops: dict[str, int] = {}
    if listed_count is not None and free_count is not None and listed_count > free_count:
        free_drops["not_free_or_no_capacity_evidence"] = listed_count - free_count

    # ------------------------------------------------------------------
    # Stage 3: probed
    # ------------------------------------------------------------------
    probed_routes: set[str] | None = None
    probed_count: int | None = None
    probed_drops: dict[str, int] = {}

    if health_cache is not None:
        hc_routes = health_cache.routes()
        probed_routes = set(hc_routes.keys())
        probed_count = len(probed_routes)
        sources.append("health-cache.json (routes)")
        # Drop reason: the screen cap limits how many routes enter the cache.
        if free_count is not None and probed_count < free_count:
            probed_drops[f"SCREEN_MAX_ROUTES={SCREEN_MAX_ROUTES}"] = free_count - probed_count

    # ------------------------------------------------------------------
    # Stage 4: answered
    # ------------------------------------------------------------------
    answered_routes: set[str] | None = None
    answered_count: int | None = None
    answered_drops: dict[str, int] = {}

    if probed_routes is not None and health_cache is not None:
        hc_routes = health_cache.routes()
        answered_routes = set()
        answer_drop_reasons: Counter[str] = Counter()
        for route_id in probed_routes:
            if (hce := hc_routes.get(route_id)) is None:
                continue
            if hce.http_status is not None:
                answered_routes.add(route_id)
            else:
                # No HTTP status = no response (timeout, error before response)
                answer_drop_reasons["no_http_response"] += 1
        answered_count = len(answered_routes)
        answered_drops = dict(answer_drop_reasons)
        if probed_count is not None and probed_count > answered_count:
            answered_drops.setdefault("no_http_response", probed_count - answered_count)

    # ------------------------------------------------------------------
    # Stage 5: tool_ok
    # ------------------------------------------------------------------
    tool_ok_routes: set[str] | None = None
    tool_ok_count: int | None = None
    tool_ok_drops: dict[str, int] = {}

    if answered_routes is not None and health_cache is not None:
        hc_routes = health_cache.routes()
        tool_ok_routes = set()
        tool_drop_reasons: Counter[str] = Counter()
        for route_id in answered_routes:
            if (hce := hc_routes.get(route_id)) is None:
                continue
            cap = agentic_capability(hce, now)
            if cap["qualified"]:
                tool_ok_routes.add(route_id)
            else:
                # Classify why the route failed the tool check.
                if not hce.agentic_ok and (
                    hce.session_agentic_ok is None or not hce.session_agentic_ok
                ):
                    tool_drop_reasons["tool_call_failed_or_absent"] += 1
                else:
                    tool_drop_reasons["stale_proof"] += 1
        tool_ok_count = len(tool_ok_routes)
        tool_ok_drops = dict(tool_drop_reasons)

    # ------------------------------------------------------------------
    # Stage 5b: registry_proof (S3b — routes proved by discovery registry)
    # ------------------------------------------------------------------
    # Counts free routes in the discovery registry with a fresh tool_ok=True
    # proof.  These routes are eligible for admission via registry proof even
    # when the health cache has no (or stale) agentic_ok entry.
    registry_proof_count: int | None = None
    if registry is not None:
        from verdict.discovery.registry import tool_ok_proof as _tok_proof

        rp_count = 0
        for entry in getattr(registry, "entries", []):
            if entry.capacity_class == "free" and entry.tool_ok is True:
                result = _tok_proof(entry.route_key, registry, now)
                if result["qualified"]:
                    rp_count += 1
        registry_proof_count = rp_count
        if "discovery-registry.json" not in sources:
            sources.append("discovery-registry.json")

    # ------------------------------------------------------------------
    # Stage 6: admitted
    # ------------------------------------------------------------------
    admitted_routes: set[str] | None = None
    admitted_count: int | None = None
    admitted_drops: dict[str, int] = {}

    if admission is not None:
        raw_admitted = admission.get("admitted", [])
        if isinstance(raw_admitted, list):
            admitted_routes = set(str(r) for r in raw_admitted if isinstance(r, str))
            admitted_count = len(admitted_routes)
            sources.append("admission-latest.json")
            # Drops: tool_ok routes not admitted (note: admission is not a
            # subset of tool_ok; admission runs on the full catalog).
            candidates = admission.get("candidates", [])
            not_admitted_reasons: Counter[str] = Counter()
            if isinstance(candidates, list):
                for c in candidates:
                    if not isinstance(c, Mapping):
                        continue
                    if not c.get("admitted", True):
                        reason = c.get("reason") or "unknown"
                        not_admitted_reasons[str(reason)] += 1
            if not_admitted_reasons:
                admitted_drops = dict(not_admitted_reasons)
        # Admission total (candidates, not just admitted)
        admission_total = (
            len(admission.get("candidates", []))
            if isinstance(admission.get("candidates"), list)
            else None
        )
    else:
        admission_total = None

    # ------------------------------------------------------------------
    # Stage 7: considered (cap only)
    # ------------------------------------------------------------------
    # max_probes_per_select applies per selection call, not across all routes.
    # We report the admitted set it draws from and the per-selection cap.
    considered_cap_name = "max_probes_per_select"
    considered_cap_value = max_probes_per_select
    considered_note = (
        f"per-selection cap: at most {considered_cap_value} probes per "
        f"`verdict choose` call ({considered_cap_name}={considered_cap_value})"
    )

    # ------------------------------------------------------------------
    # Stage 8: selected
    # ------------------------------------------------------------------
    # Not determinable from cached state; would require a live session.
    selected_note = "unknown: requires a live selection; not in cached state"

    # ------------------------------------------------------------------
    # Assemble result
    # ------------------------------------------------------------------
    return {
        "schema": FUNNEL_SCHEMA,
        "generated_at": now.isoformat(),
        "sources": sources,
        "caps": CAPS | {considered_cap_name: considered_cap_value},
        "stages": {
            "listed": _stage(listed_count),
            "free_by_fact": _stage(free_count, drops=free_drops, raw=listed_count)
            | ({"note": free_note} if free_note else {}),
            "free_by_rule": dict(free_by_rule),
            "probed": _stage(
                probed_count, cap=f"SCREEN_MAX_ROUTES={SCREEN_MAX_ROUTES}", drops=probed_drops
            ),
            "answered": _stage(answered_count, drops=answered_drops),
            "tool_ok": _stage(tool_ok_count, drops=tool_ok_drops)
            | (
                {"registry_proof": registry_proof_count} if registry_proof_count is not None else {}
            ),
            "admitted": _stage(admitted_count, drops=admitted_drops, raw=admission_total),
            "considered": {
                "count": None,
                "cap": considered_cap_name,
                "cap_value": considered_cap_value,
                "note": considered_note,
                "admits_from": admitted_count,
            },
            "selected": {"count": None, "note": selected_note},
        },
    }


# Capacity-evidence values that indicate FREE capacity in the classifier.
# See verdict/capacity.py classify_route_capacity for the reason strings.
_FREE_EVIDENCE_VALUES: frozenset[str] = frozenset(
    {
        "plan_free",
        "provider_wide_free",
        "free_tier_summary",
        "zero_price",
        "free_suffix",  # name-not-fact: S1 will remove; S0 reports it
        "openrouter_public_zero",
        "import_free_only",
        "importFreeModelsOnly",
        "all_zero_pricing",
        "assumed_free_no_paid_evidence",
        "keyless",
        "no_auth_required",
    }
)


# ---------------------------------------------------------------------------
# Top-level builder (loads all sources from filesystem)
# ---------------------------------------------------------------------------


def run_funnel(
    *, verdict_home_path: Path | None = None, health_cache_path: Path | None = None
) -> dict[str, Any]:
    """Load evidence from the filesystem and build the funnel report.

    Returns the funnel dict.  Callers must not write this dict anywhere;
    this module makes no writes.
    """
    from verdict.discovery.registry import load_registry as _load_registry

    now = datetime.now(timezone.utc)
    home = verdict_home_path or verdict_home()

    identify_report = _load_identify_report(home / "identify-latest.json")
    hc_path = health_cache_path or default_cache_path()
    health_cache_obj = _load_health_cache(hc_path)
    admission = _load_admission(home / "admission-latest.json")
    discovery_registry = _load_registry()

    return build_funnel(
        identify_report=identify_report,
        health_cache=health_cache_obj,
        admission=admission,
        now=now,
        registry=discovery_registry,
    )


# ---------------------------------------------------------------------------
# Plain-text renderer
# ---------------------------------------------------------------------------


def _fmt(count: int | None) -> str:
    return "unknown" if count is None else str(count)


def render_plain(funnel: dict[str, Any]) -> str:
    """Render the funnel as a human-readable plain-text report."""
    stages = funnel.get("stages", {})
    caps = funnel.get("caps", {})
    lines: list[str] = []

    lines.append(f"Discovery funnel  ({funnel.get('generated_at', 'n/a')})")
    lines.append("")

    stage_order = [
        "listed",
        "free_by_fact",
        "probed",
        "answered",
        "tool_ok",
        "admitted",
        "considered",
        "selected",
    ]

    for name in stage_order:
        s = stages.get(name, {})
        if name == "considered":
            cap_name = s.get("cap", "max_probes_per_select")
            cap_value = s.get("cap_value", caps.get(cap_name))
            admits_from = s.get("admits_from")
            line = (
                f"  {name:<24} (per-selection cap "
                f"{cap_name}={_fmt(cap_value)}, admits from {_fmt(admits_from)})"
            )
            lines.append(line)
            continue
        if name == "selected":
            lines.append(f"  {name:<24} unknown (requires live selection)")
            continue

        count = s.get("count")
        cap = s.get("cap")
        note = s.get("note") or ""
        line = f"  {name:<24} {_fmt(count)}"
        if cap:
            line += f"  [cap: {cap}]"
        if note:
            line += f"  ({note})"
        lines.append(line)

        # Show drops
        drops = s.get("drops", {})
        for reason, n in sorted(drops.items()):
            lines.append(f"    - dropped {n} : {reason}")

        # Show registry_proof count after tool_ok (S3b)
        if name == "tool_ok":
            rp = s.get("registry_proof")
            if rp is not None:
                lines.append(f"    via registry proof: {rp}")

        # Show free-by-rule breakdown after free_by_fact
        if name == "free_by_fact":
            by_rule = funnel.get("stages", {}).get("free_by_rule", {})
            if by_rule:
                lines.append("    by classifier rule:")
                for rule, n in sorted(by_rule.items(), key=lambda x: -x[1]):
                    lines.append(f"      {n:>5}  {rule}")

    # Caps table
    lines.append("")
    lines.append("Caps in effect:")
    for name, value in sorted(caps.items()):
        lines.append(f"  {name:<44} {value}")

    # Sources
    sources = funnel.get("sources", [])
    if sources:
        lines.append("")
        lines.append("Evidence sources:")
        for s in sources:
            lines.append(f"  {s}")

    return "\n".join(lines)
