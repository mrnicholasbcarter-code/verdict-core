"""Sanitized disposable completion publication, with only temporary local stores."""

from __future__ import annotations

import json
import stat
from collections import Counter
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_verified_models_projection import NOW, conn, row
from verdict.actions.verified_models import StorePaths, VerifiedSnapshotAdapter
from verdict.tui_completion import SCHEMA, complete, default_command_specs
from verdict.tui_completion_snapshot import (
    MAX_BYTES,
    load_snapshot,
    local_projection,
    publish_snapshot,
)


def view(count: int = 1) -> dict:
    return {
        "generated_at": NOW.isoformat(),
        "rows": [
            {
                "route_id": f"cc/model-{i:05}",
                "provider": "cc",
                "status": "VERIFIED",
                "identity": "verified",
                "coding_ok": False,
                "checked_at": (NOW - timedelta(seconds=10)).isoformat(),
                "fresh_until": (NOW + timedelta(minutes=5)).isoformat(),
                "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
                "apiKey": "sk-private",
                "account_id": "private-account",
                "reason": "https://secret.invalid",
            }
            for i in range(count)
        ],
    }


def test_publish_sanitizes_atomic_private_file(tmp_path: Path) -> None:
    path = tmp_path / "completion.json"
    assert publish_snapshot(view(), path) is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = path.read_text()
    for secret in ("sk-private", "account", "https://", "apiKey", "reason"):
        assert secret not in raw
    snapshot = load_snapshot(path, now=NOW)
    assert len(snapshot.model_rows) == 1
    assert snapshot.source_errors == ()
    assert snapshot.model_rows[0]["route_id"] == "cc/model-00000"
    assert (
        len(complete("/probe cc/", commands=default_command_specs(), snapshot=snapshot, now=NOW))
        == 1
    )
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize(
    "kind", ["missing", "corrupt", "schema", "future", "oversize", "mode", "symlink", "unsafe_rows"]
)
def test_invalid_snapshot_stays_offline(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "snapshot.json"
    publish_snapshot(view(), path)
    if kind == "missing":
        path.unlink()
    elif kind == "corrupt":
        path.write_text("no JSON")
    elif kind == "oversize":
        path.write_bytes(b" " * (MAX_BYTES + 1))
    elif kind == "mode":
        path.chmod(0o644)
    elif kind == "symlink":
        moved = tmp_path / "target"
        path.rename(moved)
        path.symlink_to(moved)
    else:
        value = json.loads(path.read_text())
        if kind == "schema":
            value["schema"] = "unsupported"
        elif kind == "future":
            value["generated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        else:
            value["model_rows"][0]["apiKey"] = "sk-private"
        path.write_text(json.dumps(value))
    with patch(
        "verdict.actions.verified_models.VerifiedSnapshotAdapter._load_metadata",
        side_effect=AssertionError("live fallback"),
    ):
        snapshot = load_snapshot(path, now=NOW)
    assert not snapshot.model_rows
    assert snapshot.source_errors


def test_consistent_full_local_catalog_not_only_rendered_page(tmp_path: Path) -> None:
    paths = StorePaths.defaults(tmp_path)
    catalog = {
        "inventory_rows": [row(f"cc/model-{i:05}") for i in range(6773)],
        "connections": [conn("cc")],
    }
    paths.catalog.write_text(json.dumps(catalog))
    adapter = VerifiedSnapshotAdapter(
        "http://127.0.0.1:20128", paths, local_only=True, clock=lambda: NOW
    )
    reads: Counter[Path] = Counter()
    original = Path.read_bytes

    def read(path: Path) -> bytes:
        reads[path] += 1
        return original(path)

    with (
        patch.object(Path, "read_bytes", read),
        patch.object(adapter, "_load_metadata", side_effect=AssertionError("live catalog")),
    ):
        projected = local_projection(adapter, now=NOW)
    assert len(projected["rows"]) == 6773
    assert all(
        reads[path] == 1
        for path in (paths.catalog, paths.health_cache, paths.ladder, paths.worker, paths.receipt)
    )
    path = tmp_path / "completion.json"
    assert publish_snapshot(projected, path) is None
    snapshot = load_snapshot(path, now=NOW)
    assert len(snapshot.model_rows) == 6773
    assert path.stat().st_size < MAX_BYTES


def test_bounds_truncation_and_source_errors_suppress_model_suggestions(tmp_path: Path) -> None:
    path = tmp_path / "completion.json"
    warning = publish_snapshot(view(10005), path)
    assert "incomplete" in warning
    snapshot = load_snapshot(
        path, now=NOW, runs=[{"run": f"run-{i}", "outcome": "COMPLETE"} for i in range(300)]
    )
    assert len(snapshot.model_rows) <= 10000
    assert len(snapshot.run_rows) == 200
    assert not complete("/probe cc/", commands=default_command_specs(), snapshot=snapshot, now=NOW)


def test_failure_preserves_previous_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "completion.json"
    publish_snapshot(view(), path)
    before = path.read_bytes()
    with patch("verdict.tui_completion_snapshot.os.replace", side_effect=OSError("sk-private")):
        warning = publish_snapshot(view(2), path)
    assert warning == "completion snapshot publication failed; evidence unchanged"
    assert path.read_bytes() == before
    assert len(list(tmp_path.iterdir())) == 1


def test_snapshot_schema_and_stale_display(tmp_path: Path) -> None:
    path = tmp_path / "completion.json"
    publish_snapshot(view(), path)
    snapshot = load_snapshot(path, now=NOW + timedelta(minutes=5))
    assert snapshot.schema == SCHEMA
    result = complete(
        "/probe cc/",
        commands=default_command_specs(),
        snapshot=snapshot,
        now=NOW + timedelta(minutes=5),
    )
    assert "STALE snapshot" in result[0].description
