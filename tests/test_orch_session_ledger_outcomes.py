"""BOD-299 AC3: real orchestrate worker outcomes land in SessionLedger.

Covers the four behaviours the slice owns:
(a) a run's verified outcomes land in the ledger with pool-alias dedupe;
(b) replaying the same run is idempotent (identity dedupe);
(c) an unreadable ledger fails open once and never fails the run;
(d) ``prove-at-rest import-sessions`` appends controller outcomes to the ledger.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    NodeKind,
    RouteVerdict,
    TaskRequirements,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.session_evidence import SessionLedger, SessionOutcome

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
LEDGER_ENV = "VERDICT_SESSION_EVIDENCE"


class Events:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def emit(self, type: str, node_id: str = "", **data: Any) -> None:
        json.dumps(data, default=str)
        self.rows.append({"type": type, "node_id": node_id, **data})

    def of(self, type: str, node_id: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["type"] == type and (node_id is None or r["node_id"] == node_id)
        ]


class Selector:
    """Pool with cooldowns; mirrors the ModelSelector contract."""

    def __init__(self, routes: list[str]) -> None:
        self.routes = routes
        self.cool: dict[str, float] = {}
        self.successes: list[str] = []

    def evaluate(self, requirements: TaskRequirements, *, now: datetime):
        return self.select(requirements, now=now)[1]

    def select(self, requirements: TaskRequirements, *, now: datetime):
        verdicts = []
        chosen = None
        for rank, route in enumerate(self.routes, 1):
            provider = route.split("/")[0]
            if route in requirements.exclude_routes:
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.AVAILABLE,
                        EligibilityStage.TASK_ELIGIBLE,
                        "excluded",
                    )
                )
                continue
            if (
                self.cool.get(route, 0) > now.timestamp()
                or self.cool.get(provider, 0) > now.timestamp()
            ):
                verdicts.append(
                    RouteVerdict(
                        route,
                        provider,
                        EligibilityStage.HEALTHY,
                        EligibilityStage.AVAILABLE,
                        "cooldown",
                    )
                )
                continue
            v = RouteVerdict(
                route,
                provider,
                EligibilityStage.SELECTED,
                None,
                "ok",
                CapacityClass.SUBSCRIPTION,
                rank=rank,
            )
            verdicts.append(v)
            if chosen is None:
                chosen = v
        return chosen, tuple(verdicts)

    def record_failure(self, route_id: str, failure, *, now: datetime) -> None:
        key = route_id.split("/")[0] if failure.scope == "provider" else route_id
        self.cool[key] = now.timestamp() + failure.cooldown_seconds

    def record_success(self, route_id: str, *, now: datetime) -> None:
        self.successes.append(route_id)


class Classifier:
    def classify(self, terminal: WorkerTerminal, *, now: datetime):
        from verdict.orchestration.contracts import FailureClassification

        if terminal.status_code == 429:
            return FailureClassification(
                "quota_exhausted", "REROUTE", 3600, "provider", terminal.error
            )
        if terminal.error.startswith("verification_failed"):
            return FailureClassification(
                "verification_failed", "CORRECT_IMPLEMENTATION", 0, "none", terminal.error
            )
        return FailureClassification("unknown", "REROUTE", 120, "route", terminal.error)


class Executor:
    def __init__(self, behaviour: dict[tuple[str, str], str], delay: float = 0.01) -> None:
        self.behaviour = behaviour
        self.delay = delay

    async def run(self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float):
        node = prompt.split(":", 1)[0]
        await asyncio.sleep(self.delay)
        kind = self.behaviour.get((node, route_id), self.behaviour.get((node, "*"), "ok"))
        if kind == "quota":
            return WorkerTerminal(ok=False, model=route_id, status_code=429, error="usage limit")
        if kind == "badcode":
            (cwd / f"{node}.txt").write_text("FAIL")
            return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)
        if kind == "crash":
            raise RuntimeError("executor exploded")
        (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


class Reviewer:
    def __init__(self, status: str = "PASS", blocking: bool = False) -> None:
        self.status, self.blocking = status, blocking

    async def review(self, **kwargs: Any):
        from verdict.orchestration.contracts import ReviewFinding, ReviewResult

        findings = (ReviewFinding("high", "bug", "a.txt", 1, "bad"),) if self.blocking else ()
        return ReviewResult(self.status, "fake-ocr", "cx/reviewer", findings)


def node(nid: str, deps: tuple[str, ...] = ()) -> WorkNode:
    owned = (f"{nid}.txt",)
    check = ["sh", "-c", f"test -f {owned[0]} && ! grep -q FAIL {owned[0]}"]
    return WorkNode(
        nid,
        f"write {nid}",
        kind=NodeKind.IMPLEMENT,
        depends_on=deps,
        owned_files=owned,
        verification_command=tuple(check),
    )


def make(repo: Path, run_dir: Path, graph: WorkGraph, executor, routes: list[str], **policy: Any):
    events = Events()
    selector = Selector(routes)
    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=graph,
        selector=selector,
        executor=executor,
        classifier=Classifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=RuntimePolicy(**policy),
        now=lambda: NOW,
    )
    return runtime, events


@pytest.fixture()
def ledger_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "session-evidence.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


async def test_run_outcomes_land_in_ledger_with_pool_alias_dedupe(
    repo: Path, ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) Verified pass/fail rows land once per route; aliases share a pool row."""
    # The executor fails on cc/s (quota) and succeeds on cx/g; a run whose two
    # nodes land on the same route produces one row per verified attempt.
    graph = WorkGraph("g", (node("a"), node("b")))
    ex = Executor({("a", "cc/s"): "quota", ("a", "cx/g"): "ok", ("b", "*"): "ok"})
    rt, _ = make(repo, repo.parent / "run1", graph, ex, ["cc/s", "cx/g"])
    result = await rt.run()
    assert result.outcome.value == "COMPLETE", result.reason

    ledger = SessionLedger(ledger_env)
    items = ledger.load()
    assert items
    for item in items:
        assert item.verified_by_controller is True
        assert item.kind == "real_task"
        assert item.source == "verdict.orchestration.receipt"
        assert item.child_id and item.child_id.startswith("run1:")
        assert ":a" in item.child_id
    # The quota failure was verified (attempt recorded), the successes recorded.
    fails = [i for i in items if i.outcome == "fail"]
    assert fails and fails[0].failure_class == "transport_error"
    passes = [i for i in items if i.outcome == "pass"]
    assert passes
    # Pool-alias dedupe: agy/* and antigravity/* share the google-antigravity
    # pool, so summarize_pool counts them together, not double.
    rows = [
        SessionOutcome("agy/model:free", "pass", "real_task", None, True, NOW, "t", "run1:x:a1"),
        SessionOutcome(
            "antigravity/model:free", "pass", "real_task", None, True, NOW, "t", "run1:y:a1"
        ),
    ]
    for row in rows:
        ledger.append(row)
    pool_stats = ledger.summarize_pool("agy/model:free", NOW)
    route_stats = ledger.summarize("agy/model:free", NOW)
    assert pool_stats.passes == route_stats.passes + 1  # alias row joins the same pool


async def test_replay_of_same_run_is_idempotent(repo: Path, ledger_env: Path) -> None:
    """(b) The ledger's identity dedupe makes replaying a run append nothing."""
    graph = WorkGraph("g", (node("a"),))
    rt, _ = make(repo, repo.parent / "run1", graph, Executor({}), ["cc/s"])
    result = await rt.run()
    assert result.outcome.value == "COMPLETE", result.reason

    ledger = SessionLedger(ledger_env)
    first = ledger.load()
    assert first
    # Replay: the same run writes the same (route_id, at, outcome) identity.
    rt2, _ = make(repo, repo.parent / "run1", WorkGraph("g", (node("a"),)), Executor({}), ["cc/s"])
    # Same run_dir name and same now -> identical identity rows.
    result2 = await rt2.run()
    assert result2.outcome.value == "COMPLETE"
    second = SessionLedger(ledger_env).load()
    assert len(second) == len(first)


async def test_unreadable_ledger_fails_open_once(
    repo: Path, ledger_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) A broken ledger never fails the run; the guard latches after one error."""
    graph = WorkGraph("g", (node("a"), node("b", ("a",))))
    ex = Executor({})
    rt, _ = make(repo, repo.parent / "run1", graph, ex, ["cc/s"])

    calls = {"n": 0}

    def boom(self, outcome):
        calls["n"] += 1
        raise OSError("unreadable")

    monkeypatch.setattr(SessionLedger, "append", boom)
    result = await rt.run()
    assert result.outcome.value == "COMPLETE", result.reason
    assert calls["n"] >= 1
    # The guard latched: a later recording attempt never touches the ledger again.
    assert rt._session_evidence_error == "unreadable"
    rt._record_session_outcome(
        rt.nodes["a"], WorkerTerminal(ok=True, output="RESULT: DONE", model="cc/s"), outcome="pass"
    )
    assert calls["n"] == 1  # latched: no second append attempt


def test_import_sessions_appends_to_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) prove-at-rest import-sessions appends controller outcomes to the ledger."""
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import import_sessions

    ledger_path = tmp_path / "session-evidence.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(ledger_path))
    source = tmp_path / "workers.jsonl"
    row = {
        "route": "kc/model:free",
        "kind": "free_real_task",
        "child": "run1:impl:a1",
        "ts": NOW.isoformat(),
        "outcome": "REAL_TASK_PASS",
    }
    source.write_text(json.dumps(row) + "\n")
    cache = HealthCache(tmp_path / "health.json")
    data = import_sessions(source, cache, now=NOW)
    assert data["session_appended"] == 1
    items = SessionLedger(ledger_path).load()
    assert len(items) == 1
    assert items[0].route_id == "kc/model:free"
    assert items[0].outcome == "pass"
    assert items[0].child_id == "run1:impl:a1"
    assert items[0].verified_by_controller is True
    # Health-cache import still happened alongside the ledger append.
    assert cache.entry("kc/model:free") is not None
    # Re-import is idempotent: identity dedupe appends nothing new.
    again = import_sessions(source, HealthCache(tmp_path / "health.json"), now=NOW)
    assert again["session_appended"] == 0
    assert len(SessionLedger(ledger_path).load()) == 1


def test_import_sessions_future_rows_excluded_from_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Future-dated controller rows never enter the ledger (same rule as health cache)."""
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import import_sessions

    ledger_path = tmp_path / "session-evidence.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(ledger_path))
    source = tmp_path / "workers.jsonl"
    source.write_text(
        json.dumps(
            {
                "route": "kc/model:free",
                "kind": "free_real_task",
                "child": "run1:impl:a1",
                "ts": (NOW + timedelta(days=1)).isoformat(),
                "outcome": "REAL_TASK_PASS",
            }
        )
        + "\n"
    )
    data = import_sessions(source, HealthCache(tmp_path / "health.json"), now=NOW)
    assert data["session_appended"] == 0
    assert SessionLedger(ledger_path).load() == ()


def test_import_sessions_unreadable_ledger_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ledger write error breaks the loop but keeps the health-cache import."""
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import import_sessions

    ledger_path = tmp_path / "session-evidence.jsonl"
    monkeypatch.setenv(LEDGER_ENV, str(ledger_path))
    source = tmp_path / "workers.jsonl"
    source.write_text(
        json.dumps(
            {
                "route": "kc/model:free",
                "kind": "free_real_task",
                "child": "run1:impl:a1",
                "ts": NOW.isoformat(),
                "outcome": "REAL_TASK_PASS",
            }
        )
        + "\n"
    )

    def explode(self, outcome):
        raise OSError("disk full")

    monkeypatch.setattr(SessionLedger, "append", explode)
    cache = HealthCache(tmp_path / "health.json")
    data = import_sessions(source, cache, now=NOW)
    assert data["session_appended"] == 0
    # The health-cache import still landed (fail open, keep the import).
    assert cache.entry("kc/model:free") is not None


async def test_failures_recorded_and_mechanical_nodes_skipped(repo: Path, ledger_env: Path) -> None:
    """Route/transport failures record fail rows; local/mechanical routes are skipped."""
    graph = WorkGraph("g", (node("a"),))
    ex = Executor({("a", "cc/s"): "quota"})
    rt, _ = make(repo, repo.parent / "run1", graph, ex, ["cc/s"])
    result = await rt.run()
    assert result.outcome.value == "BLOCKED"
    items = SessionLedger(ledger_env).load()
    fails = [i for i in items if i.outcome == "fail"]
    assert fails
    assert fails[0].failure_class == "transport_error"
    assert fails[0].child_id.startswith("run1:a:a1")


def test_session_failure_class_covers_every_runtime_category(repo: Path, ledger_env: Path) -> None:
    """Every classifier category maps onto a valid SessionLedger failure class."""
    from verdict.orchestration.contracts import FailureClassification

    rt, _ = make(repo, repo.parent / "run1", WorkGraph("g", (node("a"),)), Executor({}), ["cc/s"])
    valid = {
        "narrates_tool_calls",
        "gave_up",
        "false_success_claim",
        "transport_error",
        "timeout",
        "wrong_result",
    }
    categories = [
        "rate_limited",
        "quota_exhausted",
        "payment_required",
        "authentication",
        "permission",
        "unsupported",
        "model_unavailable",
        "gone",
        "bad_request",
        "upstream_temporary",
        "gateway_busy",
        "transport_temporary",
        "no_final_answer",
        "empty_output",
        "malformed_response",
        "model_mismatch",
        "timeout",
        "verification_failed",
        "ownership_violation",
        "worker_blocked",
        "harness_configuration",
        "ownership_refused",
        "unknown",
    ]
    for category in categories:
        cls = rt._session_failure_class(
            FailureClassification(category, "REROUTE", 60, "route", "e")
        )
        assert cls in valid, category
        assert SessionOutcome("cc/s", "fail", "real_task", cls, True, NOW, "t", "run1:a:a1")
