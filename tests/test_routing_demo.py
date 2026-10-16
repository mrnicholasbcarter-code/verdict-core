from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from verdict.live_routing import ConcreteIdentity, classify_identities, select_route
from verdict.routing_demo import (
    REQUEST_COUNT,
    SCHEMA_VERSION,
    _is_chat_identity,
    _qualify_for_request,
    assert_cheaper_first,
    build_demo_requests,
    estimate_cost_usd,
    format_human,
    load_recorded_capture,
    run_routing_demo,
    save_recorded_capture,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(
    identity_id: str,
    *,
    cost: str = "free",
    tools: bool = True,
    provider: str = "p",
    modalities: tuple[str, ...] = ("text",),
) -> ConcreteIdentity:
    return ConcreteIdentity(
        identity_id=identity_id,
        provider_id=provider,
        gateway_id="http://localhost:20128/v1",
        cost_class=cost,  # type: ignore[arg-type]
        context_limit=128000,
        output_limit=4096,
        tools=tools,
        modalities=modalities,
        spec_captured_at=_now(),
    )


def test_build_demo_requests_is_exactly_100_mixed() -> None:
    reqs = build_demo_requests()
    assert len(reqs) == REQUEST_COUNT
    assert sum(1 for r in reqs if r.request_class == "complex") == 20
    assert sum(1 for r in reqs if r.request_class == "simple") == 80


def test_estimate_cost_uses_pricing_index() -> None:
    pricing = {"prov/cheap": {"input": 1.0, "output": 2.0}}
    cost = estimate_cost_usd(pricing, "prov/cheap", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == 3.0


def test_cheaper_first_not_bypassed_for_baseline() -> None:
    rows = [_id("paid-model", cost="paid"), _id("free-model", cost="free")]
    candidates = classify_identities(rows)
    kept = [c for c in candidates if c.status == "kept"]
    chosen = select_route(kept).chosen
    assert_cheaper_first(kept, chosen)
    assert chosen.ref == "free-model"


def test_chat_qualification_rejects_non_chat_and_requires_tools() -> None:
    candidates = classify_identities(
        [
            _id("gemini/lyria-3-clip-preview", modalities=("text",)),
            _id("provider/audio-model", modalities=("audio",)),
            _id("chat-no-tools", tools=False),
            _id("chat-tools", tools=True),
        ]
    )
    simple, complex_request = build_demo_requests()[0], build_demo_requests()[4]

    assert _is_chat_identity(candidates[0].identity) is False  # type: ignore[arg-type]
    assert {candidate.ref for candidate in _qualify_for_request(candidates, simple)} == {
        "chat-no-tools",
        "chat-tools",
    }
    assert {candidate.ref for candidate in _qualify_for_request(candidates, complex_request)} == {
        "chat-tools"
    }


def test_mock_comparison_covers_models_gate_and_adaptive_ranker() -> None:
    summary = run_routing_demo(mock=True)

    assert summary["status"] == "completed"
    assert summary["mode"] == "mock"
    assert summary["request_count"] == 100
    assert set(summary["cost_comparison_usd"]) == {
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "auto-routed",
    }
    assert (
        summary["cost_comparison_usd"]["auto-routed"]
        < summary["cost_comparison_usd"]["claude-opus-5"]
    )
    assert summary["adaptive_ranker"]["mode"] == "shadow_adaptive"
    assert summary["adaptive_ranker"]["shadow"] is True
    assert summary["adaptive_ranker"]["excluded_reintroduced"] is False
    assert summary["wall_clock_ms"] < 30_000
    text = format_human(summary)
    assert "claude-opus-5" in text
    assert "adaptive_ranker" in text


def test_recorded_demo_completes_100_and_labels_mode(tmp_path: Path) -> None:
    identities = [
        _id("free-a", cost="free"),
        _id("cheap-b", cost="cheaper"),
        _id("paid-z", cost="paid"),
        _id("gemini/lyria-3-clip-preview", cost="free"),
    ]
    pricing = {
        "free-a": {"input": 0.0, "output": 0.0},
        "cheap-b": {"input": 0.5, "output": 0.5},
        "paid-z": {"input": 10.0, "output": 30.0},
    }
    capture = tmp_path / "capture.json"
    save_recorded_capture(
        capture,
        gateway_base_url="http://localhost:20128/v1",
        captured_at=_now(),
        identities=identities,
        pricing=pricing,
    )
    summary = run_routing_demo(recorded_path=capture, execute=False)
    assert summary["status"] == "completed"
    assert summary["mode"] == "recorded"
    assert summary["request_count"] == 100
    assert summary["schema_version"] == SCHEMA_VERSION
    assert summary["baseline_cost_usd"] >= summary["routed_cost_usd"]
    assert summary["savings_usd"] >= 0
    for decision in summary["decisions"]:
        assert decision["chosen_id"] != "paid-z" or decision["baseline_id"] == "paid-z"
        # cheaper-first: never paid while free/cheaper exist
        assert decision["chosen_id"] in {"free-a", "cheap-b"}
    text = format_human(summary)
    assert "mode: recorded" in text
    blob = json.dumps(summary).lower()
    assert "api_key" not in blob
    assert "authorization" not in blob


def test_blocked_when_recorded_missing(tmp_path: Path) -> None:
    summary = run_routing_demo(recorded_path=tmp_path / "missing.json", execute=False)
    assert summary["status"] == "blocked"
    assert summary["request_count"] == 0


def test_load_roundtrip(tmp_path: Path) -> None:
    identities = [_id("free-a", cost="free"), _id("paid-z", cost="paid")]
    pricing = {"free-a": {"input": 0.0, "output": 0.0}, "paid-z": {"input": 5.0, "output": 5.0}}
    capture = tmp_path / "cap.json"
    save_recorded_capture(
        capture,
        gateway_base_url="http://localhost:20128/v1",
        captured_at=_now(),
        identities=identities,
        pricing=pricing,
    )
    loaded, prices, captured, gateway = load_recorded_capture(capture)
    assert len(loaded) == 2
    assert "free-a" in prices
    assert gateway.endswith("/v1")
    assert captured.tzinfo is not None
