from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.subagent_selection import (
    CONTROLLER_MODEL,
    HealthCache,
    HealthResult,
    NoHealthyWorkerModelError,
    WorkerTask,
    WorkerTerminal,
    candidates_from_inventory,
    classify_probe_status,
    classify_worker_failure,
    execute_with_worker_failover,
    openai_health_probe,
    select_worker_model,
)

NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _row(
    model_id: str,
    *,
    cost: float = 0,
    context: int = 200_000,
    tools: bool = True,
    reasoning: bool = True,
):
    return {
        "id": model_id,
        "context_length": context,
        "capabilities": {"tool_calling": tools, "reasoning": reasoning},
        "pricing": {"input": cost, "output": cost},
    }


def _selector(model_id: str) -> str:
    return f"omniroute/{model_id}"


def _cache(tmp_path: Path) -> HealthCache:
    return HealthCache(tmp_path / "health.json", healthy_ttl_seconds=300)


def test_inventory_and_free_ranking_are_not_availability_proof(tmp_path: Path) -> None:
    """The advertised free winner must lose when its inference probe fails."""
    rows = [_row("kc/free-coder:free"), _row("paid/coder", cost=0.2)]
    visible = [_selector(row["id"]) for row in rows]
    calls: list[str] = []

    def probe(candidate):
        calls.append(candidate.selector)
        if candidate.is_free:
            return classify_probe_status(429, retry_after_seconds=90)
        return classify_probe_status(200)

    selected = select_worker_model(
        WorkerTask(required_capabilities=frozenset({"tools"}), coding=True),
        inventory_rows=rows,
        prime_selectors=visible,
        probe=probe,
        cache=_cache(tmp_path),
        now=NOW,
    )

    assert selected.model == "omniroute/paid/coder"
    assert calls == ["omniroute/kc/free-coder:free", "omniroute/paid/coder"]
    assert selected.checked[0][1] == "rate_limited"


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (400, "unsupported"),
        (401, "authentication"),
        (402, "payment_required"),
        (403, "permission"),
        (429, "rate_limited"),
        (500, "upstream_temporary"),
        (503, "upstream_temporary"),
    ],
)
def test_exact_observed_http_failures_are_unhealthy(status: int, category: str) -> None:
    result = classify_probe_status(status)
    assert result == HealthResult(False, category, status)


def test_timeout_is_temporary_unhealthy() -> None:
    assert classify_probe_status(None, timed_out=True).category == "timeout"
    assert classify_probe_status(None, timed_out=True).healthy is False


def test_failed_probe_is_cached_and_not_retried_before_cooldown(tmp_path: Path) -> None:
    rows = [_row("kc/free:free"), _row("paid/ok", cost=0.1)]
    visible = [_selector(row["id"]) for row in rows]
    calls: list[str] = []

    def probe(candidate):
        calls.append(candidate.selector)
        return classify_probe_status(503 if "free" in candidate.route_id else 200)

    cache = _cache(tmp_path)
    task = WorkerTask(required_capabilities=frozenset({"tools"}))
    first = select_worker_model(
        task, inventory_rows=rows, prime_selectors=visible, probe=probe, cache=cache, now=NOW
    )
    second = select_worker_model(
        task,
        inventory_rows=rows,
        prime_selectors=visible,
        probe=probe,
        cache=cache,
        now=NOW + timedelta(seconds=30),
    )

    assert first.model == second.model == "omniroute/paid/ok"
    assert calls == ["omniroute/kc/free:free", "omniroute/paid/ok"]


def test_prime_visibility_is_separate_from_omniroute_inventory(tmp_path: Path) -> None:
    rows = [_row("kc/inventory-only:free"), _row("paid/visible", cost=0.1)]
    selected = select_worker_model(
        WorkerTask(required_capabilities=frozenset({"tools"})),
        inventory_rows=rows,
        prime_selectors=["omniroute/paid/visible"],
        probe=lambda candidate: classify_probe_status(200),
        cache=_cache(tmp_path),
        now=NOW,
    )
    assert selected.model == "omniroute/paid/visible"


def test_capability_context_and_coding_suitability_are_hard_or_ranked(tmp_path: Path) -> None:
    rows = [
        _row("free/chat:free", context=20_000),
        _row("free/general:free", context=200_000),
        _row("free/qwen-coder:free", context=200_000),
        _row("free/no-tools:free", context=200_000, tools=False),
    ]
    selected = select_worker_model(
        WorkerTask(
            required_capabilities=frozenset({"tools"}), min_context_tokens=100_000, coding=True
        ),
        inventory_rows=rows,
        prime_selectors=[_selector(row["id"]) for row in rows],
        probe=lambda candidate: classify_probe_status(200),
        cache=_cache(tmp_path),
        now=NOW,
    )
    assert selected.model == "omniroute/free/qwen-coder:free"


def test_frontier_is_reserved_unless_task_is_worthy_or_protected(tmp_path: Path) -> None:
    rows = [_row("premium/gpt-5.6-coder-frontier", cost=0.01), _row("paid/coder", cost=0.2)]
    visible = [_selector(row["id"]) for row in rows]

    ordinary = select_worker_model(
        WorkerTask(required_capabilities=frozenset({"tools"}), coding=True),
        inventory_rows=rows,
        prime_selectors=visible,
        probe=lambda candidate: classify_probe_status(200),
        cache=_cache(tmp_path),
        now=NOW,
    )
    worthy = select_worker_model(
        WorkerTask(required_capabilities=frozenset({"tools"}), coding=True, frontier_worthy=True),
        inventory_rows=rows,
        prime_selectors=visible,
        probe=lambda candidate: classify_probe_status(200),
        cache=_cache(tmp_path / "worthy"),
        now=NOW,
    )
    assert ordinary.model == "omniroute/paid/coder"
    assert worthy.model == "omniroute/premium/gpt-5.6-coder-frontier"


def test_controller_and_opaque_routes_are_never_worker_candidates() -> None:
    rows = [_row("auto/best-free"), _row("worker/ok")]
    candidates = candidates_from_inventory(
        rows, ["omniroute/auto/best-free", "omniroute/worker/ok", CONTROLLER_MODEL]
    )
    assert [candidate.selector for candidate in candidates] == ["omniroute/worker/ok"]
    assert all(candidate.selector != CONTROLLER_MODEL for candidate in candidates)


def test_no_healthy_candidate_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(NoHealthyWorkerModelError, match="no healthy eligible"):
        select_worker_model(
            WorkerTask(required_capabilities=frozenset({"tools"})),
            inventory_rows=[_row("free/dead:free")],
            prime_selectors=["omniroute/free/dead:free"],
            probe=lambda candidate: classify_probe_status(401),
            cache=_cache(tmp_path),
            now=NOW,
        )


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


def test_probe_requires_real_inference_output_not_only_http_200() -> None:
    candidate = candidates_from_inventory([_row("worker/visible")], ["omniroute/worker/visible"])[0]
    invalid = openai_health_probe(
        opener=lambda request, timeout: _Response(200, b'{"object":"list"}')
    )(candidate)
    valid = openai_health_probe(
        opener=lambda request, timeout: _Response(
            200, b'{"choices":[{"message":{"role":"assistant","content":"OK"}}]}'
        )
    )(candidate)

    assert invalid == HealthResult(False, "malformed_response", 200)
    assert valid == HealthResult(True, "healthy", 200)


class _RuntimeHTTPError(RuntimeError):
    def __init__(self, status_code: int, headers: dict[str, str]) -> None:
        super().__init__(f"worker failed with HTTP {status_code}")
        self.status_code = status_code
        self.headers = headers


@pytest.mark.asyncio
async def test_antigravity_429_worker_escape_is_isolated_and_next_worker_runs(
    tmp_path: Path,
) -> None:
    """The exact escaped runtime 429 must stay in the worker replacement loop."""
    rows = [
        _row("antigravity/claude-opus-4-6-thinking"),
        _row("kc/qwen/qwen3.8-27b:free", cost=0.1),
    ]
    visible = [_selector(row["id"]) for row in rows]
    launched: list[str] = []
    cache = _cache(tmp_path)

    async def execute(model: str):
        launched.append(model)
        if model == "omniroute/antigravity/claude-opus-4-6-thinking":
            raise _RuntimeHTTPError(429, {"Retry-After": "120"})
        return WorkerTerminal("done", "completed by healthy replacement", True, stop_reason="stop")

    result = await execute_with_worker_failover(
        WorkerTask(required_capabilities=frozenset({"tools"}), frontier_worthy=True),
        inventory_rows=rows,
        prime_selectors=visible,
        probe=lambda candidate: classify_probe_status(200),
        execute=execute,
        cache=cache,
        now=lambda: NOW,
        max_replacements=2,
    )

    assert result.value == "completed by healthy replacement"
    assert result.candidate.selector == "omniroute/kc/qwen/qwen3.8-27b:free"
    assert launched == [
        "omniroute/antigravity/claude-opus-4-6-thinking",
        "omniroute/kc/qwen/qwen3.8-27b:free",
    ]
    assert result.attempts[0] == ("omniroute/antigravity/claude-opus-4-6-thinking", "rate_limited")
    candidate_record = cache._records["omniroute/antigravity/claude-opus-4-6-thinking"]
    provider_record = cache._records["provider:antigravity"]
    assert candidate_record["expires_at"] == provider_record["expires_at"]
    assert datetime.fromisoformat(candidate_record["expires_at"].replace("Z", "+00:00")) == (
        NOW + timedelta(seconds=120)
    )


@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (_RuntimeHTTPError(400, {}), "unsupported"),
        (_RuntimeHTTPError(401, {}), "authentication"),
        (_RuntimeHTTPError(402, {}), "payment_required"),
        (_RuntimeHTTPError(403, {}), "permission"),
        (_RuntimeHTTPError(503, {}), "upstream_temporary"),
        (TimeoutError(), "timeout"),
        (ValueError("malformed response"), "malformed_response"),
    ],
)
def test_runtime_failures_use_existing_health_policy(failure: BaseException, category: str) -> None:
    assert classify_worker_failure(failure, now=NOW).category == category


def test_runtime_429_parses_message_reset_epoch_milliseconds() -> None:
    reset_ms = int((NOW + timedelta(seconds=45)).timestamp() * 1000)
    failure = RuntimeError(f'antigravity worker failed: 429; x-ratelimit-reset="{reset_ms}"')
    result = classify_worker_failure(failure, now=NOW)
    assert result.category == "rate_limited"
    assert result.retry_after_seconds == pytest.approx(45)


@pytest.mark.asyncio
async def test_parent_fails_only_after_bounded_worker_replacements(tmp_path: Path) -> None:
    rows = [_row("one/free:free"), _row("two/free:free"), _row("three/free:free")]
    launched: list[str] = []

    async def execute(model: str):
        launched.append(model)
        raise _RuntimeHTTPError(503, {})

    with pytest.raises(NoHealthyWorkerModelError, match="replacement budget exhausted"):
        await execute_with_worker_failover(
            WorkerTask(required_capabilities=frozenset({"tools"})),
            inventory_rows=rows,
            prime_selectors=[_selector(row["id"]) for row in rows],
            probe=lambda candidate: classify_probe_status(200),
            execute=execute,
            cache=_cache(tmp_path),
            now=lambda: NOW,
            max_replacements=1,
        )

    assert len(launched) == 2
    assert CONTROLLER_MODEL not in launched
