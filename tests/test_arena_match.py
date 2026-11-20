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
