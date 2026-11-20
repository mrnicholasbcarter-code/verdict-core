"""Offline unit tests for verdict.discovery.registry and verdict.discovery.refresh.

No network calls; the injectable probe transport is used for all HTTP.
"""

from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path

from verdict.discovery.model import Route
from verdict.discovery.probe import _set_transport
from verdict.discovery.refresh import render_refresh_plain, run_refresh_action
from verdict.discovery.registry import (
    BREAKER_CLOSED,
    BREAKER_OPEN,
    REGISTRY_SCHEMA,
    REGISTRY_TTL_S,
    DiscoveryRegistry,
    RegistryEntry,
    _Breaker,
    load_registry,
    registry_path,
    run_refresh,
    save_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route(
    *,
    provider: str = "p",
    model: str = "m",
    base_url: str = "http://host.example.com/v1",
    auth: str = "none",
    upstream: str | None = None,
) -> Route:
    return Route(provider=provider, model=model, base_url=base_url, auth=auth, upstream=upstream)


def _ok_body() -> bytes:
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()


def _tool_ok_body() -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "Paris"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()


def _429_body() -> bytes:
    return json.dumps({"error": {"message": "rate limit exceeded"}}).encode()


def _env(tmp: str) -> dict[str, str]:
    return {"VERDICT_HOME": tmp}


# ---------------------------------------------------------------------------
# RegistryEntry serialization
# ---------------------------------------------------------------------------


class RegistryEntryTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        entry = RegistryEntry(
            route_key="p/m",
            capacity_class="free",
            capacity_fact="plan_free",
            base_url="http://h/v1",
            auth="none",
            upstream=None,
            answered=True,
            tool_ok=True,
            served_model="provider/model",
            latency_ms=250,
            retry_after_s=None,
            breaker_state=BREAKER_CLOSED,
            checked_at="2026-11-20T00:00:00+00:00",
        )
        d = entry.to_dict()
        restored = RegistryEntry.from_dict(d)
        self.assertEqual(entry, restored)

    def test_literal_auth_is_sanitized_in_registry(self) -> None:
        """auth="literal:secret" must be stored as "literal", not "literal:secret"."""
        from verdict.discovery.registry import _sanitize_auth

        self.assertEqual(_sanitize_auth("literal:mysecret"), "literal")
        self.assertEqual(_sanitize_auth("env:MY_KEY"), "env:MY_KEY")
        self.assertEqual(_sanitize_auth("none"), "none")


# ---------------------------------------------------------------------------
# DiscoveryRegistry serialization
# ---------------------------------------------------------------------------


class DiscoveryRegistryTest(unittest.TestCase):
    def test_roundtrip_empty(self) -> None:
        reg = DiscoveryRegistry(
            schema=REGISTRY_SCHEMA,
            written_at="2026-11-20T00:00:00+00:00",
            ttl_s=REGISTRY_TTL_S,
            entries=[],
        )
        restored = DiscoveryRegistry.from_dict(reg.to_dict())
        self.assertEqual(reg.schema, restored.schema)
        self.assertEqual(reg.written_at, restored.written_at)
        self.assertEqual(reg.entries, restored.entries)

    def test_roundtrip_with_entries(self) -> None:
        entry = RegistryEntry(
            route_key="p/m",
            capacity_class="free",
            capacity_fact="zero_price",
            base_url="http://h/v1",
            auth="none",
            upstream="provider-a",
            answered=True,
            tool_ok=False,
            served_model=None,
            latency_ms=100,
            retry_after_s=None,
            breaker_state=BREAKER_CLOSED,
            checked_at="2026-11-20T00:00:00+00:00",
        )
        reg = DiscoveryRegistry(
            schema=REGISTRY_SCHEMA,
            written_at="2026-11-20T00:00:00+00:00",
            ttl_s=REGISTRY_TTL_S,
            entries=[entry],
        )
        restored = DiscoveryRegistry.from_dict(reg.to_dict())
        self.assertEqual(len(restored.entries), 1)
        self.assertEqual(restored.entries[0].route_key, "p/m")


# ---------------------------------------------------------------------------
# save_registry / load_registry
# ---------------------------------------------------------------------------


class RegistryPersistenceTest(unittest.TestCase):
    def test_save_and_load_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            reg = DiscoveryRegistry(
                schema=REGISTRY_SCHEMA,
                written_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ttl_s=REGISTRY_TTL_S,
                entries=[],
            )
            save_registry(reg, env)
            loaded = load_registry(env)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.schema, REGISTRY_SCHEMA)

    def test_load_missing_registry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = load_registry(_env(tmp))
            self.assertIsNone(result)

    def test_load_stale_registry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            # Write a registry whose written_at is 13 hours ago (past 12-h TTL)
            old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=13)
            reg = DiscoveryRegistry(
                schema=REGISTRY_SCHEMA, written_at=old.isoformat(), ttl_s=REGISTRY_TTL_S, entries=[]
            )
            save_registry(reg, env)
            loaded = load_registry(env)
            self.assertIsNone(loaded)

    def test_load_corrupt_registry_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            path = registry_path(env)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("not json at all", encoding="utf-8")
            self.assertIsNone(load_registry(env))

    def test_save_is_atomic(self) -> None:
        """Verify no .tmp file remains after save."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            reg = DiscoveryRegistry(
                schema=REGISTRY_SCHEMA,
                written_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ttl_s=REGISTRY_TTL_S,
                entries=[],
            )
            save_registry(reg, env)
            discovery_dir = Path(tmp) / "discovery"
            tmp_files = list(discovery_dir.glob("*.tmp"))
            self.assertEqual(tmp_files, [])

    def test_missing_registry_is_unknown_not_healthy(self) -> None:
        """A None result from load_registry MUST be treated as unknown, never healthy."""
        with tempfile.TemporaryDirectory() as tmp:
            result = load_registry(_env(tmp))
            # None is the correct signal — callers must not treat this as healthy
            self.assertIsNone(result)

    def test_fresh_within_ttl_is_returned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            # Written 1 hour ago — well within the 12-hour TTL
            recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
            reg = DiscoveryRegistry(
                schema=REGISTRY_SCHEMA,
                written_at=recent.isoformat(),
                ttl_s=REGISTRY_TTL_S,
                entries=[],
            )
            save_registry(reg, env)
            loaded = load_registry(env)
            self.assertIsNotNone(loaded)


# ---------------------------------------------------------------------------
# _Breaker
# ---------------------------------------------------------------------------


class BreakerTest(unittest.TestCase):
    def test_starts_closed(self) -> None:
        b = _Breaker()
        r = _route()
        self.assertFalse(b.is_open(r))
        self.assertEqual(b.state(r), BREAKER_CLOSED)

    def test_open_pool(self) -> None:
        b = _Breaker()
        r = _route(provider="p", upstream="kilo")
        b.open_pool(r)
        self.assertTrue(b.is_open(r))
        self.assertEqual(b.state(r), BREAKER_OPEN)

    def test_open_pool_isolates_other_providers(self) -> None:
        b = _Breaker()
        r1 = _route(provider="p", upstream="kilo")
        r2 = _route(provider="p", upstream="ovh")
        b.open_pool(r1)
        self.assertTrue(b.is_open(r1))
        self.assertFalse(b.is_open(r2))

    def test_pool_key_uses_upstream_when_present(self) -> None:
        b = _Breaker()
        r = _route(provider="omniroute", upstream="kilo")
        self.assertEqual(b.pool_key(r), "kilo")

    def test_pool_key_uses_provider_when_no_upstream(self) -> None:
        b = _Breaker()
        r = _route(provider="p", upstream=None)
        self.assertEqual(b.pool_key(r), "p")


# ---------------------------------------------------------------------------
# run_refresh — dry_run
# ---------------------------------------------------------------------------


class RunRefreshDryRunTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_dry_run_no_transport_called(self) -> None:
        called: list[bool] = []

        def _t(url, h, p, timeout):
            called.append(True)
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            routes = [(_route(provider="p", model=f"m{i}"), "free", "plan_free") for i in range(3)]
            reg = run_refresh(routes, dry_run=True, env=_env(tmp))
            self.assertEqual(called, [])
            self.assertEqual(len(reg.entries), 3)
            for entry in reg.entries:
                self.assertFalse(entry.answered)
                self.assertFalse(entry.tool_ok)

    def test_dry_run_does_not_write_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            routes = [(_route(), "free", "plan_free")]
            run_refresh(routes, dry_run=True, env=env)
            # Registry file should NOT be written in dry-run
            path = registry_path(env)
            self.assertFalse(path.exists())


# ---------------------------------------------------------------------------
# run_refresh — live (with fake transport)
# ---------------------------------------------------------------------------


class RunRefreshLiveTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_answered_and_tool_ok_route(self) -> None:
        # First call is liveness probe → UP; second is tool probe → TOOL_OK
        call_count: list[int] = [0]

        def _t(url, h, p, timeout):
            call_count[0] += 1
            body = json.loads(p)
            if "tools" in body:
                return 200, {}, _tool_ok_body()
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            routes = [(_route(), "free", "plan_free")]
            reg = run_refresh(routes, context_tokens=0, env=_env(tmp))
            self.assertEqual(len(reg.entries), 1)
            entry = reg.entries[0]
            self.assertTrue(entry.answered)
            self.assertTrue(entry.tool_ok)
            self.assertEqual(entry.capacity_class, "free")
            self.assertEqual(entry.capacity_fact, "plan_free")

    def test_rate_limited_route_sets_breaker_open(self) -> None:
        def _t(url, h, p, timeout):
            return 429, {"Retry-After": "30"}, _429_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            routes = [(_route(provider="kilo", upstream="kilo"), "free", "plan_free")]
            reg = run_refresh(routes, context_tokens=0, env=_env(tmp))
            self.assertEqual(len(reg.entries), 1)
            entry = reg.entries[0]
            self.assertFalse(entry.answered)
            self.assertEqual(entry.breaker_state, BREAKER_OPEN)
            self.assertEqual(entry.retry_after_s, 30.0)

    def test_registry_written_after_live_refresh(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            routes = [(_route(), "free", "plan_free")]
            run_refresh(routes, context_tokens=0, env=env)
            loaded = load_registry(env)
            self.assertIsNotNone(loaded)

    def test_empty_routes_returns_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh([], env=_env(tmp))
            self.assertEqual(reg.entries, [])

    def test_subscription_route_accepted(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            routes = [(_route(provider="claude", model="claude-3-5"), "subscription", "plan_paid")]
            reg = run_refresh(routes, context_tokens=0, env=_env(tmp))
            self.assertEqual(len(reg.entries), 1)
            self.assertEqual(reg.entries[0].capacity_class, "subscription")

    def test_multiple_routes_all_recorded(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            routes = [
                (_route(provider=f"p{i}", model=f"m{i}"), "free", "plan_free") for i in range(5)
            ]
            reg = run_refresh(routes, context_tokens=0, env=_env(tmp))
            self.assertEqual(len(reg.entries), 5)

    def test_served_model_recorded_from_headers(self) -> None:
        def _t(url, h, p, timeout):
            return (
                200,
                {"x-omniroute-provider": "upstream_prov", "x-omniroute-model": "upstream_model"},
                _ok_body(),
            )

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            routes = [(_route(), "free", "plan_free")]
            reg = run_refresh(routes, context_tokens=0, env=_env(tmp))
            entry = reg.entries[0]
            self.assertEqual(entry.served_model, "upstream_prov/upstream_model")


# ---------------------------------------------------------------------------
# render_refresh_plain
# ---------------------------------------------------------------------------


class RenderRefreshPlainTest(unittest.TestCase):
    def test_plain_output_contains_counts(self) -> None:
        result = {
            "schema": REGISTRY_SCHEMA,
            "written_at": "2026-11-20T00:00:00+00:00",
            "ttl_s": REGISTRY_TTL_S,
            "dry_run": False,
            "counts": {"input": 10, "total": 8, "answered": 5, "tool_ok": 3, "rate_limited": 1},
            "by_provider": {"kilo": {"total": 5, "answered": 3, "tool_ok": 2, "rate_limited": 0}},
            "entries": [],
        }
        text = render_refresh_plain(result)
        self.assertIn("answered", text)
        self.assertIn("tool_ok", text)
        self.assertIn("kilo", text)

    def test_dry_run_label(self) -> None:
        result = {
            "schema": REGISTRY_SCHEMA,
            "written_at": "2026-11-20T00:00:00+00:00",
            "ttl_s": REGISTRY_TTL_S,
            "dry_run": True,
            "counts": {"input": 2, "total": 2, "answered": 0, "tool_ok": 0, "rate_limited": 0},
            "by_provider": {},
            "entries": [
                {"route_key": "p/m1", "capacity_class": "free", "capacity_fact": "plan_free"},
                {
                    "route_key": "p/m2",
                    "capacity_class": "subscription",
                    "capacity_fact": "plan_paid",
                },
            ],
        }
        text = render_refresh_plain(result)
        self.assertIn("dry-run", text)
        self.assertIn("p/m1", text)


# ---------------------------------------------------------------------------
# run_refresh_action — dry_run with no sources
# ---------------------------------------------------------------------------


class RunRefreshActionTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_action_dry_run_no_sources(self) -> None:
        """With no sources available, dry-run should return empty without crashing."""
        with tempfile.TemporaryDirectory() as tmp:
            result = run_refresh_action(dry_run=True, env=_env(tmp))
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("dry_run"), True)
            self.assertIn("counts", result)

    def test_action_uses_sources_cache_when_available(self) -> None:
        """If sources-cache.json exists, routes should be loaded from it."""
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            discovery_dir = Path(tmp) / "discovery"
            discovery_dir.mkdir(parents=True)
            sources = [
                {
                    "capacity_class": "free",
                    "capacity_fact": "plan_free",
                    "route": {
                        "provider": "kilo",
                        "model": "kilo/model-a",
                        "base_url": "http://localhost:9999/v1",
                        "auth": "none",
                        "max_parallel": 1,
                    },
                }
            ]
            (discovery_dir / "sources-cache.json").write_text(
                _json.dumps(sources), encoding="utf-8"
            )
            result = run_refresh_action(dry_run=True, env=env)
            self.assertEqual(result["counts"]["input"], 1)
            self.assertEqual(result["counts"]["total"], 1)

    def test_action_skips_priced_routes_in_sources_cache(self) -> None:
        """Routes with capacity_class != free/subscription must be ignored."""
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            discovery_dir = Path(tmp) / "discovery"
            discovery_dir.mkdir(parents=True)
            sources = [
                {
                    "capacity_class": "priced",
                    "capacity_fact": "priced",
                    "route": {
                        "provider": "openai",
                        "model": "gpt-4o",
                        "base_url": "http://localhost:9999/v1",
                        "auth": "none",
                    },
                },
                {
                    "capacity_class": "unknown",
                    "capacity_fact": "unknown",
                    "route": {
                        "provider": "mystery",
                        "model": "mystery/model",
                        "base_url": "http://localhost:9999/v1",
                        "auth": "none",
                    },
                },
            ]
            (discovery_dir / "sources-cache.json").write_text(
                _json.dumps(sources), encoding="utf-8"
            )
            result = run_refresh_action(dry_run=True, env=env)
            # Neither priced nor unknown routes should be included
            self.assertEqual(result["counts"]["input"], 0)


if __name__ == "__main__":
    unittest.main()
