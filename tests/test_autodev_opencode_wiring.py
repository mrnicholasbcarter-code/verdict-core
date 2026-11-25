"""OC2a (#1301): how ``run_autodev`` and ``verdict autodev`` use the isolated OpenCode executor.

Task groups 4 and 5 of ``openspec/changes/autodev-opencode-isolated-executor``. The OpenCode
executor is a stub with the real ``run`` signature, the verification command is run by a scripted
runner, and every test runs in a temporary home: nothing here calls a model, a probe, the real
``opencode`` or the real ``verdict serve``.
"""

from __future__ import annotations

import http.server
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
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

    @pytest.mark.parametrize(
        ("original", "interrupt"),
        [
            (PermissionError("the original exception"), KeyboardInterrupt("restore interrupted")),
            (PermissionError("the original exception"), SystemExit("restore exited")),
            (KeyboardInterrupt(), SystemExit("restore exited")),
            (KeyboardInterrupt(), KeyboardInterrupt("restore interrupted")),
        ],
    )
    def test_restore_is_interrupted_while_an_exception_propagates(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        original: BaseException,
        interrupt: BaseException,
    ) -> None:
        """Review finding 1: a ``KeyboardInterrupt`` or ``SystemExit`` out of ``restore()`` never replaces it."""
        repo = make_repo(tmp_path)

        class InterruptedRestore(SpyExecutor):
            def restore(self) -> str:
                self.restore_calls += 1
                raise interrupt

        def explode(_call: int) -> Any:
            raise original

        executor = isolated(repo, tmp_path, StubRunner(edit_a), cls=InterruptedRestore)
        raised: BaseException | None = None
        try:  # not pytest.raises: an escaped interrupt must fail this test, not end the session
            go(repo, executor, plan(("u1", "a.py")), verify=explode)
        except BaseException as exc:
            raised = exc
        assert raised is original
        assert executor.restore_calls == 1
        assert f"{type(interrupt).__name__}: {interrupt}" in capsys.readouterr().err

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


class TestRecords:
    def test_no_served_identity_is_claimed(self, tmp_path: Path) -> None:
        repo = make_repo(tmp_path)
        executor = isolated(repo, tmp_path, StubRunner(edit_a))
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
        outcome = report.outcomes[0]
        assert outcome.verified and outcome.model == "cheap/model"
        assert outcome.served_model is None
        payload = store.query_receipts(scope=AUTODEV_SCOPE)[0].payload
        assert payload["served_headers"] == {} and payload["served_model"] is None
        assert payload["model"] == "cheap/model"

    def test_credential_never_appears_in_a_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "SECRET-WORKER-KEY-123"
        monkeypatch.setenv("VERDICT_WORKER_API_KEY", secret)
        repo = make_repo(tmp_path)
        stub = StubRunner(ok=False, error="serve refused the route; key [REDACTED]")
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
        assert "[REDACTED]" in report.failed[0].reason  # the redacted text is what is recorded
        everything = [
            json.dumps(report.to_dict()),
            report.summary(),
            json.dumps(store.query_receipts(scope=AUTODEV_SCOPE)[0].payload, default=str),
            attempt_records_path(verdict_home()).read_text(),
            stub.calls[0]["prompt"],
        ]
        assert all(secret not in text for text in everything)


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


# ------------------------------------------------------------------------------------------
# 5. the CLI: the readiness check and the flag
# ------------------------------------------------------------------------------------------

GATEWAY = "http://127.0.0.1:20128/v1"


class FakeServe:
    """A loopback stand-in for ``verdict serve`` that records every request it receives."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.requests: list[str] = []
        self.release = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.requests.append(f"GET {self.path}")
                if outer.mode == "hang":
                    outer.release.wait(10)
                    return
                status, body = {
                    "ok": (200, b'{"status": "healthy", "engine": "verdict"}'),
                    "not_verdict": (200, b'{"status": "healthy", "engine": "something-else"}'),
                    "not_json": (200, b"hello"),
                    "unavailable": (503, b'{"engine": "verdict"}'),
                    "created": (201, b'{"status": "healthy", "engine": "verdict"}'),
                }[outer.mode]
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve_factory() -> Iterator[Any]:
    started: list[FakeServe] = []

    def make(mode: str = "ok") -> FakeServe:
        fake = FakeServe(mode)
        started.append(fake)
        return fake

    yield make
    for fake in started:
        fake.close()


def fake_binary(tmp_path: Path, *, version: str = "2.3.2", exit_code: int = 0) -> str:
    script = tmp_path / "fake-opencode"
    script.write_text(f"#!/bin/sh\necho {version}\nexit {exit_code}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def make_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serve_origin: str,
    *,
    binary: str | None = None,
    token: bool = True,
    key: bool = True,
) -> None:
    """The environment of a host where opencode, serve and its credentials are all in place."""
    monkeypatch.setenv("VERDICT_OPENCODE_BIN", binary or fake_binary(tmp_path))
    monkeypatch.setenv("VERDICT_SERVE_URL", serve_origin)
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    if token:
        monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "serve-token-for-the-fake")
    if key:
        from verdict import worker_assignment as wa

        path = wa.assignment_key_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wa.generate_assignment_key())


class Forbidden:
    """Patched over the things a failed readiness check must never reach."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import verdict.autodev_run as autodev
        import verdict.decomposer as decomposer

        self.calls: list[str] = []

        def forbid(name: str) -> Any:
            def raiser(*_a: Any, **_k: Any) -> Any:
                self.calls.append(name)
                raise AssertionError(f"{name} was called")

            return raiser

        monkeypatch.setattr(autodev, "prepare_live_autodev", forbid("prepare_live_autodev"))
        monkeypatch.setattr(
            autodev, "prepare_live_autodev_selected", forbid("prepare_live_autodev_selected")
        )
        monkeypatch.setattr(autodev, "run_autodev", forbid("run_autodev"))
        monkeypatch.setattr(autodev, "PatchExecutor", forbid("PatchExecutor"))
        monkeypatch.setattr(decomposer.Decomposer, "decompose", forbid("decompose"))


def call_cli(repo: Path, **kwargs: Any) -> None:
    import verdict.cli as cli

    defaults: dict[str, Any] = {
        "orchestrator_model": None,
        "executor_model": None,
        "base_url": None,
        "allow_live": True,
        "worker_harness": "opencode",
    }
    defaults.update(kwargs)
    cli.cmd_autodev("fix it", str(repo), **defaults)


def a_decision() -> Any:
    from tests.test_worker_launch_authority import _decision
    from verdict.session_economics import ConcreteRoute

    return _decision(
        ConcreteRoute("cheap/model", GATEWAY, "fixture-provider", "cheap/model", "fixture", 1, True)
    )


class TestReadiness:
    @pytest.mark.parametrize(
        ("how", "detail"),
        [("missing", "not found or not executable"), ("version_fails", "`--version` exited 3")],
    )
    def test_opencode_binary_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        serve_factory: Any,
        capsys: pytest.CaptureFixture[str],
        how: str,
        detail: str,
    ) -> None:
        serve = serve_factory("ok")
        binary = "/nonexistent/opencode" if how == "missing" else fake_binary(tmp_path, exit_code=3)
        make_ready(tmp_path, monkeypatch, serve.origin, binary=binary)
        forbidden = Forbidden(monkeypatch)
        repo = make_repo(tmp_path)
        with pytest.raises(SystemExit) as caught:
            call_cli(repo)
        assert caught.value.code == 1
        out = capsys.readouterr().out
        assert "binary check failed" in out and "opencode readiness failed" in out
        assert detail in " ".join(out.split())
        assert forbidden.calls == []
        assert serve.requests == []  # the binary is checked first

    @pytest.mark.parametrize(
        "mode", ["refused", "hang", "unavailable", "created", "not_verdict", "not_json"]
    )
    def test_serve_not_answering(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        serve_factory: Any,
        capsys: pytest.CaptureFixture[str],
        mode: str,
    ) -> None:
        import verdict.autodev_opencode as oc_module

        if mode == "refused":
            fake = serve_factory("ok")
            origin = fake.origin
            fake.close()  # nothing listens on that port any more
            requests: list[str] = []
        else:
            fake = serve_factory(mode)
            origin, requests = fake.origin, fake.requests
        monkeypatch.setattr(oc_module, "SERVE_HEALTH_TIMEOUT_SECONDS", 0.5)
        make_ready(tmp_path, monkeypatch, origin)
        forbidden = Forbidden(monkeypatch)
        with pytest.raises(SystemExit) as caught:
            call_cli(make_repo(tmp_path))
        assert caught.value.code == 1
        out = capsys.readouterr().out
        assert "serve check failed" in out
        assert forbidden.calls == []
        assert set(requests) <= {"GET /health"}  # no upstream request beyond the health request

    @pytest.mark.parametrize("missing", ["token", "key"])
    def test_credentials_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        serve_factory: Any,
        capsys: pytest.CaptureFixture[str],
        missing: str,
    ) -> None:
        serve = serve_factory("ok")
        make_ready(
            tmp_path, monkeypatch, serve.origin, token=missing != "token", key=missing != "key"
        )
        forbidden = Forbidden(monkeypatch)
        with pytest.raises(SystemExit) as caught:
            call_cli(make_repo(tmp_path))
        assert caught.value.code == 1
        out = capsys.readouterr().out
        assert "credentials check failed" in out and "verdict setup" in out
        assert "serve-token-for-the-fake" not in out
        assert forbidden.calls == []

    @pytest.mark.parametrize("state", ["present", "no_token", "no_key"])
    def test_credential_check_creates_or_modifies_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_factory: Any, state: str
    ) -> None:
        """Review finding 1 (Codex on 1470a171), a guard: the credential check reads the serve token
        (environment, credential store, ``serve.env``) and the signing key file, and nothing under the
        home directory is created, removed or changed, whether the credentials are there or not."""
        from verdict.serve_service import env_file_path

        serve = serve_factory("ok")
        monkeypatch.delenv(
            "XDG_CONFIG_HOME", raising=False
        )  # the credential store reads under HOME
        make_ready(tmp_path, monkeypatch, serve.origin, token=False, key=state == "present")
        if state != "no_token":  # the token is found in serve.env, the last place it is looked for
            env_file = env_file_path()
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("LLMGATE_AUTH_TOKEN=serve-token-from-the-file\n")

        def home_state() -> dict[str, Any]:
            home = Path.home()
            found: dict[str, Any] = {}
            for path in [home, *home.rglob("*")]:
                info = path.lstat()
                found[str(path.relative_to(home))] = (
                    stat.S_IFMT(info.st_mode),
                    stat.S_IMODE(info.st_mode),
                    info.st_mtime_ns,
                    path.read_bytes() if stat.S_ISREG(info.st_mode) else b"",
                )
            return found

        before = home_state()
        result = oc.check_opencode_readiness()
        assert result.ok is (state == "present")
        assert (
            result.failed_check
            == {"present": "", "no_token": "credentials", "no_key": "credentials"}[state]
        )
        assert home_state() == before

    def test_readiness_runs_even_when_a_decision_is_supplied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_factory: Any
    ) -> None:
        make_ready(
            tmp_path, monkeypatch, serve_factory("ok").origin, binary="/nonexistent/opencode"
        )
        forbidden = Forbidden(monkeypatch)
        with pytest.raises(SystemExit) as caught:
            call_cli(make_repo(tmp_path), execution_path_decision=a_decision())
        assert caught.value.code == 1
        assert forbidden.calls == []

    def test_one_message_in_json_mode(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        serve_factory: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        make_ready(
            tmp_path, monkeypatch, serve_factory("ok").origin, binary="/nonexistent/opencode"
        )
        forbidden = Forbidden(monkeypatch)
        with pytest.raises(SystemExit) as caught:
            call_cli(make_repo(tmp_path), output_json=True)
        assert caught.value.code == 1
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["error"] == "opencode readiness failed"
        assert "binary" in payload["reason"] and "decomposition" not in json.dumps(payload)
        assert forbidden.calls == []

    @pytest.mark.parametrize("output_json", [False, True])
    def test_version_differs_from_the_pinned_version(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        serve_factory: Any,
        capsys: pytest.CaptureFixture[str],
        output_json: bool,
    ) -> None:
        import verdict.autodev_run as autodev
        from verdict.autodev_run import AutodevReport
        from verdict.orchestration.executor_opencode import PINNED_VERSION
        from verdict.patch_executor import TokenUsage

        make_ready(
            tmp_path,
            monkeypatch,
            serve_factory("ok").origin,
            binary=fake_binary(tmp_path, version="9.9.9"),
        )
        seen: dict[str, Any] = {}

        def fake_run(objective: str, repo_path: Path, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return AutodevReport(
                objective=objective,
                repo_path=str(repo_path),
                orchestrator_model="cheap/model",
                executor_model="cheap/model",
                units_planned=0,
                outcomes=(),
                orchestrator_usage=TokenUsage(),
            )

        monkeypatch.setattr(autodev, "run_autodev", fake_run)
        call_cli(make_repo(tmp_path), execution_path_decision=a_decision(), output_json=output_json)
        out = capsys.readouterr().out
        assert "9.9.9" in out and PINNED_VERSION in out
        assert "executor" in seen  # the run continued


class TestFlag:
    def test_flag_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import verdict.autodev_opencode as oc_module
        import verdict.autodev_run as autodev
        from verdict.autodev_run import AutodevReport
        from verdict.patch_executor import TokenUsage

        def forbid(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("an OpenCode step ran without the flag")

        monkeypatch.setattr(oc_module, "check_opencode_readiness", forbid)
        monkeypatch.setattr(oc_module, "build_opencode_unit_executor", forbid)
        monkeypatch.setattr(oc_module, "seed_attempt_tree", forbid)
        seen: dict[str, Any] = {}

        def fake_run(objective: str, repo_path: Path, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return AutodevReport(
                objective=objective,
                repo_path=str(repo_path),
                orchestrator_model="cheap/model",
                executor_model="cheap/model",
                units_planned=0,
                outcomes=(),
                orchestrator_usage=TokenUsage(),
            )

        monkeypatch.setattr(autodev, "run_autodev", fake_run)
        call_cli(make_repo(tmp_path), execution_path_decision=a_decision(), worker_harness=None)
        assert "executor" not in seen  # run_autodev builds its own PatchExecutor, as before
        assert seen["retry_routes"] == ()

    def test_flag_with_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import verdict.autodev_opencode as oc_module
        import verdict.autodev_run as autodev
        from tests.test_autodev_wiring import PLAN
        from verdict.orchestration.contracts import CapacityClass
        from verdict.orchestration.eligibility import EligibilityStage

        def forbid(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("dry-run with the flag ran an OpenCode step")

        monkeypatch.setattr(oc_module, "check_opencode_readiness", forbid)
        monkeypatch.setattr(oc_module, "build_opencode_unit_executor", forbid)
        monkeypatch.setattr(autodev, "run_autodev", forbid)
        planner = _decomposer(PLAN)
        monkeypatch.setattr("verdict.decomposer.Decomposer", lambda config: planner)

        class Verdict:
            route_id = "cheap/model"
            capacity_class = CapacityClass.FREE
            reached = EligibilityStage.SELECTED

        class Selector:
            def select(self, req: Any, *, now: Any) -> Any:
                return None, (Verdict(),)

            def capacity_fact(self, route_id: str) -> Any:
                return CapacityClass.FREE, ""

        monkeypatch.setattr(
            "verdict.orchestration.eligibility_report.build_selector",
            lambda gateway, **kwargs: Selector(),
        )
        call_cli(
            make_repo(tmp_path),
            orchestrator_model="cheap/model",
            base_url=GATEWAY,
            dry_run=True,
            output_json=True,
        )
        assert json.loads(capsys.readouterr().out)["unit_count"] == 2

    def test_flag_passes_the_isolated_executor_and_no_retry_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_factory: Any
    ) -> None:
        import verdict.autodev_run as autodev
        from verdict.autodev_run import AutodevReport
        from verdict.patch_executor import TokenUsage

        make_ready(tmp_path, monkeypatch, serve_factory("ok").origin)
        monkeypatch.setattr("verdict.credentials_store.resolve_api_key", lambda: "fake-key")
        candidates = (
            {"route_id": "cheap/model", "gateway": GATEWAY, "model": "cheap/model"},
            {"route_id": "free/other", "gateway": GATEWAY, "model": "free/other"},
        )  # the selection probed two routes, so the default path would get a retry ladder
        monkeypatch.setattr(
            autodev,
            "prepare_live_autodev_selected",
            lambda *a, **k: (a_decision(), None, {}, candidates, ()),
        )
        seen: dict[str, Any] = {}

        def fake_run(objective: str, repo_path: Path, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return AutodevReport(
                objective=objective,
                repo_path=str(repo_path),
                orchestrator_model="cheap/model",
                executor_model="cheap/model",
                units_planned=0,
                outcomes=(),
                orchestrator_usage=TokenUsage(),
            )

        monkeypatch.setattr(autodev, "run_autodev", fake_run)
        call_cli(make_repo(tmp_path))
        executor = seen["executor"]
        assert isinstance(executor, oc.OpenCodeUnitExecutor)
        assert executor.isolated is True and executor.config.model == "cheap/model"
        assert seen["retry_routes"] == ()

    def test_help_says_retries_and_lineage_are_not_yet_supported(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import verdict.cli as cli

        monkeypatch.setattr(sys, "argv", ["verdict", "autodev", "--help"])
        with pytest.raises(SystemExit) as caught:
            cli.main()
        assert caught.value.code == 0
        text = " ".join(capsys.readouterr().out.split())
        assert "--worker-harness {opencode}" in text
        assert "Retries and lineage are not yet supported" in text

    @pytest.mark.parametrize("flag", [[], ["--worker-harness", "opencode"]])
    def test_dispatch_passes_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: list[str]
    ) -> None:
        import verdict.cli as cli

        captured: dict[str, Any] = {}
        monkeypatch.setattr(cli, "cmd_autodev", lambda *a, **k: captured.update(k))
        monkeypatch.setattr(
            sys, "argv", ["verdict", "autodev", "--objective", "x", "--repo", str(tmp_path), *flag]
        )
        cli.main()
        assert captured["worker_harness"] == ("opencode" if flag else None)


class TestEndToEnd:
    def test_the_real_opencode_executor_runs_under_the_autodev_run_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``OpenCodeExecutor`` and a fake worker: the signed ids, and a clean tree."""
        import verdict.orchestration.executor_opencode as exec_module
        from tests import worker_assignment_helpers as sign
        from verdict.orchestration.executor_opencode import OpenCodeExecutor

        monkeypatch.setattr(exec_module, "_check_binary", lambda *a, **k: None)
        monkeypatch.setattr(exec_module, "_check_serve", lambda *a, **k: None)
        seen = tmp_path / "assignment.txt"
        listing = tmp_path / "cwd-listing.txt"
        script = tmp_path / "fake-opencode"
        script.write_text(
            "#!/usr/bin/env python3\nimport os\n"
            + textwrap.dedent(
                f"""
                open({str(seen)!r}, "w").write(os.environ.get("VERDICT_WORKER_API_KEY", ""))
                open({str(listing)!r}, "w").write("\\n".join(sorted(os.listdir("."))))
                open("a.py", "w").write("x = 2\\n")
                """
            )
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        real = OpenCodeExecutor(
            opencode_bin=str(script),
            serve_token_provider=lambda: "serve-token",
            key_provider=lambda: bytes(range(32)),
            receipts_db_provider=lambda: None,
        )
        repo = make_repo(tmp_path)
        executor = isolated(repo, tmp_path, real)  # type: ignore[arg-type]
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
        assert len(report.verified) == 1, report.summary()
        fields = sign.decode_payload(seen.read_text())
        payload = store.query_receipts(scope=AUTODEV_SCOPE)[0].payload
        assert fields["run_id"] == payload["attempt_run_id"]
        assert fields["node_id"] == "u1" and fields["route_id"] == "cheap/model"
        # The OpenCode executor wrote nothing of its own into the worker's working directory.
        assert listing.read_text().split("\n") == [".git", "a.py", "b.py"]
        assert git(repo, "status", "--porcelain", "--untracked-files=all") == " M a.py\n"
        assert (repo / "a.py").read_text() == "x = 2\n"
