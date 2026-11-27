"""Git.changed_files records which owned ignored files it forced and refused (#1372)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_runtime import Executor, make, node
from verdict.orchestration.contracts import (
    NodeState,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.runtime import Git, OwnedIgnoredReport


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / ".gitignore").write_text("*.jsonl\n")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _staged(root: Path) -> list[str]:
    return [line for line in _git(root, "diff", "--cached", "--name-only").splitlines() if line]


def _credential_line() -> str:
    key = "sk-" + "".join(chr(ord("a") + i % 26) + str(i % 10) for i in range(20))
    assert len(key) == 43
    return f'API_KEY = "{key}"\n'


async def test_clean_owned_ignored_file_is_forced(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "x.jsonl").write_text('{"a": 1}\n')

    git = Git(repo)
    await git.changed_files(repo, base, owned_files=("fixtures/x.jsonl",))

    report = git.owned_ignored[str(repo)]
    assert report.forced == ("fixtures/x.jsonl",)
    assert report.refused == ()
    assert "fixtures/x.jsonl" in _staged(repo)


async def test_credential_shaped_file_is_refused_and_not_staged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "secret.jsonl").write_text(_credential_line())
    (repo / "fixtures" / "clean.jsonl").write_text("nothing sensitive\n")

    git = Git(repo)
    await git.changed_files(repo, base, owned_files=("fixtures",))

    report = git.owned_ignored[str(repo)]
    assert report.refused == (("fixtures/secret.jsonl", "credential_shaped"),)
    assert report.forced == ("fixtures/clean.jsonl",)
    staged = _staged(repo)
    assert "fixtures/secret.jsonl" not in staged
    assert "fixtures/clean.jsonl" in staged


async def test_symlink_is_refused(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    target = repo / "target.txt"
    target.write_text("target\n")
    (repo / "fixtures" / "link.jsonl").symlink_to(target)

    git = Git(repo)
    await git.changed_files(repo, base, owned_files=("fixtures/link.jsonl",))

    report = git.owned_ignored[str(repo)]
    assert report.refused == (("fixtures/link.jsonl", "symlink"),)
    assert report.forced == ()
    assert "fixtures/link.jsonl" not in _staged(repo)


async def test_byproduct_is_neither_forced_nor_refused(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "stray.pyc").write_bytes(b"\x00")
    (repo / "fixtures" / "x.jsonl").write_text("x\n")

    git = Git(repo)
    await git.changed_files(repo, base, owned_files=("fixtures",))

    report = git.owned_ignored[str(repo)]
    assert report.forced == ("fixtures/x.jsonl",)
    assert report.refused == ()
    assert "fixtures/stray.pyc" not in _staged(repo)


async def test_no_owned_files_stores_an_empty_report(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()

    git = Git(repo)
    await git.changed_files(repo, base)

    assert git.owned_ignored[str(repo)] == OwnedIgnoredReport()


async def test_reports_are_kept_per_worktree(repo: Path, tmp_path: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    other = tmp_path / "wt2"
    _git(repo, "worktree", "add", "-q", "-b", "second", str(other), base)
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "one.jsonl").write_text("one\n")
    (other / "fixtures").mkdir()
    (other / "fixtures" / "two.jsonl").write_text(_credential_line())

    git = Git(repo)
    await git.changed_files(repo, base, owned_files=("fixtures",))
    await git.changed_files(other, base, owned_files=("fixtures",))

    assert set(git.owned_ignored) == {str(repo), str(other)}
    assert git.owned_ignored[str(repo)] == OwnedIgnoredReport(forced=("fixtures/one.jsonl",))
    assert git.owned_ignored[str(other)] == OwnedIgnoredReport(
        refused=(("fixtures/two.jsonl", "credential_shaped"),)
    )


class _FixtureWriter(Executor):
    """Node ``a`` writes ``fixtures/x.jsonl``; the first call may write credential content."""

    def __init__(self, *, credential_first: bool = False, credential_always: bool = False) -> None:
        super().__init__({}, delay=0)
        self.credential_first = credential_first
        self.credential_always = credential_always
        self.writes = 0

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        if prompt.split(":", 1)[0] != "a":
            return await super().run(
                prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
            )
        self.writes += 1
        leaky = self.credential_always or (self.credential_first and self.writes == 1)
        (cwd / "fixtures").mkdir(parents=True, exist_ok=True)
        (cwd / "fixtures" / "x.jsonl").write_text(_credential_line() if leaky else '{"a": 1}\n')
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


def _fixture_node() -> WorkNode:
    return WorkNode(
        "a",
        "write a",
        owned_files=("fixtures/x.jsonl",),
        verification_command=("sh", "-c", "test -f fixtures/x.jsonl"),
    )


async def test_credential_shaped_owned_ignored_file_fails_the_attempt(
    repo: Path, billing_overlay_subscription: Any
) -> None:
    billing_overlay_subscription([("cc", "oauth"), ("kr", "oauth")])
    runtime, events, _ = make(
        repo,
        WorkGraph("g", (_fixture_node(),)),
        _FixtureWriter(credential_always=True),
        ["cc/s", "cx/g"],
    )

    result = await runtime.run()

    assert result.outcome is not RunOutcome.COMPLETE
    assert not any(e["state"] == NodeState.VALIDATED.value for e in events.of("node_state", "a"))
    barriers = [e for e in events.of("barrier", "a") if e["name"] == "ownership"]
    assert barriers
    refused = barriers[0]
    assert refused["ok"] is False
    assert any(
        "fixtures/x.jsonl" in p and "credential_shaped" in p for p in refused["refused_paths"]
    )
    assert "sk-" not in json.dumps(events.rows)
    failures = [e for e in events.of("failure", "a") if e["category"] == "verification_failed"]
    assert failures
    assert all("owned_file_not_committable" in e["evidence"] for e in failures)


async def test_retry_with_clean_content_completes_with_ok_ownership_barrier(
    repo: Path, billing_overlay_subscription: Any
) -> None:
    billing_overlay_subscription([("cc", "oauth"), ("kr", "oauth")])
    runtime, events, _ = make(
        repo,
        WorkGraph("g", (_fixture_node(),)),
        _FixtureWriter(credential_first=True),
        ["cc/s", "cx/g"],
    )

    result = await runtime.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    barriers = [e for e in events.of("barrier", "a") if e["name"] == "ownership"]
    assert barriers[0]["ok"] is False
    assert barriers[-1]["ok"] is True


async def test_clean_owned_ignored_file_is_listed_on_the_validated_event(
    repo: Path, billing_overlay_subscription: Any
) -> None:
    billing_overlay_subscription([("cc", "oauth"), ("kr", "oauth")])
    runtime, events, _ = make(
        repo, WorkGraph("g", (_fixture_node(),)), _FixtureWriter(), ["cc/s", "cx/g"]
    )

    result = await runtime.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    validated = [e for e in events.of("node_state", "a") if e["state"] == "VALIDATED"]
    assert len(validated) == 1
    assert validated[0]["force_staged"] == ["fixtures/x.jsonl"]


async def test_ordinary_tracked_file_has_no_force_staged_field(
    repo: Path, billing_overlay_subscription: Any
) -> None:
    billing_overlay_subscription([("cc", "oauth"), ("kr", "oauth")])
    runtime, events, _ = make(repo, WorkGraph("g", (node("a"),)), Executor({}), ["cc/s", "cx/g"])

    result = await runtime.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    validated = [e for e in events.of("node_state", "a") if e["state"] == "VALIDATED"]
    assert len(validated) == 1
    assert "force_staged" not in validated[0]
