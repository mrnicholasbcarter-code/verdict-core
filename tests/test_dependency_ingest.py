from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from verdict.dependency_ingest import (
    DOC_NAMESPACE,
    DependencyDocResult,
    PackageRef,
    build_memory_record,
    discover_dependencies,
    find_in_repo_docs,
    ingest_dependency_docs,
    report_rows,
)
from verdict.memory_plane import MemoryPlane


def _write_fixture() -> Path:
    root = Path(tempfile.mkdtemp(prefix="dep-ingest-fixture-"))
    source = root / "verdict"
    source.mkdir()
    (source / "router.py").write_text(
        "from fastapi import Request\nimport httpx\nimport rich\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        'dependencies = ["httpx>=0.25", "PyYAML>=6.0", "rich>=13.0.0"]\n', encoding="utf-8"
    )
    contracts = root / "contracts"
    contracts.mkdir()
    (contracts / "package.json").write_text(
        json.dumps({"dependencies": {"zod": "^3.23.0"}}), encoding="utf-8"
    )
    return root


def _ref(name: str, kind: str = "declared") -> PackageRef:
    return PackageRef(name=name, usage=1, kind=kind)


def test_discover_returns_nonempty_dedup_list() -> None:
    root = _write_fixture()
    packages = discover_dependencies(
        root / "verdict",
        pyproject=root / "pyproject.toml",
        package_json=root / "contracts" / "package.json",
    )
    assert packages
    assert packages == sorted(set(packages))
    assert "fastapi" in packages
    assert "httpx" in packages
    assert "zod" in packages


def test_discover_skips_stdlib() -> None:
    root = _write_fixture()
    packages = discover_dependencies(
        root / "verdict",
        pyproject=root / "pyproject.toml",
        package_json=root / "contracts" / "package.json",
    )
    assert "re" not in packages
    assert "typing" not in packages
    assert "pathlib" not in packages


def test_ingestion_writes_record_with_provenance() -> None:
    root = _write_fixture()
    with MemoryPlane(tempfile.mkdtemp() + "/memory.db") as plane:
        results = ingest_dependency_docs(
            plane,
            [_ref("httpx"), _ref("fastapi")],
            root,
            fetcher=lambda pkg: f"summary for {pkg}",
            cap=12,
        )
        assert {r.status for r in results} == {"ingested"}
        record = plane.get(DOC_NAMESPACE, "httpx")
        assert record is not None
        assert record.content == "summary for httpx"
        provenance = record.provenance or {}
        assert provenance.get("source") == "context7"
        assert provenance.get("retrieved_at")
        assert provenance.get("adapter") == "dependency-ingest"
        assert provenance.get("schema_version") == 2


def test_missing_package_fails_gracefully() -> None:
    root = _write_fixture()

    def raise_fetcher(pkg: str) -> str:
        raise RuntimeError(f"quota for {pkg}")

    with MemoryPlane(tempfile.mkdtemp() + "/memory.db") as plane:
        results = ingest_dependency_docs(
            plane, [_ref("pydantic")], root, fetcher=raise_fetcher, cap=12
        )
        assert len(results) == 1
        assert results[0].status == "failed"
        assert "quota" in (results[0].error or "")
        assert plane.get(DOC_NAMESPACE, "pydantic") is None


def test_empty_fetcher_result_is_failure() -> None:
    root = _write_fixture()
    with MemoryPlane(tempfile.mkdtemp() + "/memory.db") as plane:
        results = ingest_dependency_docs(
            plane, [_ref("pandas")], root, fetcher=lambda pkg: "   ", cap=12
        )
        assert results[0].status == "failed"
        assert plane.get(DOC_NAMESPACE, "pandas") is None


def test_in_repo_doc_records_existing_path() -> None:
    root = _write_fixture()
    docs = root / "docs"
    docs.mkdir()
    (docs / "fastapi.md").write_text("FastAPI integration notes\n", encoding="utf-8")
    with MemoryPlane(tempfile.mkdtemp() + "/memory.db") as plane:
        results = ingest_dependency_docs(plane, [_ref("fastapi")], root, fetcher=None, cap=12)
        assert results[0].status == "exists"
        assert results[0].source == "in-repo"
        record = plane.get(DOC_NAMESPACE, "fastapi")
        assert record is not None
        assert "fastapi.md" in (record.provenance or {}).get("source_path", "")


def test_quota_error_surfaces_as_failed() -> None:
    root = _write_fixture()
    with MemoryPlane(tempfile.mkdtemp() + "/memory.db") as plane:
        results = ingest_dependency_docs(
            plane,
            [_ref("rich")],
            root,
            fetcher=lambda pkg: (_ for _ in ()).throw(RuntimeError("quota exceeded")),
            cap=12,
        )
        assert results[0].status == "failed"


def test_report_rows_is_deterministic() -> None:
    rows = [
        DependencyDocResult("httpx", "context7", None, "ingested"),
        DependencyDocResult("fastapi", "in-repo", "docs/fastapi.md", "exists"),
        DependencyDocResult("pydantic", "context7", None, "failed", "quota"),
    ]
    first = report_rows(rows)
    second = report_rows(list(reversed(rows)))
    assert first == second
    assert "httpx" in first
    assert "fastapi" in first
    assert "pydantic" in first


def test_build_memory_record_includes_provenance() -> None:
    record = build_memory_record(
        "openai", source="context7", version="1.0.0", summary="provider sdk"
    )
    assert record.namespace == DOC_NAMESPACE
    assert record.key == "openai"
    assert record.content == "provider sdk"
    assert (record.provenance or {}).get("version") == "1.0.0"
    assert re.match(r"^dep_[0-9a-f]{32}$", record.record_id)


def test_find_in_repo_docs_returns_path() -> None:
    root = _write_fixture()
    docs = root / "docs"
    docs.mkdir()
    (docs / "rich.md").write_text("rich console\n", encoding="utf-8")
    hit = find_in_repo_docs("rich", root)
    assert hit is not None
    assert hit.name == "rich.md"
