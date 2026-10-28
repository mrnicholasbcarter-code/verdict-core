from __future__ import annotations

import urllib.error

from verdict.omniroute_rankings import OmniRouteRankings


def _fetcher(payload):
    """A fake JsonFetcher returning a fixed payload and recording the URL."""
    calls: list[str] = []

    def fetch(url: str, api_key, timeout: float):
        calls.append(url)
        return payload

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_rankings_parses_actual_api_shape() -> None:
    payload = {
        "rankings": [
            {
                "id": "agy",
                "name": "Antigravity CLI",
                "category": "oauth",
                "topModel": {
                    "modelId": "gemini-3.8-flash-high",
                    "modelName": "Gemini 3.8 Flash (High)",
                    "score": 0.91,
                    "eloRaw": 1500,
                    "confidence": "medium",
                    "category": "coding",
                },
                "reliability": {
                    "state": "healthy",
                    "connections": [{"testStatus": "active", "state": "healthy"}],
                    "usage": {"requests": 10, "successes": 10, "successRate": 1.0},
                },
            }
        ]
    }
    fetch = _fetcher(payload)
    client = OmniRouteRankings("http://127.0.0.1:20128", "secret", fetch_json=fetch)
    rows = client.get_rankings("coding")
    assert len(rows) == 1
    assert rows[0].id == "agy"
    assert rows[0].top_model_id == "gemini-3.8-flash-high"
    assert rows[0].score == 0.91
    assert rows[0].reliability == 1.0
    # The real query string is used against the OmniRoute management endpoint.
    assert fetch.calls[0].startswith("http://127.0.0.1:20128/api/free-provider-rankings?")
    assert "category=coding" in fetch.calls[0]


def test_malformed_rows_are_omitted_without_inventing_values() -> None:
    payload = {
        "rankings": [
            {"id": "x", "topModel": {"modelId": "m"}, "score": 1},  # score not in topModel
            {"id": "bad", "topModel": {}, "score": 2},  # empty topModel
        ]
    }
    rows = OmniRouteRankings(
        "http://127.0.0.1:20128", "secret", fetch_json=_fetcher(payload)
    ).get_rankings("coding")
    assert rows == []


def test_http_404_returns_named_omission() -> None:
    def fetch(url, api_key, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    result = OmniRouteRankings("http://127.0.0.1:20128", "secret", fetch_json=fetch).fetch("coding")
    assert result.rows == ()
    assert result.omission == "unsupported_or_missing"


def test_generic_gateway_is_skipped_without_probing() -> None:
    """Generic OpenAI-compatible gateways must not be probed for the OmniRoute-only endpoint."""
    fetch = _fetcher({"rankings": []})
    client = OmniRouteRankings("http://generic-gateway.example/v1", "secret", fetch_json=fetch)
    result = client.fetch("coding")
    assert result.rows == ()
    assert result.omission == "generic_gateway"
    # The generic gateway must NOT have been probed at all.
    assert fetch.calls == []


def test_missing_reliability_is_unknown_not_zero() -> None:
    payload = {
        "rankings": [{"id": "test-provider", "topModel": {"modelId": "test-model", "score": 0.5}}]
    }
    rows = OmniRouteRankings(
        "http://127.0.0.1:20128", "secret", fetch_json=_fetcher(payload)
    ).get_rankings("coding")
    assert len(rows) == 1
    assert rows[0].reliability is None


def test_each_route_keeps_its_own_score() -> None:
    """Provider-best evidence is per topModel; scores are never transferred between routes."""
    payload = {
        "rankings": [
            {"id": "provider-a", "topModel": {"modelId": "model-a", "score": 0.95}},
            {"id": "provider-b", "topModel": {"modelId": "model-b", "score": 0.5}},
        ]
    }
    rows = OmniRouteRankings(
        "http://127.0.0.1:20128", "secret", fetch_json=_fetcher(payload)
    ).get_rankings("coding")
    by_model = {r.top_model_id: r.score for r in rows}
    assert by_model["model-a"] == 0.95
    assert by_model["model-b"] == 0.5


def test_malformed_rankings_returns_named_omission() -> None:
    result = OmniRouteRankings(
        "http://127.0.0.1:20128", "secret", fetch_json=_fetcher({"rankings": "not-a-list"})
    ).fetch("coding")
    assert result.rows == ()
    assert result.omission == "malformed"


def test_transport_error_returns_named_omission() -> None:
    def fetch(url, api_key, timeout):
        raise urllib.error.URLError("timeout")

    result = OmniRouteRankings("http://127.0.0.1:20128", "secret", fetch_json=fetch).fetch("coding")
    assert result.rows == ()
    assert result.omission == "unavailable"
