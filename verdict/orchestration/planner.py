"""Frontier decomposition: goal -> prompt -> WorkGraph, deterministic topology.

Only this module decides *how many* concurrent workers a plan gets and *which*
topology (SOLO / WORKER_CRITIC / PARALLEL_WORK_UNITS) runs it -- the rule is a
pure function of the parsed graph shape, never a model's opinion. The frontier
model only proposes node decomposition; ``choose_topology`` is deterministic
and replayable from the same nodes.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    NodeKind,
    OrchestrationError,
    Topology,
    WorkerExecutor,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

_NODE_FIELDS = (
    "node_id, objective, kind, story, depends_on, owned_files, required_context, "
    "acceptance, verification_command, barrier, risk, required_capabilities, "
    "coding, reasoning, min_context_tokens"
)


# ---------------------------------------------------------------- topology


def _layers(nodes: Sequence[WorkNode]) -> tuple[tuple[str, ...], ...]:
    """Topological layers over ``nodes``, tolerant of dep ids outside the set."""
    known = {n.node_id for n in nodes}
    remaining = {n.node_id: {d for d in n.depends_on if d in known} for n in nodes}
    done: set[str] = set()
    result: list[tuple[str, ...]] = []
    while remaining:
        ready = sorted(i for i, deps in remaining.items() if deps <= done)
        if not ready:
            raise OrchestrationError(f"choose_topology: dependency cycle among {sorted(remaining)}")
        result.append(tuple(ready))
        done.update(ready)
        for i in ready:
            del remaining[i]
    return tuple(result)


def choose_topology(
    nodes: Sequence[WorkNode], *, max_parallel: int = 3, risk_hint: str = "medium"
) -> tuple[Topology, int, tuple[str, ...]]:
    """Deterministic, replayable topology choice. Never asks a model.

    Rules (first match wins):
      1. one node, kind=implement, risk_hint=low       -> SOLO(1)
      2. one node, otherwise                            -> WORKER_CRITIC(1)
      3. >=2 implement nodes AND a layer of width>=2     -> PARALLEL_WORK_UNITS(min(max_parallel, widest))
      4. otherwise (multiple nodes, no parallel layer)   -> WORKER_CRITIC(1)
    """
    if not nodes:
        raise OrchestrationError("choose_topology: nodes must be non-empty")
    if max_parallel < 1:
        raise OrchestrationError("choose_topology: max_parallel must be >= 1")

    implement_nodes = [n for n in nodes if n.kind is NodeKind.IMPLEMENT]
    layers = _layers(nodes)
    widest = max((len(layer) for layer in layers), default=0)
    risks = sorted({n.risk for n in nodes})
    rationale: list[str] = [
        f"nodes={len(nodes)} implement_nodes={len(implement_nodes)} "
        f"risk_hint={risk_hint} risks={risks}",
        f"layers={len(layers)} widest_layer={widest}",
    ]

    if len(nodes) == 1:
        node = nodes[0]
        if node.kind is NodeKind.IMPLEMENT and risk_hint == "low":
            rationale.append(
                "decision=SOLO: single implement node, low risk_hint -> one worker, no critic"
            )
            return Topology.SOLO, 1, tuple(rationale)
        rationale.append(
            f"decision=WORKER_CRITIC: single node (kind={node.kind.value}, "
            f"risk_hint={risk_hint}) -> worker+critic pair"
        )
        return Topology.WORKER_CRITIC, 1, tuple(rationale)

    if len(implement_nodes) >= 2 and widest >= 2:
        chosen = min(max_parallel, widest)
        rationale.append(
            f"decision=PARALLEL_WORK_UNITS: {len(implement_nodes)} implement nodes, "
            f"widest_layer={widest} -> max_parallel=min({max_parallel},{widest})={chosen}"
        )
        return Topology.PARALLEL_WORK_UNITS, chosen, tuple(rationale)

    rationale.append(
        f"decision=WORKER_CRITIC: {len(nodes)} nodes but no layer width>=2 "
        f"(implement_nodes={len(implement_nodes)}, widest_layer={widest}) -> sequential worker+critic"
    )
    return Topology.WORKER_CRITIC, 1, tuple(rationale)


# ---------------------------------------------------------------- planning prompt


def build_planning_prompt(goal: str, repo_map: str, constraints: str) -> str:
    """Instruct a frontier model to emit ONLY ``{"nodes": [...]}`` matching WorkNode."""
    return textwrap.dedent(f"""\
        You are decomposing a goal into a small, bounded DAG of work nodes for
        autonomous coding workers that will each run independently.

        GOAL:
        {goal.strip()}

        REPO MAP:
        {repo_map.strip()}

        CONSTRAINTS:
        {constraints.strip() or "(none)"}

        OUTPUT CONTRACT:
        Respond with ONLY a single JSON object, no prose, no markdown code fences:
        {{"nodes": [...]}}
        Each element of "nodes" is an object with EXACTLY these fields:
        {_NODE_FIELDS}
        - kind is one of: implement, research, integrate, review.
        - depends_on, owned_files, required_context, acceptance, required_capabilities
          are lists of strings.
        - verification_command is a list of strings (an argv list), for example
          ["python3", "-m", "pytest", "-q", "tests/test_x.py"].
        - required_capabilities uses ONLY model features from: tools, reasoning,
          vision, structured_output (Verdict selects the model; describe work
          in "objective"/"acceptance", not here).

        RULES:
        1. Decompose into 2-8 nodes total.
        2. Each node must be a small, bounded unit of work: no node should require
           more than roughly 300 changed lines.
        3. owned_files must be disjoint between any two nodes that can run
           concurrently (neither depends on the other, directly or transitively).
        4. Only set depends_on when a node genuinely consumes another node's
           output; never add a dependency that only reflects a desired ordering.
        5. Every node with kind="implement" MUST have a non-empty, runnable,
           scoped verification_command (for example a pytest invocation limited
           to the files/tests it touches).
        6. Include exactly one final node with kind="integrate" that depends_on
           ALL implement nodes, whose verification_command runs the combined
           test suite.
        7. Use short, stable, snake_case or kebab-case node_id values.

        Return ONLY the JSON object described above.
        """).strip()


# ---------------------------------------------------------------- parsing


def _extract_json_object(text: str) -> str:
    fence = _JSON_FENCE_RE.search(text)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    if start == -1:
        raise OrchestrationError("parse_plan: no JSON object found in planner output")
    depth = 0
    for i, ch in enumerate(candidate[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return candidate[start : i + 1]
    raise OrchestrationError("parse_plan: unterminated JSON object in planner output")


# Model-capability vocabulary Verdict understands. Planner models describe WORK
# ("edit files", "run pytest"); Verdict alone decides MODEL requirements, so free
# text never reaches the eligibility ladder.
KNOWN_MODEL_CAPABILITIES = frozenset({"tools", "reasoning", "vision", "structured_output"})
_CAPABILITY_ALIASES = {
    "tool_calling": "tools",
    "tool_use": "tools",
    "function_calling": "tools",
    "thinking": "reasoning",
    "json": "structured_output",
}
WORKER_MIN_CONTEXT_TOKENS = 32_000  # agent system prompt + tools + hydrated node context


def normalize_node_requirements(item: Mapping[str, Any]) -> dict[str, Any]:
    """Map planner-proposed requirements onto Verdict's capability vocabulary.

    - every code-writing worker needs tool calling (it edits files and runs checks);
    - unknown capability phrases are dropped (recorded in ``acceptance`` context
      is unnecessary: they describe work, not model features);
    - min_context_tokens is floored at what a real agent turn needs.
    """
    data = dict(item)
    raw = data.get("required_capabilities") or []
    caps: set[str] = set()
    if isinstance(raw, list):
        for value in raw:
            key = str(value).strip().lower().replace(" ", "_").replace("-", "_")
            key = _CAPABILITY_ALIASES.get(key, key)
            if key in KNOWN_MODEL_CAPABILITIES:
                caps.add(key)
    kind = str(data.get("kind", "implement"))
    if kind in {"implement", "research", "review"}:
        caps.add("tools")
    if data.get("reasoning") is True:
        caps.add("reasoning")
    data["required_capabilities"] = sorted(caps)
    try:
        requested = int(data.get("min_context_tokens") or 0)
    except (TypeError, ValueError):
        requested = 0
    data["min_context_tokens"] = max(requested, WORKER_MIN_CONTEXT_TOKENS)
    return data


def parse_plan(text: str, goal: str, *, max_parallel: int = 3) -> WorkGraph:
    """Parse frontier output (tolerant of fences/prose) into a validated, topologized WorkGraph."""
    raw = _extract_json_object(text)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OrchestrationError(f"parse_plan: invalid JSON: {exc}") from exc
    if not isinstance(payload, Mapping) or "nodes" not in payload:
        raise OrchestrationError("parse_plan: JSON object must have a 'nodes' key")
    raw_nodes = payload["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise OrchestrationError("parse_plan: 'nodes' must be a non-empty list")

    nodes: list[WorkNode] = []
    for i, item in enumerate(raw_nodes):
        if not isinstance(item, Mapping):
            raise OrchestrationError(f"parse_plan: node[{i}] must be an object")
        try:
            nodes.append(WorkNode.from_dict(normalize_node_requirements(item)))
        except (OrchestrationError, TypeError) as exc:
            raise OrchestrationError(f"parse_plan: node[{i}]: {exc}") from exc

    graph = WorkGraph(goal=goal, nodes=tuple(nodes), max_parallel=max_parallel)
    topology, chosen_max_parallel, rationale = choose_topology(
        graph.nodes, max_parallel=max_parallel
    )
    return WorkGraph(
        goal=goal,
        nodes=graph.nodes,
        topology=topology,
        rationale=rationale,
        max_parallel=chosen_max_parallel,
    )


# ---------------------------------------------------------------- repo map


def repo_map(
    root: Path, *, max_files: int = 400, include: tuple[str, ...] = ("verdict", "tests", "docs")
) -> str:
    """Compact listing of tracked python/markdown files with line counts, for minimal hydration."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrchestrationError(f"repo_map: git ls-files failed: {exc}") from exc

    prefixes = tuple(p.rstrip("/") + "/" for p in include)
    solo = tuple(p.rstrip("/") for p in include)
    tracked: list[str] = []
    for line in proc.stdout.splitlines():
        rel = line.strip()
        if not rel or not (rel.endswith(".py") or rel.endswith(".md")):
            continue
        if not (rel.startswith(prefixes) or rel in solo):
            continue
        tracked.append(rel)
    tracked.sort()
    tracked = tracked[:max_files]

    lines = [f"{len(tracked)} tracked files (py/md) under {', '.join(include)}:"]
    for rel in tracked:
        path = root / rel
        try:
            count = sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            count = 0
        lines.append(f"{rel}\t{count}")
    return "\n".join(lines)


# ---------------------------------------------------------------- frontier planner


class PlanningExecutorError(OrchestrationError):
    """The planning model call itself failed; carries the structured terminal."""

    def __init__(self, message: str, terminal: WorkerTerminal) -> None:
        super().__init__(message)
        self.terminal = terminal


class FrontierPlanner:
    """Runs one frontier planning pass (with one repair round) into a validated WorkGraph."""

    async def plan(
        self,
        goal: str,
        *,
        repo: Path,
        executor: WorkerExecutor,
        route_id: str,
        timeout_seconds: float = 600,
        constraints: str = "",
    ) -> tuple[WorkGraph, WorkerTerminal]:
        repo_map_text = repo_map(repo)
        prompt = build_planning_prompt(goal, repo_map_text, constraints)
        terminal = await executor.run(
            prompt, route_id=route_id, cwd=repo, timeout_seconds=timeout_seconds
        )
        if not terminal.ok:
            raise PlanningExecutorError(
                "FrontierPlanner: planning executor failed: "
                f"{terminal.error or terminal.stop_reason or 'unknown error'}",
                terminal,
            )
        try:
            return parse_plan(terminal.output, goal), terminal
        except OrchestrationError as first_error:
            repair_prompt = (
                f"{prompt}\n\nYour previous response failed validation with this error:\n"
                f"{first_error}\n\nEmit ONLY the corrected JSON object. No prose, no fences."
            )
            repaired = await executor.run(
                repair_prompt, route_id=route_id, cwd=repo, timeout_seconds=timeout_seconds
            )
            if not repaired.ok:
                raise PlanningExecutorError(
                    "FrontierPlanner: repair executor failed: "
                    f"{repaired.error or repaired.stop_reason or 'unknown error'}",
                    repaired,
                ) from first_error
            try:
                graph = parse_plan(repaired.output, goal)
            except OrchestrationError as second_error:
                raise OrchestrationError(
                    f"FrontierPlanner: plan invalid after one repair round: {second_error}"
                ) from second_error
            return graph, repaired


# ---------------------------------------------------------------- node hydration


def hydrate_node_prompt(
    node: WorkNode, *, repo: Path, goal: str, max_context_bytes: int = 60_000
) -> str:
    """Minimal, bounded hydration for one node's worker prompt. Never leaks sibling nodes."""
    lines: list[str] = [
        f"GOAL: {goal.strip()}",
        "",
        f"NODE: {node.node_id}",
        f"OBJECTIVE: {node.objective}",
    ]
    if node.acceptance:
        lines.append("ACCEPTANCE:")
        lines.extend(f"  - {item}" for item in node.acceptance)
    lines.append(f"OWNED_FILES: {', '.join(node.owned_files) or '(none)'}")
    if node.verification_command:
        lines.append(f"VERIFICATION_COMMAND: {' '.join(node.verification_command)}")
    lines.append("")

    budget = max_context_bytes
    context_blocks: list[str] = []
    truncation_notes: list[str] = []
    for rel in node.required_context:
        path = repo / rel
        try:
            data = path.read_bytes()
        except OSError as exc:
            context_blocks.append(f"--- {rel} (unreadable: {exc}) ---")
            continue
        if budget <= 0:
            truncation_notes.append(f"{rel}: omitted (context budget exhausted)")
            continue
        keep = min(len(data), budget)
        chunk = data[:keep]
        text = chunk.decode("utf-8", errors="replace")
        block = f"--- {rel} ---\n{text}"
        if keep < len(data):
            block += f"\n[TRUNCATED: {len(data) - keep} bytes omitted]"
            truncation_notes.append(f"{rel}: truncated to {keep} of {len(data)} bytes")
        context_blocks.append(block)
        budget -= keep

    if context_blocks:
        lines.append("REQUIRED_CONTEXT:")
        lines.extend(context_blocks)
        lines.append("")
    if truncation_notes:
        lines.append("TRUNCATION_NOTES:")
        lines.extend(f"  - {note}" for note in truncation_notes)
        lines.append("")

    lines.append("RULES:")
    lines.append("  - Only edit files listed in OWNED_FILES. Never touch any other file.")
    lines.append("  - Run VERIFICATION_COMMAND yourself and ensure it passes before finishing.")
    lines.append(
        "  - Finish your final message with exactly 'RESULT: DONE' on success, "
        "or 'RESULT: BLOCKED <reason>' if you cannot complete the objective."
    )
    return "\n".join(lines)
