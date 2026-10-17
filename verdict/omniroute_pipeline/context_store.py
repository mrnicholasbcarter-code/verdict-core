"""``usable_context`` evidence per route, seeded from existing needle results.

Kept beside the census passports (same ~/.verdict dir, atomic 0600) rather
than inside the strict CapabilityPassport schema.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from verdict.omniroute_pipeline.free_rule import _atomic_write
from verdict.omniroute_pipeline.probes import seed_usable_context

STORE_NAME = "usable-context.json"
REPROBE_AFTER = timedelta(days=7)


def default_context_store_path() -> Path:
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent / STORE_NAME


def load_usable_context(path: Path) -> dict[str, dict[str, object]]:
    """Stored measurements over the seeds; a measured value replaces its seed."""
    rows: dict[str, dict[str, object]] = {
        route: {
            "usable_context": seed.usable_context,
            "measured_at": seed.measured_at.isoformat() if seed.measured_at else None,
            "source": seed.source,
        }
        for route, seed in seed_usable_context().items()
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rows
    if isinstance(data, dict):
        rows.update({str(k): v for k, v in data.items() if isinstance(v, dict)})
    return rows


def record_usable_context(
    path: Path, route: str, usable_context: int | None, *, now: datetime, failed_step: int | None
) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[route] = {
        "usable_context": usable_context,
        "measured_at": now.isoformat(),
        "source": "census needle",
        "failed_step": failed_step,
    }
    _atomic_write(path, json.dumps(data, sort_keys=True))


def context_due(row: dict[str, object] | None, now: datetime) -> bool:
    """Probe when unmeasured, or the last measurement is a week old and did not top out."""
    if row is None or row.get("measured_at") is None:
        return True
    try:
        at = datetime.fromisoformat(str(row["measured_at"]))
    except (TypeError, ValueError):
        return True
    # Untrusted cache: an offset-naive stamp is invalid evidence, so the
    # measurement is treated as missing rather than crashing the census.
    if at.tzinfo is None:
        return True
    return now - at >= REPROBE_AFTER or (
        row.get("failed_step") is None and row.get("source") == "census needle"
    )
