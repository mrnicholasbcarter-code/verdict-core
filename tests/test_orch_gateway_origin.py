"""BOD-433 S03-C: the executor must talk to the gateway the dispatch permit binds.

No network and no provider calls: the DirectGatewayExecutor's HTTP send is
replaced by a spy, so a mismatch is proven by zero sends and zero permits.
"""

from __future__ import annotations

import asyncio
import json
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
from tests.test_orch_executors import make_fake_prime, success_payload
from tests.test_orch_runtime import Executor
from tests.test_orch_runtime import repo as repo
from verdict.orchestration.contracts import (
    LOCAL_EXECUTOR_ORIGIN,
    NodeState,
    RunOutcome,
    WorkerTerminal,
    executor_gateway_origin,
    gateway_origin_matches,
    normalize_gateway_origin,
)
from verdict.orchestration.executors import (
    DirectGatewayExecutor,
    FaultInjectingExecutor,
    MixedExecutor,
    PrimeHeadlessExecutor,
    ScriptedExecutor,
)
from verdict.orchestration.prime_settings import PRIME_AGENT_DIR_ENV


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


@pytest.mark.parametrize(
    "base_url",
    [
        "http://other",
        "http://gateway.test:0",  # port 0 is not the default port 80
        "http://gateway.test:99999",  # invalid port: fails closed
        "gateway.test",  # no scheme: fails closed
        "http://[::1",  # malformed: urlsplit raises, fails closed
    ],
)
async def test_other_gateway_origin_is_denied_before_any_permit_or_send(
    repo: Path, base_url: str
) -> None:
    ex = GatewaySpy(base_url)
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


async def test_executor_without_an_origin_is_denied_before_any_permit_or_send(repo: Path) -> None:
    ex = Executor({}, delay=0)
    assert executor_gateway_origin(ex, "a", route_id="cc/s", cwd=Path("x")) is None
    rt, ev = _make(repo, ex, ["cc/s", "cx/g"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.calls == []
    assert result.nodes["a"].state is NodeState.BLOCKED
    assert "executor origin unknown" in result.nodes["a"].reason
    assert [f["category"] for f in ev.of("failure", "a")] == ["dispatch_denied"]
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is None


async def test_local_executor_sentinel_is_allowed(repo: Path) -> None:
    ex = PermitSpy()
    assert executor_gateway_origin(ex, "a", route_id="cc/s", cwd=Path("x")) == LOCAL_EXECUTOR_ORIGIN
    rt, _ = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cc/s")]
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is not None


def _write_owned(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
    node = prompt.split(":", 1)[0]
    (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
    return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


async def test_scripted_executor_has_no_origin_unless_declared(repo: Path) -> None:
    calls: list[str] = []

    def script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        calls.append(route_id)
        return _write_owned(prompt, route_id, cwd)

    ex = ScriptedExecutor(script)
    assert executor_gateway_origin(ex, "a", route_id="cc/s", cwd=Path("x")) is None
    rt, _ = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert "executor origin unknown" in result.nodes["a"].reason
    assert calls == []


async def test_scripted_executor_with_the_declared_local_sentinel_is_allowed(repo: Path) -> None:
    ex = ScriptedExecutor(_write_owned, gateway_origin=LOCAL_EXECUTOR_ORIGIN)
    rt, _ = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is not None


def _url_capturing_prime(tmp_path: Path) -> tuple[str, Path]:
    """A fake prime binary that records the baseUrl of the models.json it launched with."""
    capture = tmp_path / "seen.json"
    prime = make_fake_prime(
        tmp_path,
        "from pathlib import Path\n"
        "root = Path(os.environ['PRIME_AGENT_CODING_AGENT_DIR'])\n"
        "url = json.loads((root / 'models.json').read_text())['providers']['omniroute']['baseUrl']\n"
        f"Path({str(capture)!r}).write_text(json.dumps(url))\n"
        f"print({success_payload(model='cc/s')!r})\n",
    )
    return prime, capture


def _checked_prime(tmp_path: Path, cwd: Path) -> tuple[PrimeHeadlessExecutor, Path, Path]:
    """A Prime executor whose origin was checked for ``cwd`` on ``cc/s`` (on ORIGIN)."""
    prime, capture = _url_capturing_prime(tmp_path)
    agent_dir = _agent_dir(tmp_path, _models(ORIGIN + "/v1"))
    ex = PrimeHeadlessExecutor(prime_bin=prime, env={PRIME_AGENT_DIR_ENV: str(agent_dir)})
    assert executor_gateway_origin(ex, "a", route_id="cc/s", cwd=cwd) == ORIGIN + "/v1"
    return ex, agent_dir, capture


def _launch(ex: PrimeHeadlessExecutor, cwd: Path, route_id: str = "cc/s") -> WorkerTerminal:
    cwd.mkdir(parents=True, exist_ok=True)
    return asyncio.run(ex.run("a: go", route_id=route_id, cwd=cwd, timeout_seconds=30))


def test_prime_launch_uses_the_models_json_pinned_at_bind_time(tmp_path: Path) -> None:
    """Changing the operator's models.json after the origin check does not reach the launch."""
    cwd = tmp_path / "run" / "worktrees" / "a-a1"
    ex, agent_dir, capture = _checked_prime(tmp_path, cwd)
    (agent_dir / "models.json").write_text(_models("http://attacker.test/v1"))
    terminal = _launch(ex, cwd)
    assert terminal.ok, terminal.error
    assert json.loads(capture.read_text()) == ORIGIN + "/v1"


@pytest.mark.parametrize("cwd_name", ["elsewhere", "worktrees/a-a2", "worktrees/b-a1"])
def test_prime_launch_off_the_checked_cwd_never_execs_a_changed_origin(
    tmp_path: Path, cwd_name: str
) -> None:
    ex, agent_dir, capture = _checked_prime(tmp_path, tmp_path / "run" / "worktrees" / "a-a1")
    (agent_dir / "models.json").write_text(_models("http://attacker.test/v1"))
    terminal = _launch(ex, tmp_path / "run" / cwd_name)
    assert not terminal.ok
    assert "executor origin changed" in terminal.error
    assert not capture.exists()


def test_prime_launch_off_the_checked_cwd_on_the_same_origin_still_runs(tmp_path: Path) -> None:
    ex, _, capture = _checked_prime(tmp_path, tmp_path / "run" / "worktrees" / "a-a1")
    terminal = _launch(ex, tmp_path / "elsewhere")
    assert terminal.ok, terminal.error
    assert json.loads(capture.read_text()) == ORIGIN + "/v1"


def test_prime_launch_with_a_missing_pinned_dir_never_execs(tmp_path: Path) -> None:
    cwd = tmp_path / "run" / "worktrees" / "a-a1"
    ex, _, capture = _checked_prime(tmp_path, cwd)
    for pinned in ex._pinned.values():
        pinned[0].cleanup()
    terminal = _launch(ex, cwd)
    assert not terminal.ok
    assert "executor origin changed" in terminal.error
    assert not capture.exists()


def test_prime_launch_without_an_origin_check_uses_a_fresh_dir(tmp_path: Path) -> None:
    prime, capture = _url_capturing_prime(tmp_path)
    agent_dir = _agent_dir(tmp_path, _models("http://other.test/v1"))
    ex = PrimeHeadlessExecutor(prime_bin=prime, env={PRIME_AGENT_DIR_ENV: str(agent_dir)})
    terminal = _launch(ex, tmp_path / "planner")
    assert terminal.ok, terminal.error
    assert json.loads(capture.read_text()) == "http://other.test/v1"


class PrimeSpy(PrimeHeadlessExecutor):
    """A PrimeHeadlessExecutor whose launch is a spy; its origin comes from models.json."""

    def __init__(self, agent_dir: Path) -> None:
        super().__init__(env={PRIME_AGENT_DIR_ENV: str(agent_dir)})
        self.sends: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.sends.append(route_id)
        node = prompt.split(":", 1)[0]
        (cwd / f"{node}.txt").write_text(f"{node} by {route_id}\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


def _agent_dir(tmp: Path, models: str | None) -> Path:
    agent_dir = tmp / "prime-agent"
    agent_dir.mkdir()
    if models is not None:
        (agent_dir / "models.json").write_text(models)
    return agent_dir


def _models(base_url: str, provider: str = "omniroute") -> str:
    return json.dumps({"providers": {provider: {"baseUrl": base_url, "api": "openai"}}})


async def test_prime_executor_on_the_bound_gateway_mints_a_permit_and_sends(repo: Path) -> None:
    ex = PrimeSpy(_agent_dir(repo.parent, _models(ORIGIN + "/v1")))
    assert executor_gateway_origin(ex, "a", route_id="cc/s", cwd=Path("x")) == ORIGIN + "/v1"
    rt, _ = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.sends == ["cc/s"]
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is not None


@pytest.mark.parametrize(
    ("models", "reason"),
    [
        (_models("http://127.0.0.1:20128/v1"), "executor origin mismatch"),
        (None, "executor origin unknown"),
        ("{not json", "executor origin unknown"),
        (_models(ORIGIN + "/v1", provider="openai-compatible"), "executor origin unknown"),
        (json.dumps({"providers": {"omniroute": {"baseUrl": 7}}}), "executor origin unknown"),
        (_models(LOCAL_EXECUTOR_ORIGIN), "executor origin unknown"),
    ],
)
async def test_prime_executor_off_the_bound_gateway_is_denied(
    repo: Path, models: str | None, reason: str
) -> None:
    ex = PrimeSpy(_agent_dir(repo.parent, models))
    rt, ev = _make(repo, ex, ["cc/s"], _authority({"cc/s": _passport("cc/s")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.sends == []
    assert reason in result.nodes["a"].reason
    assert [f["category"] for f in ev.of("failure", "a")] == ["dispatch_denied"]
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is None


def test_wrappers_report_the_origin_of_the_executor_that_runs_the_node() -> None:
    other = DirectGatewayExecutor(base_url="http://other:9/v1", api_key="fixture-only")
    plain: Any = Executor({})
    mixed = MixedExecutor({"b": other}, default=plain)
    assert executor_gateway_origin(mixed, "a", route_id="cc/s", cwd=Path("x")) is None
    assert (
        executor_gateway_origin(mixed, "b", route_id="cc/s", cwd=Path("x")) == "http://other:9/v1"
    )
    wrapped = FaultInjectingExecutor(mixed, {})
    assert (
        executor_gateway_origin(wrapped, "b", route_id="cc/s", cwd=Path("x")) == "http://other:9/v1"
    )
    assert (
        executor_gateway_origin(
            FaultInjectingExecutor(other, {}), "a", route_id="cc/s", cwd=Path("x")
        )
        == "http://other:9/v1"
    )


def test_normalize_gateway_origin() -> None:
    assert normalize_gateway_origin("http://gateway.test") == "http://gateway.test:80"
    assert normalize_gateway_origin("HTTP://Gateway.Test:80/v1/") == "http://gateway.test:80"
    assert normalize_gateway_origin("https://g.test/") == "https://g.test:443"
    assert normalize_gateway_origin("http://127.0.0.1:20128") == "http://127.0.0.1:20128"
    assert normalize_gateway_origin("http://g.test:81") != normalize_gateway_origin("http://g.test")
    assert normalize_gateway_origin("http://g.test:0") == "http://g.test:0"


@pytest.mark.parametrize(
    "url", ["http://g.test:99999", "http://g.test:x", "http://[::1", "g.test", "http://", ""]
)
def test_unparseable_origin_matches_nothing(url: str) -> None:
    assert normalize_gateway_origin(url) is None
    assert not gateway_origin_matches(url, url)
