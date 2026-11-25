"""OC2a (#1301): how ``run_autodev`` and ``verdict autodev`` use the isolated OpenCode executor.

Task groups 4 and 5 of ``openspec/changes/autodev-opencode-isolated-executor``. The OpenCode
executor is a stub with the real ``run`` signature, the verification command is run by a scripted
runner, and every test runs in a temporary home: nothing here calls a model, a probe, the real
``opencode`` or the real ``verdict serve``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_autodev_opencode import (  # noqa: F401  (the fixture isolates HOME and git)
    StubRunner,
    _isolated_home,
    edit_a,
    git,
    make_repo,
    snapshot,
    write,
)
from tests.test_autodev_wiring import _decomposer, run_autodev
from verdict import autodev_opencode as oc
from verdict.autodev_run import AUTODEV_SCOPE, AutodevError
from verdict.outcome_records import attempt_records_path
from verdict.patch_executor import PatchExecutor, PatchExecutorConfig
from verdict.paths import verdict_home
from verdict.receipt_store import ReceiptStore

# A lint-only command is the one a unit may use without owning a test file (the decomposer's rule).
VERIFY_PREFIX = [sys.executable, "-m", "ruff", "check"]


def plan(*units: tuple[str, str]) -> list[dict[str, Any]]:
    """One plan unit per (unit id, owned file), each verified by a lint-only command the scripted runner answers."""
    return [
        {
            "unit_id": unit_id,
            "objective": f"repair {owned}",
            "owned_files": [owned],
            "verification_command": [*VERIFY_PREFIX, "--select", "F401", owned],
            "context": "",
        }
        for unit_id, owned in units
    ]


class ScriptedRunner:
    """Runs everything for real except the unit's verification command, which it scripts."""

    def __init__(self, verify: Any = None) -> None:
        self.verify = verify
        self.verify_calls = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        if list(argv)[:4] == VERIFY_PREFIX and "--fix" not in argv:
            self.verify_calls += 1
            if self.verify is None:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return self.verify(self.verify_calls)
        return subprocess.run(argv, **kwargs)


def failing(code: int = 1) -> Any:
    return lambda _call: subprocess.CompletedProcess(["verify"], code, "", "assertion failed")


class SpyExecutor(oc.OpenCodeUnitExecutor):
    """The real executor, counting its ``restore()`` calls."""

    restore_calls = 0

    def restore(self) -> str:
        self.restore_calls += 1
        return super().restore()


def isolated(
    repo: Path, tmp_path: Path, runner: StubRunner, *, cls: type[SpyExecutor] = SpyExecutor
) -> SpyExecutor:
    temp_root = tmp_path / "attempts"
    temp_root.mkdir(exist_ok=True)
    return cls(
        repo, runner, model="cheap/model", base_url="http://127.0.0.1:8000", temp_root=temp_root
    )


def go(
    repo: Path,
    executor: Any,
    units: list[dict[str, Any]],
    *,
    verify: Any = None,
    mechanical: bool = False,
    runner: Any = None,
    **kwargs: Any,
) -> Any:
    return run_autodev(
        "fix it",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(units),
        executor=executor,
        mechanical=mechanical,
        runner=runner or ScriptedRunner(verify),
        **kwargs,
    )


def edit_owned(name: str, text: str = "x = 2\n") -> Any:
    return lambda tree: write(tree, name, text)


class TestRestoreWiring:
    def test_verification_fails_after_an_applied_attempt(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        write(repo, "b.py", "dirty = 1\n")  # a pre-existing dirty, unowned file
        before = snapshot(repo)
        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        report = go(repo, executor, plan(("u1", "a.py")), verify=failing())
        assert len(report.failed) == 1
        reason = report.failed[0].reason
        assert reason.startswith("verification exited 1")
        assert "restored" in reason
        assert snapshot(repo) == before
        assert (repo / "b.py").read_text() == "dirty = 1\n"
        assert executor.restore_calls == 1

    def test_verification_command_writes_to_the_repository(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)

        def rewrite(_call: int) -> Any:
            write(repo, "a.py", "rewritten by the verification command\n")
            write(repo, "created_by_command.txt", "new\n")
            return subprocess.CompletedProcess(["verify"], 1, "", "failed")

        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        report = go(repo, executor, plan(("u1", "a.py")), verify=rewrite)
        reason = report.failed[0].reason
        assert (repo / "a.py").read_text() == "rewritten by the verification command\n"
        assert (repo / "created_by_command.txt").exists()
        assert "not restored" in reason and "a.py" in reason

    def test_user_edit_after_the_transfer_is_kept(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)

        def user_edits(_call: int) -> Any:
            write(repo, "a.py", "the user's own edit\n")
            return subprocess.CompletedProcess(["verify"], 1, "", "failed")

        report = go(
            repo,
            isolated(repo, tmp_path, StubRunner(edit_a)),
            plan(("u1", "a.py")),
            verify=user_edits,
        )
        assert (repo / "a.py").read_text() == "the user's own edit\n"
        assert "a.py" in report.failed[0].reason and "not restored" in report.failed[0].reason

    def test_verified_unit_is_not_restored(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        report = go(repo, executor, plan(("u1", "a.py")))
        assert len(report.verified) == 1
        assert (repo / "a.py").read_text() == "x = 2\n"
        assert executor.restore_calls == 0

    @pytest.mark.parametrize("second", ["rejected", "error", "raises"])
    def test_earlier_verified_unit_is_not_undone(self, tmp_path: Path, second: str) -> None:
        repo = make_repo(tmp_path)

        def behave(tree: Path) -> None:
            if stub.calls[-1]["node_id"] == "u1":
                write(tree, "a.py", "x = 2\n")
            elif second == "error":
                stub.ok, stub.error = False, "exited 1"  # unit two: the worker fails
            elif second == "raises":
                raise RuntimeError("the OpenCode executor blew up on unit two")
            # unit two with "rejected": the worker changes nothing

        stub = StubRunner(behave)
        executor = isolated(repo, tmp_path, stub)
        report = go(repo, executor, plan(("u1", "a.py"), ("u2", "b.py")))
        assert [o.verified for o in report.outcomes] == [True, False]
        if second == "raises":
            assert report.outcomes[1].reason.startswith("executor error: RuntimeError")
        assert (repo / "a.py").read_text() == "x = 2\n"
        assert (repo / "b.py").read_text() == "y = 2\n"

    def test_verification_raises(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)

        def explode(_call: int) -> Any:
            raise PermissionError("verification runner exploded")

        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        with pytest.raises(PermissionError, match="exploded"):
            go(repo, executor, plan(("u1", "a.py")), verify=explode)
        assert executor.restore_calls == 1
        assert snapshot(repo) == before

    def test_attribution_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import verdict.autodev_run as run_module

        repo = make_repo(tmp_path)
        before = snapshot(repo)
        real = run_module._touched_since
        calls: list[int] = []

        def explode(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            raise RuntimeError("attribution failed")

        monkeypatch.setattr(run_module, "_touched_since", explode)
        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        with pytest.raises(RuntimeError, match="attribution failed"):
            go(repo, executor, plan(("u1", "a.py")))
        monkeypatch.setattr(run_module, "_touched_since", real)
        assert calls and executor.restore_calls == 1
        assert snapshot(repo) == before

    def test_interrupt_after_the_transfer(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)

        def interrupt(_call: int) -> Any:
            raise KeyboardInterrupt

        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        with pytest.raises(KeyboardInterrupt):
            go(repo, executor, plan(("u1", "a.py")), verify=interrupt)
        assert executor.restore_calls == 1
        assert snapshot(repo) == before

    def test_executor_raises_after_its_transfer(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)

        class RaisesAfter(SpyExecutor):
            def execute_unit(self, unit: Any) -> Any:
                super().execute_unit(unit)
                raise RuntimeError("raised after the transfer was written")

        executor = isolated(repo, tmp_path, StubRunner(edit_a), cls=RaisesAfter)
        report = go(repo, executor, plan(("u1", "a.py")))
        assert report.failed[0].reason.startswith("executor error: RuntimeError")
        assert executor.restore_calls == 1
        assert snapshot(repo) == before

    def test_interrupt_inside_the_executor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = make_repo(tmp_path)
        before = snapshot(repo)
        real_rename = os.rename

        def interrupted(src: str, dst: str, *args: Any, **kwargs: Any) -> None:
            real_rename(src, dst, *args, **kwargs)  # the rename over a.py really happens ...
            if dst == "a.py":
                raise KeyboardInterrupt  # ... and the interrupt arrives right after it

        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        monkeypatch.setattr(os, "rename", interrupted)
        try:
            with pytest.raises(KeyboardInterrupt):
                go(repo, executor, plan(("u1", "a.py")))
        finally:
            monkeypatch.setattr(os, "rename", real_rename)
        assert executor.restore_calls == 1
        assert snapshot(repo) == before

    def test_restore_fails_while_an_exception_propagates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = make_repo(tmp_path)

        class BrokenRestore(SpyExecutor):
            def restore(self) -> str:
                self.restore_calls += 1
                raise RuntimeError("restore itself broke")

        def explode(_call: int) -> Any:
            raise PermissionError("the original exception")

        executor = isolated(repo, tmp_path, StubRunner(edit_a), cls=BrokenRestore)
        with pytest.raises(PermissionError, match="the original exception"):
            go(repo, executor, plan(("u1", "a.py")), verify=explode)
        assert "restore itself broke" in capsys.readouterr().err

    def test_restore_capability_alone_grants_nothing(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)

        class RestoreOnly:
            """Has restore() but is not marked isolated."""

            def __init__(self, base_url: str) -> None:
                self.config = PatchExecutorConfig(model="cheap/model", base_url=base_url)
                self.response_headers: dict[str, str] = {}

            def execute_unit(self, unit: Any) -> Any:
                from verdict.patch_executor import PatchAttempt

                return PatchAttempt(unit.unit_id, "cheap/model", "applied")

            def restore(self) -> str:
                return ""

        with pytest.raises(AutodevError, match="gateway does not match"):
            go(repo, RestoreOnly("http://127.0.0.1:8000"), plan(("u1", "a.py")))
        # Not isolated, so not subject to the retry refusal either: the matching gateway passes.
        matching = RestoreOnly("http://127.0.0.1:20128/v1")
        report = go(
            repo,
            matching,
            plan(("u1", "a.py")),
            retry_routes=[{"model": "other/model", "gateway": "http://127.0.0.1:20128/v1"}],
        )
        assert len(report.verified) == 1  # no retry refusal, and no retry was needed

    def test_patch_executor_is_never_restored(self) -> None:
        assert not hasattr(PatchExecutor, "restore")
        assert not hasattr(PatchExecutor, "preflight")
        assert not hasattr(PatchExecutor, "begin_run")
        assert getattr(PatchExecutor, "isolated", False) is not True


class TestRunId:
    def test_run_id_and_unit_id_reach_the_opencode_executor(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        stub = StubRunner(lambda tree: write(tree, "a.py", "x = 2\n"))
        executor = isolated(repo, tmp_path, stub)
        store = ReceiptStore(":memory:")
        report = run_autodev(
            "fix it",
            repo,
            store=store,
            decomposer=_decomposer(plan(("u1", "a.py"))),
            executor=executor,
            mechanical=False,
            runner=ScriptedRunner(),
        )
        assert len(report.verified) == 1
        payload = store.query_receipts(scope=AUTODEV_SCOPE)[0].payload
        assert payload["attempt_run_id"].startswith("autodev-")
        assert stub.calls[0]["run_id"] == payload["attempt_run_id"]
        assert stub.calls[0]["node_id"] == "u1"

    def test_patch_executor_gets_no_run_id(self, tmp_path: Path) -> None:
        # The default path passes no run id to anything: PatchExecutor has no begin_run to call.
        repo = make_repo(tmp_path)
        executor = PatchExecutor(
            repo,
            PatchExecutorConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
            transport=lambda *_a, **_k: {
                "status_code": 200,
                "body": {"choices": [{"message": {"content": "no diff"}}]},
            },
        )
        report = go(repo, executor, plan(("u1", "a.py")))
        assert len(report.failed) == 1


class TestRetries:
    def test_retry_routes_with_the_isolated_executor(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        route = {"model": "other/model", "gateway": "http://127.0.0.1:20128/v1"}

        class NeverPlans:
            """A decomposer that must not be reached."""

            def __init__(self) -> None:
                self.config = PatchExecutorConfig(
                    model="cheap/model", base_url="http://127.0.0.1:20128/v1"
                )

            def decompose(self, *_a: Any, **_k: Any) -> Any:
                raise AssertionError("the decomposer was called before the retry refusal")

        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        with pytest.raises(AutodevError, match="retries"):
            run_autodev(
                "fix it",
                repo,
                store=ReceiptStore(":memory:"),
                decomposer=NeverPlans(),  # type: ignore[arg-type]
                executor=executor,
                mechanical=False,
                retry_routes=[route],
            )
        with pytest.raises(AutodevError, match="retries"):
            run_autodev(
                "fix it",
                repo,
                store=ReceiptStore(":memory:"),
                decomposer=NeverPlans(),  # type: ignore[arg-type]
                executor=executor,
                mechanical=False,
                retry_executor_factory=lambda _route: executor,  # type: ignore[arg-type,return-value]
            )

    def test_failed_unit_is_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import verdict.autodev_run as run_module

        def never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("PatchExecutor was constructed")

        repo = make_repo(tmp_path)
        stub = StubRunner()  # the worker changes nothing: the attempt is rejected
        executor = isolated(repo, tmp_path, stub)
        monkeypatch.setattr(run_module, "PatchExecutor", never)
        report = go(repo, executor, plan(("u1", "a.py")))
        assert len(report.failed) == 1 and len(report.outcomes) == 1
        assert report.failed[0].reason.startswith("worker made no change")
        assert len(stub.calls) == 1

    def test_attempt_record_for_an_isolated_unit_is_written(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        executor = isolated(repo, tmp_path, StubRunner(edit_a))
        report = go(repo, executor, plan(("u1", "a.py")))
        assert len(report.verified) == 1
        rows = [
            json.loads(line)
            for line in attempt_records_path(verdict_home()).read_text().splitlines()
            if line.strip()
        ]
        mine = [row for row in rows if row.get("node_id") == "u1"]
        assert len(mine) == 1
        assert mine[0]["model"] == "cheap/model" and mine[0]["outcome"] == "accepted"
        assert mine[0]["effective_parameters"] is None  # unsent settings stay unknown
        assert mine[0]["harness"] == "verdict-autodev-executor"


# ------------------------------------------------------------------------------------------
# 4.3 the precondition hook
# ------------------------------------------------------------------------------------------

RUFF_VERIFY = [sys.executable, "-m", "ruff", "check", "a.py"]


def ruff_unit() -> list[dict[str, Any]]:
    return [
        {
            "unit_id": "u1",
            "objective": "repair a.py",
            "owned_files": ["a.py"],
            "verification_command": RUFF_VERIFY,
            "context": "",
        }
    ]


class MechanicalRunner:
    """A fake for the mechanical tier: ``ruff --fix`` edits a.py and the check keeps failing."""

    def __init__(self, repo: Path, *, check_codes: list[int], edit: str) -> None:
        self.repo = repo
        self.check_codes = list(check_codes)
        self.edit = edit
        self.fixed = 0

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        argv = list(argv)
        if argv[:3] == [sys.executable, "-m", "ruff"]:
            if "--fix" in argv:
                self.fixed += 1
                (self.repo / "a.py").write_text(self.edit)
                return subprocess.CompletedProcess(argv, 0, "", "")
            code = self.check_codes.pop(0) if self.check_codes else 0
            return subprocess.CompletedProcess(argv, code, "a.py:1:1: F401", "")
        return subprocess.run(argv, **kwargs)


class TestPreflightHook:
    def test_refusal_starts_no_worker_and_records_no_attempt(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        write(repo, "a.py", "x = 99\n")
        before = snapshot(repo)
        stub = StubRunner(edit_a)
        executor = isolated(repo, tmp_path, stub)
        report = go(repo, executor, plan(("u1", "a.py")))
        outcome = report.outcomes[0]
        assert outcome.tier == "refused" and outcome.verified is False
        assert "a.py" in outcome.reason and "commit or stash" in outcome.reason
        assert stub.calls == [] and list((tmp_path / "attempts").iterdir()) == []
        assert snapshot(repo) == before
        records = attempt_records_path(verdict_home())
        assert not records.exists() or "u1" not in records.read_text()
        assert report.unreported_units == ()
        assert report.model_units == ()
        assert "model (cheap/model): 0" in report.summary()
        assert report.failed == (outcome,)

    def test_preflight_that_raises_refuses_the_unit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        stub = StubRunner(edit_a)

        class Exploding(SpyExecutor):
            def preflight(self, unit: Any) -> str | None:
                raise OSError("git could not be started")

        executor = isolated(repo, tmp_path, stub, cls=Exploding)
        report = go(repo, executor, plan(("u1", "a.py")))
        outcome = report.outcomes[0]
        assert outcome.tier == "refused" and "precondition check failed" in outcome.reason
        assert "git could not be started" in outcome.reason
        assert stub.calls == []

    def test_mechanical_edit_does_not_refuse(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        stub = StubRunner(edit_a)
        executor = isolated(repo, tmp_path, stub)
        mech = MechanicalRunner(repo, check_codes=[1, 0], edit="import os\nx = 1  # fixed\n")
        report = go(repo, executor, ruff_unit(), mechanical=True, runner=mech)
        assert mech.fixed == 1
        assert len(stub.calls) == 1  # the worker started on the edited file
        assert report.outcomes[0].tier == "model"

    def test_refused_unit_keeps_a_mechanical_edit(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        write(repo, "a.py", "import os\nx = 1\nusers = 'edit'\n")
        stub = StubRunner(edit_a)
        executor = isolated(repo, tmp_path, stub)
        mech = MechanicalRunner(
            repo, check_codes=[1], edit="import os\nx = 1\nusers = 'edit'\n# fixed by ruff\n"
        )
        report = go(repo, executor, ruff_unit(), mechanical=True, runner=mech)
        outcome = report.outcomes[0]
        assert outcome.tier == "refused" and "a.py" in outcome.reason
        assert stub.calls == [] and list((tmp_path / "attempts").iterdir()) == []
        assert (repo / "a.py").read_text() == "import os\nx = 1\nusers = 'edit'\n# fixed by ruff\n"
        assert outcome.changed_files == ("a.py",)
        assert git(repo, "status", "--porcelain", "--untracked-files=all") == " M a.py\n"

    def test_mechanical_tier_fixes_a_modified_owned_file(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        write(repo, "a.py", "import os\nx = 1\nusers = 'edit'\n")
        stub = StubRunner(edit_a)
        executor = isolated(repo, tmp_path, stub)
        mech = MechanicalRunner(repo, check_codes=[0, 0], edit="x = 1\nusers = 'edit'\n")
        report = go(repo, executor, ruff_unit(), mechanical=True, runner=mech)
        assert report.outcomes[0].tier == "mechanical" and report.outcomes[0].verified
        assert stub.calls == []  # the refusal was never acted on

    def test_default_path_unaffected(self, tmp_path: Path) -> None:
        from tests.test_autodev_wiring import A_DIFF, PLAN, _executor

        repo = make_repo(tmp_path)
        write(repo, "a.py", "import os\nx = 1\ny = 5\n")  # an uncommitted change to the owned file
        assert not hasattr(PatchExecutor, "preflight")
        report = run_autodev(
            "fix ruff errors",
            repo,
            store=ReceiptStore(":memory:"),
            decomposer=_decomposer([PLAN[0]]),
            executor=_executor(repo, {"a.py": A_DIFF}),
            mechanical=False,
        )
        assert [o.tier for o in report.outcomes] == ["model"]
        assert report.outcomes[0].verified, report.summary()
