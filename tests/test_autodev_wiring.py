from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict.autodev_run import AUTODEV_SCOPE, DEFAULT_EXECUTOR_MODEL, AutodevError, run_autodev
from verdict.decomposer import (
    DEFAULT_ORCHESTRATOR_MODEL,
    Decomposer,
    DecompositionConfig,
    DecompositionError,
)
from verdict.models import ModelInfo
from verdict.patch_executor import PatchExecutor, PatchExecutorConfig
from verdict.receipt_store import ReceiptStore

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

    return Decomposer(DecompositionConfig(model="orch/model"), transport=transport)


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

    return PatchExecutor(repo, PatchExecutorConfig(model="cheap/model"), transport=transport)


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
        executor=PatchExecutor(repo, PatchExecutorConfig(model="cheap/model"), transport=transport),
        mechanical=False,
    )

    assert report.unreported_units == ("fix-a",)
    assert "not estimated" in report.summary()


def test_default_routes_resolve_from_live_selector_not_hardcoded_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.autodev_run import (
        _resolve_default_executor_model,
        _resolve_default_orchestrator_model,
    )

    def fake_select(role: str, **kwargs: Any) -> ModelInfo:
        del kwargs
        model_id = "alt/live-executor" if role == "scout" else "alt/live-orchestrator"
        return ModelInfo(id=model_id, provider="alt")

    monkeypatch.setattr("verdict.subagent_models.select_model_for_role", fake_select)
    assert _resolve_default_executor_model() == "alt/live-executor"
    assert _resolve_default_orchestrator_model() == "alt/live-orchestrator"
    assert _resolve_default_executor_model() != DEFAULT_EXECUTOR_MODEL
    assert _resolve_default_orchestrator_model() != DEFAULT_ORCHESTRATOR_MODEL


def test_default_routes_fall_back_when_live_selector_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from verdict.autodev_run import (
        _resolve_default_executor_model,
        _resolve_default_orchestrator_model,
    )

    def boom(role: str, **kwargs: Any) -> ModelInfo:
        del role, kwargs
        raise RuntimeError("gateway unavailable")

    monkeypatch.setattr("verdict.subagent_models.select_model_for_role", boom)
    assert _resolve_default_executor_model() == DEFAULT_EXECUTOR_MODEL
    assert _resolve_default_orchestrator_model() == DEFAULT_ORCHESTRATOR_MODEL
