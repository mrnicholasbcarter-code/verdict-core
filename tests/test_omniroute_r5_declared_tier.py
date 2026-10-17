"""R5 declared-tier writes at admit time (spec v6.1 rev 2 phase g, O3 bands)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.classifier import classify, classify_known
from verdict.free_tier_admit import (
    RANKING_MODE_ENV,
    CatalogIdentity,
    FreeTierAdmitReceipt,
    OmniRouteAdmitSnapshot,
    declared_tier_from_elo,
    load_declared_tiers,
)

FRONTIER = "anthropic/claude-opus-4"


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    path = tmp_path / "declared-tiers.json"
    path.write_text(json.dumps(data))
    return path


def _admit_tiers(declared_ids: tuple[str, ...]) -> dict[str, int]:
    snap = OmniRouteAdmitSnapshot(
        catalog=tuple(CatalogIdentity(m, m.split("/", 1)[0]) for m in declared_ids),
        free_tier=(),
        connections=(),
    )
    receipt = FreeTierAdmitReceipt(
        admitted=declared_ids,
        exclusions=(),
        chosen=None,
        empty_intersection=False,
        active_providers=(),
        free_tier_providers=(),
    )
    return {m.id: m.capability_tier for m in receipt.as_eligibility_result(snap).admitted}


def test_declared_tier_written_tier0_pattern_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert classify(FRONTIER) == 0
    monkeypatch.setenv(RANKING_MODE_ENV, "write")
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    from verdict.catalog_cache import default_catalog_cache_path

    path = default_catalog_cache_path().parent / "declared-tiers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"nvidia/z-ai/glm-5.3": 1479, FRONTIER: 1416}))
    tiers = _admit_tiers(("nvidia/z-ai/glm-5.3", FRONTIER))
    assert tiers == {"nvidia/z-ai/glm-5.3": 1, FRONTIER: 0}


def test_pattern_fallback_when_unmapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(RANKING_MODE_ENV, raising=False)
    models = ("nvidia/z-ai/glm-5.3", "ollama-cloud/gemma4:31b", "kc/reflection/beam-free")
    assert _admit_tiers(models) == {m: classify(m) for m in models}


def test_declared_overrides_pattern_both_directions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RANKING_MODE_ENV, "write")
    up, down = "ollama-cloud/gemma4:31b", "charm-hyper/kimi-k2-thinking"
    declared = load_declared_tiers(_write(tmp_path, {up: 1452, down: 1430}))
    assert declared == {up: 1, down: 2}
    assert classify(up, overrides=declared) == 1
    assert classify(down, overrides=declared) == 2


def test_declared_tier0_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert declared_tier_from_elo(9999) == 1
    assert declared_tier_from_elo(1450) == 1
    assert declared_tier_from_elo(1449) == 2
    assert declared_tier_from_elo(1380) == 2
    assert declared_tier_from_elo(1379) is None
    monkeypatch.setenv(RANKING_MODE_ENV, "write")
    path = _write(tmp_path, {"x/low": 1300, "x/bad": "0", "x/flag": True, FRONTIER: 1500})
    assert load_declared_tiers(path) == {}
    monkeypatch.delenv(RANKING_MODE_ENV)
    assert load_declared_tiers(_write(tmp_path, {"nvidia/z-ai/glm-5.3": 1479})) == {}


def test_unknown_unranked_stays_none() -> None:
    from verdict.orchestration.eligibility import _capability_tier

    assert classify_known("kc/reflection/beam-free") is None
    assert _capability_tier({}, "kc/reflection/beam-free") is None
