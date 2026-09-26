from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.subagent_selection import (
    HealthCache,
    NoHealthyWorkerModelError,
    WorkerTask,
    classify_probe_status,
    eligible_worker_candidates,
    select_worker_model,
)

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
    rows = [
        _row("antigravity/claude-opus-4-6-thinking"),
        _row("kr/qwen3-coder-next"),
    ]
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
    rows = [
        _row("kr/claude-sonnet-5"),
        _row("antigravity/claude-opus-4-6-thinking"),
    ]
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
    rows = [
        _row("cc/claude-fable-5-1"),
        _row("cc/claude-opus-5-5"),
        _row("kr/claude-sonnet-5"),
    ]
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
