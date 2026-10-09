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
