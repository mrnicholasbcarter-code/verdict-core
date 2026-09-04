from __future__ import annotations

from pathlib import Path

import httpx

from verdict.live_routing_gateway import DEFAULT_GATEWAY
from verdict.routing_demo import run_routing_demo


def _gateway_up() -> bool:
    try:
        response = httpx.get(f"{DEFAULT_GATEWAY.rstrip('/')}/models", timeout=5.0)
        return response.status_code < 500
    except httpx.HTTPError:
        return False


def test_live_demo_completes_or_blocks_honestly(tmp_path: Path) -> None:
    if not _gateway_up():
        summary = run_routing_demo(gateway_base_url="http://127.0.0.1:9/v1", execute=False)
        assert summary["status"] == "blocked"
        assert summary["request_count"] == 0
        return

    capture = tmp_path / "live-capture.json"
    summary = run_routing_demo(
        gateway_base_url=DEFAULT_GATEWAY, save_capture_path=capture, execute=True
    )
    assert summary["status"] in {"completed", "blocked"}
    if summary["status"] == "blocked":
        # catalog unreachable mid-flight or no qualified — not a green fake
        assert summary["request_count"] == 0
        return
    assert summary["mode"] == "live"
    assert summary["request_count"] == 100
    assert summary["wall_clock_ms"] < 60_000
    assert capture.is_file()
    assert summary["baseline_cost_usd"] >= summary["routed_cost_usd"]


def test_unreachable_gateway_is_blocked_not_green() -> None:
    summary = run_routing_demo(gateway_base_url="http://127.0.0.1:9/v1", execute=False)
    assert summary["status"] == "blocked"
    assert summary["mode"] == "live"
    assert summary["request_count"] == 0
