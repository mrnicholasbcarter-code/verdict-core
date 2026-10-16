"""BOD-353: autodev wires every executor/planner attempt to the outcome-attempt writer."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict.decomposer import Decomposer, DecompositionConfig
from verdict.outcome_records import attempt_records_path
from verdict.patch_executor import PatchExecutor, PatchExecutorConfig
from verdict.receipt_store import ReceiptStore
from verdict.session_economics import ConcreteRoute

PLAN = [
    {
        "unit_id": "fix-a",
        "objective": "drop the unused import in a.py",
        "owned_files": ["a.py"],
        "verification_command": [sys.executable, "-m", "ruff", "check", "--select", "F401", "a.py"],
        "context": "",
    }
]

A_DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1 @@
-import os
 x = 1
"""


def run_autodev(*args: Any, **kwargs: Any) -> Any:
    from tests.test_worker_launch_authority import _decision
    from verdict.autodev_run import run_autodev as _run_autodev

    if kwargs.get("execution_path_decision") is None:
        kwargs["execution_path_decision"] = _decision(
            ConcreteRoute(
                "cheap/model",
                "http://127.0.0.1:20128/v1",
                "fixture-provider",
                "cheap/model",
                "fixture",
                1,
                True,
            )
        )
    return _run_autodev(*args, **kwargs)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("import os\nx = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _decomposer(plan: Any, *, model: str = "cheap/model") -> Decomposer:
    content = plan if isinstance(plan, str) else json.dumps(plan)

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            },
        }

    return Decomposer(
        DecompositionConfig(model=model, base_url="http://127.0.0.1:20128/v1"), transport=transport
    )


def _executor(repo: Path, diffs: dict[str, str], *, model: str = "cheap/model") -> PatchExecutor:
    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        prompt = str(payload["messages"][-1]["content"])
        content = next((d for name, d in diffs.items() if f"- {name}" in prompt), "no diff")
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        }

    return PatchExecutor(
        repo,
        PatchExecutorConfig(model=model, base_url="http://127.0.0.1:20128/v1"),
        transport=transport,
    )


def test_primary_success_writes_one_executor_and_one_planner_attempt(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))
    run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN),
        executor=_executor(repo, {"a.py": A_DIFF}),
        mechanical=False,
    )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    executor_rows = [r for r in rows if r["harness"] == "verdict-autodev-executor"]
    planner_rows = [r for r in rows if r["harness"] == "verdict-autodev-planner"]
    assert len(executor_rows) == 1
    assert executor_rows[0]["outcome"] == "accepted"
    assert executor_rows[0]["failure_class"] == "success"
    assert executor_rows[0]["model"] == "cheap/model"
    assert len(planner_rows) == 1
    assert planner_rows[0]["outcome"] == "accepted"


def test_repair_then_success_writes_two_executor_attempts(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diff that fails git apply --check, then a repair turn that succeeds."""
    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    bad_then_good = {"n": 0}

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        bad_then_good["n"] += 1
        # a.py is small, so the repair turn asks for full-file contents.
        content = "not a diff at all" if bad_then_good["n"] == 1 else "```file:a.py\nx = 1\n```"
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        }

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
        transport=transport,
    )
    run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN),
        executor=executor,
        mechanical=False,
    )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    executor_rows = [r for r in rows if r["harness"] == "verdict-autodev-executor"]
    assert len(executor_rows) == 2, executor_rows
    assert executor_rows[0]["outcome"] == "failed"
    assert executor_rows[1]["outcome"] == "accepted"


def test_primary_failure_then_retry_success_writes_two_executor_records(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Primary fails with an unrepairable "no diff" rejection (straight to retry,
    no repair sub-turn); the retry executor succeeds. That is exactly 2 executor
    records: the primary's failed attempt and the retry's accepted attempt."""
    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    # A patch naming a file outside the unit boundary is rejected but never
    # repaired (per PatchExecutor._apply_diff_reply), so this is the primary's
    # one and only executor attempt before the retry ladder takes over.
    out_of_bounds_diff = """diff --git a/other.py b/other.py
--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
    primary = _executor(repo, {"a.py": out_of_bounds_diff}, model="primary/model")
    retry = _executor(repo, {"a.py": A_DIFF}, model="retry/model")

    from tests.test_worker_launch_authority import _decision as _mk_decision
    from verdict.session_economics import ConcreteRoute

    decision = _mk_decision(
        ConcreteRoute(
            "primary/model", "http://127.0.0.1:20128/v1", "p", "primary/model", "fixture", 1, True
        )
    )

    retry_ladder = [{"model": "retry/model", "gateway": "http://127.0.0.1:20128/v1"}]

    def retry_factory(route: Mapping[str, Any]) -> PatchExecutor:
        return retry

    import verdict.autodev_run as autodev_run

    def fake_select_retry_route(unit: Any, ladder: Any, objective: str) -> Any:
        return _mk_decision(
            ConcreteRoute(
                "retry/model", "http://127.0.0.1:20128/v1", "p", "retry/model", "fixture", 1, True
            )
        )

    monkeypatch.setattr(autodev_run, "_select_retry_route", fake_select_retry_route)

    from verdict.autodev_run import run_autodev as _run_autodev

    _run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN, model="primary/model"),
        executor=primary,
        execution_path_decision=decision,
        retry_routes=retry_ladder,
        retry_executor_factory=retry_factory,
        mechanical=False,
    )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    executor_rows = [r for r in rows if r["harness"] == "verdict-autodev-executor"]
    assert len(executor_rows) == 2, executor_rows
    assert executor_rows[0]["outcome"] == "failed"
    assert executor_rows[0]["model"] == "primary/model"
    assert executor_rows[1]["outcome"] == "accepted"
    assert executor_rows[1]["model"] == "retry/model"


def test_write_failure_is_logged_and_does_not_fail_the_run(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken attempt-record store must never break the autodev run or its receipt."""
    import logging

    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    import verdict.autodev_run as autodev_run

    monkeypatch.setattr(autodev_run, "write_attempt_record", boom)

    with caplog.at_level(logging.WARNING, logger="verdict.autodev_run"):
        report = run_autodev(
            "fix ruff errors",
            repo,
            store=ReceiptStore(":memory:"),
            decomposer=_decomposer(PLAN),
            executor=_executor(repo, {"a.py": A_DIFF}),
            mechanical=False,
        )

    assert len(report.verified) == 1, report.summary()
    assert len(report.receipt_ids) == 1
    assert not attempt_records_path(store_dir).exists()
    assert any("outcome attempt record" in r.message for r in caplog.records)


def test_repair_applied_but_verification_failed_is_not_recorded_as_accepted(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair turn that applies but then fails verification must not be 'accepted'."""
    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    turn = {"n": 0}

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        turn["n"] += 1
        # Second turn applies cleanly (full-file reply, and differs from the
        # original so it is not rejected as a no-op) but leaves the F401
        # unfixed, so the unit's own verification command still fails.
        content = "not a diff at all" if turn["n"] == 1 else "```file:a.py\nimport os\nx = 2\n```"
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        }

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
        transport=transport,
    )
    run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN),
        executor=executor,
        mechanical=False,
    )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    executor_rows = [r for r in rows if r["harness"] == "verdict-autodev-executor"]
    assert len(executor_rows) == 2, executor_rows
    assert executor_rows[0]["outcome"] == "failed"
    # The repair applied the patch, but verification (ruff F401) still failed:
    # this must be recorded as failed/test_failed, never accepted/success.
    assert executor_rows[1]["outcome"] == "failed", executor_rows[1]
    assert executor_rows[1]["failure_class"] == "test_failed", executor_rows[1]


def test_total_planner_failure_still_writes_every_planner_attempt_and_reraises(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When decompose() raises DecompositionError, its own attempts must still be recorded."""
    from verdict.decomposer import Decomposer, DecompositionConfig, DecompositionError

    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": "not json at all"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            },
        }

    decomposer = Decomposer(
        DecompositionConfig(
            model="cheap/model",
            planner_models=("cheap/model", "cheap/model-2"),
            base_url="http://127.0.0.1:20128/v1",
        ),
        transport=transport,
    )

    with pytest.raises(DecompositionError):
        run_autodev(
            "fix ruff errors",
            repo,
            store=ReceiptStore(":memory:"),
            decomposer=decomposer,
            executor=_executor(repo, {"a.py": A_DIFF}),
            mechanical=False,
        )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    planner_rows = [r for r in rows if r["harness"] == "verdict-autodev-planner"]
    assert len(planner_rows) >= 2, planner_rows
    assert all(r["outcome"] == "failed" for r in planner_rows)


def test_identity_mismatch_writes_exactly_one_record_and_no_retry_record(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fatal served-model mismatch is one false_report record; the retry ladder never runs."""
    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"content": A_DIFF}}],
                "model": "some-other-model",
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        }

    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
        transport=transport,
    )
    run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN),
        executor=executor,
        retry_routes=[{"model": "retry/model", "gateway": "http://127.0.0.1:20128/v1"}],
        mechanical=False,
    )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    executor_rows = [r for r in rows if r["harness"] == "verdict-autodev-executor"]
    assert len(executor_rows) == 1, executor_rows
    assert executor_rows[0]["outcome"] == "failed"
    assert executor_rows[0]["failure_class"] == "false_report"


def test_refused_retry_selection_writes_no_retry_record(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the retry-route optimizer itself refuses, no retry executor attempt happens."""
    store_dir = tmp_path / "home"
    monkeypatch.setenv("VERDICT_HOME", str(store_dir))

    import verdict.autodev_run as autodev_run
    from verdict.autodev_run import AutodevError

    def fake_select_retry_route(unit: Any, ladder: Any, objective: str) -> Any:
        raise AutodevError("autodev retry selection refused: no eligible candidates")

    monkeypatch.setattr(autodev_run, "_select_retry_route", fake_select_retry_route)

    run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN),
        executor=_executor(repo, {}),  # "no diff" -> rejected, repairable -> executor_failed=True
        retry_routes=[{"model": "retry/model", "gateway": "http://127.0.0.1:20128/v1"}],
        mechanical=False,
    )

    rows = [
        json.loads(line)
        for line in attempt_records_path(store_dir).read_text(encoding="utf-8").splitlines()
    ]
    executor_rows = [r for r in rows if r["harness"] == "verdict-autodev-executor"]
    assert all(r["model"] != "retry/model" for r in executor_rows), executor_rows
