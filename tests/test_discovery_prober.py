"""Offline unit tests for verdict.discovery.probe and verdict.discovery.toolprobe.

No network calls; the injectable transport is used for all HTTP.
"""

from __future__ import annotations

import json
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
from verdict.discovery.probe import probe_all, probe_one, set_transport
from verdict.discovery.toolprobe import (
    BAD_TOOL_CALL,
    NO_TOOL_CALL,
    TOOL_ERROR,
    TOOL_OK,
    classify,
    filler,
    tool_probe,
    tool_probe_all,
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
    max_parallel: int | None = None,
) -> Route:
    return Route(
        provider=provider,
        model=model,
        base_url=base_url,
        auth=auth,
        upstream=upstream,
        max_parallel=max_parallel,
    )


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
        set_transport(None)

    def test_up_on_200_with_content(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, json.dumps(_ok_body()).encode()

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, UP)
        self.assertIsNotNone(r.latency_ms)

    def test_down_on_500(self) -> None:
        def _t(url, h, p, timeout):
            return 500, {}, b'{"error": "internal"}'

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, DOWN)
        self.assertEqual(r.http_code, 500)

    def test_rate_limited_on_429(self) -> None:
        def _t(url, h, p, timeout):
            return 429, {"Retry-After": "30"}, b'{"error": "too many requests"}'

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, RATE_LIMITED)
        self.assertEqual(r.retry_after_s, 30.0)
        self.assertEqual(r.http_code, 429)

    def test_rate_limited_on_200_with_rate_message(self) -> None:
        body = json.dumps({"error": "rate limit exceeded"}).encode()

        def _t(url, h, p, timeout):
            return 200, {}, body

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, RATE_LIMITED)

    def test_auth_error_on_401(self) -> None:
        def _t(url, h, p, timeout):
            return 401, {}, b'{"error": "unauthorized"}'

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, AUTH_ERROR)
        self.assertEqual(r.http_code, 401)

    def test_auth_error_on_403(self) -> None:
        def _t(url, h, p, timeout):
            return 403, {}, b'{"error": "forbidden"}'

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, AUTH_ERROR)

    def test_garbage_on_html_response(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {"content-type": "text/html"}, b"<html>...</html>"

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, GARBAGE)

    def test_garbage_on_non_json(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, b"not json at all"

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, GARBAGE)

    def test_garbage_on_empty_choices(self) -> None:
        body = json.dumps({"choices": []}).encode()

        def _t(url, h, p, timeout):
            return 200, {}, body

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, GARBAGE)

    def test_timeout_on_socket_timeout(self) -> None:
        def _t(url, h, p, timeout):
            raise TimeoutError("timed out")

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.status, TIMEOUT)

    def test_error_on_connection_error(self) -> None:
        def _t(url, h, p, timeout):
            raise OSError("connection refused")

        set_transport(_t)
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

        set_transport(_t)
        probe_one(_route(auth="env:MISSING_VAR_NEVER_SET"))
        self.assertEqual(called, [], "transport must not be called when key is missing")

    def test_served_model_from_omniroute_headers(self) -> None:
        def _t(url, h, p, timeout):
            return (
                200,
                {"x-omniroute-provider": "prov", "x-omniroute-model": "model-id"},
                json.dumps(_ok_body()).encode(),
            )

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.answered_model, "prov/model-id")

    def test_retry_after_seconds(self) -> None:
        def _t(url, h, p, timeout):
            return 429, {"Retry-After": "60"}, b""

        set_transport(_t)
        r = probe_one(_route())
        self.assertEqual(r.retry_after_s, 60.0)

    def test_retry_after_http_date(self) -> None:
        future = datetime.now(timezone.utc) + timedelta(seconds=90)
        date_str = format_datetime(future)

        def _t(url, h, p, timeout):
            return 429, {"Retry-After": date_str}, b""

        set_transport(_t)
        r = probe_one(_route())
        self.assertIsNotNone(r.retry_after_s)
        assert r.retry_after_s is not None
        self.assertGreater(r.retry_after_s, 60.0)

    def test_literal_auth_bearer_sent(self) -> None:
        seen: list[dict[str, str]] = []

        def _t(url, h, p, timeout):
            seen.append(h)
            return 200, {}, json.dumps(_ok_body()).encode()

        set_transport(_t)
        probe_one(_route(auth="literal:mytoken"))
        self.assertEqual(seen[0]["Authorization"], "Bearer mytoken")

    def test_secret_does_not_leak_into_detail(self) -> None:
        import os

        os.environ["_TEST_SECRET_XYZ"] = "supersecret"
        try:

            def _t(url, h, p, timeout):
                raise OSError("connect failed: supersecret in error")

            set_transport(_t)
            r = probe_one(_route(auth="env:_TEST_SECRET_XYZ"))
            self.assertNotIn("supersecret", r.detail or "")
        finally:
            os.environ.pop("_TEST_SECRET_XYZ", None)

    def test_user_agent_is_verdict(self) -> None:
        seen: list[dict[str, str]] = []

        def _t(url, h, p, timeout):
            seen.append(h)
            return 200, {}, json.dumps(_ok_body()).encode()

        set_transport(_t)
        probe_one(_route())
        self.assertEqual(seen[0]["User-Agent"], "verdict/0.1")


# ---------------------------------------------------------------------------
# probe_all — ordering and robustness
# ---------------------------------------------------------------------------


class ProbeAllTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_transport(None)

    def test_returns_in_input_order(self) -> None:
        routes = [_route(model=f"m{i}") for i in range(5)]

        def _t(url, h, p, timeout):
            return 200, {}, json.dumps(_ok_body()).encode()

        set_transport(_t)
        results = probe_all(routes)
        self.assertEqual(len(results), 5)
        for i, r in enumerate(results):
            self.assertEqual(r.route.model, f"m{i}")

    def test_empty_routes(self) -> None:
        results = probe_all([])
        self.assertEqual(results, [])

    def test_error_on_transport_exception(self) -> None:
        def _t(url, h, p, timeout):
            raise RuntimeError("boom")

        set_transport(_t)
        results = probe_all([_route()])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, ERROR)

    def test_per_host_semaphore_serialises_one_provider(self) -> None:
        """With max_parallel=1 only one concurrent probe per host is allowed."""
        import threading

        concurrent: list[int] = [0]
        peak: list[int] = [0]
        lock = threading.Lock()

        def _t(url, h, p, timeout):
            with lock:
                concurrent[0] += 1
                if concurrent[0] > peak[0]:
                    peak[0] = concurrent[0]
            import time

            time.sleep(0.01)
            with lock:
                concurrent[0] -= 1
            return 200, {}, json.dumps(_ok_body()).encode()

        set_transport(_t)
        routes = [_route(model=f"m{i}", max_parallel=1) for i in range(4)]
        probe_all(routes, per_host=1)
        self.assertEqual(peak[0], 1)


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
        status, detail = classify(429, b"")
        self.assertEqual(status, TOOL_ERROR)
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
        set_transport(None)

    def test_tool_ok_result(self) -> None:
        def _t(url, h, p, timeout):
            return 200, {}, _tool_call_body("Paris")

        set_transport(_t)
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

        set_transport(_t)
        tool_probe(_route())
        self.assertNotIn("temperature", seen[0])

    def test_context_tokens_pads_request(self) -> None:
        seen: list[dict[str, object]] = []

        def _t(url, h, p, timeout):
            seen.append(json.loads(p))
            return 200, {}, _tool_call_body()

        set_transport(_t)
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

        set_transport(_t)
        _, _, _, served = tool_probe(_route())
        self.assertEqual(served, "prov/mymodel")

    def test_no_key_returns_tool_error(self) -> None:
        status, detail, _, _ = tool_probe(_route(auth="env:MISSING_VAR_TOOL_TEST"))
        self.assertEqual(status, TOOL_ERROR)
        self.assertIn("env var", detail or "")

    def test_transport_exception_returns_tool_error(self) -> None:
        def _t(url, h, p, timeout):
            raise RuntimeError("network gone")

        set_transport(_t)
        status, _, _, _ = tool_probe(_route())
        self.assertEqual(status, TOOL_ERROR)


# ---------------------------------------------------------------------------
# tool_probe_all
# ---------------------------------------------------------------------------


class ToolProbeAllTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_transport(None)

    def test_only_probes_up_routes(self) -> None:
        from verdict.discovery.model import ProbeResult

        called: list[str] = []

        def _t(url, h, p, timeout):
            body = json.loads(p)
            called.append(body["model"])
            return 200, {}, _tool_call_body()

        set_transport(_t)
        up = ProbeResult(route=_route(provider="p", model="m1"), status=UP)
        down = ProbeResult(route=_route(provider="p", model="m2"), status=DOWN)
        result = tool_probe_all([up, down])
        self.assertIn("p/m1", result)
        self.assertNotIn("p/m2", result)
        self.assertEqual(called, ["m1"])

    def test_empty_input(self) -> None:
        result = tool_probe_all([])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
