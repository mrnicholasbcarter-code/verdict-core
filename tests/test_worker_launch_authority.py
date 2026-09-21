from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from verdict.autodev_run import AutodevError, run_autodev, run_packet_autodev
from verdict.decomposer import Decomposer, DecompositionConfig
from verdict.execution_path import ExecutionPathRequest, optimize_execution_path
from verdict.patch_executor import PatchExecutor, PatchExecutorConfig
from verdict.receipt_store import ReceiptStore
from verdict.runtime_certification import CertificationState
from verdict.session_economics import ConcreteRoute


def _decision(route: ConcreteRoute, *, now: Any = None, trajectory_id: str = "traj-104"):
    # Local proof-only fixture reuses the canonical BOD-104 contracts and optimizer.
    from tests.test_execution_path import NOW, _budget, _cost, _offer, _plan, _slice

    plan = _plan(candidate_id=route.route_id, intrinsic=True)
    expected = _cost(
        f"direct_cheap:{route.route_id}",
        assistance=plan.assistance_cost,
        execution_tokens=100,
        is_free=True,
    )
    offer = _offer(
        strategy="direct_cheap",
        route=route,
        plan=plan,
        expected=expected,
        budget=_budget(candidate_id=route.route_id),
        cert_state=CertificationState.READY,
        is_cheap=True,
    )
    return optimize_execution_path(
        ExecutionPathRequest(
            task_slice=_slice(),
            trajectory_id=trajectory_id,
            offers=(offer,),
            now=NOW if now is None else now,
        )
    )


def _repo(path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=path,
        check=True,
    )
    return path


def test_autodev_refuses_before_decomposition_without_execution_path_decision(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    invoked: list[str] = []
    decomposer = Decomposer(
        DecompositionConfig(model="provider/model"),
        transport=lambda *args: invoked.append("called"),
    )
    executor = PatchExecutor(
        repo,
        PatchExecutorConfig(model="provider/model"),
        transport=lambda *args: invoked.append("called"),
    )
    with pytest.raises(AutodevError, match="missing ExecutionPathDecision"):
        run_autodev("implement", repo, decomposer=decomposer, executor=executor)
    assert invoked == []


def test_autodev_rejects_wrong_executor_model_before_model_call(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    route = ConcreteRoute("candidate", "gateway-a", "provider-a", "provider/model", "pool", 1, True)
    decision = _decision(route)
    invoked: list[str] = []
    with pytest.raises(AutodevError, match="does not match"):
        run_autodev(
            "implement",
            repo,
            execution_path_decision=decision,
            executor_model="other/model",
            base_url="gateway-a",
            decomposer=Decomposer(
                DecompositionConfig(model="provider/model"),
                transport=lambda *args: invoked.append("called"),
            ),
            executor=PatchExecutor(
                repo,
                PatchExecutorConfig(model="provider/model"),
                transport=lambda *args: invoked.append("called"),
            ),
        )
    assert invoked == []


def test_packet_autodev_refuses_missing_decision_before_executor_factory(tmp_path: Path) -> None:
    called: list[bool] = []
    with pytest.raises(AutodevError, match="missing ExecutionPathDecision"):
        run_packet_autodev(
            None, tmp_path, admitted_route={}, executor_factory=lambda **kw: called.append(True)
        )
    assert not called


def test_autodev_launch_uses_decision_model_and_records_authority(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    route = ConcreteRoute(
        "candidate", "http://gateway.test/v1", "provider-a", "provider/model", "pool", 1, True
    )
    decision = _decision(route)
    plan = '[{"unit_id":"change","objective":"change x","owned_files":["a.py"],"verification_command":["python3","-c","pass"],"context":""}]'
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    called: list[str] = []

    def decompose(model: str, payload: dict[str, Any], timeout: float) -> Any:
        called.append(model)
        return {"status_code": 200, "body": {"choices": [{"message": {"content": plan}}]}}

    def execute(model: str, payload: dict[str, Any], timeout: float) -> Any:
        called.append(model)
        return {
            "status_code": 200,
            "body": {"model": model, "choices": [{"message": {"content": diff}}]},
        }

    store = ReceiptStore(":memory:")
    report = run_autodev(
        "change x",
        repo,
        execution_path_decision=decision,
        decomposer=Decomposer(
            DecompositionConfig(model="provider/model", base_url=route.gateway), transport=decompose
        ),
        executor=PatchExecutor(
            repo,
            PatchExecutorConfig(model="provider/model", base_url=route.gateway),
            transport=execute,
        ),
        base_url=route.gateway,
        store=store,
        mechanical=False,
        runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    assert called == ["provider/model", "provider/model"]
    assert report.executor_model == "provider/model"
    assert (repo / "a.py").read_text(encoding="utf-8") == "x = 2\n"
    payload = store.query_receipts(scope="autodev")
    assert payload[0].payload["execution_path_decision_digest"] == decision.decision_digest
    assert (
        payload[0].payload["strategy_authority"] == "verdict.execution_path.optimize_execution_path"
    )


@pytest.mark.parametrize(
    "override, message",
    [
        ({"requested_identity": "other/model"}, "model"),
        ({"actual_identity": "other/model"}, "actual_identity"),
        ({"provider": "provider-b"}, "provider"),
        ({"gateway": "http://other.test/v1"}, "gateway"),
        ({"base_url": "http://other.test/v1"}, "endpoint"),
    ],
)
def test_packet_route_must_match_authoritative_decision(
    tmp_path: Path, override: dict[str, str], message: str
) -> None:
    route = ConcreteRoute(
        "candidate", "http://gateway.test/v1", "provider-a", "provider/model", "pool", 1, True
    )
    admitted = {
        "requested_identity": route.model,
        "model": route.model,
        "provider": route.provider,
        "gateway": route.gateway,
        "base_url": route.gateway,
    }
    admitted.update(override)
    invoked: list[bool] = []
    with pytest.raises(AutodevError, match=message):
        run_packet_autodev(
            None,
            tmp_path,
            admitted_route=admitted,
            execution_path_decision=_decision(route),
            executor_factory=lambda **kwargs: invoked.append(True),
        )
    assert invoked == []


@pytest.mark.parametrize("alias", [None, ""])
def test_packet_route_cannot_mask_conflicting_served_identity(
    tmp_path: Path, alias: str | None
) -> None:
    route = ConcreteRoute(
        "candidate", "http://gateway.test/v1", "provider-a", "provider/model", "pool", 1, True
    )
    admitted: dict[str, Any] = {
        "model": route.model,
        "provider": route.provider,
        "gateway": route.gateway,
        "base_url": route.gateway,
        "actual_identity": "other/model",
    }
    if alias is not None:
        admitted["requested_identity"] = alias
    invoked: list[bool] = []
    with pytest.raises(AutodevError, match="actual_identity"):
        run_packet_autodev(
            None,
            tmp_path,
            admitted_route=admitted,
            execution_path_decision=_decision(route),
            executor_factory=lambda **kwargs: invoked.append(True),
        )
    assert invoked == []


def test_subagent_resolver_uses_only_decision_route() -> None:
    from verdict.subagent_resolver import resolve_subagent_model

    missing = resolve_subagent_model("worker")
    assert missing is not None
    assert "missing ExecutionPathDecision" in missing["error"]

    route = ConcreteRoute("candidate", "gateway-a", "provider-a", "provider/model", "pool", 1, True)
    resolved = resolve_subagent_model("worker", execution_path_decision=_decision(route))
    assert resolved["model_id"] == "provider/model"
    assert resolved["provider"] == "provider-a"
    assert resolved["gateway"] == "gateway-a"
    assert resolved["route_id"] == "candidate"
    assert resolved["capability_tier"] == 1
    assert resolved["selected_strategy"] == "direct_cheap"
    assert resolved["strategy_authority"] == "verdict.execution_path.optimize_execution_path"
