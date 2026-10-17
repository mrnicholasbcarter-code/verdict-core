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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from verdict.omniroute_pipeline.free_rule import (
    LAST_GOOD_NAME,
    Getter,
    classify_free,
    fetch_provider_free_flags,
    load_free_flags,
    write_free_flags,
)
from verdict.omniroute_pipeline.probes import is_fake_200

FLAG_ENV = "VERDICT_PIPELINE_FREE_RULE"


def free_rule_enabled() -> bool:
    return os.getenv(FLAG_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def default_free_flags_dir() -> Path:
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent


def free_rule_tick(
    snapshot: Any, *, now: datetime, cache_dir: Path | None = None, get: Getter | None = None
) -> dict[str, Any] | None:
    """Return a free-rule summary, or None when the flag is off."""
    if not free_rule_enabled():
        return None
    directory = cache_dir or default_free_flags_dir()
    last_good = directory / LAST_GOOD_NAME
    fresh = fetch_provider_free_flags(now, get)
    if fresh is not None:
        write_free_flags(directory / f"free-flags-{now.date().isoformat()}.json", fresh)
        write_free_flags(last_good, fresh)
    flags = fresh or load_free_flags(last_good, now=now)
    counts: Counter[str] = Counter()
    for identity in getattr(snapshot, "catalog", ()):
        price = (
            {"input": identity.input_cost, "output": identity.output_cost}
            if identity.price_known
            else None
        )
        counts[classify_free(identity.identity_id, price, flags).value] += 1
    return {
        "flags_source": "fresh" if fresh is not None else ("last_good" if flags else "none"),
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
        if getattr(exchange, "ok", False) and is_fake_200(body):
            return dataclasses.replace(
                exchange,
                ok=False,
                http_status=402,
                error_category="payment_required",
                detail="fail_fake",
            )
        return exchange

    return guarded
