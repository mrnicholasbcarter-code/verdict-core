"""Offline unit tests for verdict.discovery.probe and verdict.discovery.toolprobe.

No network calls; the injectable transport is used for all HTTP.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from verdict.discovery.model import (
    AUTH_ERROR,
    DOWN,
    ERROR,
    GARBAGE,
    NO_KEY,
    RATE_LIMITED,
    TIMEOUT,
    UP,
    Route,
)
from verdict.discovery.probe import _set_transport, probe_one
from verdict.discovery.toolprobe import (
    BAD_TOOL_CALL,
    NO_TOOL_CALL,
    TOOL_ERROR,
    TOOL_OK,
    classify,
    filler,
    tool_probe,
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


def _ok_body(*, content: str = "ok", model: str = "test-model") -> dict[str, object]:
    return {"model": model, "choices": [{"message": {"role": "assistant", "content": content}}]}


def _tool_call_body(city: str = "Paris") -> bytes:
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
                                    "arguments": json.dumps({"city": city}),
                                },
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()


# ---------------------------------------------------------------------------
# probe_one — status classification
# ---------------------------------------------------------------------------


class ProbeOneStatusTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_up_on_200_with_content(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, json.dumps(_ok_body()).encode()

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, UP)
        self.assertIsNotNone(r.latency_ms)

    def test_down_on_500(self) -> None:
        def _t(url, h, p, timeout):
            return 500, {}, b'{"error": "internal"}'

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, DOWN)
        self.assertEqual(r.http_code, 500)

    def test_rate_limited_on_429(self) -> None:
        def _t(url, h, p, timeout):
            return 429, {"Retry-After": "30"}, b'{"error": "too many requests"}'

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, RATE_LIMITED)
        self.assertEqual(r.retry_after_s, 30.0)
        self.assertEqual(r.http_code, 429)

    def test_rate_limited_on_200_with_rate_message(self) -> None:
        body = json.dumps({"error": "rate limit exceeded"}).encode()

        def _t(url, h, p, timeout):
            return 200, {}, body

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, RATE_LIMITED)

    def test_auth_error_on_401(self) -> None:
        def _t(url, h, p, timeout):
            return 401, {}, b'{"error": "unauthorized"}'

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, AUTH_ERROR)
        self.assertEqual(r.http_code, 401)

    def test_auth_error_on_403(self) -> None:
        def _t(url, h, p, timeout):
            return 403, {}, b'{"error": "forbidden"}'

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, AUTH_ERROR)

    def test_garbage_on_html_response(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {"content-type": "text/html"}, b"<html>...</html>"

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, GARBAGE)

    def test_garbage_on_non_json(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, b"not json at all"

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, GARBAGE)

    def test_garbage_on_empty_choices(self) -> None:
        body = json.dumps({"choices": []}).encode()

        def _t(url, h, p, timeout):
            return 200, {}, body

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, GARBAGE)

    def test_timeout_on_socket_timeout(self) -> None:
        def _t(url, h, p, timeout):
            raise TimeoutError("timed out")

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, TIMEOUT)

    def test_error_on_connection_error(self) -> None:
        def _t(url, h, p, timeout):
            raise OSError("connection refused")

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, ERROR)

    def test_no_key_when_env_var_unset(self) -> None:
        # auth="env:MISSING_VAR_12345" and the var is not set
        r = probe_one(_route(auth="env:MISSING_VAR_12345"))
        self.assertEqual(r.status, NO_KEY)

    def test_no_key_does_not_call_transport(self) -> None:
        called: list[bool] = []

        def _t(url, h, p, timeout):
            called.append(True)
            return 200, {}, json.dumps(_ok_body()).encode()

        _set_transport(_t)
        probe_one(_route(auth="env:MISSING_VAR_NEVER_SET"))
        self.assertEqual(called, [], "transport must not be called when key is missing")

    def test_served_model_from_omniroute_headers(self) -> None:
        def _t(url, h, p, timeout):
            return (
                200,
                {"x-omniroute-provider": "prov", "x-omniroute-model": "model-id"},
                json.dumps(_ok_body()).encode(),
            )

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.answered_model, "prov/model-id")

    def test_retry_after_seconds(self) -> None:
        def _t(url, h, p, timeout):
            return 429, {"Retry-After": "60"}, b""

        _set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.retry_after_s, 60.0)

    def test_retry_after_http_date(self) -> None:
        import unittest.mock as _mock

        # Use a fixed "now" so the result is exact even on slow CI.
        # patch() replaces the `datetime` module reference inside probe.py so
        # datetime.datetime.now() returns fixed_now.  Only line 91 of probe.py
        # uses the datetime module, so nothing else is affected.
        fixed_now = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)
        future = fixed_now + timedelta(seconds=90)
        date_str = format_datetime(future)

        def _t(url, h, p, timeout):
            return 429, {"Retry-After": date_str}, b""

        _set_transport(_t)
        with _mock.patch("verdict.discovery.probe.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = fixed_now
            mock_dt.timezone.utc = timezone.utc
            r = probe_one(_route())
        self.assertIsNotNone(r.retry_after_s)
        assert r.retry_after_s is not None
        self.assertAlmostEqual(r.retry_after_s, 90.0, places=0)

    def test_literal_auth_bearer_sent(self) -> None:
        seen: list[dict[str, str]] = []

        def _t(url, h, p, timeout):
            seen.append(h)
            return 200, {}, json.dumps(_ok_body()).encode()

        _set_transport(_t)
        probe_one(_route(auth="literal:mytoken"))
        self.assertEqual(seen[0]["Authorization"], "Bearer mytoken")

    def test_secret_does_not_leak_into_detail(self) -> None:
        import os

        os.environ["_TEST_SECRET_XYZ"] = "supersecret"
        try:

            def _t(url, h, p, timeout):
                raise OSError("connect failed: supersecret in error")

            _set_transport(_t)
            r = probe_one(_route(auth="env:_TEST_SECRET_XYZ"))
            self.assertNotIn("supersecret", r.detail or "")
        finally:
            os.environ.pop("_TEST_SECRET_XYZ", None)

    def test_user_agent_is_verdict(self) -> None:
        seen: list[dict[str, str]] = []

        def _t(url, h, p, timeout):
            seen.append(h)
            return 200, {}, json.dumps(_ok_body()).encode()

        _set_transport(_t)
        probe_one(_route())
        self.assertEqual(seen[0]["User-Agent"], "verdict/0.1")


# ---------------------------------------------------------------------------
# toolprobe — classify
# ---------------------------------------------------------------------------


class ToolProbeClassifyTest(unittest.TestCase):
    def test_correct_call(self) -> None:
        self.assertEqual(classify(200, _tool_call_body("Paris")), (TOOL_OK, None))

    def test_city_case_insensitive(self) -> None:
        self.assertEqual(classify(200, _tool_call_body("PARIS")), (TOOL_OK, None))

    def test_wrong_city(self) -> None:
        status, detail = classify(200, _tool_call_body("London"))
        self.assertEqual(status, BAD_TOOL_CALL)
        self.assertIn("London", detail or "")

    def test_no_tool_call(self) -> None:
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "It is sunny."}}]}
        ).encode()
        self.assertEqual(classify(200, body), (NO_TOOL_CALL, None))

    def test_non_200_is_tool_error(self) -> None:
        # Non-429 error codes → TOOL_ERROR
        from verdict.discovery.toolprobe import TOOL_ERROR as _TE

        for code in (401, 403, 500, 503):
            status, detail = classify(code, b"")
            self.assertEqual(status, _TE, f"HTTP {code} must be TOOL_ERROR")
            self.assertIn(str(code), detail or "")

    def test_429_is_rate_limited(self) -> None:
        # HTTP 429 → RATE_LIMITED (typed signal, not TOOL_ERROR)
        from verdict.discovery.model import RATE_LIMITED

        status, detail = classify(429, b"")
        self.assertEqual(status, RATE_LIMITED, "HTTP 429 must return RATE_LIMITED")
        self.assertIn("429", detail or "")

    def test_malformed_body(self) -> None:
        status, _ = classify(200, b"not json")
        self.assertEqual(status, NO_TOOL_CALL)

    def test_arguments_as_object(self) -> None:
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": {"city": "Paris"},
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ).encode()
        self.assertEqual(classify(200, body), (TOOL_OK, None))


# ---------------------------------------------------------------------------
# filler padding
# ---------------------------------------------------------------------------


class FillerTest(unittest.TestCase):
    def test_filler_produces_at_least_requested_tokens(self) -> None:
        text = filler(100)
        # At least 100 * 4 = 400 chars
        self.assertGreaterEqual(len(text), 400)

    def test_filler_zero_tokens(self) -> None:
        self.assertEqual(filler(0), "")


# ---------------------------------------------------------------------------
# tool_probe function
# ---------------------------------------------------------------------------


class ToolProbeTest(unittest.TestCase):
    def tearDown(self) -> None:
        _set_transport(None)

    def test_tool_ok_result(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, _tool_call_body("Paris")

        _set_transport(_t)
        status, detail, latency_ms, _served = tool_probe(_route())
        self.assertEqual(status, TOOL_OK)
        self.assertIsNone(detail)
        self.assertIsNotNone(latency_ms)

    def test_no_temperature_in_request(self) -> None:
        """Some models reject requests that include temperature."""
        seen: list[dict[str, object]] = []

        def _t(url, h, p, timeout):
            seen.append(json.loads(p))
            return 200, {}, _tool_call_body()

        _set_transport(_t)
        tool_probe(_route())
        self.assertNotIn("temperature", seen[0])

    def test_context_tokens_pads_request(self) -> None:
        seen: list[dict[str, object]] = []

        def _t(url, h, p, timeout):
            seen.append(json.loads(p))
            return 200, {}, _tool_call_body()

        _set_transport(_t)
        tool_probe(_route(), context_tokens=500)
        messages = seen[0].get("messages", [])
        self.assertGreater(len(messages), 1, "system message with filler expected")
        system_msg = messages[0]
        self.assertEqual(system_msg["role"], "system")
        content = str(system_msg.get("content", ""))
        self.assertGreater(len(content), 500 * 4 // 2)  # rough check

    def test_served_model_from_headers(self) -> None:
        def _t(url, h, p, timeout):
            return (
                200,
                {"x-omniroute-provider": "prov", "x-omniroute-model": "mymodel"},
                _tool_call_body(),
            )

        _set_transport(_t)
        _, _, _, served = tool_probe(_route())
        self.assertEqual(served, "prov/mymodel")

    def test_no_key_returns_tool_error(self) -> None:
        status, detail, _, _ = tool_probe(_route(auth="env:MISSING_VAR_TOOL_TEST"))
        self.assertEqual(status, TOOL_ERROR)
        self.assertIn("env var", detail or "")

    def test_transport_exception_returns_tool_error(self) -> None:
        def _t(url, h, p, timeout):
            raise RuntimeError("network gone")

        _set_transport(_t)
        status, _, _, _ = tool_probe(_route())
        self.assertEqual(status, TOOL_ERROR)


# ---------------------------------------------------------------------------
# Transport exception — single route error must not crash the whole refresh
# ---------------------------------------------------------------------------


class TransportExceptionTest(unittest.TestCase):
    """An exception from the transport for one route must not abort the refresh."""

    def tearDown(self) -> None:
        _set_transport(None)

    def test_transport_exception_records_error_and_keeps_other_routes(self) -> None:
        """If transport raises for route_a, it gets status=ERROR; route_b is unaffected."""
        import urllib.parse

        from verdict.discovery.registry import run_refresh

        route_a = _route(provider="p", model="boom", base_url="http://boom.example.com/v1")
        route_b = _route(provider="q", model="ok", base_url="http://ok.example.com/v1")

        def _ok_body():
            return json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "choices": [{"message": {"content": "ok"}}],
                }
            ).encode()

        def _t(url: str, h: dict, p: bytes, timeout: float):
            hostname = urllib.parse.urlsplit(url).hostname or ""
            if hostname == "boom.example.com":
                raise RuntimeError("boom")
            return 200, {}, _ok_body()

        _set_transport(_t)

        with tempfile.TemporaryDirectory() as tmp:
            env = {"VERDICT_HOME": tmp, "HOME": tmp, "VERDICT_RECEIPTS_DB": ":memory:"}
            reg = run_refresh(
                [(route_a, "free", "plan_free"), (route_b, "free", "plan_free")],
                context_tokens=0,
                wall_budget_s=300.0,
                env=env,
            )

        self.assertEqual(len(reg.entries), 2)
        entries_by_key = {e.route_key: e for e in reg.entries}
        # route_a: transport raised → answered=False (ERROR result)
        self.assertFalse(entries_by_key[route_a.key].answered)
        # route_b: separate pool, was not affected
        self.assertTrue(entries_by_key[route_b.key].answered)


class LoadOmniRouteRoutesTest(unittest.TestCase):
    """Tests for verdict.discovery.refresh._load_omniroute_routes.

    Uses parse_omniroute_evidence offline and patches fetch_omniroute_evidence
    so no network calls are made.
    """

    def _make_evidence(self, route_id: str, provider: str, cap_class: str, cap_fact: str) -> object:
        from datetime import datetime, timezone

        from verdict.omniroute_evidence import OmniRouteEvidence

        return OmniRouteEvidence(
            fetched_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
            connections=(),
            usable_providers=frozenset(),
            proven_by_traffic={},
            cooled_models=frozenset(),
            source_errors=(),
            route_capacities={route_id: cap_class},
            route_capacity_evidence={route_id: cap_fact},
            inventory_rows={route_id: {"id": route_id, "owned_by": provider}},
        )

    def test_free_route_becomes_probeable_route(self) -> None:
        """A route with capacity 'free' becomes a Route tuple in the result."""
        from unittest.mock import patch

        from verdict.discovery.refresh import _load_omniroute_routes

        evidence = self._make_evidence(
            "groq/llama-3.3-70b-versatile", "groq", "free", "plan_label_free"
        )
        with patch("verdict.omniroute_evidence.fetch_omniroute_evidence", return_value=evidence):
            env = {"OMNIROUTE_API_KEY": "test-key", "OMNIROUTE_BASE_URL": "http://localhost:20128"}
            result = _load_omniroute_routes(env=env)

        self.assertTrue(result.available, "fetch succeeded; available must be True")
        self.assertEqual(len(result.routes), 1)
        route, cap_class, cap_fact = result.routes[0]
        self.assertEqual(route.model, "groq/llama-3.3-70b-versatile")
        self.assertEqual(cap_class, "free")
        self.assertEqual(cap_fact, "plan_label_free")
        self.assertEqual(route.auth, "env:OMNIROUTE_API_KEY")

    def test_metered_route_excluded(self) -> None:
        """A route with capacity 'metered' is not included in the result."""
        from unittest.mock import patch

        from verdict.discovery.refresh import _load_omniroute_routes

        evidence = self._make_evidence(
            "openai/gpt-4o", "openai", "metered", "apikey_positive_pricing"
        )
        with patch("verdict.omniroute_evidence.fetch_omniroute_evidence", return_value=evidence):
            env = {"OMNIROUTE_API_KEY": "test-key"}
            result = _load_omniroute_routes(env=env)

        self.assertTrue(result.available, "fetch succeeded; available must be True")
        self.assertEqual(result.routes, [], "metered route must be excluded")

    def test_missing_api_key_returns_empty_without_fetch(self) -> None:
        """When no API key is present, returns [] without calling fetch."""
        from unittest.mock import MagicMock, patch

        from verdict.discovery.refresh import _load_omniroute_routes

        mock_fetch = MagicMock()
        with patch("verdict.omniroute_evidence.fetch_omniroute_evidence", mock_fetch):
            result = _load_omniroute_routes(env={})

        self.assertFalse(result.available, "missing key → available must be False")
        self.assertEqual(result.reason, "api_key_missing")
        self.assertEqual(result.routes, [])
        mock_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
