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
            tool_skip_reason=None,
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
            tool_skip_reason=None,
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
                self.assertIsNone(
                    entry.tool_ok, "dry-run entries are not probed — tool_ok must be None"
                )
                self.assertEqual(entry.tool_skip_reason, "not_answered")

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
        self.assertIsNone(reg.entries[0].tool_ok, "skipped tool probe must be None, not False")
        self.assertEqual(reg.entries[0].tool_skip_reason, "wall_budget_exhausted")
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


# ---------------------------------------------------------------------------
# Tri-state tool_ok: not_probed vs. measured failure vs. measured ok
# ---------------------------------------------------------------------------


class ToolOutcomeTriStateTest(unittest.TestCase):
    """tool_ok must be True/False/None — skipped routes must never read as failures."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_measured_tool_failure_has_tool_ok_false(self) -> None:
        """A route that answers liveness but fails the tool call → tool_ok=False."""
        route = _route()
        no_tool_body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "text only"}}]}
        ).encode()

        def _t(url: str, h: dict, p: bytes, timeout: float):
            return 200, {}, _ok_body() if b'"tools"' not in p else no_tool_body

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh(
                [(route, "free", "plan_free")], context_tokens=0, wall_budget_s=300.0, env=_env(tmp)
            )

        self.assertEqual(len(reg.entries), 1)
        entry = reg.entries[0]
        self.assertTrue(entry.answered)
        self.assertIs(entry.tool_ok, False, "measured failure must be False, not None")
        self.assertIsNone(entry.tool_skip_reason, "measured failure carries no skip reason")

    def test_not_probed_does_not_count_as_tool_failure_in_summary(self) -> None:
        """A not_probed route must not inflate the 'tool_ok' count or look like a failure."""
        route = _route()

        def _t(url: str, h: dict, p: bytes, timeout: float):
            if b'"tools"' in p:
                raise AssertionError("tool probe must not run when budget is exhausted")
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh(
                [(route, "free", "plan_free")], context_tokens=0, wall_budget_s=-1.0, env=_env(tmp)
            )

        entry = reg.entries[0]
        self.assertIsNone(entry.tool_ok)
        # tool_ok=None must not count as ok *or* as a measured failure
        ok_count = sum(1 for e in reg.entries if e.tool_ok is True)
        fail_count = sum(1 for e in reg.entries if e.tool_ok is False)
        self.assertEqual(ok_count, 0, "not_probed must not count as ok")
        self.assertEqual(fail_count, 0, "not_probed must not count as a measured failure")

    def test_old_registry_missing_tool_ok_loads_as_not_probed(self) -> None:
        """An old registry entry without tool_ok must load as None (not False)."""
        raw = {
            "schema": REGISTRY_SCHEMA,
            "written_at": "2026-11-20T00:00:00+00:00",
            "ttl_s": REGISTRY_TTL_S,
            "entries": [
                {
                    "route_key": "p/m",
                    "capacity_class": "free",
                    "capacity_fact": "plan_free",
                    "base_url": "http://h/v1",
                    "auth": "none",
                    "upstream": None,
                    "answered": True,
                    # tool_ok intentionally absent (old file format)
                    "served_model": None,
                    "latency_ms": None,
                    "retry_after_s": None,
                    "breaker_state": "closed",
                    "checked_at": "2026-11-20T00:00:00+00:00",
                }
            ],
        }
        reg = DiscoveryRegistry.from_dict(raw)
        self.assertEqual(len(reg.entries), 1)
        self.assertIsNone(
            reg.entries[0].tool_ok,
            "old entry without tool_ok must load as None (not_probed), never as False",
        )

    def test_null_tool_ok_in_registry_file_loads_as_not_probed(self) -> None:
        """An entry with explicit tool_ok=null must also load as None."""
        raw = {
            "schema": REGISTRY_SCHEMA,
            "written_at": "2026-11-20T00:00:00+00:00",
            "ttl_s": REGISTRY_TTL_S,
            "entries": [
                {
                    "route_key": "p/m",
                    "capacity_class": "free",
                    "capacity_fact": "plan_free",
                    "base_url": "http://h/v1",
                    "auth": "none",
                    "upstream": None,
                    "answered": True,
                    "tool_ok": None,
                    "tool_skip_reason": "wall_budget_exhausted",
                    "served_model": None,
                    "latency_ms": None,
                    "retry_after_s": None,
                    "breaker_state": "closed",
                    "checked_at": "2026-11-20T00:00:00+00:00",
                }
            ],
        }
        reg = DiscoveryRegistry.from_dict(raw)
        self.assertIsNone(reg.entries[0].tool_ok)
        self.assertEqual(reg.entries[0].tool_skip_reason, "wall_budget_exhausted")


# ---------------------------------------------------------------------------
# Per-route deadline check inside tool_probe_all
# ---------------------------------------------------------------------------


class PerRouteDeadlineTest(unittest.TestCase):
    """Deadline must be checked before each route in tool_probe_all."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_per_route_deadline_crossing_marks_remaining_not_probed(self) -> None:
        """When deadline crosses mid-stage, later routes get not_probed, not failures."""
        import urllib.parse
        from unittest.mock import patch

        from verdict.discovery.model import UP as _UP
        from verdict.discovery.model import ProbeResult
        from verdict.discovery.toolprobe import TOOL_NOT_PROBED, TOOL_OK, tool_probe_all

        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")

        probed_hosts: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float):
            hostname = urllib.parse.urlsplit(url).hostname or ""
            probed_hosts.append(hostname)
            return 200, {}, _tool_ok_body()

        _set_transport(_t)

        # With concurrency=1 routes are dispatched serially: a then b.
        # Fake clock: before-deadline for route_a's check, after for route_b's check.
        # tool_probe also calls time.monotonic() once for latency; account for that.
        deadline = 1000.0
        call_count = 0

        def fake_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            # Calls 1,2 → route_a's deadline check + latency start (before deadline)
            # Calls 3+ → route_b's deadline check (after deadline)
            return 999.0 if call_count <= 2 else 1001.0

        results = [ProbeResult(route=route_a, status=_UP), ProbeResult(route=route_b, status=_UP)]

        with patch("verdict.discovery.toolprobe.time.monotonic", fake_monotonic):
            out = tool_probe_all(results, timeout=5.0, concurrency=1, per_host=1, deadline=deadline)

        # route_a was probed (deadline not yet reached), route_b was not
        self.assertIn(route_a.key, out)
        self.assertIn(route_b.key, out)
        self.assertEqual(out[route_a.key][0], TOOL_OK, "route_a should have been probed")
        self.assertEqual(out[route_b.key][0], TOOL_NOT_PROBED, "route_b deadline should fire")
        self.assertEqual(out[route_b.key][1], "wall_budget_exhausted")
        self.assertIn("a.example.com", probed_hosts, "route_a must be probed")
        self.assertNotIn("b.example.com", probed_hosts, "route_b must not be probed")

    def test_registry_records_not_probed_from_tool_probe_all_result(self) -> None:
        """run_refresh stores tool_ok=None/wall_budget_exhausted from TOOL_NOT_PROBED results.

        Uses the pre-stage exhausted-budget path (wall_budget_s=-1) so that
        tool_probe_all returns TOOL_NOT_PROBED for every UP route.  This verifies
        that _make_entry maps TOOL_NOT_PROBED → (tool_ok=None, tool_skip_reason=…).
        The per-route deadline check in tool_probe_all itself is tested by
        test_per_route_deadline_crossing_marks_remaining_not_probed above.
        """
        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")

        def _t(url: str, h: dict, p: bytes, timeout: float):
            if b'"tools"' in p:
                raise AssertionError("tool probe must not run when budget exhausted")
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=-1.0,  # deadline already in the past
                env=_env(tmp),
            )

        entries_by_key = {e.route_key: e for e in reg.entries}
        for key in (route_a.key, route_b.key):
            entry = entries_by_key[key]
            self.assertTrue(entry.answered, f"{key}: liveness probe should still run")
            self.assertIsNone(entry.tool_ok, f"{key}: not_probed must be None not False")
            self.assertEqual(entry.tool_skip_reason, "wall_budget_exhausted")


# ---------------------------------------------------------------------------
# Per-pool breaker: 429 on first route stops remaining routes in that pool
# ---------------------------------------------------------------------------


class PoolBreakerCascadeTest(unittest.TestCase):
    """A 429 on route[0] must stop routes[1] and [2] without probing them."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_pool_429_on_first_route_skips_rest_with_pool_breaker_open(self) -> None:
        """Pool with 3 routes: first returns 429, other two must NOT be probed."""
        import urllib.parse

        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")
        route_c = Route(provider="kilo", model="m-c", base_url="http://c.example.com/v1")

        probed_urls: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float) -> tuple:
            hostname = urllib.parse.urlsplit(url).hostname or ""
            probed_urls.append(hostname)
            if hostname == "a.example.com":
                return 429, {"retry-after": "60"}, _429_body()
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh(
                [
                    (route_a, "free", "plan_free"),
                    (route_b, "free", "plan_free"),
                    (route_c, "free", "plan_free"),
                ],
                context_tokens=0,
                wall_budget_s=300.0,
                env=_env(tmp),
            )

        # Exactly one HTTP request must be made (to route_a); b and c are not probed
        self.assertEqual(
            probed_urls, ["a.example.com"], "only the first route in pool must be probed"
        )
        self.assertEqual(len(reg.entries), 3)

        entries_by_key = {e.route_key: e for e in reg.entries}
        # route_a: probed, got 429 → answered=False (not UP), tool_ok=None
        entry_a = entries_by_key[route_a.key]
        self.assertFalse(entry_a.answered)
        self.assertIsNone(entry_a.tool_ok)

        # route_b and route_c: not probed due to pool breaker
        for key, label in [(route_b.key, "route_b"), (route_c.key, "route_c")]:
            entry = entries_by_key[key]
            self.assertFalse(entry.answered, f"{label}: must not be answered")
            self.assertIsNone(entry.tool_ok, f"{label}: must be not_probed (None)")
            self.assertEqual(
                entry.tool_skip_reason,
                "pool_breaker_open",
                f"{label}: skip reason must be pool_breaker_open",
            )

    def test_pool_429_during_tool_stage_skips_remaining_tool_probes(self) -> None:
        """In the tool stage, a 429 on the first tool probe stops the rest in that pool."""
        import urllib.parse

        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")

        tool_probe_urls: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float) -> tuple:
            hostname = urllib.parse.urlsplit(url).hostname or ""
            if b'"tools"' in p:
                tool_probe_urls.append(hostname)
                # First tool probe returns 429 (tool_error with 429 detail)
                if hostname == "a.example.com":
                    return 429, {}, _429_body()
                return 200, {}, _tool_ok_body()
            # Liveness: both answer OK
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=300.0,
                env=_env(tmp),
            )

        # route_a tool probe was attempted and returned 429; route_b must be skipped
        self.assertIn("a.example.com", tool_probe_urls, "route_a must be tool-probed")
        self.assertNotIn("b.example.com", tool_probe_urls, "route_b must not be tool-probed")

        entries_by_key = {e.route_key: e for e in reg.entries}
        entry_a = entries_by_key[route_a.key]
        entry_b = entries_by_key[route_b.key]

        # route_a answered liveness but tool probe returned 429 → tool_ok=False (measured)
        self.assertTrue(entry_a.answered)
        self.assertIs(entry_a.tool_ok, False, "tool 429 is a measured failure")

        # route_b answered liveness but tool probe was skipped due to pool breaker
        self.assertTrue(entry_b.answered)
        self.assertIsNone(entry_b.tool_ok, "route_b tool probe was skipped by pool breaker")
        self.assertEqual(entry_b.tool_skip_reason, "pool_breaker_open")


if __name__ == "__main__":
    unittest.main()
