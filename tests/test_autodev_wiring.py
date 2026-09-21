from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict.autodev_run import AUTODEV_SCOPE, AutodevError
from verdict.autodev_run import run_autodev as _run_autodev
from verdict.decomposer import Decomposer, DecompositionConfig, DecompositionError
from verdict.patch_executor import PatchExecutor, PatchExecutorConfig
from verdict.receipt_store import ReceiptStore
from verdict.session_economics import ConcreteRoute

PLAN = [
    {
        "unit_id": "fix-a",
        "objective": "drop the unused import in a.py",
        "owned_files": ["a.py"],
        "verification_command": ["ruff", "check", "--select", "F401", "a.py"],
        "context": "",
    },
    {
        "unit_id": "fix-b",
        "objective": "drop the unused import in b.py",
        "owned_files": ["b.py"],
        "verification_command": ["ruff", "check", "--select", "F401", "b.py"],
        "context": "",
    },
]

A_DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1 @@
-import os
 x = 1
"""

ESCAPING_DIFF = """diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,2 +1 @@
-import os
 y = 2
"""


def run_autodev(*args: Any, **kwargs: Any) -> Any:
    """Supply the explicit optimizer fixture required by the launch API."""
    if kwargs.get("execution_path_decision") is None:
        from tests.test_worker_launch_authority import _decision

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
    (tmp_path / "b.py").write_text("import os\ny = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def _decomposer(plan: Any, *, usage: dict[str, int] | None = None) -> Decomposer:
    content = plan if isinstance(plan, str) else json.dumps(plan)

    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        body: dict[str, Any] = {"choices": [{"message": {"content": content}}]}
        if usage is not None:
            body["usage"] = usage
        return {"status_code": 200, "body": body}

    return Decomposer(
        DecompositionConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
        transport=transport,
    )


def _executor(repo: Path, diffs: dict[str, str]) -> PatchExecutor:
    """Serve a diff per unit, keyed by the owned file named in the prompt."""

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
        PatchExecutorConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
        transport=transport,
    )


def test_decomposition_yields_more_than_one_validated_unit(repo: Path) -> None:
    result = _decomposer(PLAN).decompose("fix ruff errors", repo_root=repo)

    assert len(result.units) == 2
    assert all(u.verification_command for u in result.units)


def test_unit_lacking_a_verification_command_fails_decomposition(repo: Path) -> None:
    broken = [dict(PLAN[0], verification_command=[])]

    with pytest.raises(DecompositionError):
        _decomposer(broken).decompose("fix ruff errors", repo_root=repo)


def test_full_loop_verifies_units_and_persists_receipts(repo: Path, tmp_path: Path) -> None:
    db = tmp_path / "receipts.db"
    report = run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(db),
        decomposer=_decomposer(PLAN, usage={"prompt_tokens": 800, "completion_tokens": 200}),
        executor=_executor(repo, {"a.py": A_DIFF, "b.py": ESCAPING_DIFF}),
        mechanical=False,
    )

    assert report.units_planned == 2
    assert len(report.verified) == 2, report.summary()
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\n"

    # The split is measured, not estimated.
    assert report.orchestrator_usage.total_tokens == 1000
    assert report.executor_usage.total_tokens == 240
    assert report.to_dict()["tokens"]["expensive_share"] == pytest.approx(1000 / 1240, rel=1e-3)

    # One receipt per unit, and it survives the process holding the handle.
    reopened = ReceiptStore(db)
    records = reopened.query_receipts(scope=AUTODEV_SCOPE)
    assert len(records) == 2
    assert {r.payload["unit_id"] for r in records} == {"fix-a", "fix-b"}
    assert all(r.payload["verified"] for r in records)


def test_receipts_keep_the_measured_token_counts(repo: Path, tmp_path: Path) -> None:
    """The store redacts `*_tokens` keys by default; the split needs the numbers."""
    db = tmp_path / "receipts.db"
    run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(db),
        decomposer=_decomposer([PLAN[0]]),
        executor=_executor(repo, {"a.py": A_DIFF}),
        mechanical=False,
    )

    usage = ReceiptStore(db).query_receipts(scope=AUTODEV_SCOPE)[0].payload["usage"]
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 120


def test_out_of_bounds_patch_leaves_the_unit_unverified(repo: Path) -> None:
    stray = """diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1,2 +1 @@
-import os
 y = 2
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
 import os
-x = 1
+x = 99
"""
    store = ReceiptStore(":memory:")
    report = run_autodev(
        "fix ruff errors",
        repo,
        store=store,
        decomposer=_decomposer([PLAN[1]]),
        executor=_executor(repo, {"b.py": stray}),
        mechanical=False,
    )

    assert len(report.failed) == 1
    assert "outside the unit boundary" in report.failed[0].reason
    assert (repo / "a.py").read_text(encoding="utf-8") == "import os\nx = 1\n"
    assert (repo / "b.py").read_text(encoding="utf-8") == "import os\ny = 2\n"
    assert store.query_receipts(scope=AUTODEV_SCOPE)[0].payload["verified"] is False


def test_failing_verification_is_recorded_as_a_failure(repo: Path) -> None:
    # The patch applies but leaves the F401 in place, so the unit's own command fails.
    noop = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
 import os
-x = 1
+x = 2
"""
    report = run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer([PLAN[0]]),
        executor=_executor(repo, {"a.py": noop}),
        mechanical=False,
    )

    assert len(report.failed) == 1
    assert "verification exited" in report.failed[0].reason


def test_mechanical_tier_fixes_units_at_zero_tokens(repo: Path) -> None:
    report = run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN, usage={"prompt_tokens": 800, "completion_tokens": 200}),
        executor=_executor(repo, {}),  # would return "no diff" and fail
        mechanical=True,
    )

    assert len(report.verified) == 2, report.summary()
    assert len(report.mechanical) == 2
    assert report.executor_usage.total_tokens == 0
    assert all(o.model == "none" for o in report.mechanical)
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_units_are_attributed_their_own_changes_not_the_whole_dirty_tree(repo: Path) -> None:
    report = run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer(PLAN),
        executor=_executor(repo, {"a.py": A_DIFF, "b.py": ESCAPING_DIFF}),
        mechanical=False,
    )

    by_id = {o.unit_id: o for o in report.outcomes}
    assert by_id["fix-a"].changed_files == ("a.py",)
    # fix-b runs second, with a.py already dirty; it must not inherit that change.
    assert by_id["fix-b"].changed_files == ("b.py",)


def test_a_partial_mechanical_fix_is_still_attributed_when_the_unit_fails(repo: Path) -> None:
    """`ruff --fix` writes before the tier decides; those edits must be reported.

    The unit owns a file with one auto-fixable import and one error ruff cannot
    fix, so the mechanical tier repairs part of it and then declines the unit.
    The model tier then fails. The tree changed regardless, and a report that
    said `changed_files=[]` would understate what the run did to the repo.
    """
    (repo / "a.py").write_text("import os\nundefined_name\n", encoding="utf-8")
    unit = dict(PLAN[0], verification_command=["ruff", "check", "--select", "F", "a.py"])

    report = run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer([unit]),
        executor=_executor(repo, {}),  # returns "no diff", so the model tier fails
        mechanical=True,
    )

    outcome = report.outcomes[0]
    assert outcome.verified is False
    assert outcome.tier == "model"
    assert outcome.changed_files == ("a.py",)
    assert "import os" not in (repo / "a.py").read_text(encoding="utf-8")


def test_run_requires_a_git_repository(tmp_path: Path) -> None:
    with pytest.raises(AutodevError, match="not a git repository"):
        run_autodev("x", tmp_path, decomposer=_decomposer(PLAN))


def test_missing_provider_usage_is_reported_as_unknown(repo: Path) -> None:
    def transport(model_id: str, payload: Mapping[str, Any], timeout_seconds: float) -> Any:
        return {"status_code": 200, "body": {"choices": [{"message": {"content": A_DIFF}}]}}

    report = run_autodev(
        "fix ruff errors",
        repo,
        store=ReceiptStore(":memory:"),
        decomposer=_decomposer([PLAN[0]]),
        executor=PatchExecutor(
            repo,
            PatchExecutorConfig(model="cheap/model", base_url="http://127.0.0.1:20128/v1"),
            transport=transport,
        ),
        mechanical=False,
    )

    assert report.unreported_units == ("fix-a",)
    assert "not estimated" in report.summary()


def test_legacy_default_routes_fail_closed_without_execution_path_decision() -> None:
    from verdict.autodev_run import (
        _resolve_default_executor_model,
        _resolve_default_orchestrator_model,
    )

    with pytest.raises(AutodevError, match="ExecutionPathDecision"):
        _resolve_default_executor_model()
    with pytest.raises(AutodevError, match="ExecutionPathDecision"):
        _resolve_default_orchestrator_model()


def _public_request_payload() -> dict[str, Any]:
    return {
        "schema_version": "1",
        "trajectory_id": "traj-cli-authority",
        "slice_id": "slice-cli-authority",
        "acceptance_criteria": ["worker launches only on the selected route"],
        "proof_criteria": ["selected and served identities match"],
        "candidates": [
            {
                "strategy": "direct_cheap",
                "route_id": "route-selected-model",
                "gateway": "http://gateway.test/v1",
                "provider": "provider-a",
                "model": "provider/model",
                "capability_tier": 2,
                "eligible": True,
                "is_free": True,
                "execution_tokens": 16,
                "verification_tokens": 4,
                "certification_state": "ready",
                "certification_freshness": "fresh",
            }
        ],
    }


def _write_request(path: Path) -> None:
    path.write_text(json.dumps(_public_request_payload()), encoding="utf-8")


def test_cli_request_file_builds_in_process_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from verdict.autodev_run import _require_launch_decision
    from verdict.cli import _execution_path_decision_from_request_file

    path = tmp_path / "request.json"
    _write_request(path)
    # The core CLI must work without importing the optional FastAPI server module.
    monkeypatch.setitem(sys.modules, "verdict.api", None)
    decision = _execution_path_decision_from_request_file(str(path), task="bounded task")
    trusted = _require_launch_decision(decision, surface="test")
    assert trusted.selected_route.model == "provider/model"
    assert trusted.task_slice_id == "slice-cli-authority"


def test_cli_request_file_rejects_invalid_contract(tmp_path: Path) -> None:
    from verdict.cli import _execution_path_decision_from_request_file
    from verdict.execution_path import ExecutionPathError

    path = tmp_path / "request.json"
    path.write_text(json.dumps({"schema_version": "999"}), encoding="utf-8")
    with pytest.raises(ExecutionPathError, match="schema_version"):
        _execution_path_decision_from_request_file(str(path), task="bounded task")


def test_cli_main_supplies_in_process_decision_to_autodev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    import verdict.cli as cli

    path = tmp_path / "request.json"
    _write_request(path)
    captured: dict[str, Any] = {}

    def fake_cmd_autodev(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setattr(cli, "cmd_autodev", fake_cmd_autodev)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verdict",
            "autodev",
            "--objective",
            "bounded task",
            "--repo",
            str(tmp_path),
            "--execution-path-request",
            str(path),
        ],
    )
    cli.main()
    decision = captured["execution_path_decision"]
    assert decision.selected_route.model == "provider/model"
    assert decision.task_slice_id == "slice-cli-authority"


def test_cli_packet_execute_binds_request_to_packet_objective(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    import verdict.cli as cli

    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps({"objective": "packet task"}), encoding="utf-8")
    request_path = tmp_path / "request.json"
    _write_request(request_path)
    captured: dict[str, Any] = {}

    def fake_execute(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "cmd_autodev_packet_execute", fake_execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verdict",
            "autodev",
            "packet",
            "execute",
            "--packet",
            str(packet_path),
            "--repo",
            str(tmp_path),
            "--execution-path-request",
            str(request_path),
        ],
    )
    cli.main()
    decision = captured["execution_path_decision"]
    assert decision.task_slice_id == "slice-cli-authority"
    assert decision.selected_route.model == "provider/model"


def test_resolver_main_launches_from_public_request_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from verdict.subagent_resolver import main as resolver_main

    path = tmp_path / "request.json"
    _write_request(path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "resolver",
            "--role",
            "worker",
            "--execution-path-request",
            str(path),
            "--task",
            "bounded task",
            "--json",
        ],
    )
    assert resolver_main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_id"] == "provider/model"
    assert payload["provider"] == "provider-a"
