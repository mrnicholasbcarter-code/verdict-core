"""Cross-surface free-first tier ordering without provider traffic."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.admission import RuntimeEvidence, admit
from verdict.free_tier_admit import snapshot_from_payloads
from verdict.intelligence import IntelligenceService
from verdict.metadata.records import (
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.model_passports import ModelPassport
from verdict.orchestration.contracts import TaskRequirements, WorkerTerminal, WorkGraph, WorkNode
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.subagent_selection import HealthResult

NOW = datetime.now(timezone.utc)
FREE = "free/route"
SUBSCRIPTION = "sub/route"
FREE_BUDGET = ("free/a", "free/b", "free/c")


def _rows(ids: tuple[str, ...] = (FREE, SUBSCRIPTION)) -> list[dict[str, Any]]:
    return [
        {
            "id": route_id,
            "owned_by": route_id.split("/", 1)[0],
            "context_length": 128_000,
            "capabilities": {"tool_calling": True},
            "pricing": {"input": 0, "output": 0}
            if route_id.startswith("free/")
            else {"input": 1, "output": 1},
        }
        for route_id in ids
    ]


def _connections(ids: tuple[str, ...] = (FREE, SUBSCRIPTION)) -> list[dict[str, Any]]:
    return [
        {
            "provider": route_id.split("/", 1)[0],
            "authType": "apikey" if route_id.startswith("free/") else "oauth",
            "isActive": True,
            "testStatus": "active",
            "plan_label": "free" if route_id.startswith("free/") else "max",
            "import_free_only": route_id.startswith("free/"),
        }
        for route_id in ids
    ]


def _metadata(ids: tuple[str, ...] = (FREE, SUBSCRIPTION)) -> MetadataSnapshot:
    provenance = FieldProvenance(source="fixture", fetched_at=NOW.isoformat())
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at=NOW.isoformat(),
        sources={},
        records=tuple(
            ModelMetadataRecord(
                id=f"model/{route_id.replace('/', '-')}",
                provider=route_id.split("/", 1)[0],
                omniroute_ids=(route_id,),
                caps=CapabilityCaps(
                    tools=ProvenancedField(value=True, provenance=provenance),
                    context=ProvenancedField(value=128_000, provenance=provenance),
                ),
                # The subscription has a better rating; capacity must still win.
                scores=SoftScores(
                    aa_coding=ProvenancedField(
                        value=10.0 if route_id.startswith("free/") else 99.0, provenance=provenance
                    )
                ),
            )
            for route_id in ids
        ),
    )


def _cache(tmp_path: Path, ids: tuple[str, ...] = (FREE, SUBSCRIPTION)) -> HealthCache:
    cache = HealthCache(tmp_path / "health-cache.json")
    for route_id in ids:
        cache.record(
            route_id,
            ProbeResult(CATEGORY_OK, True, True, probe_class="agentic", agentic_ok=True),
            NOW,
        )
    cache.save()
    return cache


def _ladder(tmp_path: Path, ids: tuple[str, ...] = (FREE, SUBSCRIPTION)) -> EligibilityLadder:
    rows = _rows(ids)
    connections = _connections(ids)
    return EligibilityLadder(
        rows,
        connections,
        lambda route_id: HealthResult(True, "healthy"),
        tmp_path / "state.json",
        admitted=admit(rows, connections, RuntimeEvidence(), now=NOW),
        health_cache=_cache(tmp_path, ids),
        metadata=_metadata(ids),
    )


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))
    monkeypatch.setenv("VERDICT_PROVE_AT_REST_STATE", str(tmp_path / "passports.json"))
    monkeypatch.setenv("TMPDIR", "/tmp")


def _patch_live_selector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    from verdict.metadata import store
    from verdict.orchestration import run

    ids = (FREE, SUBSCRIPTION)
    calls: list[str] = []
    monkeypatch.setattr(store, "load_store", lambda *args, **kwargs: _metadata(ids))
    monkeypatch.setattr(run, "fetch_inventory", lambda *args, **kwargs: _rows(ids))
    monkeypatch.setattr(run, "fetch_connections", lambda *args, **kwargs: _connections(ids))
    monkeypatch.setattr(
        "verdict.omniroute_health.load_omniroute_health", lambda *args, **kwargs: RuntimeEvidence()
    )

    def probe_factory(*args: Any, **kwargs: Any) -> Any:
        def probe(candidate: Any) -> HealthResult:
            calls.append(candidate.route_id)
            return HealthResult(True, "healthy")

        return probe

    monkeypatch.setattr("verdict.subagent_selection.openai_health_probe", probe_factory)
    _cache(tmp_path, ids)
    return calls


def _serve_service() -> IntelligenceService:
    ids = (FREE, SUBSCRIPTION)
    snapshot = snapshot_from_payloads(
        catalog={
            "data": [
                {
                    "id": route_id,
                    "owned_by": route_id.split("/", 1)[0],
                    "pricing": {"input": 0, "output": 0}
                    if route_id.startswith("free/")
                    else {"input": 1, "output": 1},
                }
                for route_id in ids
            ]
        },
        free_tier={"perModel": [{"modelId": "route", "provider": "free", "freeType": "keyless"}]},
        providers={
            "connections": [
                {
                    "provider": route_id.split("/", 1)[0],
                    "isActive": True,
                    "testStatus": "active",
                    "authType": "apikey" if route_id.startswith("free/") else "oauth",
                    "providerSpecificData": {
                        "plan": "free" if route_id.startswith("free/") else "max",
                        "importFreeModelsOnly": route_id.startswith("free/"),
                    },
                }
                for route_id in ids
            ]
        },
    )
    passports = {
        route_id: ModelPassport(
            provider=route_id.split("/", 1)[0],
            model_id=route_id,
            auth_state="authorized",
            availability_state="eligible",
            qualified_at=NOW - timedelta(minutes=1),
            last_verified_timestamp=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(minutes=5),
            tool_support=True,
        )
        for route_id in ids
    }
    return IntelligenceService(
        primary_model="fixture/primary",
        providers={},
        profile="production",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        metadata_snapshot=_metadata(ids),
        passports=passports,
        admit_now=NOW,
        require_execution_path_authority=True,
        candidate_top_k=2,
        context_roots=(),
        mcp_root="",
    )


def _offer(route_id: str) -> Any:
    from tests.test_live_candidate_shortlist import _offer as base_offer

    return base_offer(route_id)


def _request() -> Any:
    from tests.test_live_candidate_shortlist import _slice
    from verdict.execution_path import ExecutionPathRequest

    return ExecutionPathRequest(
        task_slice=_slice(),
        trajectory_id="traj-bod142",
        offers=(_offer(FREE), _offer(SUBSCRIPTION)),
        now=NOW,
    )


def _serve_context(request: Any) -> dict[str, Any]:
    return {
        "execution_path_request": request,
        "tools_required": True,
        "task_family": "coding",
        "spend_policy": "paid_allowed",
    }


def test_capable_free_route_wins_choose_route_serve_and_dag_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_gateway_network: None
) -> None:
    """Each production surface picks FREE before a stronger subscription alternative."""
    _isolate(monkeypatch, tmp_path)
    probe_calls = _patch_live_selector(monkeypatch, tmp_path)
    ladder = _ladder(tmp_path / "proof")
    verdicts = ladder.evaluate(TaskRequirements(), now=NOW)
    ranked = sorted(
        (v for v in verdicts if v.rank is not None),
        key=lambda v: v.rank if v.rank is not None else -1,
    )
    assert [v.route_id for v in ranked] == [FREE, SUBSCRIPTION]

    from verdict.actions.extra import _action_choose_live
    from verdict.actions.registry import _action_route

    choose = _action_choose_live(
        task_class="implementation", requires="tools,code", task="fix a Python function"
    )
    route = _action_route(
        task="fix a Python function", criticality="low", context={"tools_required": True}
    )
    assert choose.ok and route.ok
    assert choose.data["selected"]["route_id"] == FREE
    assert route.data["selected"]["route_id"] == FREE
    assert SUBSCRIPTION not in probe_calls

    service = _serve_service()
    request = _request()
    prepared = service._prepare_execution_path_request(
        "fix code", "low", _serve_context(request), request
    )
    assert [entry.route_id for entry in prepared.pool_receipt.shortlist] == [FREE, SUBSCRIPTION]
    decision = asyncio.run(
        service.route("fix code", criticality="low", context=_serve_context(request))
    )
    assert decision.model == "route"
    assert decision.provider == "free"

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
    (repo / "work.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "work.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

    class Events:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def emit(self, event: str, node_id: str = "", **data: Any) -> None:
            self.rows.append({"event": event, "node_id": node_id, **data})

    class Executor:
        gateway_origin = "local:"

        def __init__(self) -> None:
            self.routes: list[str] = []

        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            self.routes.append(route_id)
            (cwd / "work.py").write_text("value = 2\n")
            return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)

    events, executor = Events(), Executor()
    runtime = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run",
        graph=WorkGraph(
            "fix code",
            (
                WorkNode(
                    "fix", "fix work", owned_files=("work.py",), verification_command=("true",)
                ),
            ),
        ),
        selector=_ladder(tmp_path / "dag"),
        executor=executor,
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=lambda node, cwd: "fix work",
        policy=RuntimePolicy(require_review=False, run_repo_gates=False, max_attempts_per_node=3),
        now=lambda: NOW,
    )
    result = asyncio.run(runtime.run())
    assert result.outcome.value == "COMPLETE", result.reason
    assert executor.routes == [FREE]
    assert [row["route_id"] for row in events.rows if row["event"] == "dispatch"] == [FREE]
    assert SUBSCRIPTION not in executor.routes


def test_three_free_routes_consume_dag_attempt_budget_before_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_gateway_network: None
) -> None:
    """A three-attempt DAG budget cannot displace capable FREE capacity with subscription."""
    _isolate(monkeypatch, tmp_path)
    ids = (*FREE_BUDGET, SUBSCRIPTION)
    attempted: list[str] = []

    class Events:
        def emit(self, event: str, node_id: str = "", **data: Any) -> None:
            pass

    class Executor:
        gateway_origin = "local:"

        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            attempted.append(route_id)
            return WorkerTerminal(ok=False, error="temporary", model=route_id)

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=repo, check=True)
    (repo / "work.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "work.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)

    runtime = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run",
        graph=WorkGraph(
            "fix code",
            (
                WorkNode(
                    "fix", "fix work", owned_files=("work.py",), verification_command=("true",)
                ),
            ),
        ),
        selector=_ladder(tmp_path / "dag", ids),
        executor=Executor(),
        classifier=FailureIntelligence(),
        events=Events(),
        prompt_for=lambda node, cwd: "fix work",
        policy=RuntimePolicy(require_review=False, run_repo_gates=False, max_attempts_per_node=3),
        now=lambda: NOW,
    )
    result = asyncio.run(runtime.run())
    assert result.outcome.value == "BLOCKED"
    assert attempted == list(FREE_BUDGET)
    assert SUBSCRIPTION not in attempted
