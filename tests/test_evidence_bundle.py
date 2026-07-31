"""Tests for deterministic, content-addressed release evidence bundles."""

import json
from pathlib import Path

import pytest

from scripts.evidence_bundle import MANIFEST_NAME, collect_evidence, create_bundle, verify_bundle


def _write_fixtures(root: Path) -> None:
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "result.json").write_text('{"ok": true}\n', encoding="utf-8")
    (root / "summary.md").write_text("# Summary\n", encoding="utf-8")


def test_identical_inputs_produce_identical_manifest_and_bundle(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    output_dir = tmp_path / "bundles"
    _write_fixtures(evidence_dir)

    first = create_bundle(collect_evidence(evidence_dir), evidence_dir, output_dir)
    first_manifest = (evidence_dir / MANIFEST_NAME).read_bytes()
    first_bytes = first.read_bytes()

    second = create_bundle(collect_evidence(evidence_dir), evidence_dir, output_dir)

    assert second == first
    assert (evidence_dir / MANIFEST_NAME).read_bytes() == first_manifest
    assert second.read_bytes() == first_bytes
    verify_bundle(evidence_dir, second)


def test_tampering_missing_and_unexpected_files_fail_closed(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    bundle_dir = tmp_path / "bundles"
    _write_fixtures(evidence_dir)
    bundle = create_bundle(collect_evidence(evidence_dir), evidence_dir, bundle_dir)

    (evidence_dir / "summary.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_bundle(evidence_dir, bundle)

    (evidence_dir / "summary.md").unlink()
    with pytest.raises(ValueError, match="missing"):
        verify_bundle(evidence_dir, bundle)

    (evidence_dir / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected"):
        verify_bundle(evidence_dir, bundle)


def test_manifest_rejects_duplicate_and_traversal_paths(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    manifest_path = evidence_dir / MANIFEST_NAME

    for path in ("../outside.json", "nested/../result.json"):
        manifest_path.write_text(
            json.dumps(
                {"schema_version": 1, "artifacts": [{"path": path, "size": 0, "sha256": "0" * 64}]}
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unsafe evidence path"):
            verify_bundle(evidence_dir)

    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {"path": "same.json", "size": 0, "sha256": "0" * 64},
                    {"path": "same.json", "size": 0, "sha256": "0" * 64},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        verify_bundle(evidence_dir)
