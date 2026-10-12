"""`verdict orchestrate --constraints-file` feeds the planner's CONSTRAINTS section."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration import cli, run


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
