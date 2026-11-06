"""Unit tests for scripts/sync_ref_docs.py (BOD-346)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.sync_ref_docs import (
    AUTHORITY,
    MAX_PAGE_BYTES,
    Page,
    SyncReport,
    build_pages,
    redact,
    resolve_installed_tag,
    select_paths,
    split_markdown,
    sync,
)

REPO, TAG = "acme/tool", "v1.2.3"


class FakeStore:
    def __init__(self) -> None:
        self.pages: dict[str, Page] = {}
        self.writes = 0
        self.deletes: list[str] = []

    def write(self, page: Page, tags: list[str]) -> None:
        self.pages[page.path] = page
        self.writes += 1

    def delete(self, path: str) -> None:
        self.pages.pop(path, None)
        self.deletes.append(path)


def _run(
    tree: list[dict[str, Any]], blobs: dict[str, str], store: FakeStore, manifest: dict[str, Any]
) -> SyncReport:
    return sync(
        repo=REPO,
        tag=TAG,
        project="ref-tool",
        tree=tree,
        fetch_blob=blobs.__getitem__,
        store=store,
        manifest=manifest,
        save_manifest=lambda m: None,
    )


def test_one_page_per_h2_section_with_breadcrumbs() -> None:
    text = "---\ntitle: x\n---\n# Guide\n\nIntro.\n\n## Install\n\nRun it.\n\n## Use\n\n### Flags\n\nUse flags.\n"
    title, chunks = split_markdown(text, 2000)
    assert title == "Guide"
    assert [c.crumbs for c in chunks] == [(), ("Install",), ("Use",)]
    assert chunks[2].text.startswith("## Use")


def test_headings_inside_code_fences_do_not_split() -> None:
    text = "# T\n\n## Real\n\n```bash\n# not a heading\n## nor this\n```\n"
    _, chunks = split_markdown(text, 2000)
    assert len(chunks) == 1
    assert "# not a heading" in chunks[0].text


def test_oversize_section_splits_by_subheading_then_lines() -> None:
    sub = "\n".join(f"### Sub {i}\n\n" + ("word " * 150) for i in range(4))
    table = "\n".join(f"| row {i} | {'x' * 60} |" for i in range(200))
    text = f"# T\n\n## Big\n\n{sub}\n\n## Table\n\n{table}\n"
    _, chunks = split_markdown(text, 1500)
    assert all(len(c.text.encode()) <= 1500 for c in chunks)
    assert ("Big", "Sub 0") in [c.crumbs for c in chunks]
    assert sum(1 for c in chunks if c.crumbs == ("Table",)) > 1


def test_split_code_fence_is_closed_and_reopened() -> None:
    code = "\n".join(f"line {i} " + "y" * 50 for i in range(100))
    _, chunks = split_markdown(f"# T\n\n## Code\n\n```text\n{code}\n```\n", 1200)
    assert len(chunks) > 1
    for c in chunks:
        assert c.text.count("```") % 2 == 0


def test_pages_are_cited_and_within_size_limit() -> None:
    text = "# Doc\n\n" + "\n\n".join(f"## S{i}\n\n" + ("z " * 900) for i in range(3))
    pages = build_pages(REPO, TAG, "docs/a.md", "abc123", text)
    assert len(pages) >= 3
    for i, page in enumerate(pages, 1):
        assert len(page.body.encode()) <= MAX_PAGE_BYTES
        assert page.body.startswith("# Doc")
        for needed in (
            AUTHORITY,
            f"repo: {REPO}",
            f"tag: {TAG}",
            "path: docs/a.md",
            "blob sha: abc123",
            f"part: {i}/{len(pages)}",
            f"url: https://github.com/{REPO}/blob/{TAG}/docs/a.md",
        ):
            assert needed in page.body


def test_example_secrets_are_redacted() -> None:
    raw = "key sk-a3ab3c080beaee3a-69f4a4 and Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"
    out = redact(raw)
    assert "a3ab3c080beaee3a" not in out
    assert "abcdefghijklmnop" not in out
    assert redact("Bearer $OMNIROUTE_API_KEY") == "Bearer $OMNIROUTE_API_KEY"


def test_openapi_becomes_one_group_per_tag() -> None:
    spec = {
        "info": {"title": "API", "version": "1"},
        "tags": [{"name": "Free tier", "description": "free"}],
        "paths": {
            "/api/free-tier/summary": {
                "get": {
                    "tags": ["Free tier"],
                    "summary": "summary",
                    "security": [{"BearerAuth": []}],
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer", "default": 5},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "OK",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["total"],
                                        "properties": {"total": {"type": "integer"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {"securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}}},
    }
    pages = build_pages(REPO, TAG, "docs/openapi.yaml", "s1", json.dumps(spec))
    page = next(p for p in pages if p.path.startswith("docs/openapi/free-tier/"))
    for needed in (
        "GET /api/free-tier/summary",
        "Auth: BearerAuth",
        "`limit` (query, optional)",
        "object{total*: integer}",
    ):
        assert needed in page.body


def test_select_paths_excludes_i18n_and_screenshots() -> None:
    tree = [
        {"path": "docs/a.md", "type": "blob", "sha": "1"},
        {"path": "docs/i18n/de/a.md", "type": "blob", "sha": "2"},
        {"path": "docs/screenshots/x.md", "type": "blob", "sha": "3"},
        {"path": "docs/openapi.yaml", "type": "blob", "sha": "4"},
        {"path": "README.md", "type": "blob", "sha": "5"},
    ]
    assert select_paths(tree, None) == {"docs/a.md": "1", "docs/openapi.yaml": "4"}
    assert select_paths(tree, {"README.md"}) == {"README.md": "5"}


def test_unchanged_blob_sha_writes_zero_pages_and_change_rewrites_only_that_file() -> None:
    blobs = {"s1": "# A\n\n## One\n\nx\n\n## Two\n\ny\n", "s2": "# B\n\n## Only\n\nz\n"}
    tree = [
        {"path": "docs/a.md", "type": "blob", "sha": "s1"},
        {"path": "docs/b.md", "type": "blob", "sha": "s2"},
    ]
    store = FakeStore()
    manifest: dict[str, Any] = {}
    first = _run(tree, blobs, store, manifest)
    assert first.totals()["pages_written"] == 3

    store.writes = 0
    again = _run(tree, blobs, store, manifest)
    assert store.writes == 0
    assert again.totals() == {**again.totals(), "pages_written": 0, "files_unchanged": 2}

    blobs["s3"] = "# A\n\n## One\n\nchanged\n"
    tree[0] = {"path": "docs/a.md", "type": "blob", "sha": "s3"}
    changed = _run(tree, blobs, store, manifest)
    assert store.writes == 1
    assert [f.path for f in changed.files if f.status == "written"] == ["docs/a.md"]
    assert store.deletes == ["docs/a/part-002-two.md"]


def test_removed_file_deletes_its_pages() -> None:
    blobs = {"s1": "# A\n\n## One\n\nx\n", "s2": "# B\n\n## Only\n\nz\n"}
    tree = [
        {"path": "docs/a.md", "type": "blob", "sha": "s1"},
        {"path": "docs/b.md", "type": "blob", "sha": "s2"},
    ]
    store = FakeStore()
    manifest: dict[str, Any] = {}
    _run(tree, blobs, store, manifest)
    report = _run(tree[1:], blobs, store, manifest)
    assert report.totals()["files_deleted"] == 1
    assert set(store.pages) == {"docs/b/part-001-only.md"}
    assert "docs/a.md" not in manifest["files"]


def test_tag_follows_installed_package(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"name": "omniroute", "version": "3.8.53"}))
    assert resolve_installed_tag(pkg) == "v3.8.53"


def test_table_padding_is_compacted() -> None:
    _, chunks = split_markdown("# T\n\n## S\n\n| a      | b    |\n| ------ | ---- |\n", 500)
    assert chunks[0].text.endswith("| a | b |\n| --- | --- |")


def test_each_subsection_gets_its_own_page_and_stubs_merge_forward() -> None:
    subs = "\n\n".join(f"### Sub {i}\n\n" + ("w " * 250) for i in range(3))
    text = f"# T\n\n## Big\n\nLead-in.\n\n{subs}\n\n## Tail\n\n" + ("t " * 1000)
    _, chunks = split_markdown(text, 1200)
    assert [c.crumbs for c in chunks][:3] == [("Big", "Sub 0"), ("Big", "Sub 1"), ("Big", "Sub 2")]
    assert chunks[0].text.startswith("## Big\n\nLead-in.")
