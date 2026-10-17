"""Rule 0.2: a route is FREE only on explicit zero pricing or the provider's own flag.

A route is FREE iff no source shows a nonzero price AND either OmniRoute's
catalog price is explicitly 0/0 or the provider's catalog marks the exact id
free. A blank price without a provider flag fails closed (UNVERIFIED_FREE). A
``:free`` suffix or OmniRoute's own ``isFree`` is never evidence. Matching is
by exact route id; no name normalizer is used, so look-alikes stay distinct.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

FREE_FLAGS_MAX_AGE = timedelta(hours=48)
FETCH_TIMEOUT_SECONDS = 30.0
KILO_MODELS_URL = "https://api.kilo.ai/api/gateway/models"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
LAST_GOOD_NAME = "free-flags-last-good.json"

FlagEntry = dict[str, bool | float | None]


class FreeStatus(Enum):
    FREE = "free"
    NOT_FREE = "not_free"
    UNVERIFIED_FREE = "unverified_free"


@dataclass(frozen=True)
class ProviderFreeFlags:
    """Provider-catalog free flags keyed by exact OmniRoute route id."""

    fetched_at: datetime
    entries: Mapping[str, FlagEntry] = field(default_factory=dict)


def _price(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def classify_free(
    route: str,
    omniroute_price: Mapping[str, object] | None,
    flags: ProviderFreeFlags | None,
    *,
    omniroute_is_free: bool = False,
) -> FreeStatus:
    """Apply rule 0.2. ``omniroute_is_free`` is accepted and deliberately ignored."""
    del omniroute_is_free  # never evidence (V/free_tier_admit.py _looks_free_by_name)
    entry = flags.entries.get(route) if flags is not None else None
    om_in = _price((omniroute_price or {}).get("input"))
    om_out = _price((omniroute_price or {}).get("output"))
    prices = [om_in, om_out]
    if entry is not None:
        prices += [_price(entry.get("input")), _price(entry.get("output"))]
    if any(p is not None and p != 0 for p in prices):
        return FreeStatus.NOT_FREE
    if om_in == 0 and om_out == 0:
        return FreeStatus.FREE
    if entry is not None and entry.get("is_free") is True:
        return FreeStatus.FREE
    return FreeStatus.UNVERIFIED_FREE


def _rows(payload: object) -> list[Mapping[str, object]]:
    data = payload.get("data") if isinstance(payload, Mapping) else payload
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, Mapping)]


def _pricing(row: Mapping[str, object]) -> tuple[float | None, float | None]:
    pricing = row.get("pricing")
    if not isinstance(pricing, Mapping):
        return None, None
    return _price(pricing.get("prompt")), _price(pricing.get("completion"))


def parse_kilo_catalog(payload: object, *, prefix: str) -> dict[str, FlagEntry]:
    """Kilo marks free ids with ``isFree`` and/or 0/0 pricing."""
    out: dict[str, FlagEntry] = {}
    for row in _rows(payload):
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        prompt, completion = _pricing(row)
        zero = prompt == 0 and completion == 0
        out[f"{prefix}/{model_id}"] = {
            "is_free": row.get("isFree") is True or zero,
            "input": prompt,
            "output": completion,
        }
    return out


def parse_openrouter_catalog(payload: object, *, prefix: str) -> dict[str, FlagEntry]:
    """OpenRouter free means pricing.prompt and pricing.completion are both zero."""
    out: dict[str, FlagEntry] = {}
    for row in _rows(payload):
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        prompt, completion = _pricing(row)
        out[f"{prefix}/{model_id}"] = {
            "is_free": prompt == 0 and completion == 0,
            "input": prompt,
            "output": completion,
        }
    return out


Getter = Callable[[str, float], object]
Parser = Callable[..., dict[str, FlagEntry]]

# One URL and one parser per provider; anything not covered falls to rule (a).
PROVIDER_SOURCES: tuple[tuple[str, str, Parser], ...] = (
    ("kc", KILO_MODELS_URL, parse_kilo_catalog),
    ("openrouter", OPENROUTER_MODELS_URL, parse_openrouter_catalog),
)


def _http_get_json(url: str, timeout: float) -> object:
    import httpx

    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_provider_free_flags(now: datetime, get: Getter | None = None) -> ProviderFreeFlags | None:
    """Fetch every provider catalog (30 s timeout, one retry). None if all fail."""
    getter = get or _http_get_json
    entries: dict[str, FlagEntry] = {}
    fetched = 0
    for prefix, url, parser in PROVIDER_SOURCES:
        for _attempt in range(2):
            try:
                payload = getter(url, FETCH_TIMEOUT_SECONDS)
            except Exception:
                continue
            entries.update(parser(payload, prefix=prefix))
            fetched += 1
            break
    if fetched == 0:
        return None
    return ProviderFreeFlags(fetched_at=now, entries=entries)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_free_flags(path: Path, flags: ProviderFreeFlags) -> None:
    """Atomic 0600 write, same pattern as the admit last-good file."""
    body = {"fetched_at": flags.fetched_at.isoformat(), "entries": dict(flags.entries)}
    _atomic_write(path, json.dumps(body, sort_keys=True))


def load_free_flags(
    path: Path, *, now: datetime, max_age: timedelta = FREE_FLAGS_MAX_AGE
) -> ProviderFreeFlags | None:
    """Return cached flags if younger than ``max_age``; otherwise drop (b) evidence."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(str(data["fetched_at"]))
        raw = data["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(raw, dict) or now - fetched_at > max_age or fetched_at > now:
        return None
    entries: dict[str, FlagEntry] = {
        str(k): {
            "is_free": v.get("is_free") is True,
            "input": _price(v.get("input")),
            "output": _price(v.get("output")),
        }
        for k, v in raw.items()
        if isinstance(v, dict)
    }
    return ProviderFreeFlags(fetched_at=fetched_at, entries=entries)
