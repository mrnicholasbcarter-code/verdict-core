from __future__ import annotations

from pathlib import Path

import pytest

from verdict.context_intelligence import ContextIntelligenceError
from verdict.context_lift import run_context_lift, unaided_prompt
from verdict.live_routing import LiveSurfaceBlocked
from verdict.live_routing_gateway import DEFAULT_GATEWAY


def test_live_paired_lift_or_block(tmp_path: Path) -> None:
    try:
        result = run_context_lift(base_url=DEFAULT_GATEWAY, proof_root=tmp_path)
    except (LiveSurfaceBlocked, ContextIntelligenceError) as exc:
        code = getattr(exc, "code", "live_surface_blocked")
        if code == "live_surface_blocked":
            pytest.skip(f"live_surface_blocked: {exc}")
        raise
    receipt = result["receipt"]
    if receipt.get("conclusion") == "blocked":
        reason = receipt.get("block_reason")
        if reason in {"live_surface_blocked", "no_cheaper_identity"}:
            pytest.skip(f"{reason}: {receipt}")
        pytest.fail(f"paired lift blocked: {receipt}")
    assert receipt["endpoint"] == DEFAULT_GATEWAY
    assert receipt["identity_id"]
    assert receipt["cost_class"] in {"local", "free", "cheaper"}
    assert receipt["conclusion"] in {"lift", "no_lift"}
    assert receipt["unaided_passed"] is not None
    assert receipt["packed_passed"] is not None
    assert receipt.get("pack_digest")
    if receipt["conclusion"] == "lift":
        assert receipt["unaided_passed"] is False
        assert receipt["packed_passed"] is True
    dumped = str(receipt)
    assert "sk-" not in dumped
    assert unaided_prompt() not in dumped
