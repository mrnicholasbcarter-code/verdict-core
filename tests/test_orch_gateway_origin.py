"""BOD-433 S03-C: the executor must talk to the gateway the dispatch permit binds.

No network and no provider calls: the DirectGatewayExecutor's HTTP send is
replaced by a spy, so a mismatch is proven by zero sends and zero permits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_dagruntime_dispatch_permits import (
    ORIGIN,
    PermitSpy,
    _authority,
    _cache,
    _id,
    _make,
    _passport,
)
from tests.test_orch_runtime import repo as repo
from verdict.orchestration.contracts import (
    NodeState,
    RunOutcome,
    WorkerTerminal,
    executor_gateway_origin,
    normalize_gateway_origin,
)
from verdict.orchestration.executors import (
    DirectGatewayExecutor,
    FaultInjectingExecutor,
    MixedExecutor,
)


class GatewaySpy(DirectGatewayExecutor):
    """A DirectGatewayExecutor whose send is a spy that writes the owned file."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url=base_url, api_key="fixture-only")
        self.sends: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float, tools: bool = True
    ) -> WorkerTerminal:
        self.sends.append(route_id)
        node = prompt.split(":", 1)[0]
        (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


async def test_other_gateway_origin_is_denied_before_any_permit_or_send(repo: Path) -> None:
    ex = GatewaySpy("http://other")
    rt, ev = _make(repo, ex, ["cc/s", "cx/g"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.sends == []
    assert result.nodes["a"].state is NodeState.BLOCKED
    assert "executor origin mismatch" in result.nodes["a"].reason
    failures = ev.of("failure", "a")
    assert [f["category"] for f in failures] == ["dispatch_denied"]
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is None


@pytest.mark.parametrize(
    "base_url", [ORIGIN, ORIGIN + "/", ORIGIN + "/v1", "HTTP://Gateway.TEST:80/v1/"]
)
async def test_same_gateway_origin_mints_a_permit_and_sends(repo: Path, base_url: str) -> None:
    ex = GatewaySpy(base_url)
    rt, _ = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.sends == ["cc/s"]
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is not None


async def test_executor_without_an_origin_is_accepted(repo: Path) -> None:
    ex = PermitSpy()
    assert executor_gateway_origin(ex, "a") is None
    rt, _ = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cc/s")]


def test_wrappers_report_the_origin_of_the_executor_that_runs_the_node() -> None:
    other = DirectGatewayExecutor(base_url="http://other:9/v1", api_key="fixture-only")
    plain: Any = PermitSpy()
    mixed = MixedExecutor({"b": other}, default=plain)
    assert executor_gateway_origin(mixed, "a") is None
    assert executor_gateway_origin(mixed, "b") == "http://other:9"
    wrapped = FaultInjectingExecutor(mixed, {})
    assert executor_gateway_origin(wrapped, "b") == "http://other:9"
    assert executor_gateway_origin(FaultInjectingExecutor(other, {}), "a") == "http://other:9"


def test_normalize_gateway_origin() -> None:
    assert normalize_gateway_origin("http://gateway.test") == "http://gateway.test:80"
    assert normalize_gateway_origin("HTTP://Gateway.Test:80/v1/") == "http://gateway.test:80"
    assert normalize_gateway_origin("https://g.test/") == "https://g.test:443"
    assert normalize_gateway_origin("http://127.0.0.1:20128") == "http://127.0.0.1:20128"
    assert normalize_gateway_origin("http://g.test:81") != normalize_gateway_origin("http://g.test")
