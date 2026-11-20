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

    def test_duplicate_route_key_probed_only_once(self) -> None:
        """Two routes sharing a key must not share each other's proof.

        The dedup must happen before probing so the second route is never
        probed, and the registry must NOT show it as answered.
        """
        route_a = Route(
            provider="p", model="m", base_url="http://a.example.com/v1", auth="none", source="s1"
        )
        route_b = Route(
            provider="p", model="m", base_url="http://b.example.com/v1", auth="none", source="s2"
        )
        self.assertEqual(route_a.key, route_b.key)

        urls_called: list[str] = []

        import urllib.parse as _uparse

        def _t(url: str, h: dict, p: bytes, timeout: float):
            urls_called.append(url)
            # route_a answers OK; route_b (if ever called) would return 503
            if _uparse.urlsplit(url).hostname == "a.example.com":
                return 200, {}, _ok_body()
            return 503, {}, b"error"

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            registry = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                env=_env(tmp),
            )

        # Exactly one entry — the duplicate key was deduped before probing
        self.assertEqual(len(registry.entries), 1)
        # Only route_a's URL was called; route_b must never be probed
        self.assertTrue(
            any(_uparse.urlsplit(u).hostname == "a.example.com" for u in urls_called),
            "route_a should have been probed",
        )
        self.assertFalse(
            any(_uparse.urlsplit(u).hostname == "b.example.com" for u in urls_called),
            "route_b shares route_a's key and must not be probed separately",
        )

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

    def test_malformed_sources_cache_entries_counted(self) -> None:
        """Malformed cache entries must be counted and reported, not silently dropped."""
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            discovery_dir = Path(tmp) / "discovery"
            discovery_dir.mkdir(parents=True)
            sources = [
                # Valid free route
                {
                    "capacity_class": "free",
                    "capacity_fact": "plan_free",
                    "route": {
                        "provider": "p1",
                        "model": "m1",
                        "base_url": "http://localhost:9999/v1",
                        "auth": "none",
                    },
                },
                # Malformed: route is missing required "provider" key
                {
                    "capacity_class": "free",
                    "capacity_fact": "plan_free",
                    "route": {"model": "m2", "base_url": "http://localhost:9999/v1"},
                },
                # Malformed: route field is not a dict
                {"capacity_class": "free", "capacity_fact": "plan_free", "route": "not-a-dict"},
            ]
            (discovery_dir / "sources-cache.json").write_text(
                _json.dumps(sources), encoding="utf-8"
            )
            result = run_refresh_action(dry_run=True, env=env)
            # 1 valid route in entries, 2 malformed entries counted
            self.assertEqual(result["counts"]["input"], 1)
            self.assertIn(
                "malformed_cache_entries",
                result["counts"],
                "malformed_cache_entries must be reported in counts",
            )
            self.assertEqual(result["counts"]["malformed_cache_entries"], 2)

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


# ---------------------------------------------------------------------------
# Fix 1: dry-run must make zero network calls
# ---------------------------------------------------------------------------


class DryRunNoNetworkTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_dry_run_no_omniroute_fallback_when_cache_absent(self) -> None:
        """dry_run=True must never fall back to OmniRoute (or any network call)."""

        network_calls: list[str] = []

        def _explode(url: str, h: dict, p: bytes, timeout: float):
            network_calls.append(url)
            raise RuntimeError(f"network call not allowed in dry-run: {url}")

        _set_transport(_explode)

        with tempfile.TemporaryDirectory() as tmp:
            # No sources-cache.json — the only source is OmniRoute, which must be skipped.
            result = run_refresh_action(dry_run=True, env=_env(tmp))

        self.assertEqual(network_calls, [], "dry_run must make zero network calls")
        self.assertIsInstance(result, dict)
        # The result must name the reason (sources_cache_missing or similar)
        self.assertIn(
            result.get("source") or result.get("dry_run_source") or "",
            ("sources_cache_missing",),
            "dry_run with no cache must report sources_cache_missing",
        )

    def test_dry_run_with_cache_still_uses_it_without_omniroute(self) -> None:
        """dry_run=True with sources-cache.json must use the cache, not OmniRoute."""
        import json as _json

        network_calls: list[str] = []

        def _explode(url: str, h: dict, p: bytes, timeout: float):
            network_calls.append(url)
            raise RuntimeError(f"network call in dry-run: {url}")

        _set_transport(_explode)

        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            discovery_dir = Path(tmp) / "discovery"
            discovery_dir.mkdir(parents=True)
            sources = [
                {
                    "capacity_class": "free",
                    "capacity_fact": "plan_free",
                    "route": {
                        "provider": "p1",
                        "model": "m1",
                        "base_url": "http://localhost:9999/v1",
                        "auth": "none",
                    },
                }
            ]
            (discovery_dir / "sources-cache.json").write_text(
                _json.dumps(sources), encoding="utf-8"
            )
            result = run_refresh_action(dry_run=True, env=env)

        self.assertEqual(network_calls, [], "dry_run must make zero network calls even with cache")
        self.assertEqual(result["counts"]["input"], 1)


# ---------------------------------------------------------------------------
# Fix 2: wall_budget_s enforced for tool-probe stage
# ---------------------------------------------------------------------------


class WallBudgetEnforcementTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_tool_probe_skipped_when_budget_already_exhausted(self) -> None:
        """When wall_budget_s is already spent, tool probes must not run."""
        route = _route()
        tool_probe_calls: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float):
            if b'"tools"' in p:
                # Tool probe — must not be called when budget is exhausted
                tool_probe_calls.append(url)
                return 200, {}, _tool_ok_body()
            # Liveness probe — answers OK
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            # wall_budget_s=-1.0 means the deadline is already in the past
            reg = run_refresh(
                [(route, "free", "plan_free")], context_tokens=0, wall_budget_s=-1.0, env=_env(tmp)
            )

        # Liveness probe ran and answered, but tool probe must have been skipped
        self.assertEqual(len(reg.entries), 1)
        self.assertTrue(reg.entries[0].answered, "liveness probe should still be counted")
        self.assertFalse(reg.entries[0].tool_ok, "tool probe was skipped — not proved")
        self.assertEqual(tool_probe_calls, [], "tool probe must not be called when budget is spent")


# ---------------------------------------------------------------------------
# Fix 3: load_registry with naive written_at
# ---------------------------------------------------------------------------


class RegistryNaiveDatetimeTest(unittest.TestCase):
    def test_load_registry_naive_written_at_returns_none(self) -> None:
        """A naive (timezone-unaware) written_at must be treated as stale."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            # Write a registry with a naive timestamp (no timezone)
            reg = DiscoveryRegistry(
                schema=REGISTRY_SCHEMA,
                written_at="2026-11-20T00:00:00",  # naive — no +00:00 or Z
                ttl_s=REGISTRY_TTL_S,
                entries=[],
            )
            save_registry(reg, env)
            result = load_registry(env)
            self.assertIsNone(result, "naive written_at must be treated as stale (return None)")

    def test_load_registry_aware_written_at_still_works(self) -> None:
        """An aware written_at that is fresh must be loaded normally."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            reg = DiscoveryRegistry(
                schema=REGISTRY_SCHEMA,
                written_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ttl_s=REGISTRY_TTL_S,
                entries=[],
            )
            save_registry(reg, env)
            result = load_registry(env)
            self.assertIsNotNone(result, "fresh aware written_at should load successfully")


# ---------------------------------------------------------------------------
# Fix 4: _action_discovery_refresh parameter forwarding
# ---------------------------------------------------------------------------


class ActionDiscoveryRefreshTest(unittest.TestCase):
    def test_unknown_kwargs_raise_value_error(self) -> None:
        """_action_discovery_refresh must reject unknown parameters explicitly."""
        from verdict.actions.registry import _action_discovery_refresh

        with self.assertRaises(ValueError) as ctx:
            _action_discovery_refresh(unknown_param="bad")
        self.assertIn("unknown", str(ctx.exception).lower())

    def test_known_kwargs_forwarded_to_run_refresh_action(self) -> None:
        """context_tokens and wall_budget_s must be forwarded to run_refresh_action."""
        from unittest.mock import patch

        from verdict.actions.registry import _action_discovery_refresh

        with patch("verdict.discovery.refresh.run_refresh_action") as mock_refresh:
            mock_refresh.return_value = {
                "schema": REGISTRY_SCHEMA,
                "written_at": "2026-11-20T00:00:00+00:00",
                "ttl_s": REGISTRY_TTL_S,
                "dry_run": True,
                "source": "sources_cache_missing",
                "counts": {
                    "input": 0,
                    "total": 0,
                    "answered": 0,
                    "tool_ok": 0,
                    "rate_limited": 0,
                    "malformed_cache_entries": 0,
                },
                "by_provider": {},
                "entries": [],
            }
            _action_discovery_refresh(
                dry_run=True, context_tokens=1000, wall_budget_s=10.0, force=True
            )
        mock_refresh.assert_called_once_with(
            dry_run=True, context_tokens=1000, wall_budget_s=10.0, force=True
        )


if __name__ == "__main__":
    unittest.main()
