"""Tests for verdict/arena/snapshot.py — load_snapshot shape validation."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.arena.snapshot import _validate_snapshot_shape, load_snapshot

# ---------------------------------------------------------------------------
# _validate_snapshot_shape unit tests
# ---------------------------------------------------------------------------


def test_validate_snapshot_shape_accepts_empty_records() -> None:
    """A dict with an empty records dict is valid."""
    err = _validate_snapshot_shape({"records": {}})
    assert err is None


def test_validate_snapshot_shape_rejects_list() -> None:
    """A top-level list is invalid."""
    err = _validate_snapshot_shape([{"records": {}}])
    assert err is not None
    assert "list" in err


def test_validate_snapshot_shape_rejects_non_dict_records() -> None:
    """records must be a dict, not a list."""
    err = _validate_snapshot_shape({"records": ["something"]})
    assert err is not None
    assert "records" in err


def test_validate_snapshot_shape_rejects_non_dict_config() -> None:
    """records[sid] must be a dict, not a string."""
    err = _validate_snapshot_shape({"records": {"openai/gpt-4o": "bad"}})
    assert err is not None


def test_validate_snapshot_shape_accepts_nested_dicts() -> None:
    """A properly nested records structure passes validation."""
    data = {
        "records": {
            "openai/gpt-4o": {
                "arena_text": {
                    "default": {
                        "raw": 1200.0,
                        "lo": 1150.0,
                        "hi": 1250.0,
                        "n": 10000.0,
                        "published": "2025-01-01",
                        "via": "canonical",
                    }
                }
            }
        }
    }
    assert _validate_snapshot_shape(data) is None


# ---------------------------------------------------------------------------
# load_snapshot integration tests (file-based)
# ---------------------------------------------------------------------------


def test_load_snapshot_returns_none_for_list_shaped_file(tmp_path: Path) -> None:
    """A file containing a JSON array (not object) returns None, never raises."""
    bad_file = tmp_path / "arena-snapshot.json"
    bad_file.write_text(json.dumps([{"records": {}}]), encoding="utf-8")
    result = load_snapshot(bad_file)
    assert result is None


def test_load_snapshot_returns_none_for_mistyped_records(tmp_path: Path) -> None:
    """A file where records is a list returns None, never raises."""
    bad_file = tmp_path / "arena-snapshot.json"
    bad_file.write_text(json.dumps({"records": ["bad"]}), encoding="utf-8")
    result = load_snapshot(bad_file)
    assert result is None


def test_load_snapshot_returns_none_for_missing_file(tmp_path: Path) -> None:
    """A path that does not exist returns None, never raises."""
    result = load_snapshot(tmp_path / "no-such-file.json")
    assert result is None


def test_load_snapshot_returns_none_for_invalid_json(tmp_path: Path) -> None:
    """A file with invalid JSON returns None, never raises."""
    bad_file = tmp_path / "arena-snapshot.json"
    bad_file.write_text("{not valid json", encoding="utf-8")
    result = load_snapshot(bad_file)
    assert result is None
