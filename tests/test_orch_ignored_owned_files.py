"""Git.changed_files force-adds ignored files under a node's owned paths (#1372)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_runtime import Executor, make
from verdict.orchestration.contracts import (
    NodeState,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.runtime import Git


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


async def test_owned_ignored_file_is_committed(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "x.jsonl").write_text('{"a": 1}\n')

    git = Git(repo)
    changed = await git.changed_files(repo, base, owned_files=("fixtures/x.jsonl",))
    assert changed == ["fixtures/x.jsonl"]

    await git.commit_all(repo, "node commit")
    assert _git(repo, "show", "HEAD:fixtures/x.jsonl") == '{"a": 1}\n'


async def test_owned_directory_stages_ignored_files_but_not_byproducts(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures" / "__pycache__").mkdir(parents=True)
    (repo / "fixtures" / "y.jsonl").write_text("y\n")
    (repo / "fixtures" / "__pycache__" / "z.pyc").write_bytes(b"\x00\x01")

    changed = await Git(repo).changed_files(repo, base, owned_files=("fixtures",))

    assert changed == ["fixtures/y.jsonl"]
    assert "fixtures/__pycache__/z.pyc" not in _staged(repo)


async def test_ignored_file_outside_owned_paths_is_not_staged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "x.jsonl").write_text("owned\n")
    (repo / "other").mkdir()
    (repo / "other" / "q.jsonl").write_text("not owned\n")

    changed = await Git(repo).changed_files(repo, base, owned_files=("fixtures/x.jsonl",))

    assert changed == ["fixtures/x.jsonl"]
    assert "other/q.jsonl" not in _staged(repo)


async def test_credential_bearing_ignored_file_is_not_staged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    key = "sk-" + "".join(chr(ord("a") + i % 26) + str(i % 10) for i in range(20))
    assert len(key) == 43
    (repo / "fixtures" / "secret.jsonl").write_text(f'API_KEY = "{key}"\n')
    (repo / "fixtures" / "clean.jsonl").write_text("nothing sensitive\n")

    changed = await Git(repo).changed_files(repo, base, owned_files=("fixtures",))

    assert changed == ["fixtures/clean.jsonl"]
    assert "fixtures/secret.jsonl" not in _staged(repo)


async def test_symlink_at_owned_ignored_path_is_not_staged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    target = repo / "target.txt"
    target.write_text("target\n")
    (repo / "fixtures" / "link.jsonl").symlink_to(target)

    changed = await Git(repo).changed_files(repo, base, owned_files=("fixtures/link.jsonl",))

    assert "fixtures/link.jsonl" not in changed
    assert "fixtures/link.jsonl" not in _staged(repo)


async def test_oversized_listing_raises(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "x.jsonl").write_text("x\n")
    git = Git(repo)
    real_run = git.run

    async def run(argv: Any, cwd: Path, timeout: float) -> tuple[int, str]:
        if "ls-files" in argv and "-o" in argv:
            return 0, "a" * 4000
        return await real_run(argv, cwd, timeout)

    git.run = run  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="too large"):
        await git.changed_files(repo, base, owned_files=("fixtures",))


class _IgnoredFixtureExecutor(Executor):
    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        if prompt.split(":", 1)[0] == "a":
            (cwd / "fixtures").mkdir(parents=True, exist_ok=True)
            (cwd / "fixtures" / "x.jsonl").write_text('{"a": 1}\n')
            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
            )
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


async def test_ignored_owned_file_survives_to_dependent_node_and_integration(
    repo: Path, billing_overlay_subscription: Any
) -> None:
    billing_overlay_subscription([("cc", "oauth"), ("kr", "oauth")])
    node_a = WorkNode(
        "a",
        "write a",
        owned_files=("fixtures/x.jsonl",),
        verification_command=("sh", "-c", "test -f fixtures/x.jsonl"),
    )
    node_b = WorkNode(
        "b",
        "write b",
        depends_on=("a",),
        owned_files=("b.txt",),
        verification_command=("sh", "-c", "test -f fixtures/x.jsonl && test -f b.txt"),
    )
    runtime, _, _ = make(
        repo, WorkGraph("g", (node_a, node_b)), _IgnoredFixtureExecutor({}), ["cc/s", "cx/g"]
    )

    result = await runtime.run()

    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert all(r.state is NodeState.VALIDATED for r in result.nodes.values())
    shown = subprocess.run(
        ["git", "show", f"{result.integration_ref}:fixtures/x.jsonl"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert shown.returncode == 0
    assert shown.stdout == '{"a": 1}\n'


async def test_byproducts_under_an_owned_directory_do_not_trip_the_listing_guard(
    repo: Path,
) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures" / "__pycache__").mkdir(parents=True)
    (repo / "fixtures" / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "fixtures" / "x.jsonl").write_text("x\n")
    for n in range(120):
        (repo / "fixtures" / "__pycache__" / f"module_number_{n:03d}.cpython-314.pyc").write_bytes(
            b"\x00"
        )
    (repo / "fixtures" / "node_modules" / "pkg" / "index.js").write_text("1\n")

    changed = await Git(repo).changed_files(repo, base, owned_files=("fixtures",))

    assert "fixtures/x.jsonl" in changed
    assert not any("__pycache__" in p or "node_modules" in p for p in changed)
    raw = _git(repo, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--", "fixtures")
    assert len(raw) >= 4000


async def test_owned_file_inside_an_ignored_directory_is_staged(repo: Path) -> None:
    (repo / ".gitignore").write_text("*.jsonl\nbuild/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "ignore build")
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "build" / "deep").mkdir(parents=True)
    (repo / "build" / "deep" / "out.txt").write_text("out\n")

    changed = await Git(repo).changed_files(repo, base, owned_files=("build/deep/out.txt",))

    assert "build/deep/out.txt" in changed


async def test_stray_pyc_outside_pycache_is_not_staged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "stray.pyc").write_bytes(b"\x00")
    (repo / "fixtures" / "x.jsonl").write_text("x\n")

    await Git(repo).changed_files(repo, base, owned_files=("fixtures",))

    staged = _staged(repo)
    assert "fixtures/x.jsonl" in staged
    assert "fixtures/stray.pyc" not in staged
