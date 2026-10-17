"""Census tick hook for phase a: refresh provider free flags and report rule 0.2.

Dark by default. With ``VERDICT_PIPELINE_FREE_RULE`` on it fetches the public
Kilo/OpenRouter catalogs (no key, no OmniRoute call), keeps a dated copy plus a
48h last-good, and reports how admitted catalog routes classify. It changes no
admission, writes no capability_tier and calls no OmniRoute endpoint.
"""

from __future__ import annotations

import dataclasses
import os
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict.omniroute_pipeline.free_rule import (
    LAST_GOOD_NAME,
    FreeStatus,
    Getter,
    ProviderFreeFlags,
    classify_free,
    fetch_provider_free_flags,
    load_free_flags,
    write_free_flags,
)
from verdict.omniroute_pipeline.probes import ProbeOutcome, judge_chat, judge_tool

FLAG_ENV = "VERDICT_PIPELINE_FREE_RULE"


def free_rule_enabled() -> bool:
    return os.getenv(FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def default_free_flags_dir() -> Path:
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent


def load_or_fetch_flags(
    *, now: datetime, cache_dir: Path | None = None, get: Getter | None = None
) -> tuple[ProviderFreeFlags | None, str]:
    """Fresh provider flags (dated + last-good copies written), else the 48h last-good.

    Returns the flags and their source ("fresh", "last_good" or "none") so one
    fetch per tick can serve both the free-rule report and the probe gate.
    """
    directory = cache_dir or default_free_flags_dir()
    last_good = directory / LAST_GOOD_NAME
    fresh = fetch_provider_free_flags(now, get)
    if fresh is not None:
        write_free_flags(directory / f"free-flags-{now.date().isoformat()}.json", fresh)
        write_free_flags(last_good, fresh)
        return fresh, "fresh"
    cached = load_free_flags(last_good, now=now)
    return cached, ("last_good" if cached is not None else "none")


def classify_snapshot_free(snapshot: Any, flags: ProviderFreeFlags | None) -> dict[str, FreeStatus]:
    """Rule 0.2 verdict per catalog route: snapshot prices + fresh/48h flags.

    Computed once per tick BEFORE live probing, so the chat/tool and needle
    gates read the same free evidence instead of a literal "free" string.
    """
    statuses: dict[str, FreeStatus] = {}
    for identity in getattr(snapshot, "catalog", ()):
        price = (
            {"input": identity.input_cost, "output": identity.output_cost}
            if identity.price_known
            else None
        )
        statuses[identity.identity_id] = classify_free(identity.identity_id, price, flags)
    return statuses


def free_rule_tick(
    snapshot: Any,
    *,
    now: datetime,
    cache_dir: Path | None = None,
    get: Getter | None = None,
    flags: ProviderFreeFlags | None = None,
    flags_source: str | None = None,
) -> dict[str, Any] | None:
    """Return a free-rule summary, or None when the flag is off.

    ``flags``/``flags_source`` let the census tick reuse the flags it already
    loaded for the probe gate instead of fetching twice.
    """
    if not free_rule_enabled():
        return None
    if flags is None and flags_source is None:
        flags, flags_source = load_or_fetch_flags(now=now, cache_dir=cache_dir, get=get)
    counts: Counter[str] = Counter(
        status.value for status in classify_snapshot_free(snapshot, flags).values()
    )
    return {
        "flags_source": flags_source or ("last_good" if flags else "none"),
        "flag_entries": len(flags.entries) if flags else 0,
        "routes": dict(sorted(counts.items())),
    }


def guard_transport(transport: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a census probe transport so a fake 200 becomes a hard billing failure.

    Returned unchanged while the flag is off. With it on, a 2xx whose body trips
    the fake-200 guard is reported as ``payment_required`` (spec section 2:
    FAIL_FAKE is a hard billing failure), so the census never renews it.
    """
    if not free_rule_enabled():
        return transport

    def guarded(*args: Any, **kwargs: Any) -> Any:
        exchange = transport(*args, **kwargs)
        body = getattr(exchange, "response_body", None)
        status = getattr(exchange, "http_status", None) or 0
        judge = judge_tool if len(args) > 1 and args[1] == "tool" else judge_chat
        if getattr(exchange, "ok", False) and judge(status, body) is ProbeOutcome.FAIL_FAKE:
            return dataclasses.replace(
                exchange,
                ok=False,
                http_status=402,
                error_category="payment_required",
                detail="fail_fake",
            )
        return exchange

    return guarded


def needle_prober(base_url: str, api_key: str | None) -> Callable[[str, int], ProbeOutcome]:
    """Live needle probe via /v1 with the census key and the strict budget header.

    Built only when ``VERDICT_PIPELINE_PROBES`` is on; tests inject a fake prober.
    """
    import secrets

    import httpx

    from verdict.omniroute_pipeline.probes import (
        BUDGET_FALLBACK_HEADER,
        judge_needle,
        needle_payload,
    )

    endpoint = base_url.rstrip("/").removesuffix("/v1") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json", **BUDGET_FALLBACK_HEADER}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def probe(route: str, size_tokens: int) -> ProbeOutcome:
        needle = secrets.token_hex(4)
        try:
            response = httpx.post(
                endpoint,
                json=needle_payload(route, size_tokens, needle),
                headers=headers,
                timeout=300.0,
            )
            body: object = response.json()
        except (httpx.HTTPError, ValueError):
            return ProbeOutcome.FAIL
        return judge_needle(response.status_code, body, needle)

    return probe


def run_context_probes(
    candidates: list[str],
    *,
    proven: Callable[[str], bool],
    family_used: dict[str, int],
    max_per_provider: int | None,
    max_per_tick: int,
    prober: Callable[[str, int], ProbeOutcome],
    store_path: Path,
    now: datetime,
    states: dict[str, Any],
    blocklist: Any,
    free_status: Mapping[str, str] | None = None,
    catalog_max: Mapping[str, int | None] | None = None,
    max_candidates: int | None = None,
    max_tokens: int | None = None,
) -> tuple[int, int]:
    """At most ``max_per_tick`` one-step context probes on proven FREE routes.

    ``free_status`` carries the tick's rule 0.2 verdicts (FreeStatus values);
    a route without positive FREE evidence is never sent a needle.
    ``catalog_max`` maps routes to their catalog context limit, so a needle
    never exceeds it. A needle dispatches only when the remaining
    ``max_candidates``/``max_tokens`` budgets cover its input+output tokens,
    and its reservation is returned with the dispatch count:
    ``(probes_dispatched, tokens_reserved)``.
    """
    from verdict.omniroute_pipeline.context_store import (
        context_due,
        load_usable_context,
        record_usable_context,
    )
    from verdict.omniroute_pipeline.probes import (
        NEEDLE_OUTPUT_TOKENS,
        next_context_step,
        probe_allowed,
    )
    from verdict.omniroute_pipeline.state import ModelState, should_probe, transition

    rows = load_usable_context(store_path)
    done = 0
    reserved = 0
    for route in candidates:
        if done >= max_per_tick:
            break
        family = route.split("/", 1)[0]
        if blocklist.is_blocked(family) or not should_probe(
            states.get(route, ModelState.CATALOGED)
        ):
            continue
        if not probe_allowed(
            route, free_status=(free_status or {}).get(route, "unverified_free")
        ) or not proven(route):
            continue
        if max_per_provider is not None and family_used.get(family, 0) >= max_per_provider:
            continue
        row = rows.get(route)
        if not context_due(row, now):
            continue
        current = row.get("usable_context") if row else None
        step = next_context_step(
            current if isinstance(current, int) else None,
            catalog_max.get(route) if catalog_max is not None else None,
        )
        if step is None:
            continue
        # Reserve the needle's actual input+output tokens before dispatch.
        cost = step + NEEDLE_OUTPUT_TOKENS
        if max_candidates is not None and max_candidates < 1:
            continue
        if max_tokens is not None and max_tokens < cost:
            continue
        outcome = prober(route, step)
        if max_candidates is not None:
            max_candidates -= 1
        if max_tokens is not None:
            max_tokens -= cost
        reserved += cost
        family_used[family] = family_used.get(family, 0) + 1
        done += 1
        states[route] = transition(
            states.get(route, ModelState.CTX_MEASURED),
            probe=outcome,
            route=route,
            blocklist=blocklist,
        )
        passed = outcome is ProbeOutcome.PASS
        record_usable_context(
            store_path,
            route,
            step if passed else (current if isinstance(current, int) else None),
            now=now,
            failed_step=None if passed else step,
        )
    return done, reserved


CENSUS_PROBE_KEY_RELPATH = ".config/verdict/omniroute-api-key"


def census_probe_key(home: Path | None = None) -> str | None:
    """The single key resolver for census reads and probes (controller 10:05Z).

    Only the census probe key file is read, never the environment or any other
    credential source, so an admin key exported in the shell cannot reach the
    census. ``None`` when the file is missing or empty: census then fails closed.
    """
    path = (home or Path.home()) / CENSUS_PROBE_KEY_RELPATH
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None
