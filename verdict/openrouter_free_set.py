"""OpenRouter public /models free-set membership (BOD-418).

OpenRouter FREE capacity is decided only by a fresh public catalog price check:
both ``pricing.prompt`` and ``pricing.completion`` must be explicitly numeric
zero, and no other pricing field may be a positive billable amount. Missing or
invalid pricing is not zero. The ``:free`` suffix alone never establishes FREE.

The public catalog needs no API key. Results are cached on disk with a TTL.
A failed fetch with no still-valid cache yields no free assertion.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.availability import is_opaque_route_id
from verdict.http_safety import open_no_redirect
from verdict.paths import verdict_home

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
ENV_CACHE_PATH = "VERDICT_OPENROUTER_FREE_SET_CACHE"
ENV_TTL_SECONDS = "VERDICT_OPENROUTER_FREE_SET_TTL_S"
# Brief proposed bound; chosen as the default for public pricing freshness.
DEFAULT_TTL_SECONDS = 6 * 60 * 60
OPENROUTER_PREFIXES = frozenset({"openrouter", "kc", "kilocode"})
# Capacity FREE-gating (BOD-418) applies to the openrouter/ catalog prefix.
# kc/kilocode :free keep the existing free_suffix rule (D5) unless a caller
# also intersects them with the public set.
OPENROUTER_CATALOG_PREFIX = "openrouter"

# Catalog modalities that are not coding/chat text generation. Image/audio on
# the *input* side can still be multimodal chat; generation of audio/image/
# embedding/etc. is what excludes coding qualification.
_NON_TEXT_OUTPUT = frozenset({"audio", "image", "embedding", "embeddings", "video"})


@dataclass(frozen=True)
class OpenRouterFreeEntry:
    """One public catalog row that cleared the 0/0 pricing gate."""

    model_id: str
    pricing: Mapping[str, float]
    output_modalities: tuple[str, ...]
    opaque: bool


@dataclass(frozen=True)
class OpenRouterFreeSet:
    """Cached free membership derived from the public OpenRouter catalog."""

    free_ids: frozenset[str]
    entries: Mapping[str, OpenRouterFreeEntry]
    fetched_at: datetime
    source: str  # "live" | "cache"
    ttl_seconds: float

    def contains(self, model_id: str) -> bool:
        return _bare_id(model_id) in self.free_ids

    def coding_candidate_ids(self) -> frozenset[str]:
        """0-cost text/chat ids that are concrete (not the free-router alias)."""
        out: set[str] = set()
        for model_id, entry in self.entries.items():
            if entry.opaque:
                continue
            if not _is_text_chat_output(entry.output_modalities):
                continue
            out.add(model_id)
        return frozenset(out)


FetchFn = Callable[[str, float], Mapping[str, Any]]


def default_cache_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    configured = (source.get(ENV_CACHE_PATH) or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (verdict_home(env) / "openrouter-free-set.json").resolve()


def default_ttl_seconds(env: Mapping[str, str] | None = None) -> float:
    source = os.environ if env is None else env
    raw = (source.get(ENV_TTL_SECONDS) or "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return float(DEFAULT_TTL_SECONDS)


def _bare_id(route_or_model: str) -> str:
    """Strip gateway prefixes (openrouter/, kc/, kilocode/) to the catalog id."""
    value = route_or_model.strip()
    lowered = value.lower()
    for prefix in ("openrouter/", "kc/", "kilocode/"):
        if lowered.startswith(prefix):
            return value[len(prefix) :]
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def pricing_is_explicitly_free(pricing: object) -> bool:
    """True only when prompt and completion are numeric zero and no field is >0."""
    if not isinstance(pricing, Mapping):
        return False
    prompt = _number(pricing.get("prompt"))
    completion = _number(pricing.get("completion"))
    if prompt is None or completion is None:
        return False
    if prompt != 0.0 or completion != 0.0:
        return False
    for value in pricing.values():
        parsed = _number(value)
        if parsed is not None and parsed > 0.0:
            return False
    return True


def _output_modalities(row: Mapping[str, Any]) -> tuple[str, ...]:
    arch = row.get("architecture")
    if isinstance(arch, Mapping):
        raw = arch.get("output_modalities")
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            return tuple(str(item).lower() for item in raw if str(item).strip())
        modality = arch.get("modality")
        if isinstance(modality, str) and "->" in modality:
            right = modality.split("->", 1)[1]
            return tuple(part.strip().lower() for part in right.split("+") if part.strip())
    raw = row.get("output_modalities")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return tuple(str(item).lower() for item in raw if str(item).strip())
    return ()


def _is_text_chat_output(modalities: Sequence[str]) -> bool:
    if not modalities:
        # Fail closed for coding qualification when modality is unknown: the
        # caller can still see membership via ``contains``, but coding probes
        # should not treat unknown-modality free rows as qualified.
        return False
    lowered = {item.lower() for item in modalities}
    if lowered & _NON_TEXT_OUTPUT:
        return False
    return "text" in lowered


def parse_free_entries(payload: object) -> dict[str, OpenRouterFreeEntry]:
    """Parse public /models JSON into explicitly-free concrete entries."""
    raw: object
    if isinstance(payload, Mapping):
        raw = payload.get("data", payload.get("models", payload.get("items", [])))
    else:
        raw = payload
    if not isinstance(raw, list):
        return {}
    out: dict[str, OpenRouterFreeEntry] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        pricing = row.get("pricing")
        if not pricing_is_explicitly_free(pricing):
            continue
        assert isinstance(pricing, Mapping)
        parsed_pricing = {
            str(key): parsed
            for key, value in pricing.items()
            if (parsed := _number(value)) is not None
        }
        modalities = _output_modalities(row)
        out[model_id] = OpenRouterFreeEntry(
            model_id=model_id,
            pricing=parsed_pricing,
            output_modalities=modalities,
            opaque=is_opaque_route_id(model_id) or model_id.lower() in {"openrouter/free", "free"},
        )
    return out


def fetch_openrouter_models(
    url: str = OPENROUTER_MODELS_URL, *, timeout: float = 30.0
) -> Mapping[str, Any]:
    """GET the public OpenRouter catalog. No Authorization header."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "verdict-openrouter-free-set/1"},
        method="GET",
    )
    with open_no_redirect(request, timeout=timeout) as response:
        body = response.read()
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("openrouter /models payload is not an object")
    return payload


def _read_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_cache(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, sort_keys=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, delete=False, encoding="utf-8"
        ) as tf:
            tf.write(body)
            temp_path = Path(tf.name)
        temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temp_path.replace(path)
    except OSError:
        pass


def _set_from_entries(
    entries: Mapping[str, OpenRouterFreeEntry],
    *,
    fetched_at: datetime,
    source: str,
    ttl_seconds: float,
) -> OpenRouterFreeSet:
    return OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=dict(entries),
        fetched_at=fetched_at,
        source=source,
        ttl_seconds=ttl_seconds,
    )


def _cache_payload(
    free_set: OpenRouterFreeSet, raw_payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    return {
        "fetched_at": free_set.fetched_at.isoformat().replace("+00:00", "Z"),
        "ttl_seconds": free_set.ttl_seconds,
        "free_ids": sorted(free_set.free_ids),
        "entries": {
            model_id: {
                "pricing": dict(entry.pricing),
                "output_modalities": list(entry.output_modalities),
                "opaque": entry.opaque,
            }
            for model_id, entry in sorted(free_set.entries.items())
        },
        # Keep a slim raw copy only of free rows so reloads do not need a refetch
        # to recover modality metadata if entries were written by an older cache.
        "raw_free_rows": [
            row
            for row in (
                (raw_payload or {}).get("data", []) if isinstance(raw_payload, Mapping) else []
            )
            if isinstance(row, Mapping) and row.get("id") in free_set.free_ids
        ],
    }


def _load_cached_set(path: Path, *, now: datetime, ttl_seconds: float) -> OpenRouterFreeSet | None:
    raw = _read_cache(path)
    if raw is None:
        return None
    fetched_raw = raw.get("fetched_at")
    if not isinstance(fetched_raw, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    try:
        cached_ttl = float(raw.get("ttl_seconds", ttl_seconds))
    except (TypeError, ValueError):
        cached_ttl = ttl_seconds
    if cached_ttl <= 0:
        return None
    age = (now - fetched_at).total_seconds()
    if age < 0 or age >= cached_ttl:
        return None
    entries_raw = raw.get("entries")
    entries: dict[str, OpenRouterFreeEntry] = {}
    if isinstance(entries_raw, Mapping):
        for model_id, item in entries_raw.items():
            if not isinstance(model_id, str) or not isinstance(item, Mapping):
                continue
            pricing = item.get("pricing")
            if not isinstance(pricing, Mapping) or not pricing_is_explicitly_free(pricing):
                continue
            modalities = item.get("output_modalities")
            mods = tuple(str(m).lower() for m in modalities) if isinstance(modalities, list) else ()
            opaque = bool(item.get("opaque")) or is_opaque_route_id(model_id)
            entries[model_id] = OpenRouterFreeEntry(
                model_id=model_id,
                pricing={str(k): float(v) for k, v in pricing.items() if _number(v) is not None},
                output_modalities=mods,
                opaque=opaque,
            )
    elif isinstance(raw.get("free_ids"), list):
        # Older/partial cache: membership only.
        for model_id in raw["free_ids"]:
            if isinstance(model_id, str) and model_id.strip():
                mid = model_id.strip()
                entries[mid] = OpenRouterFreeEntry(
                    model_id=mid,
                    pricing={"prompt": 0.0, "completion": 0.0},
                    output_modalities=("text",),
                    opaque=is_opaque_route_id(mid),
                )
    if not entries and not isinstance(raw.get("free_ids"), list):
        return None
    return _set_from_entries(entries, fetched_at=fetched_at, source="cache", ttl_seconds=cached_ttl)


def load_openrouter_free_set(
    *,
    now: datetime | None = None,
    cache_path: Path | None = None,
    ttl_seconds: float | None = None,
    fetch: FetchFn | None = None,
    timeout: float = 30.0,
    url: str = OPENROUTER_MODELS_URL,
    refresh: bool | None = None,
) -> OpenRouterFreeSet | None:
    """Return the cached-or-fresh free set, or None when membership cannot be asserted.

    ``refresh`` defaults to True outside pytest and False under
    ``PYTEST_CURRENT_TEST``, so hermetic tests never open openrouter.ai unless
    they pass an explicit ``fetch`` or ``refresh=True``.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    path = cache_path or default_cache_path()
    ttl = float(DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
    cached = _load_cached_set(path, now=current, ttl_seconds=ttl)
    if refresh is None:
        refresh = fetch is not None or not os.environ.get("PYTEST_CURRENT_TEST")
    if not refresh:
        return cached
    fetch_fn = fetch or (lambda target, to: fetch_openrouter_models(target, timeout=to))
    try:
        payload = fetch_fn(url, timeout)
        entries = parse_free_entries(payload)
        free_set = _set_from_entries(entries, fetched_at=current, source="live", ttl_seconds=ttl)
        _write_cache(
            path, _cache_payload(free_set, payload if isinstance(payload, Mapping) else None)
        )
        return free_set
    except (
        OSError,
        ValueError,
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        json.JSONDecodeError,
    ):
        return cached


def is_openrouter_family_route(route_id: str) -> bool:
    prefix = route_id.split("/", 1)[0].lower()
    return prefix in OPENROUTER_PREFIXES


def is_openrouter_catalog_route(route_id: str) -> bool:
    """True for gateway ids under the ``openrouter/`` catalog prefix."""
    return route_id.split("/", 1)[0].lower() == OPENROUTER_CATALOG_PREFIX


def openrouter_route_is_public_free(route_id: str, free_set: OpenRouterFreeSet | None) -> bool:
    """True when an ``openrouter/`` route is in the public 0/0 set.

    Non-openrouter routes are unaffected (returns False so callers do not use
    this helper as a global FREE oracle). Missing free_set means no assertion.
    """
    if free_set is None or not is_openrouter_catalog_route(route_id):
        return False
    bare = _bare_id(route_id)
    if bare.lower() in {"free", "openrouter/free"} or is_opaque_route_id(route_id):
        # Opaque free-router alias is never a certified concrete FREE identity.
        return False
    return free_set.contains(bare)


def intersect_with_gateway_ids(
    free_set: OpenRouterFreeSet, gateway_ids: Collection[str]
) -> frozenset[str]:
    """Return gateway route ids whose bare OpenRouter id is publicly free."""
    known = {_bare_id(item) for item in free_set.free_ids}
    out: set[str] = set()
    for route_id in gateway_ids:
        if not is_openrouter_family_route(route_id):
            continue
        bare = _bare_id(route_id)
        if bare in known and not is_opaque_route_id(route_id):
            if bare.lower() in {"free", "openrouter/free"}:
                continue
            out.add(route_id)
    return frozenset(out)


def key_limit_on_non_free_is_route_scoped(
    *, route_id: str, http_status: int | None, detail: str, free_set: OpenRouterFreeSet | None
) -> bool:
    """Paid/non-free OpenRouter key-limit 403 must not cool the FREE pool.

    Only returns True when the public free set positively shows the route is
    not free. Missing free_set fails closed (account/provider scope).
    """
    if http_status not in {402, 403}:
        return False
    if not is_openrouter_catalog_route(route_id):
        return False
    text = detail.lower()
    if "key limit" not in text and "total limit" not in text and "key limit exceeded" not in text:
        return False
    if free_set is None:
        return False
    # Confirmed-free routes keep account-level failure semantics.
    if openrouter_route_is_public_free(route_id, free_set):
        return False
    # Positively known non-member of the public 0/0 set.
    return not free_set.contains(_bare_id(route_id))


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "OPENROUTER_MODELS_URL",
    "OpenRouterFreeEntry",
    "OpenRouterFreeSet",
    "default_cache_path",
    "default_ttl_seconds",
    "fetch_openrouter_models",
    "intersect_with_gateway_ids",
    "is_openrouter_catalog_route",
    "is_openrouter_family_route",
    "key_limit_on_non_free_is_route_scoped",
    "load_openrouter_free_set",
    "openrouter_route_is_public_free",
    "parse_free_entries",
    "pricing_is_explicitly_free",
]
