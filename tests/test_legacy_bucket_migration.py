"""Offline legacy provider/pool migration; all paths are temporary."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.health_cache import HealthCache, HealthCacheError

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
POOL = "google-antigravity"
OLD = ("agy/google-antigravity", "antigravity/google-antigravity")


def stamp(seconds: int = 0) -> str:
    return (NOW + timedelta(seconds=seconds)).isoformat()


def bucket(*, count: int = 1, capacity: int = 10, window: int = 60,
           zero: int | None = None) -> dict:
    value = {"capacity": capacity, "window_seconds": window,
             "timestamps": [stamp()] * count}
    if zero is not None:
        value["zeroed_until"] = stamp(zero)
    return value


def write_cache(tmp_path: Path, buckets: dict) -> Path:
    path = tmp_path / "health-cache.json"
    path.write_text(json.dumps({"schema_version": "1", "routes": {},
                               "buckets": buckets, "cursor": {"next": "other/model"}}))
    return path


def test_alias_usage_cooldown_and_restrictions(tmp_path: Path) -> None:
    path = write_cache(tmp_path, {
        OLD[0]: bucket(count=2, capacity=8, window=120, zero=30),
        OLD[1]: bucket(count=3, capacity=6, window=90, zero=90),
        POOL: bucket(count=1, capacity=10, zero=60),
    })
    original = path.read_bytes()
    cache = HealthCache(path)
    assert path.read_bytes() == original  # loading is offline/read-only
    merged = cache.bucket_for("agy", POOL)
    assert (merged.capacity, merged.window_seconds) == (6, 120)
    assert len(merged.timestamps) == len(set(merged.token_ids)) == 6
    assert merged.zeroed_until == NOW + timedelta(seconds=90)
    assert merged.remaining(NOW) == 0
    assert set(cache._buckets) == {POOL}
    cache.save()
    first = path.read_bytes()
    for _ in range(2):
        cache = HealthCache(path)
        cache.save()
        assert path.read_bytes() == first
    assert cache.cursor == {"next": "other/model"}


def test_unknown_keys_and_unrelated_state_unchanged(tmp_path: Path) -> None:
    keys = ("agy/userpool", "agy/sonnet", "unknown/unknown", "unknown/userpool",
            "cursor/cursor-api", "cu/cursor-api", "plain", "agy/x/y")
    path = write_cache(tmp_path, {key: bucket() for key in keys})
    payload = json.loads(path.read_text())
    payload["cooldowns"] = {"provider:other": {
        "key": "provider:other", "category": "rate_limited",
        "checked_at": stamp(), "until": stamp(300)}}
    path.write_text(json.dumps(payload))
    cache = HealthCache(path)
    assert set(cache._buckets) == set(keys)
    cache.save()
    actual = json.loads(path.read_text())
    assert actual["cooldowns"] == payload["cooldowns"]
    assert actual["cursor"] == payload["cursor"]


@pytest.mark.parametrize("key,pool", [
    ("af/api-airforce", "api-airforce"), ("ollamacloud/ollama-cloud", "ollama-cloud"),
    ("bm/bluesminds", "bluesminds"), ("kc/openrouter-free", "openrouter-free"),
    ("openrouter/openrouter-free", "openrouter-free"), ("cu/cursor", "cursor"),
    ("cua/cursor-api", "cursor-api"), ("dva/devin-cli", "devin-cli"),
])
def test_recognized_pool_policy(tmp_path: Path, key: str, pool: str) -> None:
    cache = HealthCache(write_cache(tmp_path, {key: bucket()}))
    assert set(cache._buckets) == {pool}
    assert len(cache._buckets[pool].timestamps) == 1


def test_owned_tokens_tombstones_and_stale_writer(tmp_path: Path) -> None:
    live = bucket(count=2, window=120)
    live["token_ids"] = ["owned", "released-token"]
    live["ledger_at"] = stamp(5)
    tombstones = bucket(count=1, capacity=7, window=90, zero=50)
    tombstones["token_ids"] = ["zeroed-token"]
    tombstones["removed"] = {"released-token": stamp(), "zeroed-token": stamp()}
    tombstones["released"] = {"reservation": stamp()}
    tombstones["ledger_at"] = stamp(10)
    path = write_cache(tmp_path, {OLD[0]: live, OLD[1]: tombstones})
    cache = HealthCache(path)
    merged = cache.bucket_for("agy", POOL)
    assert merged.token_ids == ["owned"]
    assert merged.removed == {"released-token": NOW, "zeroed-token": NOW}
    assert merged.released == {"reservation": NOW}
    assert merged.ledger_at == NOW + timedelta(seconds=10)
    stale = HealthCache(path)
    # Simulate an already-running old writer retaining provider/pool keys.
    stale._buckets = {OLD[0]: stale._buckets[POOL]}
    cache.merge_and_save(lambda current: current.zero_bucket("agy", NOW + timedelta(seconds=100), pool=POOL))
    stale.save()
    final = HealthCache(path).bucket_for("antigravity", POOL)
    assert final.token_ids == []
    assert "owned" in final.removed
    assert final.zeroed_until == NOW + timedelta(seconds=100)
    assert final.capacity == 7 and final.window_seconds == 120
    assert set(HealthCache(path)._buckets) == {POOL}
    first = path.read_bytes()
    stale.save()
    assert path.read_bytes() == first


def test_stale_writer_cannot_relax_restrictions_or_duplicate_legacy_usage(tmp_path: Path) -> None:
    path = write_cache(tmp_path, {OLD[0]: bucket(count=2, capacity=9, window=120),
                                  OLD[1]: bucket(count=2, capacity=5, window=60)})
    stale = HealthCache(path)
    current = HealthCache(path)
    current.save()
    stale._buckets = {OLD[0]: stale._buckets[POOL]}
    stale._buckets[OLD[0]].capacity = 10
    stale._buckets[OLD[0]].window_seconds = 30
    stale.save()
    final = HealthCache(path).bucket_for("agy", POOL)
    assert (final.capacity, final.window_seconds) == (5, 120)
    assert len(final.timestamps) == 4


@pytest.mark.parametrize("field,value", [
    ("capacity", 0), ("capacity", None), ("capacity", 1.5),
    ("window_seconds", 0), ("window_seconds", None),
])
def test_invalid_migrated_bounds_fail_closed(tmp_path: Path, field: str, value: object) -> None:
    legacy = bucket()
    legacy[field] = value
    path = write_cache(tmp_path, {OLD[0]: legacy})
    original = path.read_bytes()
    with pytest.raises(HealthCacheError):
        HealthCache(path)
    assert path.read_bytes() == original


def test_highwater_prunes_only_after_longest_window(tmp_path: Path) -> None:
    older = bucket(window=120)
    older["timestamps"] = [stamp(-80)]
    newer = bucket(capacity=4)
    newer["ledger_at"] = stamp(10)
    path = write_cache(tmp_path, {OLD[0]: older, POOL: newer})
    cache = HealthCache(path)
    assert len(cache.bucket_for("agy", POOL).timestamps) == 2
    cache.merge_and_save(lambda _: None)
    assert len(HealthCache(path).bucket_for("agy", POOL).timestamps) == 2


@pytest.mark.parametrize("broken", [
    {"capacity": 0}, {"window_seconds": 0}, {"token_ids": ["partial"]},
    {"removed": {"uncertain": stamp()}},
])
def test_ambiguous_merge_rejected_without_rewrite(tmp_path: Path, broken: dict) -> None:
    legacy = bucket(count=2)
    legacy.update(broken)
    path = write_cache(tmp_path, {OLD[0]: legacy, POOL: bucket()})
    original = path.read_bytes()
    with pytest.raises(HealthCacheError):
        HealthCache(path)
    assert path.read_bytes() == original


def test_invalid_canonical_bounds_in_migrating_group_rejected(tmp_path: Path) -> None:
    canonical = bucket()
    canonical["capacity"] = 0
    path = write_cache(tmp_path, {OLD[0]: bucket(), POOL: canonical})
    with pytest.raises(HealthCacheError):
        HealthCache(path)


def test_owned_identity_dedupes_and_unrelated_route_survives(tmp_path: Path) -> None:
    owned = bucket()
    owned["token_ids"] = ["shared-owned-token"]
    path = write_cache(tmp_path, {OLD[0]: owned, OLD[1]: owned, POOL: bucket()})
    payload = json.loads(path.read_text())
    payload["routes"] = {"other/model": {
        "route_id": "other/model", "category": "ok", "checked_at": stamp(),
        "until": stamp(1800), "consecutive_failures": 0,
        "chat_ok": True, "tool_ok": True, "healthy": True, "write_revision": 4}}
    path.write_text(json.dumps(payload))
    cache = HealthCache(path)
    before = cache.entry("other/model")
    assert len(cache.bucket_for("agy", POOL).token_ids) == 2
    assert "shared-owned-token" in cache.bucket_for("agy", POOL).token_ids
    cache.merge_and_save(lambda _: None)
    assert HealthCache(path).entry("other/model") == before
