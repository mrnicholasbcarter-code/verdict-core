"""The real-registry guard tolerates the operator's sync timer, not test writes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tests import conftest


def _ns(stamp: str) -> int:
    ran = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return int(ran.timestamp() * 1e9)


def test_change_matching_a_sync_backup_stamp_is_the_timer(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    registry = tmp_path / "models.json"
    registry.write_text("{}")
    (tmp_path / "models.json.verdict-sync-20260927T201649Z.bak").write_text("{}")
    monkeypatch.setattr(conftest, "_REAL_PRIME_MODELS", registry)
    assert conftest._changed_by_sync_timer((_ns("20260927T201649Z") + 2_000_000_000, 10))


def test_change_without_a_matching_sync_backup_is_a_test_write(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    registry = tmp_path / "models.json"
    registry.write_text("{}")
    (tmp_path / "models.json.verdict-sync-20260927T160000Z.bak").write_text("{}")
    (tmp_path / "models.json.bak-20260927T201649").write_text("{}")  # other name: ignored
    monkeypatch.setattr(conftest, "_REAL_PRIME_MODELS", registry)
    assert not conftest._changed_by_sync_timer((_ns("20260927T201649Z"), 10))
    assert not conftest._changed_by_sync_timer(None)
