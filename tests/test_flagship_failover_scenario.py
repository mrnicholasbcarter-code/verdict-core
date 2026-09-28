"""BOD-279 prep: flagship failover scenario through the REAL run loop.

Runs ``run_golden_path`` OFFLINE with:
- Pre-built WorkGraph (plan → 2 implement nodes → integrate)
- ScriptedExecutor producing real file edits in a tmp git repo
- FaultInjectingExecutor injecting ONE rate_limit (429) on node-1 attempt 1
- Real EligibilityLadder over a static route inventory (no OmniRoute)
- Real FailureIntelligence classifier
- Real OpenCodeReviewer with a scripted OCR CLI runner

All assertions read from events.jsonl and the receipt — never from test-side state.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import FakeProbe, conn
from tests.test_orch_eligibility import row as inv_row
from verdict.orchestration.contracts import (
    NodeKind,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.executors import FaultInjectingExecutor, ScriptedExecutor
from verdict.orchestration.receipt import verify_run_receipt
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.review import OcrRun, OpenCodeReviewer
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy

# ---------------------------------------------------------------------------
# Fixture inventory: two providers, three routes. Route A will be rate-limited
# on the first attempt of node-1; the ladder must pick route B as replacement.
# Route C exists for the reviewer (different family for independence).
# ---------------------------------------------------------------------------

ROUTE_A = "alpha/model-a"
ROUTE_B = "beta/model-b"
ROUTE_C = "gamma/model-c"  # reviewer-only (different family)

INVENTORY = [
    inv_row(ROUTE_A, owned_by="alpha"),
    inv_row(ROUTE_B, owned_by="beta"),
    inv_row(ROUTE_C, owned_by="gamma"),
]
CONNECTIONS = [conn("alpha"), conn("beta"), conn("gamma")]

# ---------------------------------------------------------------------------
# Clean OCR fixture
# ---------------------------------------------------------------------------

_OCR_CLEAN = (Path(__file__).parent / "fixtures" / "ocr" / "sample-clean.json").read_text()


# ---------------------------------------------------------------------------
# Graph: 2 implement nodes (node-1, node-2) → 1 integrate node
# ---------------------------------------------------------------------------


def _impl_node(nid: str, deps: tuple[str, ...] = ()) -> WorkNode:
    check = ("sh", "-c", f"test -f {nid}.txt && ! grep -q FAIL {nid}.txt")
    return WorkNode(
        node_id=nid,
        objective=f"write {nid}",
        kind=NodeKind.IMPLEMENT,
        depends_on=deps,
        owned_files=(f"{nid}.txt",),
        verification_command=check,
    )


GRAPH = WorkGraph(
    goal="flagship failover test",
    nodes=(
        _impl_node("node-1"),
        _impl_node("node-2"),
        WorkNode(
            node_id="integrate",
            objective="merge and verify",
            kind=NodeKind.INTEGRATE,
            depends_on=("node-1", "node-2"),
            owned_files=(),
            verification_command=("sh", "-c", "test -f node-1.txt && test -f node-2.txt"),
            barrier="integration",
        ),
    ),
    max_parallel=2,
)


# ---------------------------------------------------------------------------
# Scripted worker: writes the owned file on success
# ---------------------------------------------------------------------------


def _worker_script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
    """Deterministic worker: writes <node_id>.txt with the route that produced it."""
    # The prompt starts with "## Node: <node_id>\n" from hydrate_node_prompt
    # but we can infer the node from the worktree directory name (node-X-aN).
    node_id = cwd.name.rsplit("-a", 1)[0]
    (cwd / f"{node_id}.txt").write_text(f"{node_id} implemented by {route_id}\n")
    return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


# ---------------------------------------------------------------------------
# OCR CLI stub (same pattern as test_assignment_failover_e2e)
# ---------------------------------------------------------------------------


class OcrCli:
    """``ocr`` process boundary: clean review, reports the --model it was given."""

    def __init__(self) -> None:
        self.models: list[str] = []

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        argv = list(argv)
        if "--version" in argv:
            return OcrRun(exit_code=0, stdout="open-code-review v1.12.9\n")
        model = argv[argv.index("--model") + 1]
        self.models.append(model)
        payload = json.loads(_OCR_CLEAN)
        payload["manifest"]["execution"]["model"] = model
        out = Path(argv[argv.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload))
        return OcrRun(exit_code=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@test.invalid"],
        ["config", "user.name", "test"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True, capture_output=True)
    return root


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    events_path = run_dir / "events.jsonl"
    assert events_path.exists(), f"events.jsonl missing in {run_dir}"
    return [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]


def _events_of(events: list[dict[str, Any]], etype: str, node_id: str | None = None) -> list[dict[str, Any]]:
    """Return flattened event dicts (type + node_id + data fields merged)."""
    result = []
    for e in events:
        if e.get("type") != etype:
            continue
        if node_id is not None and e.get("node_id") != node_id:
            continue
        flat = {"type": e["type"], "node_id": e.get("node_id", "")}
        flat.update(e.get("data", {}))
        result.append(flat)
    return result


def _run_scenario(tmp_path: Path, *, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Execute the flagship failover scenario and return the run_dir."""
    repo = _init_repo(tmp_path)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    state_path = tmp_path / "ladder-state.json"

    ladder = EligibilityLadder(INVENTORY, CONNECTIONS, FakeProbe(), state_path)

    inner = ScriptedExecutor(_worker_script)
    # Inject ONE rate_limit fault on node-1's first dispatch (dispatch #0 in the run).
    # Key "@node-1" targets the first attempt of node-1 regardless of which route
    # the ladder picks. The queue has one fault, so only the first attempt fails.
    executor = FaultInjectingExecutor(inner, {"@node-1": ["rate_limit"]})

    classifier = FailureIntelligence()

    ocr = OcrCli()
    reviewer = OpenCodeReviewer(
        ladder,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "review",
        runner=ocr,
    )
    monkeypatch.setenv("TEST_OCR_KEY", "test-key")

    result = asyncio.run(
        run_golden_path(
            "flagship failover test",
            repo=repo,
            runs_root=runs_root,
            selector=ladder,
            executor=executor,
            classifier=classifier,
            reviewer=reviewer,
            graph=GRAPH,
            policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
        )
    )

    assert result.outcome == RunOutcome.COMPLETE.value, (
        f"run did not COMPLETE: {result.outcome} — {result.reason}"
    )
    return result.run_dir


# ===========================================================================
# Tests
# ===========================================================================


class TestFlagshipFailoverScenario:
    """End-to-end assertions from events.jsonl and the receipt."""

    @pytest.fixture(autouse=True)
    def run_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._run_dir = _run_scenario(tmp_path, monkeypatch=monkeypatch)
        self._events = _load_events(self._run_dir)
        self._receipt = json.loads((self._run_dir / "receipt.json").read_text())

    # ---- event ordering ---------------------------------------------------

    def test_event_sequence_present_and_ordered(self) -> None:
        """UNDERSTAND/PLAN/HYDRATE/SELECT/EXECUTE events present in order."""
        type_sequence = [e["type"] for e in self._events]

        # Must contain these event types in this order (not necessarily contiguous)
        required_order = [
            "run_started",
            "understand",
            "plan_ready",
            "eligibility",
            "selection",
            "hydrate",
            "terminal",
            "verify",
            "run_finished",
        ]
        indices = []
        for req in required_order:
            idx = next((i for i, t in enumerate(type_sequence) if t == req), None)
            assert idx is not None, f"event type {req!r} not found in event log"
            indices.append(idx)

        # Verify ordering: each required event appears after the previous one
        for i in range(1, len(indices)):
            assert indices[i] > indices[i - 1], (
                f"{required_order[i]!r} (pos {indices[i]}) should come after "
                f"{required_order[i - 1]!r} (pos {indices[i - 1]})"
            )

    # ---- node-1 failover --------------------------------------------------

    def test_node1_first_attempt_fails_rate_limit(self) -> None:
        """Node-1's first attempt fails with category rate_limit."""
        failures = _events_of(self._events, "failure", "node-1")
        assert len(failures) >= 1
        first_failure = failures[0]
        assert first_failure["category"] == "rate_limited"
        assert first_failure.get("fault_injected") is True

    def test_provider_cooldown_recorded(self) -> None:
        """A provider cooldown is recorded after the rate_limit failure."""
        cooldowns = _events_of(self._events, "cooldown", "node-1")
        assert len(cooldowns) >= 1
        cd = cooldowns[0]
        assert cd["scope"] == "provider"
        assert cd["category"] == "rate_limited"

    def test_reassign_picks_different_route(self) -> None:
        """A reassign event picks a DIFFERENT route by the real selector."""
        reassigns = _events_of(self._events, "reassign", "node-1")
        assert len(reassigns) >= 1
        r = reassigns[0]
        assert r["from_route"] != r["to_route"]
        # The replacement must be a real route from our inventory
        assert r["to_route"] in {ROUTE_A, ROUTE_B, ROUTE_C}
        assert r["from_route"] in {ROUTE_A, ROUTE_B, ROUTE_C}

    def test_node1_validated_on_replacement(self) -> None:
        """Node-1 reaches VALIDATED state on the replacement attempt."""
        node_states = _events_of(self._events, "node_state", "node-1")
        validated = [e for e in node_states if e["state"] == "VALIDATED"]
        assert len(validated) == 1
        # The validated route must differ from the first-attempt route
        selections = _events_of(self._events, "selection", "node-1")
        assert len(selections) >= 2
        assert selections[0]["route_id"] != selections[1]["route_id"]
        assert validated[0]["route_id"] == selections[1]["route_id"]

    # ---- parent run continues ---------------------------------------------

    def test_node2_completes_independently(self) -> None:
        """Node-2 completes without failures (no fault injected)."""
        failures = _events_of(self._events, "failure", "node-2")
        assert len(failures) == 0
        validated = [
            e for e in _events_of(self._events, "node_state", "node-2")
            if e["state"] == "VALIDATED"
        ]
        assert len(validated) == 1

    # ---- verification and review ------------------------------------------

    def test_verify_passes(self) -> None:
        """VERIFY passes for both implement nodes."""
        for nid in ("node-1", "node-2"):
            verifies = _events_of(self._events, "verify", nid)
            # Last verify for each node should be ok=True
            ok_verifies = [v for v in verifies if v["ok"]]
            assert len(ok_verifies) >= 1, f"no passing verify for {nid}"

    def test_review_passes(self) -> None:
        """Review event has status PASS."""
        reviews = _events_of(self._events, "review")
        assert len(reviews) == 1
        assert reviews[0]["status"] == "PASS"

    # ---- outcome and receipt ----------------------------------------------

    def test_outcome_complete(self) -> None:
        """Run outcome is COMPLETE."""
        finished = _events_of(self._events, "run_finished")
        assert len(finished) >= 1
        assert finished[-1]["outcome"] == "COMPLETE"

    def test_receipt_verifies_clean(self) -> None:
        """verify_run_receipt returns no problems."""
        problems = verify_run_receipt(self._run_dir)
        assert problems == [], f"receipt problems: {problems}"

    # ---- determinism ------------------------------------------------------


class TestDeterminism:
    """Two runs with the same inputs produce the same per-node event sequences and routes.

    Global event ordering is non-deterministic when nodes run concurrently
    (max_parallel=2), so we compare per-node sequences and route selections.
    """

    def test_two_runs_same_per_node_sequences_and_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()

        run_a = _run_scenario(dir_a, monkeypatch=monkeypatch)
        run_b = _run_scenario(dir_b, monkeypatch=monkeypatch)

        events_a = _load_events(run_a)
        events_b = _load_events(run_b)

        # Same event type multiset (all events present, same counts)
        from collections import Counter
        assert Counter(e["type"] for e in events_a) == Counter(e["type"] for e in events_b), (
            "event type counts differ between runs"
        )

        # Per-node event type sequences are deterministic
        for nid in ("node-1", "node-2", "integrate"):
            seq_a = [e["type"] for e in events_a if e.get("node_id") == nid]
            seq_b = [e["type"] for e in events_b if e.get("node_id") == nid]
            assert seq_a == seq_b, f"per-node event sequence differs for {nid}"

        # Same selected routes for each node
        for nid in ("node-1", "node-2"):
            sel_a = [e["route_id"] for e in _events_of(events_a, "selection", nid)]
            sel_b = [e["route_id"] for e in _events_of(events_b, "selection", nid)]
            assert sel_a == sel_b, f"selected routes differ for {nid}: {sel_a} vs {sel_b}"
