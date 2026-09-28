"""Tests for verdict.tracing — BOD-90 optional OTel integration.

Verifies:
- Off by default: no spans, opentelemetry never imported.
- On with in-memory exporter: span hierarchy and attributes.
- Attribute allowlist blocks secret-like keys and large strings.
- Suite passes without the tracing extra installed.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Any
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_tracing_module() -> Any:
    """Re-import verdict.tracing with a clean singleton."""
    for name in list(sys.modules):
        if name.startswith("verdict.tracing"):
            del sys.modules[name]
    return importlib.import_module("verdict.tracing")


# ---------------------------------------------------------------------------
# 1. Off by default — no spans, opentelemetry never imported
# ---------------------------------------------------------------------------


class TestTracingOff:
    """When VERDICT_TRACING is unset, tracing is a complete no-op."""

    def test_start_run_span_returns_noop(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VERDICT_TRACING"}
        with mock.patch.dict(os.environ, env, clear=True):
            mod = _fresh_tracing_module()
            span = mod.start_run_span("run-1", "goal-1", "/repo")
            assert isinstance(span, mod._NoOpSpan)
            # end() is safe
            span.end()

    def test_record_event_noop_when_off(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "VERDICT_TRACING"}
        with mock.patch.dict(os.environ, env, clear=True):
            mod = _fresh_tracing_module()
            noop = mod._NoOpSpan()
            # Should not raise
            mod.record_event(noop, "dispatch", "node-a", {"route_id": "kr/model"})

    def test_opentelemetry_not_imported_when_off(self) -> None:
        """Ensure the opentelemetry package is never imported when tracing is off."""
        env = {k: v for k, v in os.environ.items() if k != "VERDICT_TRACING"}
        # Remove any cached otel modules
        otel_mods = [m for m in sys.modules if m.startswith("opentelemetry")]
        saved = {m: sys.modules.pop(m) for m in otel_mods}
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                mod = _fresh_tracing_module()
                span = mod.start_run_span("r", "g", "/r")
                mod.record_event(span, "dispatch", "n", {})
                span.end()
                # No opentelemetry modules should have been imported
                newly_imported = [m for m in sys.modules if m.startswith("opentelemetry")]
                assert newly_imported == [], f"unexpected imports: {newly_imported}"
        finally:
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# 2. On with in-memory exporter — span hierarchy and attributes
# ---------------------------------------------------------------------------


def _otel_available() -> bool:
    try:
        import opentelemetry.sdk.trace  # noqa: F401

        return True
    except (ImportError, ModuleNotFoundError):
        return False


@pytest.mark.skipif(not _otel_available(), reason="opentelemetry not installed")
class TestTracingOn:
    """When VERDICT_TRACING=1 and OTel is installed, real spans are produced."""

    def test_span_hierarchy_and_attributes(self) -> None:
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()

        with mock.patch.dict(os.environ, {"VERDICT_TRACING": "1"}):
            mod = _fresh_tracing_module()

            # Patch the singleton to use our in-memory exporter
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            resource = Resource.create({"service.name": "test"})
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            mod._state._initialised = True
            mod._state._tracer = trace.get_tracer("test")

            # Start a run span
            root = mod.start_run_span("run-42", "goal-abc", "/tmp/repo")
            assert not isinstance(root, mod._NoOpSpan)

            # Record some events
            mod.record_event(
                root,
                "dispatch",
                "node-1",
                {"attempt": 1, "route_id": "kr/claude-sonnet", "ok": True},
            )
            mod.record_event(
                root,
                "node_state",
                "node-1",
                {
                    "state": "VALIDATED",
                    "duration_seconds": 12.5,
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                },
            )

            root.end()

            spans = exporter.get_finished_spans()
            names = [s.name for s in spans]
            assert "orchestration.run" in names
            assert "event.dispatch" in names
            assert "event.node_state" in names

            # Verify root span attributes
            root_span = next(s for s in spans if s.name == "orchestration.run")
            assert root_span.attributes["run_id"] == "run-42"
            assert root_span.attributes["goal_id"] == "goal-abc"
            assert root_span.attributes["repo"] == "/tmp/repo"

            # Verify child span attributes
            dispatch_span = next(s for s in spans if s.name == "event.dispatch")
            assert dispatch_span.attributes["event.type"] == "dispatch"
            assert dispatch_span.attributes["event.node_id"] == "node-1"
            assert dispatch_span.attributes["event.attempt"] == 1
            assert dispatch_span.attributes["event.route_id"] == "kr/claude-sonnet"

            # Verify parent-child relationship
            assert dispatch_span.parent is not None
            assert dispatch_span.parent.span_id == root_span.context.span_id

            # Verify usage attributes on node_state span
            ns_span = next(s for s in spans if s.name == "event.node_state")
            assert ns_span.attributes["event.usage.prompt_tokens"] == 100
            assert ns_span.attributes["event.usage.completion_tokens"] == 50
            assert ns_span.attributes["event.usage.total_tokens"] == 150
            assert ns_span.attributes["event.duration_seconds"] == 12.5


# ---------------------------------------------------------------------------
# 3. Attribute allowlist blocks secret-like keys and large strings
# ---------------------------------------------------------------------------


class TestAttributeAllowlist:
    """Verify the allowlist and secret-detection logic."""

    def test_non_allowed_keys_dropped(self) -> None:
        mod = _fresh_tracing_module()
        result = mod._filter_attributes(
            {"run_id": "ok", "not_allowed": "dropped", "event.type": "dispatch", "random_key": 42}
        )
        assert "run_id" in result
        assert "event.type" in result
        assert "not_allowed" not in result
        assert "random_key" not in result

    def test_secret_like_keys_blocked(self) -> None:
        mod = _fresh_tracing_module()
        # Even if a key is in ALLOWED_ATTRIBUTES, _looks_secret blocks it.
        # But our allowed keys don't contain secret fragments.
        # Test _looks_secret directly.
        assert mod._looks_secret("api_key") is True
        assert mod._looks_secret("AUTH_TOKEN") is True
        assert mod._looks_secret("password_hash") is True
        assert mod._looks_secret("x_secret_value") is True
        assert mod._looks_secret("run_id") is False
        assert mod._looks_secret("event.type") is False

    def test_large_strings_truncated(self) -> None:
        mod = _fresh_tracing_module()
        long_val = "x" * 1000
        result = mod._safe_value(long_val)
        assert len(result) <= mod._MAX_ATTR_VALUE_LEN + 1  # +1 for ellipsis char
        assert result.endswith("\u2026") or result.endswith("…")

    def test_safe_value_preserves_types(self) -> None:
        mod = _fresh_tracing_module()
        assert mod._safe_value(42) == 42
        assert mod._safe_value(3.14) == 3.14
        assert mod._safe_value(True) is True
        assert mod._safe_value(False) is False
        assert mod._safe_value("hello") == "hello"


# ---------------------------------------------------------------------------
# 4. NoOpSpan contract
# ---------------------------------------------------------------------------


class TestNoOpSpan:
    """The no-op span is a safe stand-in with all expected methods."""

    def test_context_manager(self) -> None:
        mod = _fresh_tracing_module()
        span = mod._NoOpSpan()
        with span as s:
            assert s is span
            s.set_attribute("k", "v")
            s.set_status("ok")
        span.end()

    def test_is_enabled_variants(self) -> None:
        mod = _fresh_tracing_module()
        for val, expected in [
            ("1", True),
            ("on", True),
            ("true", True),
            ("yes", True),
            ("0", False),
            ("off", False),
            ("", False),
            ("false", False),
        ]:
            with mock.patch.dict(os.environ, {"VERDICT_TRACING": val}):
                assert mod._is_enabled() is expected, f"VERDICT_TRACING={val!r}"


class TestUsageAttributes:
    """Usage counters survive the secret filter; strings under usage keys do not."""

    def test_usage_keys_allowed_despite_token_in_name(self) -> None:
        from verdict.tracing import _filter_attributes

        out = _filter_attributes(
            {
                "event.usage.input_tokens": 9393,
                "event.usage.output_tokens": 44,
                "event.usage.turns": 2,
                "event.usage.prompt_tokens": 100,
            }
        )
        assert out == {
            "event.usage.input_tokens": 9393,
            "event.usage.output_tokens": 44,
            "event.usage.turns": 2,
            "event.usage.prompt_tokens": 100,
        }

    def test_non_allowlisted_secret_keys_still_dropped(self) -> None:
        from verdict.tracing import _filter_attributes

        out = _filter_attributes(
            {"event.api_key": "x", "event.auth_token": "y", "event.state": "OK"}
        )
        assert out == {"event.state": "OK"}
