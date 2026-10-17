"""S12 name mapping + S11 ingest in shadow mode (spec v6.1 phase d): no tier writes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    strict=True, raises=ImportError, reason="phase d: ranking/names not implemented yet"
)
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def test_name_map_family_version_variant() -> None:
    from verdict.omniroute_pipeline.names import map_name  # type: ignore[import-not-found]

    m = map_name("nvidia/z-ai/glm-5.3")
    assert (m.family, m.version, m.variant) == ("glm", "5.3", None)


def test_unmapped_reported_never_guessed() -> None:
    from verdict.omniroute_pipeline.names import map_name  # type: ignore[import-not-found]

    m = map_name("kc/reflection/beam-free")
    assert m.mapped is False
    assert m.elo is None


def test_shadow_mode_writes_no_capability_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    from verdict.omniroute_pipeline.ranking import run_ranking  # type: ignore[import-not-found]

    import verdict.free_tier_admit as fta

    before = fta.admit_free_tier_active
    out = run_ranking(routes=["nvidia/z-ai/glm-5.3"], elo={"glm-5.3": 1479}, mode="shadow")
    assert out.tier_writes == 0
    assert out.declared_tiers["nvidia/z-ai/glm-5.3"] is not None
    assert fta.admit_free_tier_active is before


def test_ranking_last_good_daily(tmp_path: Path) -> None:
    from verdict.omniroute_pipeline.ranking import (
        load_ranking_cache,  # type: ignore[import-not-found]
    )

    assert load_ranking_cache(tmp_path / "rank.json", now=NOW + timedelta(days=2)) is None
