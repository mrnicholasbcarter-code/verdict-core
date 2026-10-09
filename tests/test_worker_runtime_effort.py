"""BOD-300 effort choice and spawn provenance."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_worker_runtime import OK, Adapter, row
from verdict.orchestration.effort import choose_effort
from verdict.subagent_selection import HealthCache, HealthResult, WorkerTask
from verdict.worker_runtime import PrimeFileAdapter, WorkerController


@pytest.mark.parametrize(
    "kind,expected",
    [("implement", "medium"), ("review", "high"), ("plan", "high"), ("trivial", "low")],
)
def test_task_effort_defaults(kind: str, expected: str) -> None:
    assert choose_effort(kind, {}) == expected


@pytest.mark.parametrize(
    "model,expected",
    [
        ({"thinkingLevelMap": {"high": None, "medium": None, "low": 1024}}, "low"),
        ({"reasoning": False}, None),
        ({"thinkingLevelMap": {}}, "high"),
        ({"thinkingLevelMap": None}, "high"),
        ({"thinkingLevelMap": {"high": "high"}}, "high"),
        ({"thinkingLevelMap": {"high": None, "max": "max"}}, None),
        ({"thinkingLevelMap": {"high": None, "off": 0}}, "off"),
        ({"thinkingLevelMap": {"high": None, "made_up": 1024}}, None),
        ({"thinkingLevelMap": {"max": None}}, "high"),
    ],
)
def test_clamps_only_explicit_unsupported_levels(
    model: dict[str, Any], expected: str | None
) -> None:
    assert choose_effort("review", model) == expected


class EffortAdapter(Adapter):
    def __init__(self, results: list[Any], **kwargs: Any) -> None:
        super().__init__(results, **kwargs)
        self.efforts: list[str | None] = []

    async def spawn(
        self, prompt: str, *, name: str, model: str, thinking: str | None = None
    ) -> dict[str, Any]:
        self.efforts.append(thinking)
        return await super().spawn(prompt, name=name, model=model)


async def test_controller_chooses_effort_and_private_attempt_receipts(tmp_path: Path) -> None:
    rows = [row("p000/free"), row("p001/free")]
    rows[1]["thinkingLevelMap"] = {"high": None, "medium": "medium"}
    adapter = EffortAdapter([None, OK], spawn_failure=True)
    receipts = tmp_path / "spawn-receipts.jsonl"
    receipts.touch(mode=0o666)
    receipts.chmod(0o666)
    runtime = WorkerController(
        WorkerTask(task_kind="review"),
        inventory_rows=rows,
        prime_selectors=[f"omniroute/{r['id']}" for r in rows],
        probe=lambda _: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=HealthCache(tmp_path / "health.json"),
        run_dir=tmp_path,
    )
    assert (await runtime.run("review")).state == "SUCCESS"
    assert adapter.efforts == ["high", "medium"]
    records = [json.loads(line) for line in receipts.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["chosen_model"] == "omniroute/p000/free"
    assert records[0]["executed_model"] is None
    assert records[1]["chosen_model"] == records[1]["executed_model"] == "omniroute/p001/free"
    assert records[1]["chosen_effort"] == "medium"
    assert records[1]["executed_effort"] == "unverified"
    assert records[1]["reason"] == {
        "task_kind": "review",
        "default_effort": "high",
        "clamp": "high->medium",
    }
    assert receipts.stat().st_mode & 0o777 == 0o600
    selections = [e for e in runtime.events if e["event"] == "selection"]
    assert selections[1]["chosen_effort"] == "medium"
    assert selections[1]["reason"] == records[1]["reason"]


async def test_file_adapter_writes_thinking_request(tmp_path: Path) -> None:
    class Capture(PrimeFileAdapter):
        async def rpc(self, method: str, **params: Any) -> Any:
            assert method == "spawn"
            assert params["thinking"] == "high"
            return {"rlm_child_id": "child", "model": params["model"]}

    await Capture(tmp_path).spawn("task", name="child", model="omniroute/kr/model", thinking="high")


@pytest.mark.parametrize("thinking", ["high", None])
async def test_native_bridge_passes_override_only_when_present(
    tmp_path: Path, thinking: str | None
) -> None:
    import runpy
    from dataclasses import dataclass

    module = runpy.run_path(".prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py")

    @dataclass
    class Child:
        rlm_child_id: str = "child"
        name: str = "test"
        model: str = "omniroute/kr/model"

    class Handle:
        running = True

    class RLM:
        async def spawn(self, prompt: str, **kwargs: Any) -> Child:
            assert prompt == "task"
            assert kwargs["model"] == "omniroute/kr/model"
            assert kwargs["name"] == "test"
            assert kwargs.get("thinking") == thinking
            assert ("thinking" in kwargs) == (thinking is not None)
            handle.running = False
            return Child()

        async def delete_subagent(self, child_id: str) -> None:
            assert child_id == "child"

    handle = Handle()
    operation = object.__new__(module["PrimeWorkerOperation"])
    operation.directory = tmp_path
    operation.handle = handle
    operation.rlm = RLM()
    operation.controller_model = "cc/controller"
    operation.allowed_route_prefixes = ("cc/", "kr/")
    (tmp_path / "request.json").write_text(
        json.dumps(
            {
                "id": "r",
                "method": "spawn",
                "prompt": "task",
                "name": "test",
                "model": "omniroute/kr/model",
                "thinking": thinking,
            }
        )
    )
    await operation.serve()
    assert json.loads((tmp_path / "response.json").read_text())["value"]["rlm_child_id"] == "child"


async def test_real_file_rpc_serializes_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.worker_runtime as runtime

    original = runtime.atomic_json

    def respond(path: Path, value: Any) -> None:
        original(path, value)
        request = json.loads(path.read_text())
        assert request["thinking"] == "low"
        original(
            tmp_path / "response.json",
            {"id": request["id"], "value": {"rlm_child_id": "child", "model": request["model"]}},
        )

    monkeypatch.setattr(runtime, "atomic_json", respond)
    result = await PrimeFileAdapter(tmp_path).spawn(
        "task", name="child", model="omniroute/kr/model", thinking="low"
    )
    assert result["rlm_child_id"] == "child"


@pytest.mark.parametrize("reported", ["low", None, "invented"])
async def test_receipt_keeps_model_mismatch_and_only_reported_prime_effort(
    tmp_path: Path, reported: str | None
) -> None:
    class Reported(EffortAdapter):
        async def spawn(
            self, prompt: str, *, name: str, model: str, thinking: str | None = None
        ) -> dict[str, Any]:
            return {"rlm_child_id": name, "model": "omniroute/other/model", "thinking": reported}

    runtime = WorkerController(
        WorkerTask(),
        inventory_rows=[row("p000/free")],
        prime_selectors=["omniroute/p000/free"],
        probe=lambda _: HealthResult(True, "healthy"),
        adapter=Reported([OK]),
        cache=HealthCache(tmp_path / "health.json"),
        run_dir=tmp_path,
    )
    assert (await runtime.run("implement")).state == "FAIL_CLOSED"
    receipt = json.loads((tmp_path / "spawn-receipts.jsonl").read_text())
    assert receipt["chosen_model"] == "omniroute/p000/free"
    assert receipt["executed_model"] == "omniroute/other/model"
    assert receipt["executed_effort"] == (reported if reported == "low" else "unverified")
    assert (tmp_path / "spawn-receipts.jsonl").stat().st_mode & 0o777 == 0o600


async def test_callback_adapter_accepts_effort_but_does_not_claim_it() -> None:
    from verdict.worker_runtime import CallbackAdapter

    async def execute(model: str) -> Any:
        return OK

    handle = await CallbackAdapter(execute).spawn("task", name="c", model="m", thinking="medium")
    assert handle == {"rlm_child_id": "c", "model": "m"}
