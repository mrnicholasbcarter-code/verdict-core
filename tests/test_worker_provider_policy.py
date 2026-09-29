from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.subagent_selection import (
    HealthCache,
    HealthResult,
    NoHealthyWorkerModelError,
    WorkerTask,
    WorkerTerminal,
    classify_probe_status,
    eligible_worker_candidates,
    select_worker_model,
)
from verdict.worker_runtime import WorkerController

NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def _row(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "context_length": 200_000,
        "capabilities": {"tool_calling": True, "reasoning": True},
        "pricing": {"input": 0.0, "output": 0.0},
    }


def _selector(model_id: str) -> str:
    return f"omniroute/{model_id}"


def test_worker_provider_scope_is_a_hard_gate_before_probe(tmp_path: Path) -> None:
    rows = [_row("antigravity/claude-opus-4-6-thinking"), _row("kr/qwen3-coder-next")]
    visible = [_selector(str(row["id"])) for row in rows]
    task = WorkerTask(
        required_capabilities=frozenset({"tools"}),
        coding=True,
        frontier_worthy=True,
        allowed_route_prefixes=frozenset({"cc/", "kr/"}),
    )

    candidates = eligible_worker_candidates(task, rows, visible)
    assert [candidate.route_id for candidate in candidates] == ["kr/qwen3-coder-next"]

    probed: list[str] = []

    def probe(candidate):
        probed.append(candidate.route_id)
        return classify_probe_status(200)

    selected = select_worker_model(
        task,
        inventory_rows=rows,
        prime_selectors=visible,
        probe=probe,
        cache=HealthCache(tmp_path / "health.json"),
        now=NOW,
    )

    assert selected.candidate.route_id == "kr/qwen3-coder-next"
    assert probed == ["kr/qwen3-coder-next"]


def test_authorized_pool_exhaustion_fails_closed_without_provider_escape(tmp_path: Path) -> None:
    rows = [_row("kr/claude-sonnet-5"), _row("antigravity/claude-opus-4-6-thinking")]
    visible = [_selector(str(row["id"])) for row in rows]
    task = WorkerTask(
        required_capabilities=frozenset({"tools"}),
        frontier_worthy=True,
        allowed_route_prefixes=frozenset({"cc/", "kr/"}),
    )
    probed: list[str] = []

    def probe(candidate):
        probed.append(candidate.route_id)
        if candidate.route_id.startswith("kr/"):
            return classify_probe_status(403)
        return classify_probe_status(200)

    with pytest.raises(NoHealthyWorkerModelError, match="no healthy eligible"):
        select_worker_model(
            task,
            inventory_rows=rows,
            prime_selectors=visible,
            probe=probe,
            cache=HealthCache(tmp_path / "health.json"),
            now=NOW,
        )

    assert probed == ["kr/claude-sonnet-5"]


def test_active_controller_identity_is_never_a_worker_candidate() -> None:
    rows = [_row("cc/claude-fable-5-1"), _row("cc/claude-opus-5-5"), _row("kr/claude-sonnet-5")]
    visible = [_selector(str(row["id"])) for row in rows]
    task = WorkerTask(
        required_capabilities=frozenset({"tools"}),
        frontier_worthy=True,
        allowed_route_prefixes=frozenset({"cc/", "kr/"}),
        excluded_route_ids=frozenset({"cc/claude-fable-5-1"}),
    )

    candidates = eligible_worker_candidates(task, rows, visible)
    route_ids = {candidate.route_id for candidate in candidates}
    assert "cc/claude-fable-5-1" not in route_ids
    assert "cc/claude-opus-5-5" in route_ids
    assert "kr/claude-sonnet-5" in route_ids


def test_provider_scoped_failure_persists_across_sibling_routes(tmp_path: Path) -> None:
    rows = [_row("cc/a"), _row("cc/b")]
    visible = [_selector(str(row["id"])) for row in rows]
    candidates = eligible_worker_candidates(
        WorkerTask(allowed_route_prefixes=frozenset({"cc/"})), rows, visible
    )
    first = next(candidate for candidate in candidates if candidate.route_id == "cc/a")
    cache = HealthCache(tmp_path / "health.json")

    cache.record_failure(first, classify_probe_status(403), now=NOW)

    sibling = cache.usable("omniroute/cc/b", now=NOW)
    assert sibling is not None
    assert sibling.healthy is False
    assert sibling.category == "permission"


class _RuntimeAdapter:
    def __init__(self) -> None:
        self.spawned: list[str] = []
        self.deleted: list[str] = []

    async def spawn(self, prompt: str, *, name: str, model: str) -> dict[str, Any]:
        self.spawned.append(model)
        return {"rlm_child_id": name, "model": model}

    async def collect(self, handle: dict[str, Any]) -> WorkerTerminal:
        model = str(handle["model"])
        if model.startswith("omniroute/cc/"):
            return WorkerTerminal("error", error="HTTP 403 provider usage unavailable")
        return WorkerTerminal("done", "WORKER_OK", True, stop_reason="stop")

    async def delete(self, handle: dict[str, Any]) -> None:
        self.deleted.append(str(handle["model"]))


@pytest.mark.asyncio
async def test_provider_terminal_failure_rolls_to_next_provider_on_first_failure(
    tmp_path: Path,
) -> None:
    # cc routes have non-zero pricing → subscription class (ranks first for this
    # non-frontier task in the free-first order when cc is claude_subscription).
    # kr/c has non-zero pricing → metered class (ranks after claude_subscription).
    # This preserves the test intent: cc/a is tried first, fails (403), cc/b
    # is skipped (same provider), then kr/c is tried and succeeds.
    rows = [_row("cc/a"), _row("cc/b"), {**_row("kr/c"), "pricing": {"input": 1.0, "output": 2.0}}]
    visible = [_selector(str(row["id"])) for row in rows]
    adapter = _RuntimeAdapter()
    runtime = WorkerController(
        WorkerTask(
            required_capabilities=frozenset({"tools"}),
            allowed_route_prefixes=frozenset({"cc/", "kr/"}),
        ),
        inventory_rows=rows,
        prime_selectors=visible,
        probe=lambda candidate: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=HealthCache(tmp_path / "health.json"),
    )

    outcome = await runtime.run("same task")

    assert outcome.state == "SUCCESS"
    assert outcome.candidate is not None
    assert outcome.candidate.route_id == "kr/c"
    assert adapter.spawned == ["omniroute/cc/a", "omniroute/kr/c"]
    assert adapter.deleted == ["omniroute/cc/a"]
    assert any(
        event.get("event") == "failure"
        and event.get("provider") == "cc"
        and event.get("classification") == "permission"
        and event.get("provider_wide") is True
        for event in runtime.events
    )
    assert any(
        event.get("event") == "exclusion"
        and event.get("model") == "omniroute/cc/b"
        and event.get("provider") == "cc"
        and event.get("classification") == "permission"
        and event.get("provider_wide") is True
        for event in runtime.events
    )


def test_worker_capability_floor_drops_unknown_and_insufficient_before_ranking() -> None:
    """BOD-271 on the Prime worker path: the live kr lanes landed on unrecognized
    models (qwen3-coder-next, minimax-m2.5) that rejected Prime requests."""
    rows = [
        _row("kr/qwen3-coder-next"),
        _row("kr/minimax-m2.5"),
        _row("kr/claude-haiku-4.5"),
        _row("kr/claude-sonnet-5"),
        _row("kr/claude-opus-5.5"),
    ]
    visible = [_selector(str(row["id"])) for row in rows]
    coding = WorkerTask(
        required_capabilities=frozenset({"tools"}),
        coding=True,
        allowed_route_prefixes=frozenset({"kr/"}),
        max_capability_tier=1,
    )
    ranked = [c.route_id for c in eligible_worker_candidates(coding, rows, visible)]
    # Unknown and small models are gone; frontier is filtered (not frontier-worthy);
    # the cheapest sufficient known model is first.
    assert ranked == ["kr/claude-sonnet-5"]

    bounded = WorkerTask(allowed_route_prefixes=frozenset({"kr/"}))
    ranked = [c.route_id for c in eligible_worker_candidates(bounded, rows, visible)]
    # Any tier accepted: known small model first, unknown models rank last.
    assert ranked[0] == "kr/claude-haiku-4.5"
    assert set(ranked[-2:]) == {"kr/qwen3-coder-next", "kr/minimax-m2.5"}


def test_worker_task_config_rejects_invalid_capability_floor() -> None:
    from verdict.worker_runtime import worker_task_from_config

    assert worker_task_from_config({"max_capability_tier": 1}).max_capability_tier == 1
    with pytest.raises(ValueError):
        worker_task_from_config({"max_capability_tier": 7})


def test_minimax_is_not_classified_as_a_mini_variant() -> None:
    from verdict.classifier import classify_known

    assert classify_known("kr/minimax-m2.5") is None
    assert classify_known("cx/gpt-5.4-mini") == 3
    assert classify_known("gpt-4o-mini") == 2
