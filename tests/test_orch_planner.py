"""Tests for verdict.orchestration.planner (frontier decomposition and topology selection frontier decomposition + topology)."""

from __future__ import annotations

import hashlib
import re
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
    _agents_md_where_things_live,
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


@pytest.mark.parametrize("acceptance", ["  criterion\nunchanged  ", ["first", " second "]])
def test_parse_plan_preserves_acceptance_text(acceptance: object) -> None:
    import json

    payload = json.loads(_VALID_NODES_JSON)
    payload["nodes"][0]["acceptance"] = acceptance
    graph = parse_plan(json.dumps(payload), goal="g")
    expected = (acceptance,) if isinstance(acceptance, str) else tuple(acceptance)
    assert graph.node("impl-a").acceptance == expected


@pytest.mark.parametrize("acceptance", ["", "   ", {}, 7, False, None])
def test_parse_plan_rejects_malformed_acceptance(acceptance: object) -> None:
    import json

    payload = json.loads(_VALID_NODES_JSON)
    payload["nodes"][0]["acceptance"] = acceptance
    with pytest.raises(OrchestrationError, match="acceptance must be a list"):
        parse_plan(json.dumps(payload), goal="g")


@pytest.mark.parametrize(
    "field", ["depends_on", "owned_files", "required_context", "verification_command"]
)
@pytest.mark.parametrize("value", ["text", {}, 7])
def test_parse_plan_does_not_coerce_other_list_fields(field: str, value: object) -> None:
    import json

    payload = json.loads(_VALID_NODES_JSON)
    payload["nodes"][0]["acceptance"] = "criterion"
    payload["nodes"][0][field] = value
    with pytest.raises(OrchestrationError, match=f"{field} must be a list"):
        parse_plan(json.dumps(payload), goal="g")


def test_parse_plan_singleton_acceptance_still_validates_structure() -> None:
    import json

    payload = json.loads(_VALID_NODES_JSON)
    payload["nodes"][0]["acceptance"] = "criterion"
    payload["nodes"][0]["depends_on"] = ["ghost"]
    with pytest.raises(OrchestrationError, match="unknown dependencies"):
        parse_plan(json.dumps(payload), goal="g")


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


def test_repo_map_lists_verdict_before_tests_in_include_order(tmp_path: Path) -> None:
    """Files are listed in include-tuple order, not alphabetically across all prefixes."""
    repo = _init_git_repo(tmp_path)
    text = repo_map(repo, include=("verdict", "tests"))
    verdict_pos = text.index("verdict/mod.py")
    tests_pos = text.index("tests/test_mod.py")
    assert verdict_pos < tests_pos


def _init_git_repo_with_agents_md(tmp_path: Path) -> Path:
    """Like _init_git_repo but adds AGENTS.md with a 'Where things live' section."""
    _init_git_repo(tmp_path)
    agents_text = (
        "# AGENTS.md\n\n"
        "## Where things live\n\n"
        "| Path | What |\n"
        "|---|---|\n"
        "| `verdict/cli.py` | CLI entry |\n"
        "| `verdict/commands/` | Subcommands |\n\n"
        "## Another section\n\nSome text.\n"
    )
    (tmp_path / "AGENTS.md").write_text(agents_text, encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add agents"], cwd=tmp_path, check=True)
    return tmp_path


def test_repo_map_includes_agents_md_where_things_live_section(tmp_path: Path) -> None:
    repo = _init_git_repo_with_agents_md(tmp_path)
    text = repo_map(repo, include=("verdict", "tests"))
    assert "Where things live" in text
    assert "verdict/cli.py" in text  # from AGENTS.md table
    assert "verdict/commands/" in text  # from AGENTS.md table
    assert "Another section" not in text  # section boundary respected


def test_repo_map_handles_missing_agents_md(tmp_path: Path) -> None:
    """repo_map works without AGENTS.md, returning only the file listing."""
    repo = _init_git_repo(tmp_path)
    text = repo_map(repo, include=("verdict", "tests"))
    assert "verdict/mod.py" in text
    assert "Where things live" not in text


def test_repo_map_handles_agents_md_without_section(tmp_path: Path) -> None:
    """repo_map works when AGENTS.md exists but lacks 'Where things live'."""
    _init_git_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\n\nNo section here.\n", encoding="utf-8")
    subprocess.run(["git", "add", "AGENTS.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add agents"], cwd=tmp_path, check=True)
    text = repo_map(tmp_path, include=("verdict", "tests"))
    assert "verdict/mod.py" in text
    assert "Where things live" not in text


def test_agents_md_where_things_live_byte_cap_with_multibyte_text(tmp_path: Path) -> None:
    """The 4096-byte cap is enforced on the UTF-8 encoding, not on character count.

    A section filled with 3-byte characters (€, U+20AC) would exceed 4096 bytes
    if sliced as characters.  The result must be at most 4096 bytes when re-encoded
    and must decode cleanly (no partial trailing sequences).
    """
    # Each '€' is 3 bytes in UTF-8.  1500 of them = 4500 bytes > 4096.
    wide_row = "| `verdict/cli.py` | " + "€" * 1500 + " |\n"
    agents_text = (
        "# AGENTS.md\n\n"
        "## Where things live\n\n"
        "| Path | What |\n"
        "|---|---|\n" + wide_row + "\n## Another section\n\nSome text.\n"
    )
    (tmp_path / "AGENTS.md").write_text(agents_text, encoding="utf-8")
    # Precondition: the section really is over the cap, so the cap has to act.
    assert len(wide_row.encode("utf-8")) > 4096

    result = _agents_md_where_things_live(tmp_path)

    encoded = result.encode("utf-8")
    assert len(encoded) <= 4096, f"byte length {len(encoded)} exceeds 4096"
    # Must decode cleanly — no partial trailing multibyte sequence was kept.
    encoded.decode("utf-8")
    # The cap truncates; it does not drop the section. An empty result (extractor
    # failure) or a result cut far below the cap must fail here.
    assert result.startswith("## Where things live\n"), result[:60]
    assert "| Path | What |" in result
    # A 3-byte character cut mid-sequence loses at most 2 bytes, so a correct cap
    # keeps 4094..4096 bytes and ends on a whole character.
    assert 4094 <= len(encoded) <= 4096, f"kept {len(encoded)} bytes; cap is 4096"
    assert result.endswith("\u20ac")
    assert "Another section" not in result


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

    answer_only_supported = True

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


class _RecordingEvents:
    """Event sink that records every ``emit`` as ``{"type": kind, **data}``."""

    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, kind: str, **data: object) -> None:
        self.emitted.append({"type": kind, **data})


def _repo_map_in_prompt(prompt: str) -> str:
    """The map text a planning prompt carries between ``REPO MAP:`` and ``CONSTRAINTS:``."""
    found = re.search(r"REPO MAP:\n(.*?)\n\s*CONSTRAINTS:", prompt, re.DOTALL)
    assert found is not None, "planning prompt has no REPO MAP section"
    return found.group(1).strip()


@pytest.mark.asyncio
async def test_frontier_planner_emits_plan_context_with_repo_map_sha256(tmp_path: Path) -> None:
    """plan() emits plan_context whose digest is the SHA-256 of the map the planner was sent."""
    repo = _init_git_repo_with_agents_md(tmp_path)
    events = _RecordingEvents()

    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])
    await FrontierPlanner().plan(
        "test goal", repo=repo, executor=executor, route_id="cc/test", events=events
    )
    plan_ctx = [e for e in events.emitted if e["type"] == "plan_context"]
    assert len(plan_ctx) == 1
    sha = plan_ctx[0]["repo_map_sha256"]
    # sha256 is a 64-character hex string
    assert isinstance(sha, str) and len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)
    # It is the hash of the map text inside the prompt the executor actually received,
    # not just any well-formed digest.
    sent_map = _repo_map_in_prompt(executor.calls[0])
    assert sent_map, "the planner prompt carried an empty repo map"
    assert sha == hashlib.sha256(sent_map.encode()).hexdigest()
    assert sha != hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_frontier_planner_prompt_carries_repo_map_with_agents_section(tmp_path: Path) -> None:
    """The prompt sent to the planner holds the AGENTS.md section and the ordered file listing."""
    repo = _init_git_repo_with_agents_md(tmp_path)
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])
    await FrontierPlanner().plan("test goal", repo=repo, executor=executor, route_id="cc/test")
    assert len(executor.calls) == 1
    sent_map = _repo_map_in_prompt(executor.calls[0])
    # The AGENTS.md table reached the planner without any --constraints-file.
    assert '[AGENTS.md "Where things live"]' in sent_map
    assert "| `verdict/commands/` | Subcommands |" in sent_map
    assert "Another section" not in sent_map
    # The file listing follows it, with verdict/ listed before tests/.
    assert "[File listing]" in sent_map
    assert sent_map.index("verdict/mod.py") < sent_map.index("tests/test_mod.py")
    assert sent_map.index('[AGENTS.md "Where things live"]') < sent_map.index("[File listing]")


@pytest.mark.asyncio
async def test_frontier_planner_repair_call_resends_same_map_and_emits_no_second_plan_context(
    tmp_path: Path,
) -> None:
    """One plan_context per plan() call; the validation-repair call carries the same map.

    The spec says exactly this: the repair prompt is the planning prompt plus the
    validation error, so it holds the same repo map, and no second plan_context
    event is emitted for it.
    """
    repo = _init_git_repo_with_agents_md(tmp_path)
    events = _RecordingEvents()
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output="not json at all"),
            WorkerTerminal(ok=True, output=_VALID_NODES_JSON),
        ]
    )
    await FrontierPlanner().plan(
        "test goal", repo=repo, executor=executor, route_id="cc/test", events=events
    )
    assert len(executor.calls) == 2, "the first answer was invalid, so a repair call must follow"
    first_map = _repo_map_in_prompt(executor.calls[0])
    repair_map = _repo_map_in_prompt(executor.calls[1])
    assert first_map and repair_map == first_map
    assert "Your previous response failed validation" in executor.calls[1]
    kinds = [e["type"] for e in events.emitted]
    assert kinds == ["plan_context", "plan_repair_started", "plan_repair_terminal"]
    assert events.emitted[0]["repo_map_sha256"] == hashlib.sha256(first_map.encode()).hexdigest()


@pytest.mark.asyncio
async def test_frontier_planner_no_plan_context_without_events(tmp_path: Path) -> None:
    """plan() emits no plan_context when events=None (default)."""
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor([WorkerTerminal(ok=True, output=_VALID_NODES_JSON)])
    # Should not raise; events=None means no emit attempted
    graph, _ = await FrontierPlanner().plan(
        "test goal", repo=repo, executor=executor, route_id="cc/test"
    )
    assert isinstance(graph, WorkGraph)


class _ToolAwareExecutor(_ScriptedExecutor):
    """Fake executor that records the ``tools`` keyword of every call."""

    def __init__(self, outputs: list[WorkerTerminal]) -> None:
        super().__init__(outputs)
        self.tools: list[bool] = []

    async def run(  # type: ignore[override]
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float, tools: bool = True
    ) -> WorkerTerminal:
        self.tools.append(tools)
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


@pytest.mark.asyncio
async def test_frontier_planner_plan_and_repair_calls_run_without_tools(tmp_path: Path) -> None:
    """Planning needs one JSON answer; a tool-enabled planner looped until timeout."""
    repo = _init_git_repo(tmp_path)
    executor = _ToolAwareExecutor(
        [
            WorkerTerminal(ok=True, output="not json at all"),
            WorkerTerminal(ok=True, output=_VALID_NODES_JSON),
        ]
    )
    await FrontierPlanner().plan(
        "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
    )
    assert executor.tools == [False, False]


@pytest.mark.asyncio
async def test_frontier_planner_refuses_default_prime_headless(tmp_path: Path) -> None:
    """Prime headless is worker-only unless a legacy answer-only flag is explicit."""
    import stat

    from verdict.orchestration.executors import PrimeHeadlessExecutor

    route = "cc/claude-sonnet-5"
    (tmp_path / "repo").mkdir()
    repo = _init_git_repo(tmp_path / "repo")
    prime = tmp_path / "fake-prime"
    prime.write_text("#!/usr/bin/env python3\nraise SystemExit('should not spawn')\n")
    prime.chmod(prime.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(OrchestrationError, match="direct-gateway"):
        await FrontierPlanner().plan(
            "ship it",
            repo=repo,
            executor=PrimeHeadlessExecutor(prime_bin=str(prime)),
            route_id=route,
            timeout_seconds=30,
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["not json", '{"nodes": []}'])
async def test_frontier_planner_persists_redacted_bounded_invalid_output(
    tmp_path: Path, invalid: str
) -> None:
    from verdict.orchestration.receipt import EventLog

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    repo = _init_git_repo(repo_path)
    run_dir = tmp_path / "run"
    events = EventLog(run_dir / "events.jsonl")
    secret = "fake-planner-secret-canary"
    output = f'{invalid}\n"api_key": "{secret}"\n' + "é" * 70_000
    executor = _ScriptedExecutor(
        [WorkerTerminal(ok=True, output=output), WorkerTerminal(ok=True, output=_VALID_NODES_JSON)]
    )
    graph, _ = await FrontierPlanner().plan(
        "ship it",
        repo=repo,
        executor=executor,
        route_id="cc/claude-sonnet-5",
        events=events,
        run_dir=run_dir,
    )
    assert len(graph.nodes) == 3
    repair = next(event for event in events.read() if event.type == "plan_repair_started")
    artifact = Path(repair.data["planner_output_path"])
    assert artifact.parent == run_dir
    assert artifact.is_file()
    data = artifact.read_bytes()
    assert 0 < len(data) <= 64 * 1024
    text = data.decode("utf-8")
    assert text.startswith(invalid)
    assert secret not in text
    assert "[redacted]" in text


@pytest.mark.asyncio
async def test_frontier_planner_retains_invalid_repair_output(tmp_path: Path) -> None:
    from verdict.orchestration.receipt import EventLog

    repo = _init_git_repo(tmp_path)
    run_dir = tmp_path / "run"
    events = EventLog(run_dir / "events.jsonl")
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output="not json"),
            WorkerTerminal(ok=True, output="still invalid password=fake-repair-canary"),
        ]
    )
    with pytest.raises(OrchestrationError, match="after one repair round"):
        await FrontierPlanner().plan(
            "g", repo=repo, executor=executor, route_id="cc/s", events=events, run_dir=run_dir
        )
    artifact = run_dir / "planner-1-repair-invalid.txt"
    assert artifact.parent == run_dir
    assert artifact.read_text() == "still invalid password=[redacted]"
    assert len(list(run_dir.glob("planner-*-invalid.txt"))) == 2


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


def test_planner_assigns_shared_package_markers_to_one_dependency_owner() -> None:
    prompt = build_planning_prompt("add a package", "", "")
    assert "exactly one node must own each file" in prompt
    assert "must depend_on that owner" in prompt
    assert "Python namespace packages import without __init__.py" in prompt


def test_worker_prompt_blocks_host_changes_and_out_of_scope_files(tmp_path: Path) -> None:
    node = WorkNode(
        "a", "implement", owned_files=("pkg/a.py",), verification_command=("pytest", "-q")
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g")
    assert "Do not modify anything outside the worktree" in prompt
    assert "no files under $HOME" in prompt
    assert "no symlinks" in prompt
    assert "no PATH or shell-profile edits" in prompt
    assert "no package installs" in prompt
    assert "If a file outside OWNED_FILES is required, do not create or modify it" in prompt
    assert "naming that file" in prompt
    assert "If VERIFICATION_COMMAND cannot run as given, stop" in prompt
    assert "Do not change the host to make it run" in prompt


@pytest.mark.asyncio
async def test_planner_failover_excludes_failed_pool_and_records_floor(tmp_path, monkeypatch):
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.planner import PlanValidationError
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    requirements = []
    rows = []

    class Selector:
        def select(self, req, *, now):
            requirements.append(req)
            route = "cc/claude-haiku" if len(requirements) == 1 else "kr/claude-sonnet"
            return RouteVerdict(
                route, "claude", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION
            ), ()

        def record_failure(self, route, failure, *, now):
            rows.append({"type": "record_failure", "route": route})

        def inventory_route_ids(self):
            return {"cc/claude-haiku", "kr/claude-sonnet"}

        def set_planner_band(self, band):
            pass

    class Events:
        def emit(self, kind, **data):
            rows.append({"type": kind, **data})

    async def plan(self, *args, **kwargs):
        assert kwargs["run_dir"] == tmp_path / "run"
        if kwargs["route_id"].startswith("cc/"):
            # Node ids and file names may contain digits such as 401 or 429.
            raise PlanValidationError("concurrent nodes a401 and b429 both own ['shared.py']")
        graph = WorkGraph("g", (_impl_node("a"),))
        return graph, WorkerTerminal(ok=True)

    from tests.test_planner_band import _record, _score
    from tests.test_planner_band import _snapshot as _pb_snapshot

    _pb_snap = _pb_snapshot(
        _record(
            "p/cc_claude-haiku",
            ("cc/claude-haiku",),
            aa_intelligence=_score(90.0),
            aa_coding=_score(99.0),
        ),
        _record(
            "p/kr_claude-sonnet",
            ("kr/claude-sonnet",),
            aa_intelligence=_score(90.0),
            aa_coding=_score(98.0),
        ),
    )
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda: _pb_snap)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")
    monkeypatch.setattr(FrontierPlanner, "plan", plan)
    await plan_with_failover(
        "g",
        repo=tmp_path,
        selector=Selector(),
        executor=None,
        classifier=FailureIntelligence(),
        events=Events(),
        run_dir=tmp_path / "run",
    )
    assert requirements[0].max_capability_tier == 3
    # SH-1: an invalid plan excludes the whole backend pool from reselection.
    # It is a planning-quality failure, so nothing is cooled for later runs.
    assert requirements[1].exclude_pools == frozenset({"claude"})
    assert "cc/claude-haiku" in requirements[1].exclude_routes
    assert [r["max_capability_tier"] for r in rows if r["type"] == "plan_started"] == [3, 3]
    failed = next(r for r in rows if r.get("state") == "PLANNER_FAILED")
    assert failed["category"] == "plan_invalid"
    assert not [r for r in rows if r["type"] in {"cooldown", "record_failure"}]
    replacing = next(r for r in rows if r.get("state") == "REPLACING")
    assert replacing["excluded_pool"] == "claude"


@pytest.mark.asyncio
async def test_plan_with_failover_uses_distinct_planner_executor(tmp_path, monkeypatch):
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    rows = []
    planner_executor = object()
    worker_executor = object()

    class Selector:
        def select(self, req, *, now):
            return RouteVerdict(
                "cc/claude-sonnet",
                "claude",
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.SUBSCRIPTION,
            ), ()

        def inventory_route_ids(self):
            return {"cc/claude-sonnet"}

        def set_planner_band(self, band):
            pass

    class Events:
        def emit(self, kind, **data):
            rows.append({"type": kind, **data})

    async def plan(self, *args, **kwargs):
        assert kwargs["executor"] is planner_executor
        return WorkGraph("g", (_impl_node("a"),)), WorkerTerminal(ok=True, model=kwargs["route_id"])

    from tests.test_planner_band import _record, _score
    from tests.test_planner_band import _snapshot as _pb_snapshot

    _pb_snap = _pb_snapshot(
        _record(
            "p/cc_claude-sonnet",
            ("cc/claude-sonnet",),
            aa_intelligence=_score(90.0),
            aa_coding=_score(99.0),
        )
    )
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda: _pb_snap)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")
    monkeypatch.setattr(FrontierPlanner, "plan", plan)
    graph = await plan_with_failover(
        "g",
        repo=tmp_path,
        selector=Selector(),
        executor=worker_executor,
        planner_executor=planner_executor,
        classifier=FailureIntelligence(),
        events=Events(),
        run_dir=tmp_path / "run",
    )

    assert graph.nodes[0].node_id == "a"
    healthy = next(r for r in rows if r["type"] == "controller" and r.get("state") == "HEALTHY")
    assert healthy["route_id"] == "cc/claude-sonnet"


@pytest.mark.asyncio
async def test_direct_planner_missing_capacity_blocks_explicitly(tmp_path, monkeypatch):
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    rows = []
    repo = tmp_path / "repo"
    repo.mkdir()
    repo = _init_git_repo(repo)

    class Selector:
        def __init__(self):
            self.calls = 0

        def select(self, req, *, now):
            self.calls += 1
            if self.calls > 1:
                return None, ()
            return RouteVerdict(
                "cc/claude-sonnet",
                "claude",
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.SUBSCRIPTION,
            ), ()

        def record_failure(self, route, failure, *, now):
            rows.append({"type": "record_failure", "route": route, "category": failure.category})

        def inventory_route_ids(self):
            return {"cc/claude-sonnet"}

        def set_planner_band(self, band):
            pass

    class DirectPlanner:
        answer_only_supported = True

        async def run(self, prompt, *, route_id, cwd, timeout_seconds):
            return WorkerTerminal(ok=False, model=route_id, status_code=429, error="rate limited")

    class Events:
        def emit(self, kind, **data):
            rows.append({"type": kind, **data})

    from tests.test_planner_band import _record, _score
    from tests.test_planner_band import _snapshot as _pb_snapshot

    _pb_snap = _pb_snapshot(
        _record(
            "p/cc_claude-sonnet",
            ("cc/claude-sonnet",),
            aa_intelligence=_score(90.0),
            aa_coding=_score(99.0),
        )
    )
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda: _pb_snap)

    with pytest.raises(OrchestrationError, match="planning failed on every eligible"):
        await plan_with_failover(
            "g",
            repo=repo,
            selector=Selector(),
            executor=object(),
            planner_executor=DirectPlanner(),
            classifier=FailureIntelligence(),
            events=Events(),
            run_dir=tmp_path / "run",
            max_attempts=2,
        )

    controller = next(r for r in rows if r["type"] == "controller" and r.get("state") == "QUOTA")
    assert controller["route_id"] == "cc/claude-sonnet"
    assert next(r for r in rows if r["type"] == "cooldown")["category"] == "rate_limited"


@pytest.mark.parametrize("kind", [NodeKind.IMPLEMENT, NodeKind.RESEARCH])
def test_worker_prompt_forbids_summary_files(tmp_path, kind):
    node = WorkNode(
        "a", "do work", kind=kind, owned_files=("a.py",), verification_command=("true",)
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g")
    assert "Do not create summary/notes files; report in your final message." in prompt


@pytest.mark.asyncio
async def test_plan_repair_names_conflicting_nodes_and_file(tmp_path):
    import json

    nodes = [_impl_node(n, owned=("shared.py",)) for n in ("left", "mid", "right")]
    bad = json.dumps({"nodes": [n.to_dict() for n in nodes]})
    chained = [
        nodes[0],
        _impl_node("mid", owned=("shared.py",), depends_on=("left",)),
        _impl_node("right", owned=("shared.py",), depends_on=("mid",)),
    ]
    good = json.dumps({"nodes": [n.to_dict() for n in chained]})
    executor = _ScriptedExecutor(
        [WorkerTerminal(ok=True, output=bad), WorkerTerminal(ok=True, output=good)]
    )
    repo = _init_git_repo(tmp_path)
    await FrontierPlanner().plan("g", repo=repo, executor=executor, route_id="cc/s")
    prompt = executor.calls[1]
    assert "concurrent nodes left and mid both own ['shared.py']" in prompt
    # The graph error stops at the first pair; the repair prompt lists every pair.
    for pair in ("left and node mid", "left and node right", "mid and node right"):
        assert f"- shared.py: owned by both node {pair}\n" in prompt
    assert "Serialize" in prompt and "depends_on" in prompt
    assert "merge" in prompt.lower() and "one node" in prompt


# ---------------------------------------------------------------- planner reselection (SH-1)


def _planner_ladder(
    tmp_path: Path,
    routes: list[tuple[str, str, int]],
    connections,
    health_cache=None,
    monkeypatch=None,
):
    from tests.test_orch_eligibility import make_ladder, row
    from tests.test_planner_band import _record, _score, _snapshot

    if monkeypatch is not None:
        # Provide a rated metadata store so plan_with_failover's band setup
        # succeeds.  Equal aa_intelligence ties every route at the cutoff so
        # _non_band_routes stays empty; aa_coding by tier puts tier-1 before
        # tier-2 within the same capacity class (ADR-051 D5/D14 sort key).
        # fetched_at is 3 h before test_orch_eligibility.NOW so the scores are
        # fresh when plan_with_failover calls compute_planner_bands(now=NOW).
        from datetime import timedelta

        from tests.test_orch_eligibility import NOW as _PB_NOW

        _pb_fetched = (_PB_NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        records = [
            _record(
                f"p/{r.replace('/', '_')}",
                (r,),
                aa_intelligence=_score(90.0, _pb_fetched),
                aa_coding=_score(100.0 - tier, _pb_fetched),
            )
            for r, _, tier in routes
        ]
        snap = _snapshot(*records)
        # Patch before the ladder is constructed so _rating_evidence() caches
        # the right snapshot on its first (lazy) call.
        monkeypatch.setattr("verdict.metadata.store.load_store", lambda: snap)

    rows = [{**row(r, owned_by=owner), "capability_tier": tier} for r, owner, tier in routes]
    ladder, _ = make_ladder(tmp_path, rows, connections, health_cache=health_cache)
    return ladder


class _RoutedPlanner:
    """Answer-only planner executor; ``reply(route_id)`` decides each terminal."""

    answer_only_supported = True

    def __init__(self, reply) -> None:
        self.reply = reply
        self.routes: list[str] = []

    async def run(self, prompt, *, route_id, cwd, timeout_seconds):
        self.routes.append(route_id)
        return self.reply(route_id)


_COLLIDING_PLAN = WorkerTerminal(
    ok=True,
    output=(
        '{"nodes": ['
        '{"node_id": "a", "objective": "a", "owned_files": ["verdict/a.py"],'
        ' "verification_command": ["pytest", "tests/test_new.py"]},'
        '{"node_id": "b", "objective": "b", "owned_files": ["verdict/b.py"],'
        ' "verification_command": ["pytest", "tests/test_new.py"]}]}'
    ),
)
_GOOD_PLAN = WorkerTerminal(ok=True, output=_VALID_NODES_JSON)
_HAIKU = ("cc/claude-haiku-4-5", "claude/claude-haiku-4-5", "no-think/cc/claude-haiku-4-5")


async def _plan(tmp_path, monkeypatch, ladder, planner, now):
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    rows: list[dict] = []

    class Events:
        def emit(self, kind, **data):
            rows.append({"type": kind, **data})

    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")
    monkeypatch.setattr("verdict.orchestration.planner.repo_map", lambda repo: "offline map")
    await plan_with_failover(
        "g",
        repo=tmp_path,
        selector=ladder,
        executor=planner,
        classifier=FailureIntelligence(),
        events=Events(),
        now=lambda: now,
    )
    return rows


@pytest.mark.asyncio
async def test_invalid_plan_reselects_outside_the_alias_pool(tmp_path, monkeypatch):
    from tests.test_orch_eligibility import NOW, conn

    # Declared tiers make the haiku aliases the strongest (rating-first ranking).
    routes = [(r, "claude", 1) for r in _HAIKU]
    routes += [("kr/claude-haiku-4-5", "kiro", 1), ("cx/gpt-5.5", "codex", 2)]
    ladder = _planner_ladder(
        tmp_path, routes, [conn("claude"), conn("kiro"), conn("codex")], monkeypatch=monkeypatch
    )
    planner = _RoutedPlanner(lambda r: _GOOD_PLAN if r.startswith("cx/") else _COLLIDING_PLAN)

    rows = await _plan(tmp_path, monkeypatch, ladder, planner, NOW)

    # Same mistake, same model: no claude/ or no-think/cc/ alias, and not the
    # same model on another pool. One initial call plus one repair, then codex.
    assert planner.routes == ["cc/claude-haiku-4-5", "cc/claude-haiku-4-5", "cx/gpt-5.5"]
    replacing = next(r for r in rows if r.get("state") == "REPLACING")
    assert replacing["excluded_pool"] == "claude"
    assert set(replacing["excluded_routes"]) == {*_HAIKU, "kr/claude-haiku-4-5"}


@pytest.mark.asyncio
async def test_invalid_plan_from_no_think_wrapper_excludes_the_inner_pool(tmp_path, monkeypatch):
    from tests.test_orch_eligibility import NOW, conn

    routes = [
        ("no-think/cc/claude-haiku-4-5", "claude", 1),
        ("no-think/cc/claude-sonnet-5", "claude", 2),
        ("cx/gpt-5.5", "codex", 2),
    ]
    ladder = _planner_ladder(
        tmp_path, routes, [conn("claude"), conn("codex")], monkeypatch=monkeypatch
    )
    planner = _RoutedPlanner(lambda r: _GOOD_PLAN if r.startswith("cx/") else _COLLIDING_PLAN)

    rows = await _plan(tmp_path, monkeypatch, ladder, planner, NOW)

    # The sibling wrapper model on the same credential pool is skipped too.
    assert planner.routes[-1] == "cx/gpt-5.5"
    assert "no-think/cc/claude-sonnet-5" not in planner.routes
    replacing = next(r for r in rows if r.get("state") == "REPLACING")
    assert replacing["excluded_pool"] == "claude"


@pytest.mark.asyncio
async def test_route_failure_skips_aliases_but_keeps_sibling_models(tmp_path, monkeypatch):
    from tests.test_orch_eligibility import NOW, conn

    routes = [(r, "claude", 1) for r in _HAIKU] + [("cc/claude-sonnet-5", "claude", 2)]
    ladder = _planner_ladder(tmp_path, routes, [conn("claude")], monkeypatch=monkeypatch)
    timeout = WorkerTerminal(ok=False, error="timeout", stop_reason="timeout")
    planner = _RoutedPlanner(lambda r: timeout if "haiku" in r else _GOOD_PLAN)

    rows = await _plan(tmp_path, monkeypatch, ladder, planner, NOW)

    # The aliases rank above the sibling model; a timeout is local to the model.
    assert planner.routes == ["cc/claude-haiku-4-5", "cc/claude-sonnet-5"]
    replacing = next(r for r in rows if r.get("state") == "REPLACING")
    assert replacing["excluded_pool"] == ""
    assert set(replacing["excluded_routes"]) == set(_HAIKU)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", _HAIKU)
async def test_credential_cooldown_on_one_alias_blocks_every_alias(tmp_path, monkeypatch, first):
    from tests.test_orch_eligibility import NOW, conn
    from verdict.orchestration.contracts import TaskRequirements

    # Only ``first`` declares the strongest tier, so it ranks first and fails first.
    routes = [(r, "claude", 1 if r == first else 2) for r in _HAIKU]
    routes.append(("cx/gpt-5.5", "codex", 2))
    ladder = _planner_ladder(
        tmp_path, routes, [conn("claude"), conn("codex")], monkeypatch=monkeypatch
    )
    limited = WorkerTerminal(ok=False, status_code=429, error="rate limited")
    planner = _RoutedPlanner(lambda r: _GOOD_PLAN if r.startswith("cx/") else limited)

    rows = await _plan(tmp_path, monkeypatch, ladder, planner, NOW)

    assert planner.routes == [first, "cx/gpt-5.5"]
    cooldown = next(r for r in rows if r["type"] == "cooldown")
    assert (cooldown["scope"], cooldown["key"]) == ("pool", "claude")
    # The cooldown outlives this planning call: a fresh selection with no
    # exclusions still cannot reach the pool through any alias.
    for alias in _HAIKU:
        assert ladder.dispatch_blocker(alias, now=NOW) == "pool:claude"
    choice, _ = ladder.select(
        TaskRequirements(frontier_worthy=True, max_capability_tier=2), now=NOW
    )
    assert choice is not None and choice.route_id == "cx/gpt-5.5"


@pytest.mark.asyncio
async def test_planner_reselection_follows_cheapest_qualified_capacity(tmp_path, monkeypatch):
    from tests.test_orch_eligibility import NOW, conn

    routes = [
        ("cc/claude-sonnet-5", "claude", 1),
        ("gl/glm-5", "glm", 2),
        ("ms/mistral-large", "mistral", 2),
    ]
    connections = [conn("claude"), conn("glm", free_only=True), conn("mistral", free_only=True)]
    # Planner requirements include tools, so FREE routes need fresh agentic
    # proof (BOD-299 AC2); seed the health cache for both free routes.
    from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult

    cache = HealthCache(tmp_path / "health-cache.json")
    for free_route in ("gl/glm-5", "ms/mistral-large"):
        cache.record(
            free_route,
            ProbeResult(
                category=CATEGORY_OK,
                chat_ok=True,
                tool_ok=True,
                probe_class="agentic",
                agentic_ok=True,
            ),
            NOW,
        )
    ladder = _planner_ladder(
        tmp_path, routes, connections, health_cache=cache, monkeypatch=monkeypatch
    )
    planner = _RoutedPlanner(lambda r: _COLLIDING_PLAN if r.startswith("gl/") else _GOOD_PLAN)

    rows = await _plan(tmp_path, monkeypatch, ladder, planner, NOW)

    # D6: free qualifies and is attempted before subscription. A bad plan
    # still reselects to another qualifying route rather than readmitting it.
    started = [(r["route_id"], r["capacity_class"]) for r in rows if r["type"] == "plan_started"]
    assert started == [("gl/glm-5", "free"), ("ms/mistral-large", "free")]


@pytest.mark.parametrize("cut", [0, 1, 5, 9, 13, 20, 31])
def test_invalid_output_secret_across_the_byte_cut_never_leaks(tmp_path: Path, cut: int) -> None:
    """Redaction runs before truncation: no fragment of a secret survives the 64 KiB cut."""
    from verdict.orchestration.planner import _persist_invalid_plan_output

    secret = "https://user:hunter2pw@gateway.invalid/v1"
    prefix = "x" * (64 * 1024 - cut)
    path = _persist_invalid_plan_output(
        prefix + secret + " tail", tmp_path, attempt=1, phase="initial"
    )
    assert path is not None
    data = Path(path).read_bytes()
    assert len(data) <= 64 * 1024
    for fragment in ("hunter2", "unter2pw", "2pw@", "user:h"):
        assert fragment.encode() not in data


@pytest.mark.parametrize(
    "secret",
    [
        "Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv",
        "sk-proj-AbCd3fGh1jKlMnOpQrStUvWx",
        "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYzAbCdEfGh",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        "AKIAABCDEFGHIJKLMNOP",
    ],
)
def test_invalid_output_redacts_bare_tokens(tmp_path: Path, secret: str) -> None:
    from verdict.orchestration.planner import _persist_invalid_plan_output

    path = _persist_invalid_plan_output(
        f"plan text {secret} more", tmp_path, attempt=1, phase="initial"
    )
    assert path is not None
    text = Path(path).read_text()
    token = secret.split()[-1]
    assert token not in text and token[6:] not in text
    assert "[redacted]" in text


def test_invalid_output_write_failure_does_not_hide_the_error(tmp_path: Path) -> None:
    from verdict.orchestration.planner import _persist_invalid_plan_output

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file")
    assert _persist_invalid_plan_output("x", blocker, attempt=1, phase="initial") is None


def test_invalid_output_file_is_owner_only_and_never_follows_symlinks(tmp_path: Path) -> None:
    """Model output may hold secrets no pattern knows: keep the file 0600, no symlink writes."""
    import os
    import stat

    from verdict.orchestration.planner import _persist_invalid_plan_output

    path = _persist_invalid_plan_output("plan text", tmp_path, attempt=1, phase="initial")
    assert path is not None
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep")
    link = tmp_path / "planner-2-initial-invalid.txt"
    link.symlink_to(target)
    assert _persist_invalid_plan_output("x", tmp_path, attempt=2, phase="initial") is None
    assert target.read_text() == "keep"


# ---------------------------------------------------------------------------
# ADR-051 band integration: plan_with_failover fail-closed and band record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_with_failover_raises_on_nan_frontier_config(tmp_path, monkeypatch) -> None:
    """Blocking #3 / verify #11: NaN frontier config fails closed with PlannerBandConfigError."""
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover
    from verdict.planner_band import PlannerBandConfigError

    monkeypatch.setenv("VERDICT_PLANNER_FRONTIER_TOP", "nan")
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")

    class Selector:
        def select(self, req, *, now):
            return RouteVerdict(
                "cc/model", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION
            ), ()

        def record_failure(self, route, failure, *, now):
            pass

    class Events:
        def emit(self, kind, **data):
            pass

    with pytest.raises(PlannerBandConfigError, match="invalid_planner_band_config"):
        await plan_with_failover(
            "g",
            repo=tmp_path,
            selector=Selector(),  # type: ignore[arg-type]
            executor=None,  # type: ignore[arg-type]
            classifier=FailureIntelligence(),
            events=Events(),
        )


@pytest.mark.asyncio
async def test_plan_with_failover_empty_band_fails_closed(tmp_path, monkeypatch) -> None:
    """Blocking #3: when metadata loads but no routes are rated, fail with PlannerBandError."""

    # Import test helpers that share the same time domain
    from tests.test_planner_band import NOW, _record, _snapshot
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover
    from verdict.planner_band import PlannerBandError

    # A snapshot with a record that has aa_coding but NOT aa_intelligence → no rated models
    snap = _snapshot(_record("p/model", ("cc/model",)))
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda: snap)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")

    class Selector:
        def inventory_route_ids(self):
            return ["cc/model"]

        def set_planner_band(self, band):
            pass

        def select(self, req, *, now):
            return RouteVerdict(
                "cc/model", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION
            ), ()

        def record_failure(self, route, failure, *, now):
            pass

    class Events:
        def emit(self, kind, **data):
            pass

    with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
        await plan_with_failover(
            "g",
            repo=tmp_path,
            selector=Selector(),  # type: ignore[arg-type]
            executor=None,  # type: ignore[arg-type]
            classifier=FailureIntelligence(),
            events=Events(),
            now=lambda: NOW,
        )


@pytest.mark.asyncio
async def test_plan_started_event_has_all_band_record_fields(tmp_path, monkeypatch) -> None:
    """Blocking #6: plan_started planner_band has all 9 PlannerBandResult fields."""
    from tests.test_planner_band import NOW, _record, _score, _snapshot
    from verdict.orchestration.contracts import (
        CapacityClass,
        EligibilityStage,
        NodeKind,
        RouteVerdict,
        WorkerTerminal,
        WorkGraph,
        WorkNode,
    )
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    # Snapshot with one route that has aa_intelligence → it will be in the frontier band
    snap = _snapshot(_record("p/model", ("cc/model",), aa_intelligence=_score(90.0)))
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda: snap)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")

    rows = []

    class Selector:
        def inventory_route_ids(self):
            return ["cc/model"]

        def set_planner_band(self, band):
            pass

        def select(self, req, *, now):
            return RouteVerdict(
                "cc/model", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION
            ), ()

        def record_failure(self, route, failure, *, now):
            pass

    class Events:
        def emit(self, kind, **data):
            rows.append({"type": kind, **data})

    async def plan(self, goal, **kwargs):
        return WorkGraph(
            goal, (WorkNode("r", "research", kind=NodeKind.RESEARCH),)
        ), WorkerTerminal(ok=True)

    monkeypatch.setattr(FrontierPlanner, "plan", plan)
    await plan_with_failover(
        "g",
        repo=tmp_path,
        selector=Selector(),  # type: ignore[arg-type]
        executor=None,  # type: ignore[arg-type]
        classifier=FailureIntelligence(),
        events=Events(),
        run_dir=tmp_path / "run",
        now=lambda: NOW,
    )

    plan_started = next(r for r in rows if r["type"] == "plan_started")
    band_record = plan_started.get("planner_band")
    assert band_record is not None, "planner_band should be present in plan_started event"
    # All 9 keys from PlannerBandResult.to_dict()
    expected_keys = {
        "required",
        "used",
        "reason",
        "rank",
        "rated_models",
        "cutoff",
        "rating",
        "metric",
        "fetched_at",
    }
    assert set(band_record.keys()) == expected_keys, f"wrong keys: {set(band_record.keys())}"
    assert band_record["used"] in {"frontier", "fallback"}, (
        f"used={band_record['used']!r} not in frontier/fallback"
    )


@pytest.mark.asyncio
async def test_plan_with_failover_fails_closed_when_metadata_store_unavailable(
    tmp_path, monkeypatch
) -> None:
    """Item 1: metadata store OSError → PlannerBandError, run BLOCKED (not silently skipped)."""
    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover
    from verdict.planner_band import PlannerBandError

    def _raise_oserror() -> None:
        raise OSError("metadata store not found")

    monkeypatch.setattr("verdict.metadata.store.load_store", _raise_oserror)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")

    class Selector:
        def inventory_route_ids(self) -> list[str]:
            return ["cc/model"]

        def set_planner_band(self, band: object) -> None:
            pass

        def select(self, req: object, *, now: object) -> object:
            return RouteVerdict(
                "cc/model", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION
            ), ()

        def record_failure(self, route: object, failure: object, *, now: object) -> None:
            pass

    class Events:
        def emit(self, kind: str, **data: object) -> None:
            pass

    with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
        await plan_with_failover(
            "g",
            repo=tmp_path,
            selector=Selector(),  # type: ignore[arg-type]
            executor=None,  # type: ignore[arg-type]
            classifier=FailureIntelligence(),
            events=Events(),
        )


@pytest.mark.asyncio
async def test_plan_with_failover_blocks_when_band_has_only_metered_routes(
    tmp_path, monkeypatch
) -> None:
    """Item 2: EligibilityLadder METERED filter → plan_with_failover raises OrchestrationError.

    When the only admissible frontier route is METERED, the EligibilityLadder's
    _assess_all band re-sort filter removes it from candidates and returns None.
    plan_with_failover must then block (OrchestrationError) rather than dispatching
    to the METERED route.  The mock simulates what a real fixed ladder returns.
    """
    from tests.test_planner_band import NOW, _record, _score, _snapshot
    from verdict.orchestration.contracts import (
        CapacityClass,
        EligibilityStage,
        OrchestrationError,
        RouteVerdict,
    )
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover

    # One rated route in the frontier band.  It is METERED; the ladder should not select it.
    snap = _snapshot(
        _record("p/metered-model", ("cc/metered-model",), aa_intelligence=_score(90.0))
    )
    monkeypatch.setattr("verdict.metadata.store.load_store", lambda: snap)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")

    plan_started_routes: list[str] = []

    class Selector:
        """Simulates EligibilityLadder after the METERED exclusion fix.

        select() returns None for the planner role once the band is set,
        reflecting that all frontier candidates were METERED and filtered.
        """

        _band_set: bool = False

        def inventory_route_ids(self) -> list[str]:
            return ["cc/metered-model"]

        def set_planner_band(self, band: object) -> None:
            self._band_set = True

        def select(self, req: object, *, now: object) -> object:
            if self._band_set and getattr(req, "arena_role", None) == "planner":
                # All METERED candidates were filtered: no admissible planner.
                return None, (
                    RouteVerdict(
                        "cc/metered-model",
                        "cc",
                        EligibilityStage.AVAILABLE,
                        EligibilityStage.AVAILABLE,
                        "metered_excluded_by_planner_band",
                        CapacityClass.METERED,
                    ),
                )
            # Should not be reached with arena_role="planner" and band set.
            return RouteVerdict(
                "cc/metered-model",
                "cc",
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.METERED,
            ), ()

        def record_failure(self, route: object, failure: object, *, now: object) -> None:
            pass

    class Events:
        def emit(self, kind: str, **data: object) -> None:
            if kind == "plan_started":
                plan_started_routes.append(str(data.get("route_id", "")))

    with pytest.raises(OrchestrationError, match="none eligible"):
        await plan_with_failover(
            "g",
            repo=tmp_path,
            selector=Selector(),  # type: ignore[arg-type]
            executor=None,  # type: ignore[arg-type]
            classifier=FailureIntelligence(),
            events=Events(),
            now=lambda: NOW,
        )
    # The METERED route must never be dispatched.
    assert plan_started_routes == [], "METERED planner route must not appear in plan_started"


def test_eligibility_ladder_band_re_sort_excludes_metered_candidates(tmp_path: Path) -> None:
    """Item 2 (direct): EligibilityLadder._assess_all filters METERED after band re-sort.

    With arena_role='planner' and a band injected, METERED routes must be absent
    from candidates even when they appear in the band's frontier.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.contracts import CapacityClass, TaskRequirements
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.planner_band import BandComputation

    now = datetime.now(timezone.utc)
    # Minimal inventory: one route that will be classified METERED via positive pricing.
    # Using an OpenRouter route with a paid model so classify_route_capacity → METERED.
    row = {
        "route_id": "openrouter/openai/gpt-4o",
        "model": "openrouter/openai/gpt-4o",
        "max_output_tokens": 32_000,
        "capabilities": {"reasoning": True, "coding": True},
        "pricing": {"input": 5.0, "output": 15.0},  # positive pricing → METERED
    }
    conn = {
        "provider": "openrouter",
        "authType": "apikey",
        "isActive": True,
        "testStatus": "ok",
        "backoffLevel": 0,
        "plan_label": "max",
        "rate_limited_until": None,
        "import_free_only": False,
    }
    ladder = EligibilityLadder(
        inventory_rows=[row],
        connections=[conn],
        probe=lambda rid: type("H", (), {"healthy": True, "category": ""})(),
        state_path=tmp_path / "elig.json",
        max_probes_per_select=0,
        rankings={},
    )
    # Inject a band that includes the route.
    model_key = "openrouter/openai/gpt-4o"
    band = BandComputation(
        frontier_model_keys=frozenset({model_key}),
        fallback_model_keys=frozenset({model_key}),
        cutoff_frontier=0.0,
        cutoff_fallback=0.0,
        rated_models=1,
        fetched_at="2026-11-23T12:00:00Z",
        model_scores={model_key: 90.0},
        model_ranks={model_key: 0},
    )
    ladder.set_planner_band(band)

    requirements = TaskRequirements(
        required_capabilities=frozenset(), arena_role="planner", role_ordering="strongest_first"
    )
    _assessments, candidates = ladder._assess_all(requirements, now)
    # Every METERED candidate must be absent from the candidates list.
    metered_in_candidates = [a for a in candidates if a.capacity == CapacityClass.METERED]
    assert metered_in_candidates == [], (
        f"METERED routes must be excluded from planner candidates; found: "
        f"{[a.route_id for a in metered_in_candidates]}"
    )


@pytest.mark.asyncio
async def test_plan_with_failover_fails_closed_when_metadata_store_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileNotFoundError from load_store fails closed just like any other OSError.

    ADR-051 D2: a missing store means no ratings → no band → no_frontier_or_fallback_planner.
    Before the fix, FileNotFoundError was treated as "not configured; skip gracefully",
    allowing any admitted route to plan.  Now it must fail closed with the named reason.
    """
    from verdict.orchestration.recovery import FailureIntelligence
    from verdict.orchestration.run import plan_with_failover
    from verdict.planner_band import PlannerBandError

    def _raise_fnf() -> None:
        raise FileNotFoundError("/verdict/model-metadata.json: No such file or directory")

    monkeypatch.setattr("verdict.metadata.store.load_store", _raise_fnf)
    monkeypatch.setenv("VERDICT_DECISION_SIGNALS", "off")

    from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict

    class Selector:
        def inventory_route_ids(self) -> list[str]:
            return ["cc/model"]

        def set_planner_band(self, band: object) -> None:
            pass

        def select(self, req: object, *, now: object) -> object:
            return RouteVerdict(
                "cc/model", "cc", EligibilityStage.SELECTED, None, "ok", CapacityClass.SUBSCRIPTION
            ), ()

        def record_failure(self, route: object, failure: object, *, now: object) -> None:
            pass

    class Events:
        def emit(self, kind: str, **data: object) -> None:
            pass

    with pytest.raises(PlannerBandError, match="no_frontier_or_fallback_planner"):
        await plan_with_failover(
            "g",
            repo=tmp_path,
            selector=Selector(),  # type: ignore[arg-type]
            executor=None,  # type: ignore[arg-type]
            classifier=FailureIntelligence(),
            events=Events(),
        )


def test_eligibility_ladder_excludes_metered_planner_without_band(tmp_path: Path) -> None:
    """METERED routes are excluded for arena_role='planner' even when no band is set.

    ADR-051 D5: METERED excluded until CP1.  The exclusion must apply unconditionally
    for planner selection, not only after a band is injected via set_planner_band().
    Before the fix the filter was inside the ``if self._planner_band is not None`` guard.
    """
    from tests.test_orch_eligibility import NOW, conn, make_ladder, row
    from verdict.orchestration.contracts import CapacityClass, TaskRequirements

    # apikey + positive pricing → METERED (see test_one_capacity_classifier).
    metered_conn = {**conn("kc", plan="max"), "authType": "apikey"}
    rows_data = [{**row("kc/model", owned_by="kc"), "capability_tier": 1}]
    ladder, _ = make_ladder(tmp_path, rows_data, [metered_conn])

    # Intentionally do NOT call set_planner_band — the filter must apply regardless.
    requirements = TaskRequirements(
        required_capabilities=frozenset(), arena_role="planner", role_ordering="strongest_first"
    )
    _assessments, candidates = ladder._assess_all(requirements, NOW)
    metered_in_candidates = [a for a in candidates if a.capacity == CapacityClass.METERED]
    assert metered_in_candidates == [], (
        f"METERED routes must be excluded from planner candidates even without a band; "
        f"found: {[a.route_id for a in metered_in_candidates]}"
    )


# ---------------------------------------------------------------- integrate and review are merge-only (#1356)

# The shape both real self-build runs of 2026-11-26 hit (#1354): the integrate node was
# handed authoring work and the files to write.
_INTEGRATE_OWNS_FILES_JSON = """
{"nodes": [
  {"node_id": "impl-a", "objective": "implement a", "kind": "implement",
   "owned_files": ["verdict/a.py"], "verification_command": ["pytest", "-q", "tests/test_a.py"]},
  {"node_id": "deliver_integrate", "objective": "write the end to end test", "kind": "integrate",
   "depends_on": ["impl-a"], "owned_files": ["tests/test_deliver_e2e.py"],
   "verification_command": ["pytest", "-q", "tests/test_deliver_e2e.py"]}
]}
"""


@pytest.mark.parametrize("kind", [NodeKind.INTEGRATE, NodeKind.REVIEW])
def test_merge_only_node_with_owned_files_is_rejected_with_an_actionable_reason(
    kind: NodeKind,
) -> None:
    with pytest.raises(OrchestrationError) as raised:
        WorkNode("m", "merge", kind=kind, owned_files=("tests/test_x.py",))
    message = str(raised.value)
    assert "m:" in message
    assert kind.value in message
    assert "owned_files" in message
    assert "implement node" in message


@pytest.mark.parametrize("kind", [NodeKind.INTEGRATE, NodeKind.REVIEW])
def test_merge_only_node_without_owned_files_is_accepted(kind: NodeKind) -> None:
    node = WorkNode("m", "merge", kind=kind, depends_on=("a",), verification_command=("pytest",))
    assert node.owned_files == ()


def test_a_persisted_graph_with_an_integrate_node_that_owns_files_still_loads() -> None:
    """History stays readable: a graph written before the rule must still verify and resume.

    The retained live proof run docs/proof/dogfood-bod-225-live-2026-09-29 is such a graph; its
    receipt is rebuilt in tests/test_trace_evidence_events.py.
    """
    import json

    raw = json.loads(_INTEGRATE_OWNS_FILES_JSON)
    graph = WorkGraph.from_dict(raw, allow_merge_only_owned_files=True)
    integrate = graph.node("deliver_integrate")
    assert integrate.owned_files == ("tests/test_deliver_e2e.py",)
    reloaded = WorkGraph.from_dict(graph.to_dict(), allow_merge_only_owned_files=True)
    assert reloaded.digest() == graph.digest()
    # Only a reader of a recorded run says so: the default stays strict
    # (tests/test_orch_graph_loader.py).
    with pytest.raises(OrchestrationError, match="must not have owned_files"):
        WorkGraph.from_dict(raw)
    # A new graph never gets this: the node itself still refuses the shape.
    with pytest.raises(OrchestrationError, match="must not have owned_files"):
        WorkNode.from_dict(raw["nodes"][1])


def test_parse_plan_rejects_an_integrate_node_that_owns_files() -> None:
    with pytest.raises(OrchestrationError) as raised:
        parse_plan(_INTEGRATE_OWNS_FILES_JSON, goal="g")
    message = str(raised.value)
    assert "node[1]" in message
    assert "deliver_integrate" in message
    assert "implement node" in message


def test_parse_plan_accepts_a_file_less_integrate_node() -> None:
    graph = parse_plan(_VALID_NODES_JSON, goal="g")
    assert graph.node("int").owned_files == ()


def test_planning_prompt_states_that_integrate_nodes_are_merge_only() -> None:
    prompt = build_planning_prompt("goal", "map", "")
    assert 'kind="integrate" owns no files' in prompt
    assert "owned_files must be []" in prompt
    assert "move that work into an implement node" in prompt


@pytest.mark.asyncio
async def test_frontier_planner_repair_call_receives_the_merge_only_reason(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output=_INTEGRATE_OWNS_FILES_JSON),
            WorkerTerminal(ok=True, output=_VALID_NODES_JSON),
        ]
    )
    graph, terminal = await FrontierPlanner().plan(
        "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
    )
    assert len(executor.calls) == 2
    repair_prompt = executor.calls[1]
    assert "failed validation" in repair_prompt
    assert "deliver_integrate" in repair_prompt
    assert "must not have owned_files" in repair_prompt
    assert "implement node" in repair_prompt
    assert terminal.output == _VALID_NODES_JSON
    assert graph.node("int").owned_files == ()


@pytest.mark.asyncio
async def test_frontier_planner_fails_closed_when_the_repair_keeps_the_rule_broken(
    tmp_path: Path,
) -> None:
    repo = _init_git_repo(tmp_path)
    executor = _ScriptedExecutor(
        [
            WorkerTerminal(ok=True, output=_INTEGRATE_OWNS_FILES_JSON),
            WorkerTerminal(ok=True, output=_INTEGRATE_OWNS_FILES_JSON),
        ]
    )
    with pytest.raises(OrchestrationError, match="after one repair round") as raised:
        await FrontierPlanner().plan(
            "ship it", repo=repo, executor=executor, route_id="cc/claude-sonnet-5"
        )
    assert "must not have owned_files" in str(raised.value)
    assert len(executor.calls) == 2
