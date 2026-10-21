"""BOD-433/S03 phase 2b: worker spawn is gated by a durable dispatch permit.

No network, no provider calls; the adapter is a spy and the cache lives in the
isolated test HOME.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.admission import RuntimeEvidence, RuntimeObservation, admit
from verdict.admit_prove_confirm import passport_confirmation
from verdict.model_passports import ModelPassport
from verdict.orchestration.health_cache import (
    PERMIT_STATE_MAYBE_SENT,
    DispatchScope,
    HealthCache,
    default_cache_path,
)
from verdict.relay import dispatch_scope
from verdict.subagent_selection import HealthCache as ProbeCache
from verdict.subagent_selection import HealthResult, WorkerTask, WorkerTerminal
from verdict.worker_runtime import AttemptFailureError, WorkerController, worker_profile_digest

ROUTE = "kr/claude-ok"
SECOND = "kr/claude-two"
ORIGIN = "http://gateway.test/v1"
PROMPT = "do it"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _passport(route: str, *, qualified: datetime, ttl: int = 300) -> ModelPassport:
    return ModelPassport(
        provider=route.split("/", 1)[0],
        model_id=route,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=qualified + timedelta(seconds=ttl),
    )


def _rows(*routes: str) -> list[dict[str, Any]]:
    return [
        {
            "id": route,
            "owned_by": "kr",
            "context_length": 200_000,
            "capabilities": {"tool_calling": True},
            "pricing": {"input": 0, "output": 0},
        }
        for route in routes
    ]


class SpyAdapter:
    def __init__(self, *, fail_first: bool = False, mismatch_first: bool = False) -> None:
        self.spawns: list[str] = []
        self.fail_first = fail_first
        self.mismatch_first = mismatch_first

    async def spawn(
        self, prompt: str, *, name: str, model: str, thinking: str | None = None
    ) -> dict[str, Any]:
        self.spawns.append(model)
        return {"rlm_child_id": name, "model": model}

    async def collect(self, handle: dict[str, Any]) -> Any:
        if self.mismatch_first and len(self.spawns) == 1:
            raise AttemptFailureError("model_provenance_mismatch")
        if self.fail_first and len(self.spawns) == 1:
            raise RuntimeError("worker crashed")
        return WorkerTerminal("done", "WORKER_OK", True, stop_reason="stop")

    async def delete(self, handle: dict[str, Any]) -> None:
        return None


def _controller(
    tmp_path: Path,
    adapter: SpyAdapter,
    passports: dict[str, ModelPassport],
    *,
    routes: tuple[str, ...] = (ROUTE,),
    authority: bool = True,
) -> WorkerController:
    now = _now()
    rows = _rows(*routes)
    admitted = admit(
        rows,
        [{"provider": "kr", "isActive": True, "testStatus": "ok"}],
        RuntimeEvidence(
            tuple(
                RuntimeObservation(f"route:{r}", "healthy", "ok", "fixture:live", now.isoformat())
                for r in routes
            ),
            ("fixture:live",),
        ),
        now=now,
    )
    return WorkerController(
        WorkerTask(),
        inventory_rows=rows,
        prime_selectors=[f"omniroute/{r}" for r in routes],
        probe=lambda c: HealthResult(True, "healthy"),
        adapter=adapter,
        cache=ProbeCache(tmp_path / "probe.json"),
        admitted=admitted,
        require_admission=True,
        gateway_origin=ORIGIN if authority else None,
        confirmation=passport_confirmation(routes, now=now, passports=passports),
    )


def _scope(route: str) -> DispatchScope:
    return dispatch_scope(
        gateway_origin=ORIGIN,
        provider=route.split("/", 1)[0],
        model=route,
        route_id=route,
        profile_digest=worker_profile_digest(WorkerTask(), PROMPT),
    )


def _events(controller: WorkerController, name: str) -> list[dict[str, Any]]:
    return [e for e in controller.events if e["event"] == name]


async def test_worker_and_orchestration_handoffs_require_permits(tmp_path: Path) -> None:
    """Positive: a fresh passport mints a MAYBE_SENT permit before the spawn."""
    adapter = SpyAdapter()
    controller = _controller(tmp_path, adapter, {ROUTE: _passport(ROUTE, qualified=_now())})
    outcome = await controller.run(PROMPT)
    assert outcome.state == "SUCCESS"
    assert adapter.spawns == [f"omniroute/{ROUTE}"]
    cache = HealthCache(default_cache_path())
    attempt_id = f"{controller.operation_id}-a1"
    permit = cache.permit_for_attempt(attempt_id)
    assert permit is not None and permit.route_id == ROUTE
    assert cache.dispatch_attempt_state(attempt_id) == PERMIT_STATE_MAYBE_SENT


async def test_worker_stale_passport_and_failed_confirm_spawns_nothing(tmp_path: Path) -> None:
    adapter = SpyAdapter()
    stale = _passport(ROUTE, qualified=_now() - timedelta(hours=1))
    controller = _controller(tmp_path, adapter, {ROUTE: stale})
    controller.probe = lambda _: HealthResult(False, "authentication")
    outcome = await controller.run(PROMPT)
    assert outcome.state == "FAIL_CLOSED"
    assert adapter.spawns == []
    assert [e["classification"] for e in _events(controller, "exclusion")] == ["authentication"]


async def test_worker_without_passport_and_failed_confirm_spawns_nothing(tmp_path: Path) -> None:
    adapter = SpyAdapter()
    controller = _controller(tmp_path, adapter, {})
    controller.probe = lambda _: HealthResult(False, "authentication")
    assert (await controller.run(PROMPT)).state == "FAIL_CLOSED"
    assert adapter.spawns == []


async def test_worker_revoked_between_bind_and_handoff_spawns_nothing(tmp_path: Path) -> None:
    adapter = SpyAdapter()
    controller = _controller(tmp_path, adapter, {ROUTE: _passport(ROUTE, qualified=_now())})
    real_bind = controller._bind_proof

    def bind_then_revoke(candidate: Any, prompt: str) -> Any:
        bound = real_bind(candidate, prompt)
        HealthCache(default_cache_path()).record_revocation(
            _scope(ROUTE),
            route_id=ROUTE,
            event_id="denied-between-bind-and-handoff",
            source_revision=None,
            observed_at=_now() - timedelta(seconds=1),
        )
        return bound

    controller._bind_proof = bind_then_revoke  # type: ignore[method-assign]
    outcome = await controller.run(PROMPT)
    assert outcome.state == "FAIL_CLOSED"
    assert adapter.spawns == []
    assert [e["classification"] for e in _events(controller, "exclusion")] == ["permit_denied"]


async def test_worker_passport_older_than_revocation_spawns_nothing(tmp_path: Path) -> None:
    HealthCache(default_cache_path()).record_revocation(
        _scope(ROUTE),
        route_id=ROUTE,
        event_id="earlier-denial",
        source_revision=None,
        observed_at=_now(),
    )
    adapter = SpyAdapter()
    old = _passport(ROUTE, qualified=_now() - timedelta(seconds=30))
    controller = _controller(tmp_path, adapter, {ROUTE: old})
    assert (await controller.run(PROMPT)).state == "FAIL_CLOSED"
    assert adapter.spawns == []


async def test_worker_failure_after_permit_stays_maybe_sent_and_retry_gets_new_permit(
    tmp_path: Path,
) -> None:
    adapter = SpyAdapter(fail_first=True)
    now = _now()
    passports = {ROUTE: _passport(ROUTE, qualified=now), SECOND: _passport(SECOND, qualified=now)}
    controller = _controller(tmp_path, adapter, passports, routes=(ROUTE, SECOND))
    outcome = await controller.run(PROMPT)
    assert outcome.state == "SUCCESS"
    assert len(adapter.spawns) == 2
    cache = HealthCache(default_cache_path())
    first = cache.permit_for_attempt(f"{controller.operation_id}-a1")
    second = cache.permit_for_attempt(f"{controller.operation_id}-a2")
    assert first is not None and second is not None
    assert first.permit_id != second.permit_id and first.route_id != second.route_id
    # The failed attempt is never rewound: its permit remains consumed.
    assert cache.dispatch_attempt_state(f"{controller.operation_id}-a1") == PERMIT_STATE_MAYBE_SENT
    assert (
        cache.commit_dispatch(
            _scope(first.route_id),
            route_id=first.route_id,
            attempt_id=first.attempt_id,
            authorization_digest="x",
            expected_epoch=first.epoch,
            qualified_at=_now(),
            proof_deadline=_now() + timedelta(seconds=30),
            now=_now(),
        )
        is None
    )


async def test_model_provenance_mismatch_records_revocation(tmp_path: Path) -> None:
    adapter = SpyAdapter(mismatch_first=True)
    controller = _controller(tmp_path, adapter, {ROUTE: _passport(ROUTE, qualified=_now())})
    outcome = await controller.run(PROMPT)
    assert outcome.state == "FAIL_CLOSED"
    cache = HealthCache(default_cache_path())
    assert cache.revocation_epoch(_scope(ROUTE)) == 1
    assert cache.latest_revocation_at(_scope(ROUTE)) is not None


async def test_non_authority_controller_is_unchanged(tmp_path: Path) -> None:
    adapter = SpyAdapter()
    controller = _controller(tmp_path, adapter, {}, authority=False)
    assert (await controller.run(PROMPT)).state == "SUCCESS"
    assert (
        HealthCache(default_cache_path()).permit_for_attempt(f"{controller.operation_id}-a1")
        is None
    )


def test_passport_confirmation_uses_the_passports_own_times() -> None:
    now = _now()
    good = _passport(ROUTE, qualified=now - timedelta(seconds=10))
    expired = _passport(SECOND, qualified=now - timedelta(hours=1))
    unauthorized = replace(_passport("kr/x", qualified=now), auth_state="unauthorized")
    result = passport_confirmation(
        [ROUTE, SECOND, "kr/x", "kr/missing"],
        now=now,
        passports={ROUTE: good, SECOND: expired, "kr/x": unauthorized},
    )
    assert result["admitted"] == [ROUTE]
    row = next(r for r in result["passport"] if r["identity_id"] == ROUTE)
    assert row["qualified_at"].startswith((now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M"))
    assert result["receipt_id"].startswith("passports:")
    assert result == passport_confirmation(
        [ROUTE, SECOND, "kr/x", "kr/missing"],
        now=now,
        passports={ROUTE: good, SECOND: expired, "kr/x": unauthorized},
    )


def test_worker_profile_digest_depends_on_work_not_secrets() -> None:
    task = WorkerTask()
    assert worker_profile_digest(task, "a") != worker_profile_digest(task, "b")
    assert worker_profile_digest(task, "a") == worker_profile_digest(
        replace(task, excluded_route_ids=frozenset({"x"})), "a"
    )


async def test_worker_stale_passport_successful_confirm_mints_current_permit(tmp_path):
    adapter = SpyAdapter()
    stale = _passport(ROUTE, qualified=_now() - timedelta(hours=1))
    controller = _controller(tmp_path, adapter, {ROUTE: stale})
    assert (await controller.run(PROMPT)).state == "SUCCESS"
    assert adapter.spawns == [f"omniroute/{ROUTE}"]
    permit = HealthCache(default_cache_path()).permit_for_attempt(f"{controller.operation_id}-a1")
    assert permit is not None
    assert (
        HealthCache(default_cache_path()).dispatch_attempt_state(permit.attempt_id)
        == PERMIT_STATE_MAYBE_SENT
    )


async def test_confirm_after_negative_backoff_mints_new_epoch_not_old_epoch(tmp_path):
    from verdict.orchestration.health_cache import ProbeResult

    cache = HealthCache(default_cache_path())
    earlier = _now() - timedelta(seconds=61)
    cache.record(ROUTE, ProbeResult(category="timeout", chat_ok=False, tool_ok=False), earlier)
    cache.save()
    epoch = cache.record_revocation(
        _scope(ROUTE),
        route_id=ROUTE,
        event_id="old-negative",
        source_revision=None,
        observed_at=earlier,
    )
    assert cache.entry(ROUTE).until < _now()
    adapter = SpyAdapter()
    controller = _controller(
        tmp_path, adapter, {ROUTE: _passport(ROUTE, qualified=earlier - timedelta(seconds=1))}
    )
    assert (await controller.run(PROMPT)).state == "SUCCESS"
    permit = HealthCache(default_cache_path()).permit_for_attempt(f"{controller.operation_id}-a1")
    assert permit is not None and permit.epoch == epoch == 1
    # A later attested screen also leaves the recovered scope usable; the
    # old revocation must not spring back as a fresh authentication backoff.
    healthy = HealthCache(default_cache_path())
    healthy.record(ROUTE, ProbeResult(category="ok", chat_ok=True, tool_ok=True), _now())
    healthy.save()
    again = _controller(
        tmp_path / "again", SpyAdapter(), {ROUTE: _passport(ROUTE, qualified=_now())}
    )
    assert (await again.run(PROMPT)).state == "SUCCESS"
    assert (
        cache.commit_dispatch(
            _scope(ROUTE),
            route_id=ROUTE,
            attempt_id="old-epoch",
            authorization_digest="old",
            expected_epoch=0,
            qualified_at=_now(),
            proof_deadline=_now() + timedelta(seconds=30),
            now=_now(),
        )
        is None
    )
