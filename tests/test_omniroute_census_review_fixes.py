"""Cross-family review fixes for the discovery pipeline (PR #915, findings B1-B5).

B1 rule 0.2 gates live probing (no literal "free"); B2 the context pass
respects remaining budgets, catalog capacity and reports needle reservations;
B3 the FAIL_FAKE provider blocklist persists across ticks and blocks
siblings; B4 untrusted cached timestamps are rejected inside guarded parsing;
B5 every census probe request carries X-OmniRoute-Budget-Fallback: strict.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict.omniroute_pipeline.census_hook import FLAG_ENV as FREE_RULE_FLAG_ENV
from verdict.omniroute_pipeline.probes import PROBES_FLAG_ENV, ProbeOutcome
from verdict.omniroute_pipeline.state import STATE_FLAG_ENV

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
FREE_PRICING = {"pricing": {"input": 0, "output": 0}}


@pytest.fixture(autouse=True)
def isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    monkeypatch.delenv(PROBES_FLAG_ENV, raising=False)
    monkeypatch.delenv(STATE_FLAG_ENV, raising=False)
    monkeypatch.delenv(FREE_RULE_FLAG_ENV, raising=False)


def _snapshot(models: dict[str, dict[str, Any]]) -> Any:
    from verdict.free_tier_admit import load_omniroute_admit_snapshot

    rows: list[dict[str, object]] = []
    per_model: list[dict[str, object]] = []
    providers: set[str] = set()
    for model_id, extra in models.items():
        provider = model_id.split("/", 1)[0]
        row: dict[str, object] = {"id": model_id, "owned_by": provider}
        row.update(extra)
        rows.append(row)
        providers.add(provider)
        per_model.append(
            {"provider": provider, "modelId": model_id.split("/", 1)[1], "freeType": "keyless"}
        )

    def gateway(request: httpx.Request) -> httpx.Response:
        data: dict[str, object] = {
            "/v1/models": {"data": rows},
            "/api/v1/me/status": {"accountQuotas": []},
            "/api/free-tier/summary": {"perModel": per_model},
            "/api/providers": {
                "connections": [
                    {"provider": p, "isActive": True, "testStatus": "active"}
                    for p in sorted(providers)
                ]
            },
            "/api/usage/model-latency-stats": {"entries": []},
            "/api/resilience/connections": {
                "connections": [
                    {
                        "provider": p,
                        "isActive": True,
                        "connectionStatus": "healthy",
                        "cooldownRemainingMs": 0,
                        "breaker": {"state": "CLOSED"},
                        "lockouts": [],
                    }
                    for p in sorted(providers)
                ]
            },
        }
        return httpx.Response(200, json=data[request.url.path])

    return load_omniroute_admit_snapshot("http://gateway", transport=httpx.MockTransport(gateway))


class CountingTransport:
    """Census probe transport recording every dispatched request."""

    def __init__(self, fake_routes: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fake_routes = fake_routes

    def __call__(self, route: str, phase: str, timeout: float) -> Any:
        from verdict.prove_at_rest import ProbeExchange

        self.calls.append((route, phase))
        if route in self.fake_routes:
            return ProbeExchange(
                http_status=200,
                ok=True,
                chat_exact=True,
                tool_called=True,
                response_body={
                    "id": "chatcmpl-fake-1",
                    "choices": [{"message": {"content": "PONG"}}],
                    "usage": {"total_tokens": 5},
                },
            )
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)


class CountingProber:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, route: str, size: int) -> ProbeOutcome:
        self.calls.append((route, size))
        return ProbeOutcome.PASS


def _tick(
    tmp_path: Path, transport: Any, prober: Any, models: dict[str, dict[str, Any]], **kw: Any
) -> dict[str, Any]:
    from verdict import census_passports
    from verdict.orchestration.health_cache import HealthCache

    return census_passports.run_census(
        _snapshot(models),
        transport,
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=kw.pop("now", NOW),
        context_prober=prober,
        context_store_path=tmp_path / "usable-context.json",
        **kw,
    )


# --- B1: rule 0.2 free evidence gates every live probe -----------------------


def test_b1_positive_catalog_price_is_never_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    from verdict.free_tier_admit import admit_free_tier_active

    models = {"good/a": {"pricing": {"input": 0.5, "output": 2.5}}}
    # The free-summary row still admits the route; probing is what 0.2 refuses.
    assert "good/a" in admit_free_tier_active(_snapshot(models)).free_admitted
    transport, prober = CountingTransport(), CountingProber()
    report = _tick(tmp_path, transport, prober, models)
    assert transport.calls == []
    assert prober.calls == []
    assert report["probed"] == 0
    assert report["context_probed"] == 0


def test_b1_blank_price_without_flag_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    models = {"good/a": {}}  # blank price, no provider flag: UNVERIFIED_FREE
    transport, prober = CountingTransport(), CountingProber()
    report = _tick(tmp_path, transport, prober, models)
    assert transport.calls == []
    assert prober.calls == []
    assert report["probed"] == 0
    assert report["context_probed"] == 0


def test_b1_provider_flag_evidence_allows_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    from verdict.omniroute_pipeline.free_rule import ProviderFreeFlags

    flags = ProviderFreeFlags(fetched_at=NOW, entries={"good/a": {"is_free": True}})
    transport, prober = CountingTransport(), CountingProber()
    report = _tick(tmp_path, transport, prober, {"good/a": {}}, free_flags=flags)
    assert report["probed"] == 1
    assert transport.calls == [("good/a", "chat"), ("good/a", "tool")]


def test_b1_needles_limited_to_top5_free_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    models = {f"f/r{i}": dict(FREE_PRICING) for i in range(1, 8)}
    transport, prober = CountingTransport(), CountingProber()
    kw: dict[str, Any] = {"max_per_provider": None, "context_probe_max_per_tick": 7}
    # S05 contract caps: 4 routes, 2/provider (BOD-297/S05-A). The shared
    # coordinator's maintenance contract now bounds the actual chat/tool
    # probe count below census's own max_per_provider=None ask, so proving
    # all 7 routes takes several ticks instead of one; the needle pass
    # itself is a SEPARATE budget keyed on ``candidates`` (free evidence),
    # never on how many routes the coordinator actually probed this tick,
    # and it only ever targets the first five routes in census order
    # (spec section 2).
    for _ in range(8):
        _tick(tmp_path, transport, prober, models, **kw)
    assert {route for route, _ in prober.calls} == {f"f/r{i}" for i in range(1, 6)}


# --- B2: context-pass budgets, catalog capacity, reservations ---------------


def test_b2_needle_respects_remaining_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer repro: max_tokens=1, max_candidates=1 must not buy a 128k needle."""
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    models = {"good/a": dict(FREE_PRICING)}
    transport, prober = CountingTransport(), CountingProber()
    _tick(tmp_path, transport, prober, models)
    tight = _tick(tmp_path, transport, prober, models, max_tokens=1, max_candidates=1)
    assert tight["context_probed"] == 0
    assert prober.calls == []


def test_b2_needle_reservation_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    models = {"good/a": dict(FREE_PRICING)}
    transport, prober = CountingTransport(), CountingProber()
    _tick(tmp_path, transport, prober, models)
    second = _tick(tmp_path, transport, prober, models)
    assert second["context_probed"] == 1
    assert second["reserved_tokens"] == 128_000 + 32


def test_b2_catalog_max_never_oversized_needle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    models = {
        "cap/x": {**FREE_PRICING, "context_window": 100_000},
        "big/y": {**FREE_PRICING, "context_window": 200_000},
    }
    transport, prober = CountingTransport(), CountingProber()
    kw: dict[str, Any] = {"max_per_provider": None, "context_probe_max_per_tick": 2}
    _tick(tmp_path, transport, prober, models, **kw)
    _tick(tmp_path, transport, prober, models, **kw)
    # cap/x: even the first step (128k) exceeds its 100k catalog limit.
    assert prober.calls == [("big/y", 128_000)]
    _tick(tmp_path, transport, prober, models, **kw)
    # big/y: next step (256k) exceeds its 200k catalog limit: stop.
    assert prober.calls == [("big/y", 128_000)]


# --- B3: FAIL_FAKE provider DROP persists and blocks siblings ----------------


def test_b3_fail_fake_blocks_sibling_now_and_next_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PROBES_FLAG_ENV, "1")
    monkeypatch.setenv(STATE_FLAG_ENV, "1")
    monkeypatch.setenv(FREE_RULE_FLAG_ENV, "1")
    from verdict.omniroute_pipeline.census_hook import guard_transport

    models = {"good/a": dict(FREE_PRICING), "good/b": dict(FREE_PRICING)}
    transport = CountingTransport(fake_routes=frozenset({"good/a"}))
    prober = CountingProber()
    blocklist_path = tmp_path / "provider-blocklist.json"

    first = _tick(
        tmp_path, guard_transport(transport), prober, models, blocklist_path=blocklist_path
    )
    assert transport.calls == [("good/a", "chat")]  # sibling good/b never dispatched
    assert first["pipeline_states"]["good/a"] == "needs_account"
    assert first["blocklisted_providers"] == {"good": "fail_fake"}
    assert json.loads(blocklist_path.read_text(encoding="utf-8")) == {"good": "fail_fake"}

    second = _tick(
        tmp_path, guard_transport(transport), prober, models, blocklist_path=blocklist_path
    )
    assert transport.calls == [("good/a", "chat")]  # next tick: still no call
    assert second["probed"] == 0
    assert second["blocklisted_providers"] == {"good": "fail_fake"}


# --- B4: untrusted cached timestamps rejected in guarded parsing -------------


def test_b4_load_free_flags_rejects_naive_and_malformed_cache(tmp_path: Path) -> None:
    from verdict.omniroute_pipeline.free_rule import load_free_flags

    path = tmp_path / "free-flags-last-good.json"
    path.write_text(
        json.dumps({"fetched_at": "2026-10-17 09:00", "entries": {"kc/x": {"is_free": True}}}),
        encoding="utf-8",
    )
    assert load_free_flags(path, now=NOW) is None  # offset-naive: invalid evidence
    path.write_text("not-json", encoding="utf-8")
    assert load_free_flags(path, now=NOW) is None
    path.write_text(
        json.dumps({"fetched_at": "2026-10-17T09:00:00+00:00", "entries": []}), encoding="utf-8"
    )
    assert load_free_flags(path, now=NOW) is None  # entries not a mapping


def test_b4_context_due_rejects_naive_and_malformed_rows() -> None:
    from verdict.omniroute_pipeline.context_store import context_due

    naive = {
        "usable_context": 128_000,
        "measured_at": "2026-10-17 09:00",
        "source": "census needle",
        "failed_step": None,
    }
    assert context_due(naive, NOW) is True  # naive timestamp: treat as unmeasured
    malformed = {
        "usable_context": 128_000,
        "measured_at": "garbage",
        "source": "census needle",
        "failed_step": None,
    }
    assert context_due(malformed, NOW) is True


# --- B5: strict budget header on every census probe request ------------------


def test_b5_strict_budget_header_on_every_census_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from verdict import prove_at_rest
    from verdict.omniroute_pipeline.census_hook import guard_transport, needle_prober
    from verdict.omniroute_pipeline.probes import BUDGET_FALLBACK_HEADER
    from verdict.prove_at_rest import live_transport

    captured: list[Any] = []

    class _Response:
        status = 200

        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "id": "c1",
                    "choices": [{"message": {"content": "PONG"}, "finish_reason": "stop"}],
                    "usage": {"total_tokens": 5},
                }
            ).encode("utf-8")

    @contextmanager
    def _open(request: Any, *, timeout: float) -> Any:
        captured.append(request)
        yield _Response()

    monkeypatch.setattr(prove_at_rest, "open_no_redirect", _open)

    def headers_of(request: Any) -> dict[str, str]:
        return {key.lower(): value for key, value in request.headers.items()}

    transport = guard_transport(
        live_transport("http://gateway", api_key="scoped", extra_headers=BUDGET_FALLBACK_HEADER)
    )
    transport("good/a", "chat", 1.0)
    transport("good/a", "tool", 1.0)
    assert len(captured) == 2
    for request in captured:
        assert headers_of(request)["x-omniroute-budget-fallback"] == "strict"
        assert headers_of(request)["authorization"] == "Bearer scoped"

    # prove-at-rest policy unchanged without the census-only option.
    plain = live_transport("http://gateway", api_key="scoped")
    plain("good/a", "chat", 1.0)
    assert "x-omniroute-budget-fallback" not in headers_of(captured[-1])

    posted: dict[str, Any] = {}

    class _HttpResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": ""}}]}

    def _post(endpoint: str, **kw: Any) -> _HttpResponse:
        posted.update(kw)
        return _HttpResponse()

    monkeypatch.setattr(httpx, "post", _post)
    needle_prober("http://gateway/v1", "scoped")("good/a", 128_000)
    assert posted["headers"]["X-OmniRoute-Budget-Fallback"] == "strict"
    assert posted["headers"]["Authorization"] == "Bearer scoped"
