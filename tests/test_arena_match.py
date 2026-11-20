"""Tests for verdict/arena/match.py — build_store_index, match_rows, RestrictedIndex."""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict.arena.canonical import (
    DROP_CROSSWALK_DROP,
    DROP_NO_STORE_RECORD,
    DROP_VARIANT_COLLISION,
)
from verdict.arena.crosswalk import Crosswalk, CrosswalkDropEntry, load_crosswalk
from verdict.arena.match import build_store_index, match_rows

WORKTREE = Path(__file__).parent.parent
CW_PATH = WORKTREE / "verdict" / "data" / "arena-crosswalk.json"


def _make_text_row(
    model_name: str, organization: str, *, vote_count: float = 10000.0, rank: int = 1
) -> dict[str, object]:
    """Minimal valid Arena text row."""
    return {
        "model_name": model_name,
        "organization": organization,
        "category": "overall",
        "_config": "text",
        "rating": 1300.0,
        "rating_lower": 1280.0,
        "rating_upper": 1320.0,
        "vote_count": vote_count,
        "rank": rank,
        "leaderboard_publish_date": "2026-11-15",
    }


# ---- RestrictedIndex raises on iteration ---------------------------------------


def test_restricted_index_iter_raises() -> None:
    idx = build_store_index(frozenset({"openai/gpt-4o"}))
    with pytest.raises(RuntimeError):
        iter(idx)


def test_restricted_index_len_raises() -> None:
    idx = build_store_index(frozenset({"openai/gpt-4o"}))
    with pytest.raises(RuntimeError):
        len(idx)  # type: ignore[arg-type]


def test_restricted_index_keys_raises() -> None:
    idx = build_store_index(frozenset({"openai/gpt-4o"}))
    with pytest.raises(RuntimeError):
        idx.keys()


def test_restricted_index_values_raises() -> None:
    idx = build_store_index(frozenset({"openai/gpt-4o"}))
    with pytest.raises(RuntimeError):
        idx.values()


def test_restricted_index_items_raises() -> None:
    idx = build_store_index(frozenset({"openai/gpt-4o"}))
    with pytest.raises(RuntimeError):
        idx.items()


# ---- No-fuzzy proof: one char off → no store record --------------------------


def test_no_fuzzy_one_char_perturbation_drops() -> None:
    """A single appended char changes the key — no fuzzy walk-around."""
    crosswalk = load_crosswalk(CW_PATH)
    store_ids = frozenset({"openai/gpt-4o"})
    index = build_store_index(store_ids)

    # "GPT-4oz" parses to key "gpt-4oz", not "gpt-4o"
    rows = [_make_text_row("GPT-4oz", "openai")]
    matches, drops = match_rows(rows, index, crosswalk)

    assert matches == []
    assert len(drops) == 1
    assert drops[0].reason == DROP_NO_STORE_RECORD


# ---- Exact match succeeds -------------------------------------------------------


def test_exact_match_returns_match() -> None:
    crosswalk = load_crosswalk(CW_PATH)
    store_ids = frozenset({"anthropic/claude-opus-5"})
    index = build_store_index(store_ids)

    rows = [_make_text_row("Claude Opus 5", "anthropic")]
    matches, drops = match_rows(rows, index, crosswalk)

    assert drops == []
    assert len(matches) == 1
    assert matches[0].store_ids == ("anthropic/claude-opus-5",)


# ---- Effort slot is preserved in Parsed ----------------------------------------


def test_effort_slot_recorded_in_parsed() -> None:
    crosswalk = load_crosswalk(CW_PATH)
    store_ids = frozenset({"anthropic/claude-opus-5"})
    index = build_store_index(store_ids)

    rows = [_make_text_row("Claude Opus 5 (Max)", "anthropic")]
    matches, _drops = match_rows(rows, index, crosswalk)

    assert len(matches) == 1
    assert matches[0].parsed.effort == "max"


# ---- Variant collision: same (store_id, config, effort) → both dropped ---------


def test_variant_collision_drops_both() -> None:
    """Two rows that parse to the same (store_id, config, effort) → variant_collision."""
    # "Claude Test (High)" and "Claude Test (High, 128k)" both parse to
    # key="claude-test", effort="high", so collision_key is the same.
    mini_cw = Crosswalk(
        schema_version="1",
        org_providers={"anthropic": ["anthropic"]},
        map_entries={},
        drop_entries={},
        all_keys=(),
    )
    store_ids = frozenset({"anthropic/claude-test"})
    index = build_store_index(store_ids)

    rows = [
        _make_text_row("Claude Test (High)", "anthropic", rank=1),
        _make_text_row("Claude Test (High, 128k)", "anthropic", rank=2),
    ]
    matches, drops = match_rows(rows, index, mini_cw)

    # Both rows map to the same (store_id, config, effort) → both become drops
    assert matches == [], f"Expected no matches, got {matches}"
    reasons = {d.reason for d in drops}
    assert DROP_VARIANT_COLLISION in reasons


# ---- Crosswalk drop entry produces crosswalk_drop --------------------------------


def test_crosswalk_drop_entry_produces_drop() -> None:
    """An explicit crosswalk drop entry must yield reason=crosswalk_drop."""
    mini_cw = Crosswalk(
        schema_version="1",
        org_providers={"openai": ["openai"]},
        map_entries={},
        drop_entries={
            ("openai", "gpt-4o"): CrosswalkDropEntry(
                arena_org="openai",
                arena_key="gpt-4o",
                arena_name="GPT-4o",
                reason="test: crosswalk explicit drop",
            )
        },
        all_keys=(("openai", "gpt-4o"),),
    )
    # Even if gpt-4o is in the store index, the crosswalk drop takes priority
    index = build_store_index(frozenset({"openai/gpt-4o"}))

    rows = [_make_text_row("GPT-4o", "openai")]
    matches, drops = match_rows(rows, index, mini_cw)

    assert matches == []
    assert len(drops) == 1
    assert drops[0].reason == DROP_CROSSWALK_DROP


# ---- Twin collision: all store_ids in a twin bucket must be caught -------------


def test_collision_detection_covers_all_store_ids_for_twins() -> None:
    """Twin store_ids (same slug, different variant) must be caught by per-sid collision check.

    Bug case: store has two twins 'a/m' and 'a/m.0' (same slug 'a/m').
    Row 1 (canonical) → store_ids_found = ('a/m', 'a/m.0') → registers both per-sid keys.
    Row 2 (explicit map to just 'a/m.0') → must collide on the overlapping sid,
    even though its store_ids_found[0] differs from row 1's store_ids_found[0].
    """
    from verdict.arena.crosswalk import Crosswalk, CrosswalkMapEntry

    # 'anthropic/claude-m' and 'anthropic/claude-m.0' have the same slug 'claude-m' → twins
    twin_a = "anthropic/claude-m"
    twin_b = "anthropic/claude-m.0"
    store_ids = frozenset({twin_a, twin_b})
    index = build_store_index(store_ids)

    mini_cw = Crosswalk(
        schema_version="1",
        org_providers={"Anthropic": ["anthropic"]},
        map_entries={
            # Explicit map: 'Claude M' (Anthropic) → only twin_b
            ("Anthropic", "claude-m"): CrosswalkMapEntry(
                arena_org="Anthropic",
                arena_key="claude-m",
                arena_name="Claude M",
                models_dev_ids=(twin_b,),
                source="test",
                verified="2026-11-01",
            )
        },
        drop_entries={},
        all_keys=(("Anthropic", "claude-m"),),
    )

    rows = [
        # Row 1: canonical join → both twins
        _make_text_row("Claude M", "Anthropic", rank=1),
        # Row 2: explicit map → only twin_b; collides with row 1 on twin_b
        _make_text_row("Claude M", "Anthropic", rank=2),
    ]
    # With two identical rows the second hits the same crosswalk path and collides on twin_b
    matches, drops = match_rows(rows, index, mini_cw)

    # Both map to the same slot via overlap → both should be variant_collision drops
    reasons = [d.reason for d in drops]
    assert DROP_VARIANT_COLLISION in reasons, f"expected variant_collision in drops={drops}"
    assert len(matches) == 0, f"expected 0 matches when collision detected, got {matches}"


# ---- seen_per_sid must be cleaned up after collision removal -------------------


def test_third_row_after_collision_does_not_false_collide() -> None:
    """After rows A+B collide, row C sharing a store_id with A must NOT be
    falsely dropped as a variant_collision.

    Regression for the bug where seen_per_sid entries for the dropped match
    (A) were not removed, causing C to hit A's stale (sid, config, effort) key.

    Setup: store has two IDs — 'anthropic/model-a' and 'anthropic/model-b'.
    Row 1 (default effort): resolves to model-a and model-b (twins, same slug).
    Row 2 (default effort): resolves to model-a only (explicit map) → collides with row 1.
    Row 3 (default effort): resolves to model-b only (explicit map, different name) →
      must NOT collide; model-b's seen_per_sid entry must have been cleaned up.
    """
    from verdict.arena.crosswalk import Crosswalk, CrosswalkMapEntry

    sid_a = "anthropic/model-ab"
    sid_b = "anthropic/model-ab.0"
    store_ids = frozenset({sid_a, sid_b})
    index = build_store_index(store_ids)

    mini_cw = Crosswalk(
        schema_version="1",
        org_providers={"Anthropic": ["anthropic"]},
        map_entries={
            # Row 2: explicit → only sid_a → collides with row 1 on sid_a
            ("Anthropic", "model-ab-row2"): CrosswalkMapEntry(
                arena_org="Anthropic",
                arena_key="model-ab-row2",
                arena_name="Model AB Row2",
                models_dev_ids=(sid_a,),
                source="test",
                verified="2026-11-01",
            ),
            # Row 3: explicit → only sid_b → must NOT collide after cleanup
            ("Anthropic", "model-ab-row3"): CrosswalkMapEntry(
                arena_org="Anthropic",
                arena_key="model-ab-row3",
                arena_name="Model AB Row3",
                models_dev_ids=(sid_b,),
                source="test",
                verified="2026-11-01",
            ),
        },
        drop_entries={},
        all_keys=(("Anthropic", "model-ab-row2"), ("Anthropic", "model-ab-row3")),
    )

    rows = [
        # Row 1: canonical join → both twins (sid_a, sid_b)
        _make_text_row("Model AB", "Anthropic", rank=1),
        # Row 2: explicit → sid_a only → collides with row 1 on sid_a
        _make_text_row("Model AB Row2", "Anthropic", rank=2),
        # Row 3: explicit → sid_b only → must land as a valid match
        _make_text_row("Model AB Row3", "Anthropic", rank=3),
    ]
    matches, drops = match_rows(rows, index, mini_cw)

    # Rows 1 and 2 collide; row 3 must be a valid match (not a collision drop)
    assert len(matches) == 1, f"expected 1 match (row 3), got {matches}"
    assert matches[0].model_name == "Model AB Row3"
    collision_drops = [d for d in drops if d.reason == DROP_VARIANT_COLLISION]
    # Rows 1 and 2 each get a collision drop
    assert len(collision_drops) == 2, f"expected 2 collision drops, got {collision_drops}"


# ---- retroactive collision drop keeps the removed match's own organization -----


def test_retroactive_collision_drop_keeps_removed_match_organization() -> None:
    """Two Arena orgs resolving to the same store slot both drop; each drop names its own org."""
    store_ids = frozenset({"anthropic/claude-m"})
    index = build_store_index(store_ids)
    mini_cw = Crosswalk(
        schema_version="1",
        org_providers={"Anthropic": ["anthropic"], "Anthropic PBC": ["anthropic"]},
        map_entries={},
        drop_entries={},
        all_keys=(),
    )
    rows = [
        _make_text_row("Claude M", "Anthropic", rank=1),
        _make_text_row("Claude M", "Anthropic PBC", rank=2),
    ]

    matches, drops = match_rows(rows, index, mini_cw)

    collision_orgs = sorted(d.organization for d in drops if d.reason == DROP_VARIANT_COLLISION)
    assert matches == []
    assert collision_orgs == ["Anthropic", "Anthropic PBC"], drops
