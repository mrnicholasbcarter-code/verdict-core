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
        """dry-run with no catalog returns dry_run_requires_catalog and empty counts."""
        with tempfile.TemporaryDirectory() as tmp:
            result = run_refresh_action(dry_run=True, env=_env(tmp))
            self.assertIsInstance(result, dict)
            self.assertEqual(result.get("dry_run"), True)
            self.assertIn("counts", result)
            self.assertEqual(result.get("source"), "dry_run_requires_catalog")


# ---------------------------------------------------------------------------
# Fix 1: dry-run must make zero network calls
# ---------------------------------------------------------------------------


class DryRunNoNetworkTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_dry_run_makes_zero_network_calls(self) -> None:
        """dry_run=True must never make any network call; source is dry_run_requires_catalog."""

        network_calls: list[str] = []

        def _explode(url: str, h: dict, p: bytes, timeout: float):
            network_calls.append(url)
            raise RuntimeError(f"network call not allowed in dry-run: {url}")

        _set_transport(_explode)

        with tempfile.TemporaryDirectory() as tmp:
            result = run_refresh_action(dry_run=True, env=_env(tmp))

        self.assertEqual(network_calls, [], "dry_run must make zero network calls")
        self.assertIsInstance(result, dict)
        self.assertEqual(
            result.get("source"),
            "dry_run_requires_catalog",
            "dry_run must report dry_run_requires_catalog",
        )


# ---------------------------------------------------------------------------
# Fix 2: wall_budget_s enforced for tool-probe stage
# ---------------------------------------------------------------------------


class WallBudgetEnforcementTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_budget_exhausted_before_start_skips_liveness_and_tool(self) -> None:
        """When wall_budget_s is already spent, neither liveness nor tool probes run."""
        route = _route()
        any_probe_calls: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float):
            any_probe_calls.append(url)
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            # wall_budget_s=-1.0 means the deadline is already in the past
            reg = run_refresh(
                [(route, "free", "plan_free")], context_tokens=0, wall_budget_s=-1.0, env=_env(tmp)
            )

        # No HTTP call should have been made at all
        self.assertEqual(any_probe_calls, [], "no probe must run when budget is already spent")
        self.assertEqual(len(reg.entries), 1)
        entry = reg.entries[0]
        self.assertFalse(entry.answered, "route must not be answered (not probed)")
        self.assertEqual(
            entry.probe_skip_reason,
            "wall_budget_exhausted",
            "liveness probe must be skipped with wall_budget_exhausted",
        )
        self.assertIsNone(entry.tool_ok, "tool probe must be not_probed (None)")

    def test_tool_probe_skipped_when_budget_exhausted_after_liveness(self) -> None:
        """When the budget expires between liveness and tool stage, tool probes must not run."""
        import unittest.mock as _mock

        import verdict.discovery.registry as _reg_mod

        route = _route()
        clock: list[float] = [0.0]
        _real_monotonic = __import__("time").monotonic
        tool_probe_calls: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float):
            if b'"tools"' in p:
                tool_probe_calls.append(url)
                return 200, {}, _tool_ok_body()
            # Liveness probe — advance clock past deadline on return
            clock[0] = 1e12
            return 200, {}, _ok_body()

        _set_transport(_t)

        def fake_monotonic() -> float:
            return _real_monotonic() if clock[0] == 0.0 else clock[0]

        with (
            _mock.patch.object(_reg_mod.time, "monotonic", fake_monotonic),
            tempfile.TemporaryDirectory() as tmp,
        ):
            reg = run_refresh(
                [(route, "free", "plan_free")], context_tokens=0, wall_budget_s=300.0, env=_env(tmp)
            )

        # Liveness ran; tool probe must have been skipped by the pre-stage budget check
        self.assertEqual(tool_probe_calls, [], "tool probe must not be called when budget is spent")
        self.assertEqual(len(reg.entries), 1)
        entry = reg.entries[0]
        self.assertTrue(entry.answered, "liveness probe must have answered")
        self.assertIsNone(entry.tool_ok, "skipped tool probe must be None, not False")
        self.assertEqual(entry.tool_skip_reason, "wall_budget_exhausted")


# ---------------------------------------------------------------------------
# load_registry: non-UTF-8 file must return None
# ---------------------------------------------------------------------------


class RegistryInvalidUtf8Test(unittest.TestCase):
    def test_load_registry_invalid_utf8_returns_none(self) -> None:
        """A registry file containing non-UTF-8 bytes must return None, not raise."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            path = registry_path(env)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write bytes that are not valid UTF-8 (lone continuation byte)
            path.write_bytes(b"\xff\xfe{invalid utf-8}")
            result = load_registry(env)
            self.assertIsNone(result, "non-UTF-8 registry file must return None, not raise")


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
                "source": "dry_run_requires_catalog",
                "counts": {"input": 0, "total": 0, "answered": 0, "tool_ok": 0, "rate_limited": 0},
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

    def test_old_registry_missing_probe_skip_reason_loads_as_none(self) -> None:
        """An old registry entry without probe_skip_reason must load as None."""
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
                    "answered": False,
                    "tool_ok": None,
                    "tool_skip_reason": "not_answered",
                    # probe_skip_reason intentionally absent (old file format)
                    "served_model": None,
                    "latency_ms": None,
                    "retry_after_s": None,
                    "breaker_state": "open",
                    "checked_at": "2026-11-20T00:00:00+00:00",
                }
            ],
        }
        reg = DiscoveryRegistry.from_dict(raw)
        self.assertIsNone(
            reg.entries[0].probe_skip_reason,
            "old entry without probe_skip_reason must load as None",
        )


# ---------------------------------------------------------------------------
# Per-route deadline check in the serial tool-probe stage
# ---------------------------------------------------------------------------


class PerRouteDeadlineTest(unittest.TestCase):
    """Deadline is checked before each route in the serial tool-probe worker."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_per_route_deadline_crossing_marks_remaining_not_probed(self) -> None:
        """When deadline crosses mid tool-stage, later routes get not_probed, not failures."""
        import unittest.mock as _mock
        import urllib.parse

        import verdict.discovery.registry as _reg_mod

        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")

        tool_probed: list[str] = []

        # The transport advances a mutable clock past the deadline when route_a's
        # tool probe fires, so route_b's inline deadline check sees it as expired.
        # Routes are in the same provider pool so they're probed serially.
        clock: list[float] = [0.0]
        _real_monotonic = __import__("time").monotonic

        def _t(url: str, h: dict, p: bytes, timeout: float):
            hostname = urllib.parse.urlsplit(url).hostname or ""
            if b'"tools"' in p:
                tool_probed.append(hostname)
                clock[0] = 1e12  # push clock past any deadline (larger than any real monotonic)
                return 200, {}, _tool_ok_body()
            return 200, {}, _ok_body()

        _set_transport(_t)

        def fake_monotonic() -> float:
            return _real_monotonic() if clock[0] == 0.0 else clock[0]

        with (
            _mock.patch.object(_reg_mod.time, "monotonic", fake_monotonic),
            tempfile.TemporaryDirectory() as tmp,
        ):
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=300.0,
                env=_env(tmp),
            )

        # route_a was tool-probed, route_b was not
        self.assertIn("a.example.com", tool_probed, "route_a must be tool-probed")
        self.assertNotIn("b.example.com", tool_probed, "route_b must not be tool-probed")

        entries_by_key = {e.route_key: e for e in reg.entries}
        entry_a = entries_by_key[route_a.key]
        entry_b = entries_by_key[route_b.key]
        self.assertTrue(entry_a.answered, "route_a liveness probe answered")
        self.assertIs(entry_a.tool_ok, True, "route_a tool probe succeeded")
        self.assertTrue(entry_b.answered, "route_b liveness probe answered")
        self.assertIsNone(entry_b.tool_ok, "route_b tool probe was skipped by deadline")
        self.assertEqual(entry_b.tool_skip_reason, "wall_budget_exhausted")

    def test_registry_records_not_probed_when_budget_exhausted_before_tool_stage(self) -> None:
        """run_refresh stores tool_ok=None/wall_budget_exhausted when deadline passes
        after all liveness probes complete but before the tool stage pre-check.

        Verifies that _make_entry maps TOOL_NOT_PROBED → (tool_ok=None, tool_skip_reason=…).
        Uses two routes in different pools so liveness runs concurrently; the
        clock is advanced past the deadline by the first liveness return so that
        the remaining_budget <= 0 path fires before the tool stage begins.
        The per-route deadline check inside the serial worker is tested separately by
        test_per_route_deadline_crossing_marks_remaining_not_probed.
        """
        import unittest.mock as _mock

        import verdict.discovery.registry as _reg_mod

        # Different pools (different upstream) → concurrent liveness
        route_a = Route(
            provider="p", model="m-a", base_url="http://a.example.com/v1", upstream="pool-a"
        )
        route_b = Route(
            provider="p", model="m-b", base_url="http://b.example.com/v1", upstream="pool-b"
        )

        clock: list[float] = [0.0]
        _real_monotonic = __import__("time").monotonic

        def _t(url: str, h: dict, p: bytes, timeout: float):
            if b'"tools"' in p:
                raise AssertionError("tool probe must not run when budget exhausted")
            # Advance clock past deadline on first liveness return
            clock[0] = 1e12
            return 200, {}, _ok_body()

        _set_transport(_t)

        def fake_monotonic() -> float:
            return _real_monotonic() if clock[0] == 0.0 else clock[0]

        with (
            _mock.patch.object(_reg_mod.time, "monotonic", fake_monotonic),
            tempfile.TemporaryDirectory() as tmp,
        ):
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=300.0,
                env=_env(tmp),
            )

        entries_by_key = {e.route_key: e for e in reg.entries}
        # Both routes should have been attempted at liveness (answered=True or False)
        # but tool probes must be skipped due to the pre-stage budget check.
        for key in (route_a.key, route_b.key):
            entry = entries_by_key[key]
            # Route may or may not have been probed at liveness depending on clock,
            # but tool_ok must be None with the wall_budget_exhausted reason.
            self.assertIsNone(entry.tool_ok, f"{key}: not_probed must be None not False")
            self.assertEqual(
                entry.tool_skip_reason,
                "wall_budget_exhausted",
                f"{key}: tool_skip_reason must be wall_budget_exhausted",
            )


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

        # route_a: was probed (got 429), so probe_skip_reason must be None
        self.assertIsNone(
            entry_a.probe_skip_reason, "route_a was probed; no probe_skip_reason expected"
        )

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
            self.assertEqual(
                entry.probe_skip_reason,
                "pool_breaker_open",
                f"{label}: probe_skip_reason must be pool_breaker_open (not probed at liveness stage)",
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


# ---------------------------------------------------------------------------
# Typed RATE_LIMITED signal from tool_probe (issue #3)
# ---------------------------------------------------------------------------


class ToolProbeTypedRateLimitedTest(unittest.TestCase):
    """classify() must return RATE_LIMITED (not TOOL_ERROR) for HTTP 429."""

    def test_classify_429_returns_rate_limited(self) -> None:
        """classify() with status_code=429 must return RATE_LIMITED as the first element."""
        from verdict.discovery.model import RATE_LIMITED
        from verdict.discovery.toolprobe import TOOL_ERROR, classify

        status, detail = classify(429, b'{"error":"rate limited"}')
        self.assertEqual(status, RATE_LIMITED, "HTTP 429 must return RATE_LIMITED, not TOOL_ERROR")
        self.assertNotEqual(status, TOOL_ERROR)
        self.assertIn("429", (detail or ""))

    def test_classify_other_error_returns_tool_error(self) -> None:
        """classify() with a non-429 error code must still return TOOL_ERROR."""
        from verdict.discovery.toolprobe import TOOL_ERROR, classify

        for code in (401, 403, 500, 503):
            status, _ = classify(code, b"error")
            self.assertEqual(status, TOOL_ERROR, f"HTTP {code} must return TOOL_ERROR")

    def test_pool_429_during_tool_stage_uses_typed_check(self) -> None:
        """Tool-stage breaker fires on RATE_LIMITED from classify(), not on a detail string."""
        import urllib.parse

        route_a = Route(provider="x", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="x", model="m-b", base_url="http://b.example.com/v1")

        tool_probe_urls: list[str] = []

        def _t(url: str, h: dict, p: bytes, timeout: float) -> tuple:
            hostname = urllib.parse.urlsplit(url).hostname or ""
            if b'"tools"' in p:
                tool_probe_urls.append(hostname)
                if hostname == "a.example.com":
                    # Return a 429 body whose string representation does NOT
                    # contain "429" so a string-scan check would miss it.
                    return 429, {}, b'{"error":"quota"}'
                return 200, {}, _tool_ok_body()
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=300.0,
                env=_env(tmp),
            )

        # Typed check must have fired: route_b must be skipped despite the
        # detail body not containing "429" as text.
        self.assertIn("a.example.com", tool_probe_urls)
        self.assertNotIn("b.example.com", tool_probe_urls)
        entries_by_key = {e.route_key: e for e in reg.entries}
        # route_a: 429 is a measured failure
        self.assertIs(entries_by_key[route_a.key].tool_ok, False)
        # route_b: skipped by pool breaker
        self.assertIsNone(entries_by_key[route_b.key].tool_ok)
        self.assertEqual(entries_by_key[route_b.key].tool_skip_reason, "pool_breaker_open")


# ---------------------------------------------------------------------------
# Liveness-stage wall budget (issue #4)
# ---------------------------------------------------------------------------


class LivenessWallBudgetTest(unittest.TestCase):
    """When the wall budget runs out during the liveness stage, later routes
    in the same pool are marked wall_budget_exhausted instead of being probed
    with an inadequate timeout."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_liveness_budget_exhausted_mid_pool_marks_remaining_routes(self) -> None:
        """After route_a's liveness probe the clock advances past the deadline;
        route_b must be marked probe_skip_reason='wall_budget_exhausted'."""
        import unittest.mock as _mock
        import urllib.parse

        import verdict.discovery.registry as _reg_mod

        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")

        probed_hosts: list[str] = []
        clock: list[float] = [0.0]
        _real_monotonic = __import__("time").monotonic

        def _t(url: str, h: dict, p: bytes, timeout: float) -> tuple:
            hostname = urllib.parse.urlsplit(url).hostname or ""
            probed_hosts.append(hostname)
            # Push the clock past any real deadline when route_a is probed.
            clock[0] = 1e12
            return 200, {}, _ok_body()

        _set_transport(_t)

        def fake_monotonic() -> float:
            return _real_monotonic() if clock[0] == 0.0 else clock[0]

        with (
            _mock.patch.object(_reg_mod.time, "monotonic", fake_monotonic),
            tempfile.TemporaryDirectory() as tmp,
        ):
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=300.0,
                env=_env(tmp),
            )

        # Only route_a must have been probed; route_b sees remaining < _MIN_PROBE_BUDGET_S
        self.assertIn("a.example.com", probed_hosts)
        self.assertNotIn("b.example.com", probed_hosts)

        entries_by_key = {e.route_key: e for e in reg.entries}
        entry_a = entries_by_key[route_a.key]
        entry_b = entries_by_key[route_b.key]

        self.assertTrue(entry_a.answered, "route_a liveness must have answered")
        self.assertFalse(entry_b.answered, "route_b must not be answered (not probed)")
        self.assertEqual(
            entry_b.probe_skip_reason,
            "wall_budget_exhausted",
            "route_b must be skipped with wall_budget_exhausted, not pool_breaker_open",
        )


# ---------------------------------------------------------------------------
# Fix 5: Catalog unavailable must not overwrite prior registry
# ---------------------------------------------------------------------------


class CatalogUnavailableTest(unittest.TestCase):
    """run_refresh_action must distinguish catalog failures from an empty catalog.

    On failure (missing key, network error): keep the prior registry unchanged,
    return source="catalog_unavailable".  Only write when the catalog is
    genuinely reachable, even if it returns no probeable routes.
    """

    def tearDown(self) -> None:
        _set_transport(None)

    def _make_prior_registry(self, env: dict[str, str]) -> bytes:
        """Write a minimal prior registry and return its raw bytes."""
        prior = DiscoveryRegistry(
            schema=REGISTRY_SCHEMA,
            written_at="2026-11-19T12:00:00+00:00",
            ttl_s=REGISTRY_TTL_S,
            entries=[],
        )
        save_registry(prior, env)
        return registry_path(env).read_bytes()

    def test_missing_key_preserves_prior_registry(self) -> None:
        """No API key: prior registry must be byte-identical after the call."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            prior_bytes = self._make_prior_registry(env)
            run_refresh_action(force=True, env=env)
            after_bytes = registry_path(env).read_bytes()
            self.assertEqual(
                after_bytes, prior_bytes, "registry must not be overwritten when API key is absent"
            )

    def test_missing_key_reports_catalog_unavailable(self) -> None:
        """No API key: source must be 'catalog_unavailable'."""
        with tempfile.TemporaryDirectory() as tmp:
            result = run_refresh_action(force=True, env=_env(tmp))
            self.assertEqual(result.get("source"), "catalog_unavailable")

    def test_missing_key_no_prior_registry_does_not_write(self) -> None:
        """No API key, no prior registry: nothing must be written to disk."""
        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            run_refresh_action(force=True, env=env)
            self.assertFalse(
                registry_path(env).exists(),
                "no registry file must be written when catalog is unavailable",
            )

    def test_fetch_failure_preserves_prior_registry(self) -> None:
        """When catalog fetch raises, prior registry must be preserved byte-identical."""
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            env = {**_env(tmp), "OMNIROUTE_API_KEY": "test-key"}
            prior_bytes = self._make_prior_registry(env)
            with patch(
                "verdict.omniroute_evidence.fetch_omniroute_evidence",
                side_effect=RuntimeError("network down"),
            ):
                result = run_refresh_action(force=True, env=env)
            after_bytes = registry_path(env).read_bytes()
            self.assertEqual(
                after_bytes, prior_bytes, "prior registry must be preserved when fetch raises"
            )
            self.assertEqual(result.get("source"), "catalog_unavailable")

    def test_confirmed_empty_catalog_writes_empty_registry(self) -> None:
        """A confirmed-empty catalog (available=True, routes=[]) must write an empty registry."""
        from unittest.mock import patch

        from verdict.discovery.refresh import CatalogResult

        with tempfile.TemporaryDirectory() as tmp:
            env = _env(tmp)
            with patch("verdict.discovery.refresh.load_probeable_routes") as mock_lpr:
                mock_lpr.return_value = CatalogResult(routes=[], available=True, reason=None)
                run_refresh_action(force=True, env=env)
            self.assertTrue(
                registry_path(env).exists(),
                "empty registry must be written for a confirmed-empty catalog",
            )
            loaded = load_registry(env)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(len(loaded.entries), 0)


# ---------------------------------------------------------------------------
# Fix 6: --force flag reaches run_refresh_action via CLI parser + dispatch
# ---------------------------------------------------------------------------


class ForceArgForwardingTest(unittest.TestCase):
    def test_force_flag_accepted_by_parser(self) -> None:
        """'verdict discovery refresh --force' must parse without error."""
        import argparse

        from verdict.commands.parsers_discovery import register

        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        register(sub)
        args = p.parse_args(["discovery", "refresh", "--force"])
        self.assertTrue(
            getattr(args, "force", False), "--force must set force=True on the parsed namespace"
        )

    def test_dispatch_passes_force_to_run_refresh_action(self) -> None:
        """dispatch must forward force=True when --force is set."""
        import argparse
        from unittest.mock import patch

        from verdict.commands.parsers_discovery import register

        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        register(sub)
        args = p.parse_args(["discovery", "refresh", "--force"])

        with patch("verdict.discovery.refresh.run_refresh_action") as mock_refresh:
            mock_refresh.return_value = {
                "schema": REGISTRY_SCHEMA,
                "written_at": "2026-11-20T00:00:00+00:00",
                "ttl_s": REGISTRY_TTL_S,
                "dry_run": False,
                "source": "omniroute",
                "counts": {"input": 0, "total": 0, "answered": 0, "tool_ok": 0, "rate_limited": 0},
                "by_provider": {},
                "entries": [],
            }
            from verdict.commands.dispatch import dispatch

            dispatch(p, args)

        call_kwargs = mock_refresh.call_args.kwargs
        self.assertIs(call_kwargs.get("force"), True, "force=True must be forwarded by dispatch")


# ---------------------------------------------------------------------------
# Fix 7: rate_limited counts only measured 429 outcomes
# ---------------------------------------------------------------------------


class RateLimitedCountTest(unittest.TestCase):
    """rate_limited in the summary must count only the route that got the measured 429,
    not the sibling routes that were skipped because the pool breaker opened."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_rate_limited_counts_only_measured_429_not_skipped_routes(self) -> None:
        """Pool with 3 routes: first gets 429 at liveness; rate_limited must be 1."""
        import urllib.parse

        from verdict.discovery.refresh import _registry_to_summary

        route_a = Route(provider="kilo", model="m-a", base_url="http://a.example.com/v1")
        route_b = Route(provider="kilo", model="m-b", base_url="http://b.example.com/v1")
        route_c = Route(provider="kilo", model="m-c", base_url="http://c.example.com/v1")

        def _t(url: str, h: dict, p: bytes, timeout: float) -> tuple:
            hostname = urllib.parse.urlsplit(url).hostname or ""
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

        # All three entries must have breaker_state == "open" (pool was opened)
        for e in reg.entries:
            self.assertEqual(e.breaker_state, BREAKER_OPEN)

        summary = _registry_to_summary(reg, dry_run=False, total_input=3)
        counts = summary["counts"]
        self.assertEqual(
            counts["rate_limited"],
            1,
            "only the route that received the measured 429 must count as rate_limited",
        )
        self.assertEqual(
            counts.get("not_probed_breaker", 0),
            2,
            "the two skipped routes must count as not_probed_breaker, not rate_limited",
        )


if __name__ == "__main__":
    unittest.main()
