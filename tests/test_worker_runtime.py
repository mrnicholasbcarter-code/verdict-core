from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict.subagent_selection import (
    HealthCache,
    HealthResult,
    WorkerTask,
    WorkerTerminal,
    candidates_from_inventory,
    select_worker_model,
)
from verdict.worker_runtime import (
    PrimeFileAdapter,
    RuntimeBudget,
    WorkerController,
    validate_terminal,
)


def row(name: str) -> dict[str, Any]:
    return {
        "id": name,
        "context_length": 200000,
        "capabilities": {"tool_calling": True},
        "pricing": {"input": 0, "output": 0},
    }


OK = WorkerTerminal("done", "WORKER_OK", True, stop_reason="stop")


class Adapter:
    def __init__(
        self, results: list[Any], *, spawn_failure: bool = False, cleanup_failure: bool = False
    ) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []
        self.prompts: list[str] = []
        self.spawn_failure = spawn_failure
        self.cleanup_failure = cleanup_failure
        self.index = -1

    async def spawn(self, prompt: str, *, name: str, model: str) -> dict[str, Any]:
        self.index += 1
        self.calls.append(("spawn", model))
        self.prompts.append(prompt)
        if self.spawn_failure and self.index == 0:
            raise RuntimeError("HTTP 429 antigravity quota exhausted")
        return {"rlm_child_id": name, "model": model}

    async def collect(self, handle: dict[str, Any]) -> Any:
        value = self.results[self.index]
        if isinstance(value, Exception):
            raise value
        return value

    async def delete(self, handle: dict[str, Any]) -> None:
        self.calls.append(("delete", handle["model"]))
        if self.cleanup_failure:
            raise RuntimeError("daemon cleanup unavailable")


def controller(tmp_path: Path, adapter: Adapter, count: int = 2, **kwargs: Any) -> WorkerController:
    rows = [row(f"p{i:03}/free") for i in range(count)]
    return WorkerController(
        WorkerTask(),
        inventory_rows=rows,
        prime_selectors=[f"omniroute/{r['id']}" for r in rows],
        probe=lambda c: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=HealthCache(tmp_path / "health.json"),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("terminal", "classification"),
    [
        (WorkerTerminal("done", None, False), "child_without_reply"),
        (
            WorkerTerminal("done", "early commentary", False, stop_reason="stop"),
            "child_without_reply",
        ),
        (WorkerTerminal("done", "", True, stop_reason="stop"), "empty_output"),
        (WorkerTerminal("done", "  ", True, stop_reason="stop"), "empty_output"),
        (WorkerTerminal("done", "partial", True, stop_reason="length"), "malformed_result"),
        (WorkerTerminal("cancelled"), "child_cancelled"),
        ({"rlm_child_id": "admission"}, "malformed_result"),
        ("WORKER_OK", "malformed_result"),
        (ValueError("invalid response"), "malformed_response"),
        (RuntimeError("worker crashed"), "worker_exception"),
        (OSError("connection reset"), "transport_temporary"),
        (TimeoutError(), "timeout"),
        *[
            (WorkerTerminal("error", error=f"HTTP {code}"), category)
            for code, category in [
                (400, "unsupported"),
                (401, "authentication"),
                (402, "payment_required"),
                (403, "permission"),
                (429, "rate_limited"),
                (500, "upstream_temporary"),
                (502, "upstream_temporary"),
                (503, "upstream_temporary"),
                (599, "upstream_temporary"),
            ]
        ],
    ],
)
async def test_each_failure_replaces_same_task(
    tmp_path: Path, terminal: Any, classification: str
) -> None:
    adapter = Adapter([terminal, OK])
    runtime = controller(tmp_path, adapter)
    outcome = await runtime.run("same task")
    assert outcome.state == "SUCCESS"
    assert outcome.output == "WORKER_OK"
    assert runtime.attempts == [
        ("omniroute/p000/free", classification),
        ("omniroute/p001/free", "completed"),
    ]
    assert adapter.prompts == ["same task", "same task"]
    assert [call[0] for call in adapter.calls] == ["spawn", "delete", "spawn"]
    assert len([e for e in runtime.events if e["event"] == "final"]) == 1
    assert (await runtime.run("must not execute twice")) is outcome


async def test_antigravity_429_after_admission_replaces(tmp_path: Path) -> None:
    adapter = Adapter(
        [WorkerTerminal("error", error="429 [antigravity/model] quota; retry-after=120"), OK]
    )
    rows = [row("antigravity/model"), row("healthy/free")]
    runtime = WorkerController(
        WorkerTask(),
        inventory_rows=rows,
        prime_selectors=[f"omniroute/{r['id']}" for r in rows],
        probe=lambda c: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=HealthCache(tmp_path / "health.json"),
    )
    outcome = await runtime.run("same task")
    assert outcome.state == "SUCCESS"
    assert outcome.candidate and outcome.candidate.route_id == "healthy/free"
    assert runtime.cache._records["provider:antigravity"]["category"] == "rate_limited"
    assert [e["event"] for e in runtime.events].index("admission") < [
        e["event"] for e in runtime.events
    ].index("failure")


async def test_spawn_exception_replaces(tmp_path: Path) -> None:
    adapter = Adapter([None, OK], spawn_failure=True)
    runtime = controller(tmp_path, adapter)
    assert (await runtime.run("task")).state == "SUCCESS"
    assert [call[0] for call in adapter.calls] == ["spawn", "spawn"]


async def test_execution_timeout_reaps_before_replacement(tmp_path: Path) -> None:
    adapter = Adapter([None, OK])
    runtime = controller(tmp_path, adapter, budget=RuntimeBudget(attempt_seconds=0.01))
    outcome = await runtime.run("task")
    assert outcome.state == "SUCCESS"
    assert runtime.attempts[0][1] == "timeout"
    assert [call[0] for call in adapter.calls] == ["spawn", "delete", "spawn"]


async def test_cleanup_failure_is_actionable_not_duplicate_writer(tmp_path: Path) -> None:
    adapter = Adapter([None, OK], cleanup_failure=True)
    runtime = controller(tmp_path, adapter, budget=RuntimeBudget(attempt_seconds=0.01))
    outcome = await runtime.run("task")
    assert outcome.state == "FAIL_CLOSED"
    assert "cleanup_unconfirmed" in outcome.diagnostic
    assert len(adapter.prompts) == 1


async def test_multiple_sequential_failures_and_beyond_old_replacement_budget(
    tmp_path: Path,
) -> None:
    adapter = Adapter([WorkerTerminal("done")] * 14 + [OK])
    runtime = controller(tmp_path, adapter, count=15)
    assert (await runtime.run("task")).state == "SUCCESS"
    assert len(runtime.attempts) == 15


async def test_unique_candidates_exhausted_once(tmp_path: Path) -> None:
    adapter = Adapter([WorkerTerminal("done")] * 4)
    runtime = controller(tmp_path, adapter, count=4)
    outcome = await runtime.run("task")
    assert outcome.state == "FAIL_CLOSED"
    assert "eligible candidates exhausted" in outcome.diagnostic
    assert len(runtime.attempts) == 4
    assert len({model for model, category in runtime.attempts}) == 4
    assert outcome.render().startswith("FAIL_CLOSED")


async def test_total_budget_bounds_probes(tmp_path: Path) -> None:
    import time

    runtime = controller(tmp_path, Adapter([OK]), budget=RuntimeBudget(total_seconds=0.01))
    runtime.probe = lambda c: (time.sleep(0.05), HealthResult(True, "healthy"))[1]
    outcome = await runtime.run("task")
    assert outcome.state == "FAIL_CLOSED"
    assert "budget" in outcome.diagnostic


def test_healthy_after_old_first_twelve_cutoff(tmp_path: Path) -> None:
    rows = [row(f"p{i:03}/free") for i in range(31)]
    calls = []

    def probe(candidate: Any) -> HealthResult:
        calls.append(candidate.selector)
        return HealthResult(candidate.route_id == "p030/free", "probe")

    result = select_worker_model(
        WorkerTask(),
        inventory_rows=rows,
        prime_selectors=[f"omniroute/{r['id']}" for r in rows],
        probe=probe,
        cache=HealthCache(tmp_path / "health.json"),
    )
    assert result.model == "omniroute/p030/free"
    assert len(calls) == 31


def test_controllers_excluded_even_via_omniroute_and_duplicates_removed() -> None:
    rows = [row("cx/gpt-5.6-sol"), row("cx/gpt-6-astra"), row("other/ok"), row("other/ok")]
    candidates = candidates_from_inventory(rows, [f"omniroute/{r['id']}" for r in rows])
    assert len(candidates) == 1
    assert candidates[0].route_id == "other/ok"


class FileAdapter(PrimeFileAdapter):
    def __init__(self, directory: Path, snapshot: dict[str, Any]) -> None:
        super().__init__(directory)
        self.snapshot = snapshot

    async def rpc(self, method: str, **params: Any) -> Any:
        return [self.snapshot]


@pytest.mark.parametrize(
    "last",
    [
        {"role": "assistant", "stopReason": "stop", "content": []},
        {
            "role": "assistant",
            "stopReason": "error",
            "errorMessage": "429 antigravity",
            "content": [],
        },
        {
            "role": "assistant",
            "stopReason": "length",
            "content": [{"type": "text", "text": "partial"}],
        },
    ],
)
async def test_preview_never_overrides_actual_failed_terminal(tmp_path: Path, last: Any) -> None:
    (tmp_path / "child.jsonl").write_text(
        json.dumps({"type": "session"}) + "\n" + json.dumps({"message": last}) + "\n"
    )
    adapter = FileAdapter(
        tmp_path,
        {
            "rlm_child_id": "c",
            "status": "done",
            "settled": True,
            "replied_since_task": True,
            "answer_preview": "WORKER_OK",
        },
    )
    terminal = await adapter.collect({"rlm_child_id": "c", "session_dir": str(tmp_path)})
    with pytest.raises(RuntimeError):
        validate_terminal(terminal)


async def test_full_journal_not_truncated_preview_or_semantic_sidecar(tmp_path: Path) -> None:
    output = "valid output " * 100
    (tmp_path / "child.jsonl").write_text(
        json.dumps({"type": "session"})
        + "\n"
        + json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [{"type": "text", "text": output}],
                }
            }
        )
    )
    (tmp_path / "semantic-edges.jsonl").write_text(json.dumps({"type": "edge"}))
    adapter = FileAdapter(
        tmp_path,
        {
            "rlm_child_id": "c",
            "status": "done",
            "settled": True,
            "replied_since_task": True,
            "answer_preview": "truncated",
        },
    )
    terminal = await adapter.collect({"rlm_child_id": "c", "session_dir": str(tmp_path)})
    assert validate_terminal(terminal) == output.strip()


async def test_running_reply_not_success(tmp_path: Path) -> None:
    adapter = FileAdapter(
        tmp_path,
        {
            "rlm_child_id": "c",
            "status": "running",
            "settled": False,
            "replied_since_task": True,
            "answer_preview": "WORKER_OK",
        },
    )
    assert await adapter.collect({"rlm_child_id": "c"}) is None
