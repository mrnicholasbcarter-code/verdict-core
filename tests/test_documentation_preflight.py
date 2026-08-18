from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.documentation_preflight import (
    DocumentationPreflightError,
    DocumentationSource,
    _inventory,
    discover_sources,
    run_documentation_preflight,
)
from verdict.memory_bridge import MemoryHookController
from verdict.memory_plane import MemoryPlane


def _source(root: Path) -> DocumentationSource:
    return DocumentationSource(
        "fixture", "fixture", "https://example.test/fixture", "commit-1", root
    )


def test_discovered_project_source_keeps_repository_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "ADR-001.md").write_text("# Decision", encoding="utf-8")
    source = DocumentationSource(
        "verdict-core-docs",
        "verdict-core",
        "https://example.test/verdict",
        "working-tree",
        tmp_path,
    )
    assert source.root == tmp_path
    assert _inventory(source, fetch=None)[0].relative_path == "docs/adr/ADR-001.md"


def test_preflight_ingests_authoritative_docs_with_provenance_and_is_idempotent(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    document = docs / "ADR-001.md"
    document.write_text("# Decision\n\nUse the local memory plane.\n", encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)

    first = run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100)
    second = run_documentation_preflight(sources=[source], memory_path=db, fix=False, now=101)

    assert first.passed is True
    assert first.ingested == 1
    assert first.inventory_details["fixture"][0]["path"] == "docs/adr/ADR-001.md"
    assert first.inventory_details["fixture"][0]["raw_hash"]
    assert second.passed is True
    assert second.skipped_fresh == 1
    with MemoryPlane(db) as plane:
        record = plane.history(
            "authoritative-docs", "fixture:docs/adr/ADR-001.md#chunk-0000", scope="shared"
        )[-1]
        assert record.authority_verified is True
        assert record.provenance["commit"] == "commit-1"
        assert record.provenance["raw_hash"]
        assert record.provenance["document_hash"]


def test_preflight_manifest_reports_actual_chunk_count_and_validates_chunks(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    document = docs / "ADR-001.md"
    document.write_text("A" * 2400, encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)

    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed
    with MemoryPlane(db) as plane:
        manifests = plane.history(
            "documentation-manifest", "fixture:docs/adr/ADR-001.md", scope="shared"
        )
        manifest = manifests[-1]
        assert manifest.provenance["chunk_count"] == 2
        chunk_history = plane.history(
            "authoritative-docs", "fixture:docs/adr/ADR-001.md#chunk-0001", scope="shared"
        )
        assert chunk_history
        chunk = chunk_history[-1]
        plane._db.execute(
            "UPDATE memories SET content='tampered' WHERE record_id=?", (chunk.record_id,)
        )

    blocked = run_documentation_preflight(sources=[source], memory_path=db, fix=False, now=101)
    assert blocked.status == "blocked"
    assert blocked.stale == 1
    assert blocked.inventory_details["fixture"][0]["state"] == "stale"


@pytest.mark.parametrize(
    "tamper",
    [
        "content",
        "content_hash",
        "metadata",
        "provenance",
        "schema_version",
        "expires_at",
        "chunk_count",
    ],
)
def test_preflight_blocks_each_manifest_integrity_tamper(tmp_path: Path, tamper: str) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    (docs / "ADR-001.md").write_text("A" * 2400, encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)
    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed

    with MemoryPlane(db) as plane:
        manifest_history = plane.history(
            "documentation-manifest", "fixture:docs/adr/ADR-001.md", scope="shared"
        )
        assert manifest_history
        manifest = manifest_history[-1]
        if tamper == "content":
            plane._db.execute(
                "UPDATE memories SET content='tampered' WHERE record_id=?", (manifest.record_id,)
            )
        elif tamper == "content_hash":
            plane._db.execute(
                "UPDATE memories SET content_hash='0' WHERE record_id=?", (manifest.record_id,)
            )
        elif tamper == "metadata":
            plane._db.execute(
                "UPDATE memories SET metadata_json='{}' WHERE record_id=?", (manifest.record_id,)
            )
        elif tamper == "provenance":
            plane._db.execute(
                "UPDATE memories SET provenance_json='{}' WHERE record_id=?", (manifest.record_id,)
            )
        elif tamper == "schema_version":
            plane._db.execute(
                "UPDATE memories SET schema_version=999 WHERE record_id=?", (manifest.record_id,)
            )
        elif tamper == "expires_at":
            plane._db.execute(
                "UPDATE memories SET expires_at=1 WHERE record_id=?", (manifest.record_id,)
            )
        elif tamper == "chunk_count":
            provenance = dict(manifest.provenance or {})
            provenance["chunk_count"] = 999
            metadata = dict(manifest.metadata or {})
            metadata["chunk_count"] = 999
            plane._db.execute(
                "UPDATE memories SET provenance_json=?, metadata_json=? WHERE record_id=?",
                (json.dumps(provenance), json.dumps(metadata), manifest.record_id),
            )

    blocked = run_documentation_preflight(sources=[source], memory_path=db, fix=False, now=101)
    assert blocked.passed is False
    assert blocked.stale == 1
    assert blocked.inventory_details["fixture"][0]["state"] == "stale"


def test_fix_repairs_tampered_manifest_by_replacing_same_record_and_is_idempotent(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    (docs / "ADR-001.md").write_text("stable source", encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)
    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed
    with MemoryPlane(db) as plane:
        manifest_history = plane.history(
            "documentation-manifest", "fixture:docs/adr/ADR-001.md", scope="shared"
        )
        assert manifest_history
        manifest = manifest_history[-1]
        plane._db.execute(
            "UPDATE memories SET metadata_json='{}' WHERE record_id=?", (manifest.record_id,)
        )

    repaired = run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=101)
    assert repaired.passed
    assert repaired.ingested == 1
    second = run_documentation_preflight(sources=[source], memory_path=db, fix=False, now=102)
    assert second.passed
    assert second.skipped_fresh == 1
    with MemoryPlane(db) as plane:
        history = plane.history(
            "documentation-manifest", "fixture:docs/adr/ADR-001.md", scope="shared"
        )
        assert len(history) == 1
        assert history[-1].metadata["preflight_version"] == "1"


def test_preflight_fails_closed_when_pinned_git_tree_cannot_be_read(tmp_path: Path) -> None:
    source = DocumentationSource(
        "pinned", "fixture", "https://example.test/fixture", "a" * 40, tmp_path
    )
    report = run_documentation_preflight(sources=[source], memory_path=tmp_path / "memory.db")
    assert report.status == "blocked"
    assert report.unverifiable == 1
    assert "immutable Git tree" in report.errors[0]


def test_preflight_retires_removed_paths_and_extra_chunks(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    removed = docs / "ADR-001.md"
    changing = docs / "ADR-002.md"
    removed.write_text("removed later", encoding="utf-8")
    changing.write_text("B" * 2400, encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)

    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed
    removed.unlink()
    changing.write_text("short", encoding="utf-8")
    report = run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=101)
    assert report.passed
    with MemoryPlane(db) as plane:
        assert all(
            record.status != "active"
            for record in plane.history(
                "documentation-manifest", "fixture:docs/adr/ADR-001.md", scope="shared"
            )
        )
        assert all(
            record.status != "active"
            for record in plane.history(
                "authoritative-docs", "fixture:docs/adr/ADR-002.md#chunk-0001", scope="shared"
            )
        )


def test_preflight_reports_orphaned_records_read_only(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    document = docs / "ADR-001.md"
    document.write_text("present", encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)
    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed
    document.unlink()
    report = run_documentation_preflight(sources=[source], memory_path=db, fix=False, now=101)
    assert report.status == "blocked"
    assert report.state == "partial"
    assert report.orphaned == 2


def test_preflight_detects_changed_content_and_repairs_it(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    document = docs / "ADR-001.md"
    document.write_text("old", encoding="utf-8")
    db = tmp_path / "memory.db"
    source = _source(tmp_path)

    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed
    document.write_text("new", encoding="utf-8")
    stale = run_documentation_preflight(sources=[source], memory_path=db, fix=False, now=101)
    repaired = run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=101)

    assert stale.status == "blocked"
    assert stale.stale == 1
    assert repaired.passed is True
    assert repaired.ingested == 1


def test_preflight_refreshes_provenance_when_commit_changes_without_content_change(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    (docs / "ADR-001.md").write_text("unchanged authoritative text", encoding="utf-8")
    db = tmp_path / "memory.db"

    first_source = _source(tmp_path)
    first = run_documentation_preflight(sources=[first_source], memory_path=db, fix=True, now=100)
    assert first.passed is True

    second_source = DocumentationSource(
        "fixture", "fixture", "https://example.test/fixture", "commit-2", tmp_path
    )
    refreshed = run_documentation_preflight(
        sources=[second_source], memory_path=db, fix=True, now=101
    )
    assert refreshed.passed is True
    assert refreshed.ingested == 1

    unchanged = run_documentation_preflight(
        sources=[second_source], memory_path=db, fix=False, now=102
    )
    assert unchanged.passed is True
    assert unchanged.skipped_fresh == 1

    with MemoryPlane(db) as plane:
        manifest_history = plane.history(
            "documentation-manifest", "fixture:docs/adr/ADR-001.md", scope="shared"
        )
        assert len(manifest_history) == 2
        assert manifest_history[-1].status == "active"
        assert manifest_history[-1].provenance["commit"] == "commit-2"
        assert manifest_history[-2].status == "superseded"


def test_preflight_blocks_missing_remote_provenance_without_fetch(tmp_path: Path) -> None:
    source = DocumentationSource(
        "remote",
        "remote",
        "https://example.test/remote",
        "main",
        api_base="https://example.test/api",
    )
    report = run_documentation_preflight(
        sources=[source], memory_path=tmp_path / "memory.db", fix=True
    )
    assert report.passed is False
    assert report.unverifiable == 1
    assert "resolve" in report.errors[0]


def test_discover_sources_uses_explicit_remote_default_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERDICT_RUVECTOR_ROOT", raising=False)
    monkeypatch.delenv("VERDICT_RUVECTOR_REF", raising=False)
    source = next(item for item in discover_sources(Path.cwd()) if item.source_id == "ruvector")
    assert source.ref == "main"
    assert source.api_base


def test_discover_sources_falls_back_to_remote_when_local_ruvector_is_unresolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERDICT_RUVECTOR_ROOT", str(tmp_path / "missing-ruvector"))
    monkeypatch.setenv("VERDICT_RUVECTOR_REF", "main")
    source = next(item for item in discover_sources(tmp_path) if item.source_id == "ruvector")
    assert source.repository == "https://github.com/ruvnet/RuVector"
    assert source.ref == "main"


def test_discover_sources_falls_back_to_remote_ruflo_without_local_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERDICT_RUFLO_REF", "main")
    monkeypatch.setenv("VERDICT_RUFLO_ROOT", str(tmp_path / "missing-ruflo"))
    source = next(item for item in discover_sources(tmp_path) if item.source_id == "ruflo")
    assert source.repository == "https://github.com/ruvnet/ruflo"
    assert source.api_base == "https://api.github.com/repos/ruvnet/ruflo"
    assert source.root is None
    assert source.ref == "main"


def test_remote_ruflo_fallback_resolves_and_ingests_nested_adr_projections(tmp_path: Path) -> None:
    source = DocumentationSource(
        "ruflo",
        "ruflo",
        "https://github.com/ruvnet/ruflo",
        "main",
        api_base="https://api.example.test/repos/ruvnet/ruflo",
    )
    first = b"# Plugin ADR\n\nUse the plugin contract.\n"
    second = b"# V3 ADR\n\nUse the v3 contract.\n"
    commit = "b" * 40
    import hashlib

    def blob_sha(payload: bytes) -> str:
        return hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()

    tree = {
        "truncated": False,
        "tree": [
            {
                "type": "blob",
                "path": "plugins/ruflo-adr/docs/adrs/0001-contract.md",
                "sha": blob_sha(first),
            },
            {"type": "blob", "path": "v3/docs/adr/ADR-001-contract.md", "sha": blob_sha(second)},
        ],
    }

    def fetch(url: str) -> bytes:
        if url.endswith("/git/ref/heads/main"):
            return json.dumps({"object": {"sha": commit}}).encode()
        if url.endswith(f"/git/trees/{commit}?recursive=1"):
            return json.dumps(tree).encode()
        if url.endswith("0001-contract.md"):
            return first
        if url.endswith("ADR-001-contract.md"):
            return second
        raise AssertionError(f"unexpected fetch URL: {url}")

    report = run_documentation_preflight(
        sources=[source], memory_path=tmp_path / "memory.db", fix=True, fetch=fetch, now=100
    )
    assert report.passed
    assert report.source_commits == {"ruflo": commit}
    assert report.inventory == 2
    assert {item["path"] for item in report.inventory_details["ruflo"]} == {
        "plugins/ruflo-adr/docs/adrs/0001-contract.md",
        "v3/docs/adr/ADR-001-contract.md",
    }
    with MemoryPlane(tmp_path / "memory.db") as plane:
        active = [
            item for item in plane.export_records(scope="shared") if item["status"] == "active"
        ]
        assert len([item for item in active if item["namespace"] == "documentation-manifest"]) == 2
        assert len([item for item in active if item["namespace"] == "authoritative-docs"]) == 2


def test_preflight_resolves_remote_ref_and_verifies_blob_sha(tmp_path: Path) -> None:
    source = DocumentationSource(
        "remote",
        "ruvector",
        "https://github.com/ruvnet/RuVector",
        "main",
        api_base="https://api.example.test/repos/ruvnet/RuVector",
    )
    payload = b"# Decision\n"
    import hashlib

    blob_sha = hashlib.sha1(b"blob 11\0" + payload).hexdigest()
    tree = {
        "truncated": False,
        "tree": [{"type": "blob", "path": "docs/adr/ADR-001.md", "sha": blob_sha}],
    }
    commit = "a" * 40

    def fetch(url: str) -> bytes:
        if url.endswith("/git/ref/heads/main"):
            return json.dumps({"object": {"sha": commit}}).encode()
        if url.endswith(f"/git/trees/{commit}?recursive=1"):
            return json.dumps(tree).encode()
        return payload

    report = run_documentation_preflight(
        sources=[source], memory_path=tmp_path / "memory.db", fix=True, fetch=fetch, now=100
    )
    assert report.passed
    assert report.source_commits == {"remote": commit}


def test_preflight_rejects_truncated_remote_tree(tmp_path: Path) -> None:
    source = DocumentationSource(
        "truncated",
        "ruvector",
        "https://github.com/ruvnet/RuVector",
        "a" * 40,
        api_base="https://api.example.test/repos/ruvnet/RuVector",
    )

    def fetch(url: str) -> bytes:
        assert url.endswith("/git/trees/" + "a" * 40 + "?recursive=1")
        return json.dumps({"truncated": True, "tree": []}).encode()

    report = run_documentation_preflight(
        sources=[source], memory_path=tmp_path / "memory.db", fix=True, fetch=fetch
    )
    assert report.state == "unknown"
    assert report.unverifiable == 1
    assert "truncated" in report.errors[0]


def test_preflight_reports_partial_remote_ingestion(tmp_path: Path) -> None:
    source = DocumentationSource(
        "partial",
        "ruvector",
        "https://github.com/ruvnet/RuVector",
        "a" * 40,
        api_base="https://api.example.test/repos/ruvnet/RuVector",
    )
    tree = {
        "truncated": False,
        "tree": [
            {"type": "blob", "path": "docs/adr/ADR-001.md", "sha": "0" * 40},
            {"type": "blob", "path": "docs/adr/ADR-002.md", "sha": "0" * 40},
        ],
    }

    def fetch(url: str) -> bytes:
        if url.endswith("/git/trees/" + "a" * 40 + "?recursive=1"):
            return json.dumps(tree).encode()
        if url.endswith("ADR-001.md"):
            return b"# One\n"
        raise ValueError("simulated unavailable document")

    report = run_documentation_preflight(
        sources=[source], memory_path=tmp_path / "memory.db", fix=True, fetch=fetch
    )
    assert report.status == "blocked"
    assert report.unverifiable == 2
    assert report.state == "unknown"


def test_empty_sources_are_respected_for_diagnostics(tmp_path: Path) -> None:
    report = run_documentation_preflight(
        sources=(), memory_path=tmp_path / "memory.db", fix=False, now=100
    )
    assert report.passed is True
    assert report.sources == 0


def test_preflight_rejects_remote_content_with_wrong_git_blob_sha(tmp_path: Path) -> None:
    source = DocumentationSource(
        "remote-sha",
        "ruvector",
        "https://github.com/ruvnet/RuVector",
        "a" * 40,
        api_base="https://api.github.com/repos/ruvnet/RuVector",
    )
    tree = {
        "truncated": False,
        "tree": [{"type": "blob", "path": "docs/adr/ADR-001.md", "sha": "0" * 40}],
    }

    def fetch(url: str) -> bytes:
        if url.endswith("/git/trees/" + "a" * 40 + "?recursive=1"):
            return __import__("json").dumps(tree).encode()
        return b"# Decision\n"

    report = run_documentation_preflight(
        sources=[source], memory_path=tmp_path / "memory.db", fix=True, fetch=fetch
    )
    assert report.status == "blocked"
    assert report.unverifiable == 1
    assert "blob SHA" in report.errors[0]


def test_implementation_hook_fails_closed_until_preflight_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    def blocked(**_: object) -> None:
        raise DocumentationPreflightError("blocked")

    import verdict.documentation_preflight as preflight

    monkeypatch.setattr(preflight, "require_documentation_preflight", blocked)
    plane = MemoryPlane(tmp_path / "memory.db")
    controller = MemoryHookController(plane=plane)
    with pytest.raises(DocumentationPreflightError):
        controller.on_task_start("task", "implement", implementation=True)
    assert controller.receipt_store.query_receipts(limit=10) == []
    with pytest.raises(DocumentationPreflightError):
        controller.on_file_edit_start("src/app.py", implementation=True)
    assert controller.receipt_store.query_receipts(limit=10) == []
    plane.close()


def test_implementation_hooks_allow_work_after_verified_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs" / "adr"
    docs.mkdir(parents=True)
    (docs / "ADR-001.md").write_text("# Decision\n\nUse verified docs.", encoding="utf-8")
    db = tmp_path / "memory.db"
    source = DocumentationSource(
        "fixture-success",
        "fixture",
        "https://example.test/fixture",
        "commit-1",
        tmp_path,
        freshness_seconds=10**12,
    )
    monkeypatch.setattr(
        "verdict.documentation_preflight.discover_sources", lambda _root=None: (source,)
    )
    assert run_documentation_preflight(sources=[source], memory_path=db, fix=True, now=100).passed
    controller = MemoryHookController(plane=MemoryPlane(db))
    assert controller.on_task_start("task", "implement", implementation=True)["status"] == "success"
    assert controller.on_file_edit_start("src/app.py", implementation=True)["status"] == "success"
