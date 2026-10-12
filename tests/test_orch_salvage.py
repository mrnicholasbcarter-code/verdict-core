"""BOD-308: partial-work salvage and restoration in DagRuntime retry.

Tests exercise:
- the salvage helper functions directly (unit)
- full DagRuntime failure-to-retry restoration through the real production path
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import verdict.orchestration.salvage as salvage_module
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    FailureClassification,
    NodeKind,
    RouteVerdict,
    RunOutcome,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.salvage import restore_salvaged_files, salvage_owned_files

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ helpers


class Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> None:
        json.dumps(data, default=str)
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["type"] == type and (node_id is None or r["node_id"] == node_id)
        ]


class Selector:
    """Minimal selector that returns routes in order, with cooldown support."""

    def __init__(self, routes: list[str]) -> None:
        self.routes = routes
        self.cool: dict[str, float] = {}
        self.successes: list[str] = []

    def select(
        self, requirements: TaskRequirements, *, now: datetime
    ) -> tuple[RouteVerdict | None, tuple[RouteVerdict, ...]]:
        verdicts: list[RouteVerdict] = []
        chosen = None
        for rank, route in enumerate(self.routes, 1):
            provider = route.split("/")[0]
            if route in requirements.exclude_routes:
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.AVAILABLE,
                        EligibilityStage.TASK_ELIGIBLE,
                        "excluded",
                    )
                )
                continue
            if (
                self.cool.get(route, 0) > now.timestamp()
                or self.cool.get(provider, 0) > now.timestamp()
            ):
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.HEALTHY,
                        EligibilityStage.AVAILABLE,
                        "cooldown",
                    )
                )
                continue
            v = RouteVerdict(
                route,
                provider,
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.SUBSCRIPTION,
                rank=rank,
            )
            verdicts.append(v)
            if chosen is None:
                chosen = v
        return chosen, tuple(verdicts)

    def record_failure(
        self, route_id: str, failure: FailureClassification, *, now: datetime
    ) -> None:
        key = route_id.split("/")[0] if failure.scope == "provider" else route_id
        self.cool[key] = now.timestamp() + failure.cooldown_seconds

    def record_success(self, route_id: str, *, now: datetime) -> None:
        self.successes.append(route_id)


class Executor:
    """Scripted executor that writes partial work, then fails on first attempt."""

    def __init__(
        self,
        fail_on: set[tuple[str, str]] | None = None,
        partial_content: str = "partial work from attempt 1",
    ) -> None:
        self.fail_on = fail_on or set()
        self.partial_content = partial_content
        self.calls: list[tuple[str, str]] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        node_id = prompt.split(":", 1)[0]
        self.calls.append((node_id, route_id))
        await asyncio.sleep(0.01)

        if (node_id, route_id) in self.fail_on:
            # Write partial work before failing
            (cwd / f"{node_id}.txt").write_text(self.partial_content)
            return WorkerTerminal(ok=False, model=route_id, error="worker failed")

        # On success, build on any restored content
        existing = ""
        owned_file = cwd / f"{node_id}.txt"
        if owned_file.exists():
            existing = owned_file.read_text()
        owned_file.write_text(existing + f"\ncompleted by {route_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


class SimpleClassifier:
    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification:
        return FailureClassification("worker_error", "REROUTE", 300, "route", terminal.error[:200])


def _node(nid: str, deps: tuple[str, ...] = ()) -> WorkNode:
    check = ("sh", "-c", f"test -f {nid}.txt && ! grep -q FAIL {nid}.txt")
    return WorkNode(
        nid,
        f"write {nid}",
        kind=NodeKind.IMPLEMENT,
        depends_on=deps,
        owned_files=(f"{nid}.txt",),
        verification_command=check,
    )


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
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


# ====================================================================
# UNIT TESTS: salvage and restore helpers
# ====================================================================


class TestSalvageOwnedFiles:
    """Unit tests for salvage_owned_files."""

    def test_salvages_owned_modified_files(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").write_text("partial work")
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert result.saved == ("a.txt",)
        assert not result.skipped
        assert not result.error
        assert (salvage_dir / "a.txt").read_text() == "partial work"
        assert result.total_bytes > 0

    def test_skips_unowned_files(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").write_text("owned")
        (worktree / "b.txt").write_text("unowned")
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert result.saved == ("a.txt",)
        assert not (salvage_dir / "b.txt").exists()

    def test_skips_credential_bearing_content(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").write_text("api_key=sk-ant-abc123def456ghi789jkl012mno345pq")
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert not result.saved
        assert result.skipped == ("a.txt",)

    def test_empty_worktree_returns_empty(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert result.saved == ()
        assert result.skipped == ()
        assert not result.error

    def test_directory_ownership_salvages_children(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        (worktree / "src").mkdir(parents=True)
        (worktree / "src" / "main.py").write_text("code")
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("src/",), salvage_dir)

        assert "src/main.py" in result.saved

    def test_oversized_file_skipped(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        # Write a file larger than _MAX_FILE_BYTES (1 MiB)
        (worktree / "a.txt").write_bytes(b"x" * (1_048_576 + 1))
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert not result.saved
        assert result.skipped == ("a.txt",)

    def test_rejects_unsafe_owned_paths(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("must remain outside")
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(
            worktree, ("../outside.txt", "/absolute.txt", "./dot.txt"), salvage_dir
        )

        assert not result.saved
        assert set(result.skipped) == {"../outside.txt", "/absolute.txt", "./dot.txt"}
        assert not salvage_dir.exists()
        assert outside.read_text() == "must remain outside"

    def test_source_symlink_secret_outside_ignored(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("api_key=sk-ant-abc123def456ghi789jkl012mno345pq")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").symlink_to(outside)
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert not result.saved
        assert result.skipped == ("a.txt",)
        assert not (salvage_dir / "a.txt").exists()
        assert outside.read_text().startswith("api_key=")

    def test_unreadable_content_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").write_text("partial")
        salvage_dir = tmp_path / "salvage"

        def unreadable(_: Path) -> str:
            raise OSError("permission denied: raw path must not leak")

        monkeypatch.setattr(salvage_module, "_read_text_no_follow", unreadable)

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert not result.saved
        assert result.skipped == ("a.txt",)
        assert not result.error
        assert not (salvage_dir / "a.txt").exists()

    def test_oversized_file_is_not_read_for_secret_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").write_bytes(b"x" * (1_048_576 + 1))
        salvage_dir = tmp_path / "salvage"

        def fail_if_read(_: str) -> str:
            raise AssertionError("oversized file content was read")

        monkeypatch.setattr(salvage_module, "redact_text", fail_if_read)

        result = salvage_owned_files(worktree, ("a.txt",), salvage_dir)

        assert not result.saved
        assert result.skipped == ("a.txt",)
        assert not (salvage_dir / "a.txt").exists()

    def test_salvage_rejects_symlink_destination_component(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        (worktree / "safe").mkdir(parents=True)
        (worktree / "safe" / "a.txt").write_text("safe text")
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (salvage_dir / "safe").symlink_to(outside, target_is_directory=True)

        result = salvage_owned_files(worktree, ("safe/",), salvage_dir)

        assert not result.saved
        assert "safe/a.txt" in result.skipped
        assert not (outside / "a.txt").exists()

    def test_salvage_total_size_cap_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(salvage_module, "_MAX_TOTAL_BYTES", 5)
        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "a.txt").write_text("123")
        (worktree / "b.txt").write_text("456")
        salvage_dir = tmp_path / "salvage"

        result = salvage_owned_files(worktree, ("a.txt", "b.txt"), salvage_dir)

        assert result.saved == ("a.txt",)
        assert result.skipped == ("b.txt",)
        assert result.total_bytes == 3
        assert (salvage_dir / "a.txt").read_text() == "123"
        assert not (salvage_dir / "b.txt").exists()


class TestRestoreSalvagedFiles:
    """Unit tests for restore_salvaged_files."""

    def test_restores_salvaged_files(self, tmp_path: Path) -> None:
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        (salvage_dir / "a.txt").write_text("partial")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = restore_salvaged_files(salvage_dir, worktree, ("a.txt",))

        assert result.restored == ("a.txt",)
        assert (worktree / "a.txt").read_text() == "partial"

    def test_missing_salvage_dir_is_no_change_safe(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = restore_salvaged_files(tmp_path / "missing", worktree, ("a.txt",))

        assert result.restored == ()
        assert not result.error

    def test_rejects_unowned_files_in_salvage(self, tmp_path: Path) -> None:
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        (salvage_dir / "a.txt").write_text("owned")
        (salvage_dir / "b.txt").write_text("unowned")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = restore_salvaged_files(salvage_dir, worktree, ("a.txt",))

        assert result.restored == ("a.txt",)
        assert "b.txt" in result.skipped
        assert not (worktree / "b.txt").exists()

    def test_rejects_credential_content_on_restore(self, tmp_path: Path) -> None:
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        (salvage_dir / "a.txt").write_text("api_key=sk-ant-abc123def456ghi789jkl012mno345pq")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = restore_salvaged_files(salvage_dir, worktree, ("a.txt",))

        assert not result.restored
        assert "a.txt" in result.skipped

    def test_restore_rejects_path_traversal_and_leaves_outside_unchanged(
        self, tmp_path: Path
    ) -> None:
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        worktree = tmp_path / "wt"
        worktree.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("outside original")

        result = restore_salvaged_files(salvage_dir, worktree, ("../outside.txt",))

        assert not result.restored
        assert result.error == ""
        assert outside.read_text() == "outside original"
        assert not (worktree / "outside.txt").exists()

    def test_restore_rejects_symlink_destination_component(self, tmp_path: Path) -> None:
        salvage_dir = tmp_path / "salvage"
        (salvage_dir / "link").mkdir(parents=True)
        (salvage_dir / "link" / "secret.txt").write_text("safe text")
        worktree = tmp_path / "wt"
        worktree.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside original")
        (worktree / "link").symlink_to(outside, target_is_directory=True)

        result = restore_salvaged_files(salvage_dir, worktree, ("link/",))

        assert not result.restored
        assert "link/secret.txt" in result.skipped
        assert (outside / "secret.txt").read_text() == "outside original"

    def test_restore_rejects_symlink_source_outside(self, tmp_path: Path) -> None:
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("do not copy")
        (salvage_dir / "a.txt").symlink_to(outside)
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = restore_salvaged_files(salvage_dir, worktree, ("a.txt",))

        assert not result.restored
        assert "a.txt" in result.skipped
        assert not (worktree / "a.txt").exists()
        assert outside.read_text() == "do not copy"

    def test_restore_total_size_cap_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(salvage_module, "_MAX_TOTAL_BYTES", 5)
        salvage_dir = tmp_path / "salvage"
        salvage_dir.mkdir()
        (salvage_dir / "a.txt").write_text("123")
        (salvage_dir / "b.txt").write_text("456")
        worktree = tmp_path / "wt"
        worktree.mkdir()

        result = restore_salvaged_files(salvage_dir, worktree, ("a.txt", "b.txt"))

        assert result.restored == ("a.txt",)
        assert result.skipped == ("b.txt",)
        assert result.total_bytes == 3
        assert (worktree / "a.txt").read_text() == "123"
        assert not (worktree / "b.txt").exists()


# ====================================================================
# INTEGRATION TESTS: DagRuntime failure->retry with salvage/restore
# ====================================================================


class TestDagRuntimeSalvageRestore:
    """Full DagRuntime flow: first attempt fails with partial work,
    second attempt gets it restored, then succeeds."""

    def test_fail_salvage_retry_restore(self, repo: Path) -> None:
        """Core flow: attempt 1 fails, partial work is salvaged, attempt 2
        restores it and succeeds."""
        run_dir = repo.parent / "run1"
        events = Events()
        executor = Executor(fail_on={("a", "r1/m1")}, partial_content="partial from attempt 1")

        runtime = DagRuntime(
            repo=repo,
            run_dir=run_dir,
            graph=WorkGraph("g", (_node("a"),)),
            selector=Selector(["r1/m1", "r2/m2"]),
            executor=executor,
            classifier=SimpleClassifier(),
            events=events,
            prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
            reviewer=None,
            policy=RuntimePolicy(require_review=False, run_repo_gates=False),
            now=lambda: NOW,
        )

        result = asyncio.run(runtime.run())

        # Run should complete successfully on second attempt
        assert result.outcome is RunOutcome.COMPLETE, result.reason

        # Verify both routes were tried
        assert executor.calls == [("a", "r1/m1"), ("a", "r2/m2")]

        # Verify salvage event was emitted (content-free)
        salvage_events = events.of("salvage", "a")
        assert len(salvage_events) == 1
        assert salvage_events[0]["saved"] == ["a.txt"]
        assert salvage_events[0]["attempt"] == 1
        assert "total_bytes" in salvage_events[0]
        # No actual file content in the event
        for key, val in salvage_events[0].items():
            if isinstance(val, str) and key not in ("type", "node_id", "error"):
                assert "partial" not in val, f"content leaked in event field {key}"

        # Verify restore event was emitted
        restore_events = events.of("restore", "a")
        assert len(restore_events) == 1
        assert restore_events[0]["restored"] == ["a.txt"]
        assert restore_events[0]["attempt"] == 2

        # Verify the salvage directory exists with the partial work
        salvage_dir = run_dir / "salvage" / "a"
        assert salvage_dir.exists()
        assert (salvage_dir / "a.txt").read_text() == "partial from attempt 1"

    def test_no_salvage_on_first_attempt_success(self, repo: Path) -> None:
        """No salvage events when the first attempt succeeds."""
        run_dir = repo.parent / "run1"
        events = Events()
        executor = Executor(fail_on=set())

        runtime = DagRuntime(
            repo=repo,
            run_dir=run_dir,
            graph=WorkGraph("g", (_node("a"),)),
            selector=Selector(["r1/m1"]),
            executor=executor,
            classifier=SimpleClassifier(),
            events=events,
            prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
            reviewer=None,
            policy=RuntimePolicy(require_review=False, run_repo_gates=False),
            now=lambda: NOW,
        )

        result = asyncio.run(runtime.run())
        assert result.outcome is RunOutcome.COMPLETE
        assert not events.of("salvage")
        assert not events.of("restore")

    def test_no_restore_when_no_salvage_exists(self, repo: Path) -> None:
        """If the first attempt fails without writing owned files, no restore
        occurs on retry, and the run completes normally."""
        run_dir = repo.parent / "run1"
        events = Events()

        class NoWriteExecutor:
            """Fails without writing anything on first attempt."""

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []
                self.attempt = 0

            async def run(
                self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
            ) -> WorkerTerminal:
                node_id = prompt.split(":", 1)[0]
                self.calls.append((node_id, route_id))
                self.attempt += 1
                if self.attempt == 1:
                    return WorkerTerminal(ok=False, model=route_id, error="boom")
                (cwd / f"{node_id}.txt").write_text("done\n")
                return WorkerTerminal(
                    ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
                )

        runtime = DagRuntime(
            repo=repo,
            run_dir=run_dir,
            graph=WorkGraph("g", (_node("a"),)),
            selector=Selector(["r1/m1", "r2/m2"]),
            executor=NoWriteExecutor(),
            classifier=SimpleClassifier(),
            events=events,
            prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
            reviewer=None,
            policy=RuntimePolicy(require_review=False, run_repo_gates=False),
            now=lambda: NOW,
        )

        result = asyncio.run(runtime.run())
        assert result.outcome is RunOutcome.COMPLETE
        # No salvage event because no owned files were written
        assert not events.of("salvage")
        # No restore event because no salvage dir exists
        assert not events.of("restore")

    def test_credential_bearing_partial_work_rejected(self, repo: Path) -> None:
        """Partial work containing credentials is not salvaged."""
        run_dir = repo.parent / "run1"
        events = Events()
        executor = Executor(
            fail_on={("a", "r1/m1")},
            partial_content="api_key=sk-ant-abc123def456ghi789jkl012mno345pq",
        )

        runtime = DagRuntime(
            repo=repo,
            run_dir=run_dir,
            graph=WorkGraph("g", (_node("a"),)),
            selector=Selector(["r1/m1", "r2/m2"]),
            executor=executor,
            classifier=SimpleClassifier(),
            events=events,
            prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
            reviewer=None,
            policy=RuntimePolicy(require_review=False, run_repo_gates=False),
            now=lambda: NOW,
        )

        result = asyncio.run(runtime.run())
        assert result.outcome is RunOutcome.COMPLETE

        # Salvage should skip the credential-bearing file
        salvage_events = events.of("salvage", "a")
        assert len(salvage_events) == 1
        assert salvage_events[0]["saved"] == []
        assert "a.txt" in salvage_events[0]["skipped"]

        # No restore because nothing was saved
        assert not events.of("restore")

    def test_existing_acceptance_verification_unchanged(self, repo: Path) -> None:
        """Salvage/restore does not alter the existing ownership or verification
        barriers: a failing verification still blocks the node."""
        run_dir = repo.parent / "run1"
        events = Events()

        class AlwaysBadCode:
            """Writes owned file with FAIL content so verification rejects it."""

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run(
                self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
            ) -> WorkerTerminal:
                node_id = prompt.split(":", 1)[0]
                self.calls.append((node_id, route_id))
                (cwd / f"{node_id}.txt").write_text("FAIL")
                return WorkerTerminal(
                    ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
                )

        runtime = DagRuntime(
            repo=repo,
            run_dir=run_dir,
            graph=WorkGraph("g", (_node("a"),)),
            selector=Selector(["r1/m1", "r2/m2", "r3/m3", "r4/m4"]),
            executor=AlwaysBadCode(),
            classifier=SimpleClassifier(),
            events=events,
            prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
            reviewer=None,
            policy=RuntimePolicy(require_review=False, run_repo_gates=False),
            now=lambda: NOW,
        )

        result = asyncio.run(runtime.run())

        # Should be BLOCKED because verification always fails
        assert result.outcome is RunOutcome.BLOCKED

        # Verification barrier events should show failures
        verify_events = events.of("verify", "a")
        assert all(not v["ok"] for v in verify_events)

    def test_salvage_events_are_content_free(self, repo: Path) -> None:
        """Salvage and restore events carry metadata only, never file content."""
        run_dir = repo.parent / "run1"
        events = Events()
        secret_content = "super secret implementation details xyz123"
        executor = Executor(fail_on={("a", "r1/m1")}, partial_content=secret_content)

        runtime = DagRuntime(
            repo=repo,
            run_dir=run_dir,
            graph=WorkGraph("g", (_node("a"),)),
            selector=Selector(["r1/m1", "r2/m2"]),
            executor=executor,
            classifier=SimpleClassifier(),
            events=events,
            prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
            reviewer=None,
            policy=RuntimePolicy(require_review=False, run_repo_gates=False),
            now=lambda: NOW,
        )

        asyncio.run(runtime.run())

        # Serialize all events and check no content leaked
        all_events_str = json.dumps(events.rows)
        assert secret_content not in all_events_str
