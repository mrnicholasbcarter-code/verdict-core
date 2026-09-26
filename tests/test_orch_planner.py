"""Tests for verdict.orchestration.planner (frontier decomposition and topology selection frontier decomposition + topology)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from verdict.orchestration.contracts import (
    NodeKind,
    OrchestrationError,
    Topology,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.planner import (
    FrontierPlanner,
    build_planning_prompt,
    choose_topology,
    hydrate_node_prompt,
    parse_plan,
    repo_map,
)


def _impl_node(
    node_id: str, *, owned=(), depends_on=(), risk="low", verify=("pytest",)
) -> WorkNode:
    return WorkNode(
        node_id=node_id,
        objective=f"do {node_id}",
        kind=NodeKind.IMPLEMENT,
        depends_on=tuple(depends_on),
        owned_files=tuple(owned) or (f"verdict/{node_id}.py",),
        verification_command=tuple(verify),
        risk=risk,
    )


# ---------------------------------------------------------------- choose_topology


def test_topology_single_implement_low_risk_is_solo() -> None:
    node = _impl_node("a", risk="low")
    topology, max_parallel, rationale = choose_topology([node], risk_hint="low")
    assert topology is Topology.SOLO
    assert max_parallel == 1
    assert any("SOLO" in line for line in rationale)


def test_topology_single_implement_medium_risk_is_worker_critic() -> None:
    node = _impl_node("a", risk="medium")
    topology, max_parallel, rationale = choose_topology([node], risk_hint="medium")
    assert topology is Topology.WORKER_CRITIC
    assert max_parallel == 1
    assert any("WORKER_CRITIC" in line for line in rationale)


def test_topology_single_research_node_is_worker_critic() -> None:
    node = WorkNode(node_id="r", objective="investigate", kind=NodeKind.RESEARCH)
    topology, max_parallel, _ = choose_topology([node], risk_hint="low")
    assert topology is Topology.WORKER_CRITIC
    assert max_parallel == 1


def test_topology_parallel_work_units_for_wide_layer() -> None:
    a = _impl_node("a")
    b = _impl_node("b")
    integrate = WorkNode(
        node_id="int",
        objective="integrate",
        kind=NodeKind.INTEGRATE,
        depends_on=("a", "b"),
        verification_command=("pytest",),
    )
    topology, max_parallel, rationale = choose_topology([a, b, integrate], max_parallel=3)
    assert topology is Topology.PARALLEL_WORK_UNITS
    assert max_parallel == 2  # min(3, widest_layer=2)
    assert any("widest_layer=2" in line for line in rationale)


def test_topology_parallel_capped_by_max_parallel() -> None:
    nodes = [_impl_node(n) for n in ("a", "b", "c", "d")]
    topology, max_parallel, _ = choose_topology(nodes, max_parallel=2)
    assert topology is Topology.PARALLEL_WORK_UNITS
    assert max_parallel == 2  # min(2, widest_layer=4)


def test_topology_sequential_multi_node_without_wide_layer_is_worker_critic() -> None:
    a = _impl_node("a")
    b = _impl_node("b", depends_on=("a",))
    topology, max_parallel, rationale = choose_topology([a, b])
    assert topology is Topology.WORKER_CRITIC
    assert max_parallel == 1
    assert any("no layer width" in line for line in rationale)


def test_topology_empty_nodes_raises() -> None:
    with pytest.raises(OrchestrationError):
        choose_topology([])


def test_topology_invalid_max_parallel_raises() -> None:
    with pytest.raises(OrchestrationError):
        choose_topology([_impl_node("a")], max_parallel=0)


def test_topology_rationale_cites_evidence() -> None:
    nodes = [_impl_node("a"), _impl_node("b")]
    _, _, rationale = choose_topology(nodes)
    joined = " ".join(rationale)
    assert "nodes=2" in joined
    assert "implement_nodes=2" in joined
    assert "widest_layer" in joined


# ---------------------------------------------------------------- build_planning_prompt


def test_build_planning_prompt_contains_inputs_and_json_contract() -> None:
    prompt = build_planning_prompt("ship the widget", "verdict/x.py\t10", "no new deps")
    assert "ship the widget" in prompt
    assert "verdict/x.py" in prompt
    assert "no new deps" in prompt
    assert '{"nodes": [...]}' in prompt
    assert 'kind="integrate"' in prompt
    assert "2-8 nodes" in prompt


def test_build_planning_prompt_handles_empty_constraints() -> None:
    prompt = build_planning_prompt("goal", "map", "")
    assert "(none)" in prompt


# ---------------------------------------------------------------- parse_plan


_VALID_NODES_JSON = """
{"nodes": [
  {"node_id": "impl-a", "objective": "implement a", "kind": "implement",
   "owned_files": ["verdict/a.py"], "verification_command": ["pytest", "-q", "tests/test_a.py"]},
  {"node_id": "impl-b", "objective": "implement b", "kind": "implement",
   "owned_files": ["verdict/b.py"], "verification_command": ["pytest", "-q", "tests/test_b.py"]},
  {"node_id": "int", "objective": "integrate", "kind": "integrate",
   "depends_on": ["impl-a", "impl-b"], "verification_command": ["pytest", "-q"]}
]}
"""


def test_parse_plan_success_plain() -> None:
    graph = parse_plan(_VALID_NODES_JSON, goal="ship it")
    assert isinstance(graph, WorkGraph)
    assert graph.topology is Topology.PARALLEL_WORK_UNITS
    assert graph.max_parallel == 2
    assert {n.node_id for n in graph.nodes} == {"impl-a", "impl-b", "int"}


def test_parse_plan_success_with_json_fence() -> None:
    fenced = f"Here is the plan:\n```json\n{_VALID_NODES_JSON}\n```\nThanks."
    graph = parse_plan(fenced, goal="ship it")
    assert isinstance(graph, WorkGraph)
    assert len(graph.nodes) == 3


def test_parse_plan_success_with_leading_prose_no_fence() -> None:
    prosed = f"Sure, here you go: {_VALID_NODES_JSON}"
    graph = parse_plan(prosed, goal="ship it")
    assert len(graph.nodes) == 3


def test_parse_plan_invalid_json_raises() -> None:
    with pytest.raises(OrchestrationError):
        parse_plan("```json\n{not valid json\n```", goal="g")


def test_parse_plan_missing_nodes_key_raises() -> None:
    with pytest.raises(OrchestrationError):
        parse_plan('{"foo": []}', goal="g")


def test_parse_plan_cycle_raises() -> None:
    cyclic = """{"nodes": [
      {"node_id": "a", "objective": "a", "kind": "implement", "owned_files": ["x.py"],
       "verification_command": ["pytest"], "depends_on": ["b"]},
      {"node_id": "b", "objective": "b", "kind": "implement", "owned_files": ["y.py"],
       "verification_command": ["pytest"], "depends_on": ["a"]}
    ]}"""
    with pytest.raises(OrchestrationError, match="cycle"):
        parse_plan(cyclic, goal="g")


def test_parse_plan_unknown_dep_raises() -> None:
    bad = """{"nodes": [
      {"node_id": "a", "objective": "a", "kind": "implement", "owned_files": ["x.py"],
       "verification_command": ["pytest"], "depends_on": ["ghost"]}
    ]}"""
    with pytest.raises(OrchestrationError, match="unknown dependencies"):
        parse_plan(bad, goal="g")


def test_parse_plan_ownership_overlap_raises() -> None:
    overlap = """{"nodes": [
      {"node_id": "a", "objective": "a", "kind": "implement", "owned_files": ["shared.py"],
       "verification_command": ["pytest"]},
      {"node_id": "b", "objective": "b", "kind": "implement", "owned_files": ["shared.py"],
       "verification_command": ["pytest"]}
    ]}"""
    with pytest.raises(OrchestrationError, match="both own"):
        parse_plan(overlap, goal="g")


def test_parse_plan_single_node_default_topology_is_worker_critic() -> None:
    # parse_plan does not accept a risk_hint override, so choose_topology runs
    # with its default risk_hint ("medium"), which never yields SOLO.
    single = """{"nodes": [
      {"node_id": "a", "objective": "a", "kind": "implement", "owned_files": ["x.py"],
       "verification_command": ["pytest"], "risk": "low"}
    ]}"""
    graph = parse_plan(single, goal="g")
    assert graph.topology is Topology.WORKER_CRITIC
    assert graph.max_parallel == 1


# ---------------------------------------------------------------- repo_map


def _init_git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=tmp_path, check=True)
    (tmp_path / "verdict").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "verdict" / "mod.py").write_text("line1\nline2\nline3\n")
    (tmp_path / "tests" / "test_mod.py").write_text("line1\n")
    (tmp_path / "other" / "ignored.py").write_text("x\n")
    (tmp_path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_repo_map_lists_tracked_included_files_with_line_counts(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    text = repo_map(repo, include=("verdict", "tests"))
    assert "verdict/mod.py" in text
    assert "tests/test_mod.py" in text
    assert "other/ignored.py" not in text
    assert "3" in text  # mod.py has 3 lines


def test_repo_map_respects_max_files(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    text = repo_map(repo, include=("verdict", "tests"), max_files=1)
    assert "1 tracked files" in text


# ---------------------------------------------------------------- hydrate_node_prompt


def test_hydrate_node_prompt_includes_core_fields(tmp_path: Path) -> None:
    node = WorkNode(
        node_id="impl-a",
        objective="implement the widget",
        kind=NodeKind.IMPLEMENT,
        owned_files=("verdict/a.py",),
        verification_command=("pytest", "-q", "tests/test_a.py"),
        acceptance=("widget works",),
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="ship the widget")
    assert "ship the widget" in prompt
    assert "implement the widget" in prompt
    assert "widget works" in prompt
    assert "verdict/a.py" in prompt
    assert "pytest -q tests/test_a.py" in prompt
    assert "RESULT: DONE" in prompt
    assert "RESULT: BLOCKED" in prompt


def test_hydrate_node_prompt_includes_required_context_content(tmp_path: Path) -> None:
    (tmp_path / "verdict").mkdir()
    (tmp_path / "verdict" / "ctx.py").write_text("CONTEXT_MARKER = 1\n")
    node = WorkNode(
        node_id="impl-a",
        objective="implement",
        kind=NodeKind.IMPLEMENT,
        owned_files=("verdict/a.py",),
        required_context=("verdict/ctx.py",),
        verification_command=("pytest",),
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g")
    assert "CONTEXT_MARKER" in prompt


def test_hydrate_node_prompt_truncates_and_notes_truncation(tmp_path: Path) -> None:
    (tmp_path / "verdict").mkdir()
    big = "x" * 500
    (tmp_path / "verdict" / "big.py").write_text(big)
    node = WorkNode(
        node_id="impl-a",
        objective="implement",
        kind=NodeKind.IMPLEMENT,
        owned_files=("verdict/a.py",),
        required_context=("verdict/big.py",),
        verification_command=("pytest",),
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g", max_context_bytes=100)
    assert "TRUNCATED" in prompt
    assert "TRUNCATION_NOTES" in prompt
    assert prompt.count("x") <= 200  # bounded, not the full 500 x's


def test_hydrate_node_prompt_excludes_sibling_node_details(tmp_path: Path) -> None:
    node_a = WorkNode(
        node_id="impl-a",
        objective="implement a's secret objective",
        kind=NodeKind.IMPLEMENT,
        owned_files=("verdict/a.py",),
        verification_command=("pytest",),
    )
    node_b_objective = "implement b's totally different objective"
    prompt = hydrate_node_prompt(node_a, repo=tmp_path, goal="g")
    assert node_b_objective not in prompt
    assert "impl-b" not in prompt


# ---------------------------------------------------------------- FrontierPlanner


class _ScriptedExecutor:
    """Fake WorkerExecutor returning scripted terminals in sequence."""

    def __init__(self, outputs: list[WorkerTerminal]) -> None:
        self._outputs = list(outputs)
        self.calls: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.calls.append(prompt)
        if not self._outputs:
            raise AssertionError("no more scripted outputs")
        return self._outputs.pop(0)


@pytest.mark.asyncio
async def test_frontier_planner_success_first_try(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])
    graph, terminal = await FrontierPlanner().plan(
        "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
    )
    assert isinstance(graph, WorkGraph)
    assert terminal.ok
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_frontier_planner_repairs_after_bad_first_output(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output="not json at all"),
            WorkerTerminal(ok=True, output=_VALID_NODES_JSON),
        ]
    )
    graph, terminal = await FrontierPlanner().plan(
        "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
    )
    assert len(graph.nodes) == 3
    assert terminal.ok
    assert len(executor.calls) == 2
    assert "failed validation" in executor.calls[1]


@pytest.mark.asyncio
async def test_frontier_planner_raises_after_failed_repair(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output="not json at all"),
            WorkerTerminal(ok=True, output="still not json"),
        ]
    )
    with pytest.raises(OrchestrationError, match="after one repair round"):
        await FrontierPlanner().plan(
            "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
        )
    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_frontier_planner_raises_when_executor_fails_first_attempt(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor([WorkerTerminal(ok=False, error="rate limited")])
    with pytest.raises(OrchestrationError, match="planning executor failed"):
        await FrontierPlanner().plan(
            "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
        )
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_frontier_planner_raises_when_repair_executor_fails(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output="not json"),
            WorkerTerminal(ok=False, error="transport error"),
        ]
    )
    with pytest.raises(OrchestrationError, match="repair executor failed"):
        await FrontierPlanner().plan(
            "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
        )
    assert len(executor.calls) == 2


def test_planner_free_text_capabilities_are_normalized_to_model_vocabulary() -> None:
    import json

    from verdict.orchestration.planner import WORKER_MIN_CONTEXT_TOKENS, parse_plan

    text = json.dumps(
        {
            "nodes": [
                {
                    "node_id": "a",
                    "objective": "do a",
                    "owned_files": ["a.py"],
                    "verification_command": ["true"],
                    "required_capabilities": ["edit files", "run pytest", "Tool Calling"],
                    "min_context_tokens": 1000,
                },
                {
                    "node_id": "i",
                    "objective": "integrate",
                    "kind": "integrate",
                    "depends_on": ["a"],
                    "verification_command": ["true"],
                    "required_capabilities": ["run pytest"],
                },
            ]
        }
    )
    graph = parse_plan(text, "g")
    a = graph.node("a")
    assert a.required_capabilities == ("tools",)
    assert a.min_context_tokens == WORKER_MIN_CONTEXT_TOKENS
    assert graph.node("i").required_capabilities == ()


def test_planner_boolean_barrier_and_odd_risk_are_normalized() -> None:
    import json

    from verdict.orchestration.planner import parse_plan

    text = json.dumps(
        {
            "nodes": [
                {
                    "node_id": "a",
                    "objective": "do a",
                    "owned_files": ["a.py"],
                    "verification_command": ["true"],
                    "barrier": False,
                    "risk": "LOW",
                },
                {
                    "node_id": "i",
                    "objective": "integrate",
                    "kind": "integrate",
                    "depends_on": ["a"],
                    "verification_command": ["true"],
                    "barrier": True,
                    "risk": "critical",
                },
            ]
        }
    )
    graph = parse_plan(text, "g")
    assert graph.node("a").barrier == "" and graph.node("a").risk == "low"
    assert graph.node("i").barrier == "integration" and graph.node("i").risk == "medium"
