"""R5 declared-tier writes at admit time (spec v6.1 phase g, O3 bands)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.classifier import classify
from verdict.omniroute_pipeline.declared_tier import (
    RANKING_MODE_ENV,
    declared_tier_from_elo,
    load_declared_tiers,
    resolve_capability_tier,
)


def test_declared_tier_written_tier0_pattern_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert declared_tier_from_elo(1479) == 1
    assert declared_tier_from_elo(1450) == 1
    assert declared_tier_from_elo(1416) == 2
    assert declared_tier_from_elo(1379) is None
    assert declared_tier_from_elo(9999) != 0
    path = tmp_path / "declared-tiers.json"
    path.write_text(json.dumps({"nvidia/z-ai/glm-5.3": 1, "x/forged": 0}))
    monkeypatch.delenv(RANKING_MODE_ENV, raising=False)
    assert load_declared_tiers(path) == {}
    monkeypatch.setenv(RANKING_MODE_ENV, "write")
    declared = load_declared_tiers(path)
    assert declared == {"nvidia/z-ai/glm-5.3": 1}
    assert resolve_capability_tier("nvidia/z-ai/glm-5.3", declared) == 1
    frontier = "anthropic/claude-opus-4"
    assert classify(frontier) == 0
    assert resolve_capability_tier(frontier, {frontier: 2}) == 0


def test_pattern_fallback_when_unmapped() -> None:
    for model in ("kc/reflection/beam-free", "nvidia/z-ai/glm-5.3"):
        assert resolve_capability_tier(model, {}) == classify(model)


def test_admit_uses_resolver_and_matches_classify_when_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.free_tier_admit import (
        CatalogIdentity,
        FreeTierAdmitReceipt,
        OmniRouteAdmitSnapshot,
    )

    monkeypatch.delenv(RANKING_MODE_ENV, raising=False)
    snap = OmniRouteAdmitSnapshot(
        catalog=(CatalogIdentity("nvidia/z-ai/glm-5.3", "nvidia"),), free_tier=(), connections=()
    )
    receipt = FreeTierAdmitReceipt(
        admitted=("nvidia/z-ai/glm-5.3",),
        exclusions=(),
        chosen=None,
        empty_intersection=False,
        active_providers=(),
        free_tier_providers=(),
    )
    result = receipt.as_eligibility_result(snap)
    tiers = [m.capability_tier for m in result.admitted if m.id == "nvidia/z-ai/glm-5.3"]
    assert tiers == [classify("nvidia/z-ai/glm-5.3")]
