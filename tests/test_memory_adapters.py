from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.memory_adapters import (
    AdapterDescriptor,
    AdapterRegistry,
    AdapterUnavailableError,
    ImportPolicy,
    LocalManifestAdapter,
    ManifestError,
    build_default_adapter_registry,
    content_hash,
    export_manifest,
    import_manifest,
)


class FakeAdapter:
    adapter_id = "fixture"
    protocol_version = "1"

    def export(self, *, root: Path, options: dict[str, object]):
        return []

    def import_records(self, records, *, options: dict[str, object]):
        return {"records": len(list(records))}


def policy(tmp_path: Path, **kwargs: object) -> ImportPolicy:
    return ImportPolicy((tmp_path,), **kwargs)


def test_registry_has_versioned_available_and_unavailable_states() -> None:
    registry = AdapterRegistry()
    registry.register(FakeAdapter())
    registry.declare_unavailable(AdapterDescriptor("provider", description="not installed"))

    assert registry.resolve("fixture").available is True
    assert registry.resolve("provider").reason == "not installed"
    with pytest.raises(AdapterUnavailableError, match="provider"):
        registry.get("provider")
    with pytest.raises(ValueError, match="version"):
        registry.declare_unavailable(AdapterDescriptor("old", protocol_version="2"))


def test_export_is_dry_run_atomic_and_redacts_sensitive_content(tmp_path: Path) -> None:
    destination = tmp_path / "exports" / "manifest.json"
    records = [
        {
            "key": "a",
            "content": "safe",
            "prompt": "private prompt",
            "metadata": {"api_key": "sk-secret-value"},
        }
    ]
    report = export_manifest(records, destination, policy=policy(tmp_path), dry_run=True)
    assert report.to_dict()["dry_run"] is True
    assert not destination.exists()

    written = export_manifest(records, destination, policy=policy(tmp_path))
    assert written.records_written == 1
    payload = json.loads(destination.read_text())
    assert payload["records"][0]["prompt"] == "[redacted]"
    assert payload["records"][0]["metadata"]["api_key"] == "[redacted]"
    assert "private prompt" not in destination.read_text()


def test_import_is_idempotent_and_preserves_provenance(tmp_path: Path) -> None:
    destination = tmp_path / "manifest.json"
    record = {
        "key": "a",
        "content": "safe",
        "provenance": {"source": "upstream", "observed_at": "today"},
    }
    export_manifest([record, record], destination, policy=policy(tmp_path))
    payload = json.loads(destination.read_text())
    digest = payload["records"][0]["content_hash"]
    imported, report = import_manifest(
        destination, policy=policy(tmp_path), existing_hashes=[digest]
    )
    assert imported == []
    assert report.duplicates == 1
    assert report.records_written == 0

    export_manifest([record], destination, policy=policy(tmp_path))
    imported, report = import_manifest(destination, policy=policy(tmp_path), dry_run=True)
    assert imported[0]["provenance"]["source"] == "upstream"
    assert report.dry_run is True
    assert report.records_written == 0


def test_paths_require_allowlisted_non_tmp_root_and_no_symlink(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="allowlisted"):
        export_manifest([], tmp_path.parent / "outside.json", policy=policy(tmp_path))
    with pytest.raises(ManifestError, match="/tmp"):
        export_manifest([], Path("/tmp/manifest.json"), policy=ImportPolicy((Path("/tmp"),)))
    target = tmp_path / "target.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ManifestError, match="symlink"):
        import_manifest(link, policy=policy(tmp_path))


def test_manifest_version_hash_and_size_are_validated(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"manifest_version": "2", "records": []}))
    with pytest.raises(ManifestError, match="version"):
        import_manifest(path, policy=policy(tmp_path))

    good = tmp_path / "good.json"
    export_manifest([{"key": "a", "content": "safe"}], good, policy=policy(tmp_path))
    payload = json.loads(good.read_text())
    payload["records"][0]["content"] = "changed"
    good.write_text(json.dumps(payload))
    with pytest.raises(ManifestError, match="hash"):
        import_manifest(good, policy=policy(tmp_path))

    payload = json.loads(good.read_text())
    payload["records"][0]["content"] = "changed"
    payload["records"][0]["content_hash"] = content_hash(payload["records"][0])
    good.write_text(json.dumps(payload))
    with pytest.raises(ManifestError, match="manifest hash"):
        import_manifest(good, policy=policy(tmp_path))

    report = export_manifest(
        [{"key": "a", "content": "x" * 100}],
        tmp_path / "large.json",
        policy=policy(tmp_path, max_bytes=10),
    )
    assert report.status == "error"
    assert "manifest size limit exceeded" in report.errors


def test_content_hash_is_stable_for_canonical_records() -> None:
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})


def test_local_manifest_adapter_is_provider_neutral() -> None:
    adapter = LocalManifestAdapter()
    records = [{"key": "a", "content": "safe"}]
    assert list(adapter.export(root=Path("."), options={"records": records})) == records
    assert adapter.import_records(records, options={}) == {"records": 1, "status": "accepted"}


def test_default_registry_reports_capabilities_and_isolates_failures(tmp_path: Path) -> None:
    registry = build_default_adapter_registry()
    statuses = {item["adapter_id"]: item["status"] for item in registry.status()}
    assert statuses["document"] == "available"
    assert statuses["masterdocs-sqlite"] == "unavailable"
    assert registry.resolve("missing").status == "unknown"

    from verdict.memory_plane import MemoryPlane

    with MemoryPlane(tmp_path / "memory.db") as plane:
        summary = registry.ingest_many(
            [
                {"adapter_id": "masterdocs-sqlite"},
                {"adapter_id": "missing"},
                {"adapter_id": "document", "options": {"paths": [tmp_path / "missing.md"]}},
            ],
            plane=plane,
            root=tmp_path,
        )
    assert summary.status in {"degraded", "unknown"}
    assert [report.status for report in summary.reports] == ["unavailable", "unknown", "degraded"]


def test_registry_ingests_document_records_and_reports_duplicate(tmp_path: Path) -> None:
    document = tmp_path / "guide.md"
    document.write_text("# Guide\n\ncanonical memory", encoding="utf-8")
    registry = build_default_adapter_registry()
    from verdict.memory_plane import MemoryPlane

    with MemoryPlane(tmp_path / "memory.db") as plane:
        request = {"adapter_id": "document", "options": {"paths": [document]}}
        first = registry.ingest_many([request], plane=plane, root=tmp_path)
        second = registry.ingest_many([request], plane=plane, root=tmp_path)
        assert first.records_accepted == 1
        assert second.reports[0].duplicates == 1
        assert second.reports[0].records_accepted == 0


def test_registry_preserves_scope_and_reports_supersession(tmp_path: Path) -> None:
    registry = build_default_adapter_registry()
    from verdict.memory_plane import MemoryPlane

    with MemoryPlane(tmp_path / "memory.db") as plane:
        request = {
            "adapter_id": "masterdocs-manifest",
            "options": {
                "records": [
                    {
                        "record_id": "one",
                        "namespace": "docs",
                        "scope": "project-a",
                        "key": "adr",
                        "content": "first",
                        "source": "fixture",
                    }
                ]
            },
        }
        first = registry.ingest_many([request], plane=plane, root=tmp_path)
        request["options"]["records"][0]["record_id"] = "two"
        request["options"]["records"][0]["content"] = "second"
        second = registry.ingest_many([request], plane=plane, root=tmp_path)
        assert first.records_accepted == 1
        assert second.reports[0].superseded == 1
        assert plane.get("docs", "adr", scope="project-a").content == "second"
        assert plane.get("docs", "adr", scope="project-b") is None
