from __future__ import annotations

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
    source = next(
        item for item in discover_sources(tmp_path) if item.source_id == "verdict-core-docs"
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
