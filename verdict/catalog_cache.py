"""Persistent, credential-safe TTL cache for OmniRoute /v1/models catalog."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.paths import verdict_home

ENV_CATALOG_CACHE = "VERDICT_CATALOG_CACHE"
ENV_CATALOG_TTL_S = "VERDICT_CATALOG_TTL_S"
DEFAULT_CATALOG_TTL_SECONDS = 300.0
_SALT_FILE_NAME = "catalog-cache-salt.bin"
_FINGERPRINT_ITERATIONS = 20_000
_FINGERPRINT_MEMO: dict[tuple[bytes, str], str] = {}
_SALT_BYTES = 32


def default_catalog_cache_path() -> Path:
    configured = os.getenv(ENV_CATALOG_CACHE)
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    return (verdict_home() / "catalog-cache.json").resolve()


def _default_ttl_seconds() -> float:
    raw = os.getenv(ENV_CATALOG_TTL_S)
    if raw and raw.strip():
        try:
            val = float(raw.strip())
            if math.isfinite(val) and val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_CATALOG_TTL_SECONDS


def _load_or_create_salt(cache_path: Path) -> bytes:
    """Per-install random salt, 0600, next to the cache file.

    The salt keys the HMAC fingerprint below so the on-disk cache never
    stores (or can be used to offline-brute-force) the raw API key or a
    plain, unkeyed hash of it.
    """
    salt_path = cache_path.parent / _SALT_FILE_NAME
    try:
        existing = salt_path.read_bytes()
        if len(existing) == _SALT_BYTES:
            return existing
    except OSError:
        pass
    salt = os.urandom(_SALT_BYTES)
    try:
        salt_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=salt_path.parent, delete=False) as tf:
            tf.write(salt)
            temp_path = Path(tf.name)
        temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temp_path.replace(salt_path)
    except OSError:
        pass
    return salt


def _auth_fingerprint(api_key: str | None, cache_path: Path) -> str:
    """Keyed, non-reversible fingerprint of the API key for cache scoping.

    Never the raw key and never a plain unkeyed hash of it: a salted
    PBKDF2-SHA256 under a per-install random salt that only exists on this host.
    """
    salt = _load_or_create_salt(cache_path)
    key = api_key or ""
    memo_key = (salt, key)
    fingerprint = _FINGERPRINT_MEMO.get(memo_key)
    if fingerprint is None:
        fingerprint = hashlib.pbkdf2_hmac(
            "sha256", key.encode("utf-8"), salt, _FINGERPRINT_ITERATIONS
        ).hex()
        _FINGERPRINT_MEMO[memo_key] = fingerprint
    return fingerprint


def _scope_key(origin: str, api_key: str | None, cache_path: Path) -> str:
    return f"{origin.rstrip('/')}#{_auth_fingerprint(api_key, cache_path)}"


def _read_cache_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except Exception:
        pass
    return {}


def _atomic_write_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(data, indent=2, sort_keys=True)
        dir_path = path.parent
        with tempfile.NamedTemporaryFile("w", dir=dir_path, delete=False, encoding="utf-8") as tf:
            tf.write(body)
            temp_path = Path(tf.name)
        temp_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        temp_path.replace(path)
    except Exception:
        pass


def get_cached_catalog(
    origin: str, api_key: str | None, *, now: datetime | None = None, cache_path: Path | None = None
) -> object | None:
    path = cache_path or default_catalog_cache_path()
    data = _read_cache_file(path)
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return None

    key = _scope_key(origin, api_key, path)
    entry = entries.get(key)
    if not isinstance(entry, dict):
        return None

    cached_at_raw = entry.get("cached_at")
    ttl = entry.get("ttl_seconds", DEFAULT_CATALOG_TTL_SECONDS)
    if not isinstance(cached_at_raw, str):
        return None

    try:
        cached_at = datetime.fromisoformat(cached_at_raw)
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    try:
        ttl_value = float(ttl)
        if not math.isfinite(ttl_value) or ttl_value <= 0:
            return None
        age = (current - cached_at).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None
    if age < 0 or age >= ttl_value:
        return None

    return entry.get("catalog")


def put_cached_catalog(
    origin: str,
    api_key: str | None,
    catalog: object,
    *,
    now: datetime | None = None,
    ttl_seconds: float | None = None,
    cache_path: Path | None = None,
) -> None:
    path = cache_path or default_catalog_cache_path()
    data = _read_cache_file(path)
    entries = data.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        data["entries"] = entries

    data["version"] = "1"
    current = now or datetime.now(timezone.utc)
    ttl = ttl_seconds if (ttl_seconds is not None and ttl_seconds > 0) else _default_ttl_seconds()

    key = _scope_key(origin, api_key, path)

    entries[key] = {
        "endpoint": origin.rstrip("/"),
        "cached_at": current.isoformat(),
        "ttl_seconds": float(ttl),
        "catalog": catalog,
    }

    _atomic_write_cache(path, data)


__all__ = [
    "DEFAULT_CATALOG_TTL_SECONDS",
    "ENV_CATALOG_CACHE",
    "ENV_CATALOG_TTL_S",
    "default_catalog_cache_path",
    "get_cached_catalog",
    "put_cached_catalog",
]
