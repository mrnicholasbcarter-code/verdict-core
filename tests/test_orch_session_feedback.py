"""Offline real-terminal feedback fixtures; no providers or user state are touched."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.contracts import AttemptUsage, FailureClassification, WorkerTerminal
from verdict.orchestration.credential_pools import pool_of
from verdict.orchestration.health_cache import (
    LONG_CAP_SECONDS,
    STATE_NEGATIVE,
    STATE_UNPROBED,
    HealthCache,
    HealthCacheError,
    ProbeResult,
)
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.session_feedback import record_worker_failure

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)
ROUTE = "cc/model-a"
PROVIDER = "cc"
POOL = pool_of(ROUTE)


def record(cache: HealthCache, terminal: WorkerTerminal, **kwargs):
    failure = kwargs.pop("failure", FailureIntelligence().classify(terminal, now=NOW))
    return record_worker_failure(
        cache,
        failure=failure,
        terminal=terminal,
        route_id=ROUTE,
        provider_id=PROVIDER,
        pool=kwargs.pop("pool", POOL),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def healthy(cache: HealthCache, route: str, at: datetime) -> None:
    cache.record(route, ProbeResult("ok", True, True, pool=POOL), at)
    cache.save()


def test_persist_negative_with_unknown_turns_and_preserve_success(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    success_at = NOW - timedelta(seconds=30)
    healthy(cache, ROUTE, success_at)
    terminal = WorkerTerminal(
        ok=False, error="upstream failure", status_code=503, executor_kind="live"
    )
    result = record(cache, terminal)
    assert result.recorded and result.cooldown_key is None
    loaded = HealthCache(cache.path)
    entry = loaded.lookup(ROUTE, NOW).entry
    assert entry is not None and result.entry == entry
    assert loaded.lookup(ROUTE, NOW).state == STATE_NEGATIVE
    assert not entry.healthy and not entry.chat_ok and not entry.tool_ok
    assert entry.last_success_at == success_at
    assert entry.failure_scope == "route"
    assert entry.feedback_category == "upstream_temporary"
    assert entry.feedback_source == "worker_terminal"
    payload = json.loads(cache.path.read_text())["routes"][ROUTE]
    assert payload["feedback_turn_count"] is None
    assert payload["feedback_turns_source"] == "unknown"
    assert entry.until == NOW + timedelta(seconds=60)
    assert loaded.lookup(ROUTE, entry.until).state == STATE_UNPROBED
    assert not loaded.lookup(ROUTE, entry.until).healthy


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_credential_failure_is_pool_scoped_and_expires(tmp_path: Path, status: int) -> None:
    cache = HealthCache(tmp_path / "health.json")
    terminal = WorkerTerminal(
        ok=False, error="account failure", status_code=status, executor_kind="live"
    )
    result = record(cache, terminal)
    assert result.recorded and result.cooldown_key == f"pool:{POOL}"
    loaded = HealthCache(cache.path)
    cooldown = loaded.cooldown_for(f"pool:{pool_of('claude/model-b')}", NOW)
    assert cooldown is not None
    assert loaded.cooldown_for("pool:codex", NOW) is None
    assert cooldown.provider_id == PROVIDER and cooldown.pool == POOL
    assert result.entry is not None and result.entry.failure_scope == "pool"
    assert cooldown.until == result.entry.until
    assert loaded.cooldown_for(cooldown.key, cooldown.until) is None
    assert loaded.lookup(ROUTE, cooldown.until).state == STATE_UNPROBED
    if status == 429:
        assert loaded.bucket_remaining("claude", NOW, pool=POOL) == 0
        assert loaded.bucket_remaining("codex", NOW, pool="codex") == 10
        assert loaded.bucket_remaining("claude", cooldown.until, pool=POOL) == 10


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, "weekly quota for this model"),
        (403, "no access to model"),
        (503, "upstream failure"),
        (404, "model not found"),
    ],
)
def test_route_local_failures_do_not_poison_siblings_or_buckets(
    tmp_path: Path, status: int, error: str
) -> None:
    cache = HealthCache(tmp_path / "health.json")
    sibling = "claude/model-b"
    healthy(cache, sibling, NOW - timedelta(seconds=10))
    result = record(
        cache, WorkerTerminal(ok=False, error=error, status_code=status, executor_kind="live")
    )
    loaded = HealthCache(cache.path)
    assert result.recorded and result.cooldown_key is None
    assert loaded.lookup(sibling, NOW).healthy
    assert loaded.cooldowns() == {}
    assert loaded.bucket_remaining("claude", NOW, pool=POOL) == 10
    assert result.entry is not None and result.entry.failure_scope == "route"


def test_provider_fallback_when_pool_identity_unknown(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(
        cache, WorkerTerminal(ok=False, status_code=401, executor_kind="live"), pool=None
    )
    assert result.cooldown_key == "provider:cc"
    assert cache.cooldown_for("provider:cc", NOW) is not None
    assert result.entry is not None and result.entry.failure_scope == "provider"


@pytest.mark.parametrize("seconds", [86401.0, 1e300])
def test_worker_expiry_is_bounded_even_for_huge_retry_hint(tmp_path: Path, seconds: float) -> None:
    cache = HealthCache(tmp_path / "health.json")
    terminal = WorkerTerminal(
        ok=False, status_code=429, retry_after_seconds=seconds, executor_kind="live"
    )
    result = record(cache, terminal)
    assert result.entry is not None
    assert (result.entry.until - NOW).total_seconds() == LONG_CAP_SECONDS
    assert cache.cooldown_for(f"pool:{POOL}", NOW).until == result.entry.until
    assert cache.bucket_for(PROVIDER, POOL).zeroed_until == result.entry.until


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1.0, 0.0])
def test_invalid_classified_cooldown_does_not_write(tmp_path: Path, seconds: float) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(
        cache,
        WorkerTerminal(ok=False, executor_kind="live"),
        failure=FailureClassification("timeout", "REROUTE", seconds, "route"),
    )
    assert not result.recorded and result.reason == "invalid_cooldown"
    assert not cache.path.exists()


@pytest.mark.parametrize(
    "category",
    [
        "verification_failed",
        "ownership_violation",
        "review_failed",
        "task_feedback",
        "context_length_exceeded",
        "gateway_busy",
        "unknown",
        "success",
    ],
)
def test_non_availability_categories_never_write(tmp_path: Path, category: str) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(
        cache,
        WorkerTerminal(ok=False, executor_kind="live"),
        failure=FailureClassification(category, "REROUTE", 60, "route"),
    )
    assert not result.recorded and result.reason == "non_availability_failure"
    assert not cache.path.exists()


@pytest.mark.parametrize(
    "error",
    [
        "verification_failed: test fails",
        "ownership_violation: extra file",
        "review_failed",
        "task_feedback: fix it",
    ],
)
def test_task_error_never_writes_even_if_misclassified(tmp_path: Path, error: str) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(
        cache,
        WorkerTerminal(ok=False, error=error, executor_kind="live"),
        failure=FailureClassification("timeout", "REROUTE", 60, "route"),
    )
    assert not result.recorded and not cache.path.exists()


@pytest.mark.parametrize("kind", ["", "scripted", "fault-injected"])
def test_non_live_provenance_is_not_real_health_evidence(tmp_path: Path, kind: str) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(cache, WorkerTerminal(ok=False, status_code=503, executor_kind=kind))
    assert not result.recorded and not cache.path.exists()


@pytest.mark.parametrize("kwargs", [{"injected": True}, {"session_ref": "fault-injected:quota"}])
def test_injected_live_wrapper_cannot_poison_health(tmp_path: Path, kwargs: dict) -> None:
    cache = HealthCache(tmp_path / "health.json")
    kwargs = dict(kwargs)
    ref = kwargs.pop("session_ref", "")
    result = record(
        cache,
        WorkerTerminal(ok=False, status_code=429, executor_kind="live", session_ref=ref),
        **kwargs,
    )
    assert not result.recorded and result.reason == "injected_terminal"
    assert not cache.path.exists()


@pytest.mark.parametrize(
    ("turns", "expected"), [(None, None), (-1, None), (True, None), (2.5, None), (0, 0), (3, 3)]
)
def test_turns_are_observed_not_fabricated(tmp_path: Path, turns, expected) -> None:
    cache = HealthCache(tmp_path / "health.json")
    terminal = WorkerTerminal(
        ok=False, status_code=503, executor_kind="live", usage=AttemptUsage(turns=turns)
    )
    result = record(cache, terminal)
    assert result.entry is not None
    assert result.entry.feedback_turn_count == expected
    assert result.entry.feedback_turns_source == (
        "usage.turns" if expected is not None else "unknown"
    )
    assert HealthCache(cache.path).entry(ROUTE) == result.entry


def test_explicit_turn_count_and_opaque_provenance(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    secret_source = "session://a bearer sk-secret transcript" * 100
    terminal = WorkerTerminal(
        ok=False,
        status_code=503,
        executor_kind="live",
        usage=AttemptUsage(turns=99),
        session_ref=secret_source,
    )
    result = record(cache, terminal, turn_count=4, provenance=secret_source)
    assert result.entry is not None
    assert result.entry.feedback_turn_count == 4
    assert result.entry.feedback_turns_source == "explicit"
    assert result.entry.feedback_provenance.startswith("sha256:")
    assert len(result.entry.feedback_provenance) == 71
    assert "secret" not in cache.path.read_text() and "transcript" not in cache.path.read_text()


def test_duplicate_failure_is_idempotent_and_old_failure_cannot_replace_newer_health(
    tmp_path: Path,
) -> None:
    cache = HealthCache(tmp_path / "health.json")
    terminal = WorkerTerminal(ok=False, status_code=503, executor_kind="live")
    assert record(cache, terminal).recorded
    first = cache.entry(ROUTE)
    assert not record(cache, terminal).recorded
    assert cache.entry(ROUTE) == first
    healthy(cache, ROUTE, NOW + timedelta(seconds=1))
    assert not record(cache, terminal).recorded
    assert cache.lookup(ROUTE, NOW + timedelta(seconds=1)).healthy


def test_independent_writers_preserve_other_route_and_cooldown(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    stale = HealthCache(path)
    other = HealthCache(path)
    healthy(other, "cx/model-c", NOW)
    result = record(stale, WorkerTerminal(ok=False, status_code=429, executor_kind="live"))
    assert result.recorded
    loaded = HealthCache(path)
    assert loaded.lookup("cx/model-c", NOW).healthy
    assert loaded.cooldown_for(f"pool:{POOL}", NOW) is not None


def test_success_does_not_refresh_or_admit_unknown_route(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(cache, WorkerTerminal(ok=True, output="done", executor_kind="live"))
    assert not result.recorded and result.reason == "successful_terminal"
    assert cache.lookup(ROUTE, NOW).state == STATE_UNPROBED
    assert not cache.path.exists()


def test_ok_but_empty_is_a_real_terminal_failure(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    result = record(cache, WorkerTerminal(ok=True, output=" ", executor_kind="live"))
    assert result.recorded
    assert result.entry is not None and result.entry.category == "empty_output"


def test_naive_clock_fails_before_write(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    with pytest.raises(HealthCacheError, match="timezone-aware"):
        record(
            cache,
            WorkerTerminal(ok=False, status_code=503, executor_kind="live"),
            now=NOW.replace(tzinfo=None),
        )
    assert not cache.path.exists()


def test_old_schema_one_entry_reads_without_feedback_metadata(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    healthy(cache, ROUTE, NOW)
    assert "feedback_source" not in json.loads(cache.path.read_text())["routes"][ROUTE]
    assert HealthCache(cache.path).entry(ROUTE).feedback_turn_count is None


def test_probe_policy_unchanged_and_explicit_runtime_ttl_is_bounded(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    probe = cache.record(ROUTE, ProbeResult("authentication", False, False), NOW)
    assert (probe.until - NOW).total_seconds() == 6 * 3600
    override = cache.record(
        ROUTE, ProbeResult("authentication", False, False, negative_ttl_seconds=3600), NOW
    )
    assert (override.until - NOW).total_seconds() == 3600
    capped = cache.record(
        ROUTE, ProbeResult("rate_limited", False, False, negative_ttl_seconds=1e300), NOW
    )
    assert (capped.until - NOW).total_seconds() == LONG_CAP_SECONDS
    with pytest.raises(HealthCacheError, match="finite and positive"):
        cache.record(
            ROUTE,
            replace(ProbeResult("timeout", False, False), negative_ttl_seconds=float("nan")),
            NOW,
        )
