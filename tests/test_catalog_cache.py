"""Tests for /v1/models persistent catalog cache (Task 2)."""

from __future__ import annotations

import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from verdict.catalog_cache import get_cached_catalog, put_cached_catalog
from verdict.free_tier_admit import load_omniroute_admit_snapshot

NOW = datetime(2026, 10, 14, 10, 0, 0, tzinfo=timezone.utc)


def _mock_transport(models_calls: list[str]) -> httpx.BaseTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url_path = request.url.path
        if url_path == "/v1/models":
            models_calls.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "provider/test-model:free", "owned_by": "provider"}],
                },
            )
        elif url_path == "/api/free-tier/summary":
            return httpx.Response(
                200,
                json={
                    "perModel": [
                        {
                            "modelId": "test-model:free",
                            "provider": "provider",
                            "freeType": "keyless",
                        }
                    ]
                },
            )
        elif url_path == "/api/v1/me/status":
            return httpx.Response(200, json={"accountQuotas": []})
        elif url_path == "/api/providers":
            return httpx.Response(
                200,
                json={
                    "connections": [
                        {"provider": "provider", "isActive": True, "testStatus": "active"}
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_put_and_get_cached_catalog(tmp_path: Path) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    origin = "http://127.0.0.1:20128"
    api_key = "secret-token-123"
    catalog_data = {"object": "list", "data": [{"id": "model-1"}]}

    # Initially empty
    assert get_cached_catalog(origin, api_key, now=NOW, cache_path=cache_path) is None

    # Put into cache
    put_cached_catalog(
        origin, api_key, catalog_data, now=NOW, ttl_seconds=300, cache_path=cache_path
    )

    # Cache hit
    cached = get_cached_catalog(
        origin, api_key, now=NOW + timedelta(seconds=10), cache_path=cache_path
    )
    assert cached == catalog_data


def test_cache_no_credential_storage(tmp_path: Path) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    origin = "http://127.0.0.1:20128"
    api_key = "super-sensitive-secret-token"
    catalog_data = {"object": "list", "data": []}

    put_cached_catalog(origin, api_key, catalog_data, now=NOW, cache_path=cache_path)

    raw_text = cache_path.read_text()
    assert api_key not in raw_text
    assert "super-sensitive" not in raw_text
    assert "auth_hash" not in raw_text
    salt = (cache_path.parent / "catalog-cache-salt.bin").read_bytes()
    assert _expected_fingerprint(api_key, salt) in _fingerprints(cache_path)


def _expected_fingerprint(api_key: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", api_key.encode("utf-8"), salt, 20_000).hex()


def _fingerprints(cache_path: Path) -> set[str]:
    entries = json.loads(cache_path.read_text())["entries"]
    return {key.split("#", 1)[1] for key in entries}


def test_cache_salt_file_is_private_and_stable(tmp_path: Path) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    salt_path = tmp_path / "catalog-cache-salt.bin"

    put_cached_catalog("http://gateway", "key-one", {"a": 1}, now=NOW, cache_path=cache_path)
    assert salt_path.is_file()
    mode = stat.S_IMODE(salt_path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR

    first_salt = salt_path.read_bytes()
    # A second put with a different key must reuse the same on-disk salt
    # (same install, same host) rather than minting a new one per call.
    put_cached_catalog("http://gateway", "key-two", {"b": 2}, now=NOW, cache_path=cache_path)
    assert salt_path.read_bytes() == first_salt


def test_cache_fingerprint_is_keyed_not_plain_hash(tmp_path: Path) -> None:
    """The on-disk scope key must not equal a plain sha256 of the raw key,
    and must differ from installation to installation (different salt)."""
    cache_path_a = tmp_path / "a" / "catalog-cache.json"
    cache_path_b = tmp_path / "b" / "catalog-cache.json"
    api_key = "shared-key-across-installs"

    put_cached_catalog("http://gateway", api_key, {"x": 1}, now=NOW, cache_path=cache_path_a)
    put_cached_catalog("http://gateway", api_key, {"x": 1}, now=NOW, cache_path=cache_path_b)

    keys_a = set(json.loads(cache_path_a.read_text())["entries"])
    keys_b = set(json.loads(cache_path_b.read_text())["entries"])
    assert keys_a and keys_b
    assert keys_a.isdisjoint(keys_b)  # different per-install salt -> different fingerprint

    for cache_path in (cache_path_a, cache_path_b):
        salt = (cache_path.parent / "catalog-cache-salt.bin").read_bytes()
        assert _expected_fingerprint(api_key, salt) in _fingerprints(cache_path)


def test_cache_expiry(tmp_path: Path) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    origin = "http://127.0.0.1:20128"
    api_key = "token"
    catalog_data = {"object": "list", "data": []}

    put_cached_catalog(
        origin, api_key, catalog_data, now=NOW, ttl_seconds=60, cache_path=cache_path
    )

    # Valid before expiry
    assert (
        get_cached_catalog(origin, api_key, now=NOW + timedelta(seconds=59), cache_path=cache_path)
        is not None
    )

    # Expired after TTL
    assert (
        get_cached_catalog(origin, api_key, now=NOW + timedelta(seconds=61), cache_path=cache_path)
        is None
    )


def test_cache_endpoint_and_auth_isolation(tmp_path: Path) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    origin1 = "http://127.0.0.1:20128"
    origin2 = "https://gateway.example.com"
    key1 = "key-A"
    key2 = "key-B"

    put_cached_catalog(origin1, key1, {"origin": 1}, now=NOW, cache_path=cache_path)

    # Different origin misses
    assert get_cached_catalog(origin2, key1, now=NOW, cache_path=cache_path) is None

    # Different key misses
    assert get_cached_catalog(origin1, key2, now=NOW, cache_path=cache_path) is None

    # Same origin + key hits
    assert get_cached_catalog(origin1, key1, now=NOW, cache_path=cache_path) == {"origin": 1}


def test_cache_corrupt_fallback(tmp_path: Path) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    cache_path.write_text("corrupted json content {{{")

    origin = "http://127.0.0.1:20128"
    assert get_cached_catalog(origin, "key", now=NOW, cache_path=cache_path) is None

    # Overwriting with valid data works
    put_cached_catalog(origin, "key", {"valid": True}, now=NOW, cache_path=cache_path)
    assert get_cached_catalog(origin, "key", now=NOW, cache_path=cache_path) == {"valid": True}


def test_load_omniroute_admit_snapshot_uses_catalog_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "catalog-cache.json"
    monkeypatch.setenv("VERDICT_CATALOG_CACHE", str(cache_path))

    models_calls: list[str] = []
    transport = _mock_transport(models_calls)

    # First call: cache miss, calls /v1/models
    snap1 = load_omniroute_admit_snapshot(
        "http://127.0.0.1:20128", api_key="test-key", transport=transport
    )
    assert len(models_calls) == 1
    assert any(c.identity_id == "provider/test-model:free" for c in snap1.catalog)

    # Second call: cache hit, does NOT call /v1/models
    snap2 = load_omniroute_admit_snapshot(
        "http://127.0.0.1:20128", api_key="test-key", transport=transport
    )
    assert len(models_calls) == 1  # Still 1!
    assert any(c.identity_id == "provider/test-model:free" for c in snap2.catalog)


@pytest.mark.parametrize("ttl", ["bad", float("inf"), -1])
def test_cache_invalid_ttl_is_a_miss(tmp_path, ttl):
    import json

    path = tmp_path / "catalog.json"
    put_cached_catalog("http://gateway", "key", {"data": []}, now=NOW, cache_path=path)
    data = json.loads(path.read_text())
    next(iter(data["entries"].values()))["ttl_seconds"] = ttl
    path.write_text(json.dumps(data))
    assert get_cached_catalog("http://gateway", "key", now=NOW, cache_path=path) is None
