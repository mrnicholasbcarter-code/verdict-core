"""Unit tests for verdict.memory_migration."""

import json
from pathlib import Path

from verdict.memory_migration import (
    archive_legacy_memory,
    detect_legacy_memory_artifacts,
    ingest_legacy_artifacts,
    purge_legacy_artifacts,
)
from verdict.memory_plane import MemoryPlane


def test_detect_archive_ingest_purge_pipeline(tmp_path: Path) -> None:
    # 1. Create fake legacy artifacts in tmp_path
    legacy_json = tmp_path / ".pi_rag_context.json"
    legacy_json.write_text(json.dumps({"key": "val"}), encoding="utf-8")

    legacy_dir = tmp_path / ".openviking"
    legacy_dir.mkdir()
    (legacy_dir / "state.json").write_text(json.dumps({"ov": True}), encoding="utf-8")

    # 2. Detect
    report = detect_legacy_memory_artifacts(cwd=tmp_path, home_dir=tmp_path)
    assert len(report.detected_paths) == 2
    assert report.openviking_found is True
    assert report.total_bytes > 0

    # 3. Archive
    archive_dir = tmp_path / ".verdict" / "archive"
    archive_path = archive_legacy_memory(report, output_dir=archive_dir)
    assert archive_path is not None
    assert archive_path.exists()
    assert archive_path.stat().st_size > 0

    # 4. Ingest into MemoryPlane
    db_path = tmp_path / "memory.db"
    plane = MemoryPlane(path=db_path)
    count = ingest_legacy_artifacts(report, plane)
    assert count >= 1

    # Search MemoryPlane to verify record
    records = plane.search("legacy")
    assert len(records) >= 1
    plane.close()

    # 5. Purge
    removed = purge_legacy_artifacts(report)
    assert len(removed) == 2
    assert not legacy_json.exists()
    assert not legacy_dir.exists()
