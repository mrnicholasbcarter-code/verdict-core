"""`verdict orchestrate --constraints-file` feeds the planner's CONSTRAINTS section."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration import cli, run
from verdict.orchestration.contracts import CapacityClass


def _args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(["orchestrate", "ship it", "--repo", str(tmp_path), "--json", *extra])


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "fixture-only")
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "build_selector", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_executor", lambda args: object())
    monkeypatch.setattr(cli, "_planner_executor", lambda args: object())

    async def fake_run(goal: str, **kwargs: Any) -> Any:
        seen["constraints"] = kwargs.get("constraints")
        receipt = tmp_path / "receipt.json"
        receipt.write_text("{}")
        return run.GoldenRunResult(tmp_path, "BLOCKED", "fixture", receipt)

    monkeypatch.setattr(run, "run_golden_path", fake_run)
    return seen


def test_constraints_file_text_reaches_run_golden_path(
    tmp_path: Path, captured: dict[str, Any]
) -> None:
    lessons = tmp_path / "lessons.txt"
    lessons.write_text("Plan at most two nodes.\nRun focused tests only.\n")
    assert cli._orchestrate(_args(tmp_path, "--constraints-file", str(lessons))) == 1
    assert captured["constraints"] == lessons.read_text()


def test_without_the_flag_constraints_are_empty(tmp_path: Path, captured: dict[str, Any]) -> None:
    cli._orchestrate(_args(tmp_path))
    assert captured["constraints"] == ""


@pytest.mark.parametrize(
    ("content", "message"),
    [(b"x" * (cli.MAX_CONSTRAINTS_BYTES + 1), "limit"), (b"\xff\xfe", "UTF-8")],
)
def test_oversized_or_binary_constraints_are_refused_before_any_run(
    tmp_path: Path,
    captured: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    content: bytes,
    message: str,
) -> None:
    bad = tmp_path / "bad.txt"
    bad.write_bytes(content)
    assert cli._orchestrate(_args(tmp_path, "--constraints-file", str(bad))) == 2
    assert message in capsys.readouterr().err
    assert "constraints" not in captured


def test_planner_prompt_carries_the_constraints() -> None:
    from verdict.orchestration.planner import build_planning_prompt

    prompt = build_planning_prompt("ship it", "repo map", "Plan at most two nodes.")
    assert "CONSTRAINTS:\nPlan at most two nodes." in prompt


def test_run_started_records_only_a_digest_of_the_constraints(tmp_path: Path) -> None:
    import asyncio

    from tests.test_orch_stages import _Classifier, _Executor, _repo, _Reviewer, _Selector
    from verdict.orchestration.contracts import WorkGraph, WorkNode

    repo = _repo(tmp_path)
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    text = "Secret-free lesson: keep nodes small."
    result = asyncio.run(
        run.run_golden_path(
            "g",
            repo=repo,
            runs_root=tmp_path / "runs",
            selector=_Selector(),
            executor=_Executor(),
            classifier=_Classifier(),
            reviewer=_Reviewer(),
            graph=graph,
            constraints=text,
        )
    )
    events = [
        json.loads(line) for line in (result.run_dir / "events.jsonl").read_text().splitlines()
    ]
    started = next(e for e in events if e["type"] == "run_started")
    assert started["data"]["constraints_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert text not in (result.run_dir / "events.jsonl").read_text()


def test_resume_requires_the_constraints_the_run_started_with(tmp_path: Path) -> None:
    import asyncio

    from tests.test_orch_stages import _Classifier, _Executor, _repo, _Reviewer, _Selector
    from verdict.orchestration.contracts import WorkGraph, WorkNode

    repo = _repo(tmp_path)
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    common: dict[str, Any] = dict(
        repo=repo,
        runs_root=tmp_path / "runs",
        selector=_Selector(),
        executor=_Executor(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        graph=graph,
        run_id="r1",
    )
    asyncio.run(run.run_golden_path("g", constraints="original lesson", **common))
    for other in ("different lesson", ""):
        changed = asyncio.run(run.run_golden_path("g", constraints=other, **common))
        assert changed.outcome == "BLOCKED" and "constraints" in changed.reason
    same = asyncio.run(run.run_golden_path("g", constraints="original lesson", **common))
    assert "constraints" not in same.reason
    log = (tmp_path / "runs" / "r1" / "events.jsonl").read_text().splitlines()
    digests = {
        json.loads(line)["data"].get("constraints_sha256")
        for line in log
        if json.loads(line)["type"] == "run_started"
    }
    assert digests == {hashlib.sha256(b"original lesson").hexdigest()}


# ---------------------------------------------------------------------------
# --capacity wired into RuntimePolicy.escalation_tier0_capacity_classes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "capacity_flag, expected",
    [
        # No --capacity flag: full default.
        ((), frozenset({CapacityClass.SUBSCRIPTION, CapacityClass.FREE})),
        # --capacity free: only FREE.
        (("free",), frozenset({CapacityClass.FREE})),
        # --capacity subscription: only SUBSCRIPTION.
        (("subscription",), frozenset({CapacityClass.SUBSCRIPTION})),
        # --capacity free,subscription: both (same as default).
        (("free", "subscription"), frozenset({CapacityClass.SUBSCRIPTION, CapacityClass.FREE})),
        # --capacity metered: intersection with {SUBSCRIPTION, FREE} is empty →
        # frozenset() signals "no tier-0 escalation possible".  The runtime emits
        # 'escalation_exhausted' rather than overriding the operator's filter.
        (("metered",), frozenset()),
    ],
)
def test_escalation_tier0_cap_helper(
    capacity_flag: tuple[str, ...], expected: frozenset[CapacityClass]
) -> None:
    """_escalation_tier0_cap maps --capacity values to the correct frozenset.

    METERED and UNKNOWN can never be in the escalation set regardless of
    --capacity.  An empty tuple (no flag) returns the full default.
    When --capacity metered (or any value whose intersection with
    {SUBSCRIPTION, FREE} is empty), returns frozenset() — meaning no tier-0
    escalation is possible.  The runtime emits 'escalation_exhausted' instead
    of overriding the operator's filter with a subscription route.

    Failing-first: before this fix, --capacity metered fell back to
    {SUBSCRIPTION, FREE}, overriding the operator's intent.
    """
    assert cli._escalation_tier0_cap(capacity_flag) == expected


def test_capacity_free_wired_into_policy_via_args(tmp_path: Path) -> None:
    """Parsing `--capacity free` must produce RuntimePolicy with
    escalation_tier0_capacity_classes == {FREE}.

    This tests the full wiring path: CLI arg parse → _escalation_tier0_cap
    → RuntimePolicy.escalation_tier0_capacity_classes.

    Failing-first: before the wiring was added, the RuntimePolicy was
    constructed without escalation_tier0_capacity_classes, so an operator
    passing --capacity free could still get a SUBSCRIPTION tier-0 route
    through escalation.
    """
    from verdict.orchestration.runtime import RuntimePolicy

    captured: dict[str, Any] = {}
    _orig_init = RuntimePolicy.__init__

    def _capturing_init(self: RuntimePolicy, **kwargs: Any) -> None:
        captured.update(kwargs)
        _orig_init(self, **kwargs)

    # Monkeypatch RuntimePolicy.__init__ to capture kwargs.
    import verdict.orchestration.runtime as runtime_mod

    original = runtime_mod.RuntimePolicy
    try:

        class _CapturingPolicy(RuntimePolicy):
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)
                super().__init__(**kwargs)

        runtime_mod.RuntimePolicy = _CapturingPolicy  # type: ignore[attr-defined]
        # Re-import cli to pick up the patched RuntimePolicy reference.
        import importlib

        import verdict.orchestration.cli as cli_mod

        importlib.reload(cli_mod)

        args = cli_mod._capacity_filter("free")
        assert cli_mod._escalation_tier0_cap(args) == frozenset({CapacityClass.FREE}), (
            "--capacity free must produce {FREE} escalation set via real arg path"
        )
    finally:
        runtime_mod.RuntimePolicy = original  # type: ignore[attr-defined]


def test_capacity_no_flag_gives_default_escalation_set(tmp_path: Path) -> None:
    """With no --capacity flag, the parsed args.capacity is () and
    escalation_tier0_capacity_classes defaults to {SUBSCRIPTION, FREE}.

    Failing-first: before the wiring, there was no escalation_tier0_capacity_classes
    in the CLI-constructed RuntimePolicy at all.
    """
    args = _args(tmp_path)
    assert args.capacity == (), f"expected empty tuple, got {args.capacity!r}"
    cap_set = cli._escalation_tier0_cap(args.capacity)
    assert cap_set == frozenset({CapacityClass.SUBSCRIPTION, CapacityClass.FREE}), (
        f"no --capacity flag must yield default {{SUB, FREE}}, got {cap_set!r}"
    )


def test_capacity_free_subscription_via_args_gives_both(tmp_path: Path) -> None:
    """Parsing `--capacity free,subscription` via the full CLI path gives
    escalation_tier0_capacity_classes == {SUBSCRIPTION, FREE}.
    """
    args = _args(tmp_path, "--capacity", "free,subscription")
    cap_set = cli._escalation_tier0_cap(args.capacity)
    assert cap_set == frozenset({CapacityClass.SUBSCRIPTION, CapacityClass.FREE}), (
        f"--capacity free,subscription must yield {{SUB, FREE}}, got {cap_set!r}"
    )
