"""Integration test: ref-docs sync from a fake ``gh`` into a real, temporary ai-memory server.

Runs ``ref_docs.run_sync`` in-process with the real ``gh`` subprocess path (a
fake ``gh`` from ``tests/fixtures/ref_docs`` first on PATH) and the real
``AiMemoryStore`` against a throwaway ``ai-memory serve`` on a free loopback
port. Skipped when the ``ai-memory`` binary is not on PATH.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from verdict import ref_docs

AI_MEMORY = shutil.which("ai-memory")
pytestmark = pytest.mark.skipif(AI_MEMORY is None, reason="ai-memory binary not on PATH")

FIXTURES = Path(__file__).parent / "fixtures" / "ref_docs"
SOURCE = ref_docs.Source(
    name="widget", repo="acme/widget", project="ref-widget", unit="widget.service", package="widget"
)
FIXTURE_KEY = "sk-FIXTUREONLY0123456789abcd"


class Server:
    def __init__(self, endpoint: str, token: str) -> None:
        self.endpoint = endpoint
        self.token = token
        self._client = httpx.Client(timeout=30)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = self._client.post(
            f"{self.endpoint}/mcp",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def tool(self, name: str, workspace: str, **arguments: Any) -> dict[str, Any] | None:
        """Tool result as JSON, or None when the server answers with an error."""
        args = {**arguments, "workspace": workspace, "project": SOURCE.project}
        data = self.rpc("tools/call", {"name": name, "arguments": args})
        if "error" in data or data["result"].get("isError"):
            return None
        parsed: dict[str, Any] = json.loads(data["result"]["content"][0]["text"])
        return parsed

    def read(self, workspace: str, path: str) -> dict[str, Any] | None:
        return self.tool("memory_read_page", workspace, path=path)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Server]:
    assert AI_MEMORY is not None
    root = tmp_path_factory.mktemp("ai-memory")
    data_dir = root / "data"
    token = secrets.token_hex(32)
    port = _free_port()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(root),
        "AI_MEMORY_AUTH_TOKEN": token,
        "AI_MEMORY_EMBEDDING_PROVIDER": "none",
    }
    log = (root / "serve.log").open("w")
    subprocess.run(
        [AI_MEMORY, "init", "--data-dir", str(data_dir)],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        check=True,
    )
    proc = subprocess.Popen(
        [
            AI_MEMORY,
            *("--data-dir", str(data_dir), "serve", "--transport", "http"),
            *("--bind", f"127.0.0.1:{port}", "--no-watcher"),
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    srv = Server(f"http://127.0.0.1:{port}", token)
    try:
        deadline = time.monotonic() + 60
        while True:
            if proc.poll() is not None:
                pytest.fail(f"ai-memory serve exited: {(root / 'serve.log').read_text()[-2000:]}")
            try:
                srv.rpc("tools/list")
                break
            except httpx.HTTPError:
                if time.monotonic() > deadline:
                    pytest.fail("ai-memory serve did not answer within 60s")
                time.sleep(0.2)
        yield srv
    finally:
        proc.terminate()
        proc.wait(timeout=30)
        log.close()


@pytest.fixture
def sync_env(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str, str], dict[str, Any]]:
    """Fake gh first on PATH, temp VERDICT_HOME; returns sync(tag, workspace)."""
    monkeypatch.setenv("PATH", f"{FIXTURES / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("REF_DOCS_FIXTURE_DIR", str(FIXTURES))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "verdict"))
    monkeypatch.setenv("AI_MEMORY_AUTH_TOKEN", server.token)

    def run(tag: str, workspace: str) -> dict[str, Any]:
        return ref_docs.run_sync(
            SOURCE, tag=tag, workspace=workspace, endpoint=server.endpoint, token=server.token
        )

    return run


def _pages(report: dict[str, Any], workspace: str) -> dict[str, list[str]]:
    manifest = json.loads(Path(report["manifest"]).read_text("utf-8"))
    assert report["workspace"] == workspace
    return {path: entry["pages"] for path, entry in manifest["files"].items()}


def test_first_sync_writes_cited_pages_and_excludes_i18n(
    server: Server, sync_env: Callable[[str, str], dict[str, Any]]
) -> None:
    report = sync_env("v1.0.0", "first")
    assert report["totals"] == {
        "files": 4,
        "files_written": 4,
        "files_unchanged": 0,
        "files_deleted": 0,
        "pages_written": 9,
        "pages_deleted": 0,
    }
    pages = _pages(report, "first")
    assert sorted(pages) == [
        "docs/guide.md",
        "docs/openapi.yaml",
        "docs/reference/api.md",
        "docs/removed.md",
    ]
    tree = json.loads((FIXTURES / "trees" / "v1.0.0.json").read_text("utf-8"))
    shas = {e["path"]: e["sha"] for e in tree["tree"]}
    for path, page_paths in pages.items():
        for page_path in page_paths:
            page = server.read("first", page_path)
            assert page is not None, page_path
            body = page["body"]
            assert f"Authority: {ref_docs.AUTHORITY}." in body
            assert "- repo: acme/widget" in body and "- tag: v1.0.0" in body
            assert f"- path: {path}" in body and f"- blob sha: {shas[path]}" in body
            assert page["frontmatter"]["kind"] == "reference"
    hits = server.tool("memory_query", "first", query="Übersetzung")
    assert hits is not None and "i18n" not in json.dumps(hits)
    hits = server.tool("memory_query", "first", query="upstream providers")
    assert hits is not None and "docs/guide/part-001-intro.md" in json.dumps(hits)


def test_token_shaped_string_is_redacted_on_the_server(
    server: Server, sync_env: Callable[[str, str], dict[str, Any]]
) -> None:
    sync_env("v1.0.0", "redact")
    page = server.read("redact", "docs/guide/part-003-configure.md")
    assert page is not None
    assert "WIDGET_API_KEY" in page["body"]
    assert FIXTURE_KEY[3:] not in page["body"]
    assert "sk-<redacted>" in page["body"], "verdict redacts before the server sanitizer runs"


def test_resync_is_a_no_op_and_a_new_tag_rewrites_only_what_changed(
    server: Server, sync_env: Callable[[str, str], dict[str, Any]]
) -> None:
    first = sync_env("v1.0.0", "upgrade")
    before = _pages(first, "upgrade")

    again = sync_env("v1.0.0", "upgrade")
    assert again["totals"]["pages_written"] == 0
    assert again["totals"]["files_unchanged"] == again["totals"]["files"] == 4

    upgraded = sync_env("v1.1.0", "upgrade")
    status = {f["path"]: f for f in upgraded["files"]}
    assert status["docs/guide.md"]["status"] == "written"
    assert status["docs/guide.md"]["pages_written"] == 3
    assert status["docs/guide.md"]["pages_deleted"] == 1
    assert status["docs/removed.md"] == {
        "path": "docs/removed.md",
        "status": "deleted",
        "pages_written": 0,
        "pages_deleted": 1,
    }
    assert status["docs/openapi.yaml"]["status"] == "unchanged"
    assert status["docs/reference/api.md"]["status"] == "unchanged"
    assert upgraded["totals"]["pages_written"] == 3

    for page_path in before["docs/removed.md"]:
        assert server.read("upgrade", page_path) is None
    assert server.read("upgrade", "docs/guide/part-004-legacy-mode.md") is None
    guide = server.read("upgrade", "docs/guide/part-001-intro.md")
    assert guide is not None and "- tag: v1.1.0" in guide["body"] and "failover" in guide["body"]
    api = server.read("upgrade", "docs/reference/api/part-001-post-v1-route.md")
    assert api is not None and "- tag: v1.0.0" in api["body"]
    manifest = json.loads(Path(upgraded["manifest"]).read_text("utf-8"))
    assert manifest["tag"] == "v1.1.0" and "docs/removed.md" not in manifest["files"]


def test_status_compares_the_synced_tag_with_the_installed_package(
    sync_env: Callable[[str, str], dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sync_env("v1.1.0", "status")
    monkeypatch.setattr(ref_docs, "SOURCES", {SOURCE.name: SOURCE})
    package_json = tmp_path / "lib" / "node_modules" / "widget" / "package.json"
    package_json.parent.mkdir(parents=True)

    def row(version: str) -> dict[str, Any]:
        package_json.write_text(json.dumps({"name": "widget", "version": version}), "utf-8")
        exec_start = (
            f"{{ path=/usr/bin/node ; argv[]=/usr/bin/node {package_json.parent}/bin/widget.js ; }}"
        )
        return ref_docs.status(workspace="status", exec_start=lambda unit: exec_start)[0]

    current = row("1.1.0")
    assert current["ok"], current["problems"]
    assert current["tag"] == current["installed_tag"] == "v1.1.0"
    assert current["files"] == 3 and current["pages"] == 7
    behind = row("1.2.0")
    assert not behind["ok"]
    assert behind["problems"] == ["synced v1.1.0 but v1.2.0 is installed"]
