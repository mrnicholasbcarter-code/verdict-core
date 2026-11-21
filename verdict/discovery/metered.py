"""verdict.discovery.metered — permanent METERED overrides from probe observations.

A route whose liveness probe returned HTTP 200 but carried a non-zero
``x-omniroute-response-cost`` header is proven to charge per-request.  That
fact is written here and never expires: once METERED, always METERED.

Standard library only.  This module is imported by registry.py (at probe time)
and eligibility.py (at admission time), so it must stay import-safe.
"""

from __future__ import annotations

import datetime
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from verdict.paths import verdict_home

# Gateway prefix stripped when storing route keys so the admission-side
# lookup (which uses bare inventory ids) finds the right entry.
_GATEWAY_PREFIX = "omniroute/"


def _canonical_key(route_key: str) -> str:
    """Strip the ``omniroute/`` prefix so stored keys match inventory ids."""
    if route_key.lower().startswith(_GATEWAY_PREFIX):
        return route_key[len(_GATEWAY_PREFIX) :]
    return route_key


@dataclass
class MeteredEntry:
    """One route proven to charge per-request during a discovery probe."""

    route_key: str  # canonical (no omniroute/ prefix)
    observed_cost_usd: float  # the x-omniroute-response-cost value at detection
    evidence: str  # always "discovery_observed_charge"
    checked_at: str  # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MeteredEntry:
        return cls(
            route_key=str(d["route_key"]),
            observed_cost_usd=float(d["observed_cost_usd"]),
            evidence=str(d.get("evidence", "discovery_observed_charge")),
            checked_at=str(d["checked_at"]),
        )


def metered_path(env: Mapping[str, str] | None = None) -> Path:
    """``$VERDICT_HOME/discovery/metered.json``."""
    return verdict_home(env) / "discovery" / "metered.json"


def load_metered_overrides(env: Mapping[str, str] | None = None) -> dict[str, MeteredEntry]:
    """Load persisted METERED overrides keyed by canonical route key.

    Returns an empty dict on any read or parse error — the caller must treat
    missing entries as unknown, never as free.
    """
    path = metered_path(env)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, PermissionError, UnicodeDecodeError):
        return {}
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    result: dict[str, MeteredEntry] = {}
    for key, entry_dict in d.items():
        if not isinstance(entry_dict, dict):
            continue
        try:
            result[str(key)] = MeteredEntry.from_dict(entry_dict)
        except (KeyError, TypeError, ValueError):
            continue
    return result


def add_metered_override(
    route_key: str, observed_cost_usd: float, evidence: str, env: Mapping[str, str] | None = None
) -> None:
    """Atomically add (or refresh) a METERED override for *route_key*.

    ``route_key`` is canonicalised (``omniroute/`` prefix stripped) before
    storage so the lookup in eligibility always matches.  Existing entries for
    other routes are preserved.  Writes are atomic (write-tmp then os.replace).
    Errors are suppressed — a failed write means the route may be probed again
    next time, but no route is wrongly blocked.
    """
    canonical = _canonical_key(route_key)
    path = metered_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = load_metered_overrides(env)
        existing[canonical] = MeteredEntry(
            route_key=canonical,
            observed_cost_usd=observed_cost_usd,
            evidence=evidence,
            checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(
                json.dumps({k: v.to_dict() for k, v in existing.items()}, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            import contextlib

            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
    except Exception:
        pass  # probe write failures are non-fatal
