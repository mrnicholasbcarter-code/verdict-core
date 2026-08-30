from __future__ import annotations

import pytest

from verdict.live_routing import LiveSurfaceBlocked
from verdict.live_routing_gateway import DEFAULT_GATEWAY, fetch_models
from verdict.live_routing_run import run_live_routing


def test_live_catalog_fetch_or_block() -> None:
    try:
        identities, _captured = fetch_models(DEFAULT_GATEWAY)
    except LiveSurfaceBlocked as exc:
        pytest.skip(f"live_surface_blocked: {exc}")
    assert identities
    assert all(item.identity_id for item in identities)


def test_live_named_check_or_block() -> None:
    try:
        result = run_live_routing()
    except LiveSurfaceBlocked as exc:
        pytest.skip(f"live_surface_blocked: {exc}")
    receipt = result["receipt"]
    assert receipt["endpoint"] == DEFAULT_GATEWAY
    assert receipt["checker_passed"] is True
    assert receipt["chosen_id"]
    assert receipt["attempts"]
