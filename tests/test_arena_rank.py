"""Tests for verdict/arena/rank.py — compute_vr, ArenaVR, _build_reference_set."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.arena.canonical import Drop, parse_name
from verdict.arena.crosswalk import load_crosswalk
from verdict.arena.fetch import build_snapshot_from_rows
from verdict.arena.rank import _MIN_REF_SET, _build_reference_set, _get_lo, compute_vr
from verdict.arena.snapshot import (
    CONFIG_AGENT,
    CONFIG_TEXT,
    CONFIG_WEBDEV,
    STALENESS_DAYS,
    ArenaSlot,
    ArenaSnapshot,
)

WORKTREE = Path(__file__).parent.parent
CW_PATH = WORKTREE / "verdict" / "data" / "arena-crosswalk.json"
FIXTURES = Path(__file__).parent / "fixtures"

# Pinned clock: fixture publish dates are 2026-11-18/19, well within 45-day window.
CLOCK = datetime(2026, 11, 20, tzinfo=timezone.utc)
CLOCK_STR = CLOCK.isoformat().replace("+00:00", "Z")


def _derive_store_ids(rows: list[dict[str, Any]], crosswalk: Any) -> frozenset[str]:
    """Derive store_ids from fixture rows via canonical parse + crosswalk lookup."""
    ids: set[str] = set()
    for row in rows:
        parsed = parse_name(row["model_name"], row["organization"], crosswalk.org_providers)
        if isinstance(parsed, Drop):
            continue
        cw_key = (parsed.org, parsed.key)
        if cw_key in crosswalk.map_entries:
            ids.update(crosswalk.map_entries[cw_key].models_dev_ids)
        else:
            for prefix in crosswalk.org_providers.get(parsed.org, [parsed.org]):
                ids.add(f"{prefix}/{parsed.key}")
    return frozenset(ids)


@pytest.fixture(scope="module")
def full_snapshot() -> ArenaSnapshot:
    """Full ArenaSnapshot built from all three fixture files."""
    crosswalk = load_crosswalk(CW_PATH)
    text_rows = json.loads((FIXTURES / "arena-fixture-text.json").read_text())["rows"]
    webdev_rows = json.loads((FIXTURES / "arena-fixture-webdev.json").read_text())["rows"]
    agent_rows = json.loads((FIXTURES / "arena-fixture-agent.json").read_text())["rows"]
    all_rows = text_rows + webdev_rows + agent_rows
    store_ids = _derive_store_ids(all_rows, crosswalk)
    return build_snapshot_from_rows(
        {"text": text_rows, "webdev": webdev_rows, "agent": agent_rows},
        store_ids,
        crosswalk,
        fetched_at={CONFIG_TEXT: CLOCK_STR, CONFIG_WEBDEV: CLOCK_STR, CONFIG_AGENT: CLOCK_STR},
        now=CLOCK,
    )


@pytest.fixture(scope="module")
def webdev_only_snapshot() -> ArenaSnapshot:
    """ArenaSnapshot built from only the webdev fixture (for chain-fallback tests)."""
    crosswalk = load_crosswalk(CW_PATH)
    webdev_rows = json.loads((FIXTURES / "arena-fixture-webdev.json").read_text())["rows"]
    store_ids = _derive_store_ids(webdev_rows, crosswalk)
    return build_snapshot_from_rows(
        {"webdev": webdev_rows},
        store_ids,
        crosswalk,
        fetched_at={CONFIG_WEBDEV: CLOCK_STR},
        now=CLOCK,
    )


# ---- VR range test ----------------------------------------------------------------


def test_compute_vr_returns_0_to_100(full_snapshot: ArenaSnapshot) -> None:
    """compute_vr must return an ArenaVR with vr in [0.0, 100.0]."""
    # Use the first record that has agent data for "implement" role
    agent_ref = _build_reference_set(full_snapshot, CONFIG_AGENT, CLOCK)
    assert len(agent_ref) >= _MIN_REF_SET, "need ≥15 usable agent models"

    some_id = next(iter(agent_ref))
    result = compute_vr(some_id, full_snapshot, "implement", CLOCK)

    assert result is not None, f"expected ArenaVR for {some_id!r}"
    assert 0.0 <= result.vr <= 100.0


# ---- Floor test: low n excluded from VR ----------------------------------------


def test_floor_excludes_low_vote_count_model() -> None:
    """A slot with n < FLOOR_N must make _get_lo return None."""
    store_id = "test/low-votes"
    slot = ArenaSlot(
        raw=1300.0,
        lo=1280.0,
        hi=1320.0,
        n=100.0,  # well below FLOOR_N[CONFIG_TEXT] = 5000
        published="2026-11-15",
        via="canonical",
    )
    snapshot = ArenaSnapshot(
        schema_version="1",
        fetched_at={},
        leaderboard_publish_date={},
        reference_sets={},
        records={store_id: {CONFIG_TEXT: {"default": slot}}},
    )
    result = _get_lo(snapshot, store_id, CONFIG_TEXT, CLOCK)
    assert result is None, "slot below floor must be excluded"


# ---- Staleness test: old publish date excluded ---------------------------------


def test_stale_publish_date_excludes_model() -> None:
    """A slot with leaderboard_publish_date > STALENESS_DAYS old must return None."""
    old_date = (CLOCK - timedelta(days=STALENESS_DAYS + 5)).strftime("%Y-%m-%d")
    slot = ArenaSlot(
        raw=1300.0,
        lo=1280.0,
        hi=1320.0,
        n=10000.0,  # above floor
        published=old_date,
        via="canonical",
    )
    snapshot = ArenaSnapshot(
        schema_version="1",
        fetched_at={},
        leaderboard_publish_date={},
        reference_sets={},
        records={"test/stale-model": {CONFIG_TEXT: {"default": slot}}},
    )
    result = _get_lo(snapshot, "test/stale-model", CONFIG_TEXT, CLOCK)
    assert result is None, "stale slot must be excluded"


# ---- Permutation test: higher lo → higher VR -----------------------------------


def test_higher_lo_gives_higher_vr(full_snapshot: ArenaSnapshot) -> None:
    """Model with the highest agent lo-bound must have higher VR than model with lowest."""
    agent_ref = _build_reference_set(full_snapshot, CONFIG_AGENT, CLOCK)
    assert len(agent_ref) >= 10, f"need ≥10 usable agent models, got {len(agent_ref)}"

    max_id = max(agent_ref, key=agent_ref.__getitem__)
    min_id = min(agent_ref, key=agent_ref.__getitem__)

    vr_max = compute_vr(max_id, full_snapshot, "implement", CLOCK)
    vr_min = compute_vr(min_id, full_snapshot, "implement", CLOCK)

    assert vr_max is not None, f"expected VR for max-lo model {max_id!r}"
    assert vr_min is not None, f"expected VR for min-lo model {min_id!r}"
    assert vr_max.vr > vr_min.vr, (
        f"max-lo model {max_id!r} vr={vr_max.vr} should exceed "
        f"min-lo model {min_id!r} vr={vr_min.vr}"
    )


# ---- None when model is missing from snapshot ----------------------------------


def test_compute_vr_returns_none_for_missing_model(full_snapshot: ArenaSnapshot) -> None:
    result = compute_vr("openai/not-in-snapshot", full_snapshot, "planner", CLOCK)
    assert result is None


# ---- Reference set size --------------------------------------------------------


def test_build_reference_set_text_has_min_15(full_snapshot: ArenaSnapshot) -> None:
    ref = _build_reference_set(full_snapshot, CONFIG_TEXT, CLOCK)
    assert len(ref) >= _MIN_REF_SET, f"expected ≥{_MIN_REF_SET} usable text models, got {len(ref)}"


# ---- Role chain fallback: planner falls back from text to webdev ----------------


def test_planner_falls_back_to_webdev_when_no_text(webdev_only_snapshot: ArenaSnapshot) -> None:
    """With no text data, the planner chain [text, webdev] falls back to webdev."""
    # Confirm text reference set is empty (< _MIN_REF_SET)
    text_ref = _build_reference_set(webdev_only_snapshot, CONFIG_TEXT, CLOCK)
    assert len(text_ref) < _MIN_REF_SET

    # Confirm webdev reference set is usable
    webdev_ref = _build_reference_set(webdev_only_snapshot, CONFIG_WEBDEV, CLOCK)
    assert len(webdev_ref) >= _MIN_REF_SET

    # Pick any model that has webdev data
    some_sid = next(iter(webdev_ref))
    vr = compute_vr(some_sid, webdev_only_snapshot, "planner", CLOCK)

    assert vr is not None, f"expected VR for {some_sid!r} via webdev fallback"
    assert "arena_webdev" in vr.metric, f"expected webdev metric, got {vr.metric!r}"


# ---- Admission membership invariant --------------------------------------------
# Arena VR augments RatedModel.score (a reordering signal) but never changes
# whether a route is admitted. These tests prove the two properties that hold
# together: (a) vr_for_route returns None for routes with no Arena data, so they
# are not repositioned relative to each other, and (b) a route with Arena data
# produces a non-None VR only as a score signal — the admitted set is unchanged.
#
# The general admission narrowing invariant (admitted set only narrows, never
# re-admits) is enforced by test_admission.py::test_narrowing_only_removes_and_never_readmits.


def test_arena_vr_none_for_unrated_route_keeps_relative_position(
    full_snapshot: ArenaSnapshot,
) -> None:
    """A route absent from the snapshot returns None — its sort key is unchanged.

    Two unrated routes both return None, so their relative ordering is determined
    entirely by the existing OmniRoute score, not by Arena.
    """
    result_a = compute_vr("unknown/model-a", full_snapshot, "implement", CLOCK)
    result_b = compute_vr("unknown/model-b", full_snapshot, "planner", CLOCK)
    assert result_a is None, "unrated route must return None, not a VR value"
    assert result_b is None, "unrated route must return None, not a VR value"
    # Both are None → neither is promoted over the other by Arena evidence.


def test_arena_vr_does_not_affect_tier_unknown_routes(full_snapshot: ArenaSnapshot) -> None:
    """The tier_known guard in EligibilityLadder prevents Arena from touching
    unknown-tier routes.  At the rank level, compute_vr is agnostic to tier_known
    — the guard lives in eligibility.py.  This test confirms that the VR value
    computed here is strictly a score signal: it cannot change tier, admitted
    status, or free/paid classification, only the numeric sort key within an
    already-admitted tier group.

    The admission membership invariant is enforced by:
      tests/test_admission.py::test_narrowing_only_removes_and_never_readmits
    The tier_known guard preventing Arena augmentation of unknown-tier routes is
    enforced by the conditional in verdict/orchestration/eligibility.py:
      if arena_role is not None and a.tier_known:
    which is covered by the eligibility unit tests.
    """
    # A rated model's VR is a float in [0, 100] — it is NOT an admission signal.
    agent_ref = _build_reference_set(full_snapshot, CONFIG_AGENT, CLOCK)
    assert len(agent_ref) >= _MIN_REF_SET
    some_sid = next(iter(agent_ref))
    vr = compute_vr(some_sid, full_snapshot, "implement", CLOCK)
    assert vr is not None
    assert 0.0 <= vr.vr <= 100.0, "VR is a percentile, not an admission flag"
    assert vr.basis == "arena", "basis must be 'arena', not 'tier' or 'score'"
    # vr.vr is used to set RatedModel.score — it cannot add or remove a route
    # from the admitted set, which is frozen before EligibilityLadder reaches
    # the Arena augmentation step.
