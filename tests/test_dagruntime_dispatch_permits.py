"""BOD-433 phase 2c: every DagRuntime worker send needs a durable dispatch permit.

No network and no provider calls. The executor is a spy, passports are in-memory
fixtures, and the health cache lives in the isolated test HOME.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.test_orch_runtime import NOW, Classifier, Events, Executor, Reviewer, Selector, node
from tests.test_orch_runtime import repo as repo
from verdict.admit_prove_confirm import passport_confirmation
from verdict.model_passports import ModelPassport
from verdict.orchestration.contracts import NodeState, RunOutcome, WorkerTerminal, WorkGraph
from verdict.orchestration.health_cache import (
    PERMIT_STATE_MAYBE_SENT,
    DispatchScope,
    HealthCache,
    default_cache_path,
)
from verdict.orchestration.runtime import (
    DagRuntime,
    DispatchAuthority,
    RuntimePolicy,
    node_profile_digest,
    permit_attempt_id,
)
from verdict.relay import dispatch_scope

ORIGIN = "http://gateway.test"


def _passport(route: str, *, qualified: datetime = NOW, ttl: int = 300) -> ModelPassport:
    return ModelPassport(
        provider=route.split("/", 1)[0],
        model_id=route,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=qualified + timedelta(seconds=ttl),
    )


def _authority(passports: dict[str, ModelPassport]) -> DispatchAuthority:
    def confirm(route_ids: Sequence[str], now: datetime) -> dict[str, Any]:
        return passport_confirmation(route_ids, now=now, passports=passports)

    return DispatchAuthority(gateway_origin=ORIGIN, confirm=confirm)


def _scope(route: str, nid: str = "a") -> DispatchScope:
    return dispatch_scope(
        gateway_origin=ORIGIN,
        provider=route.split("/", 1)[0],
        model=route,
        route_id=route,
        profile_digest=node_profile_digest(node(nid)),
    )


def _attempt_id(cwd: Path) -> str:
    """The permit attempt id of the worktree an executor was handed."""
    # cwd = <run_dir>/worktrees/<node>-a<n>
    node_id, attempt = cwd.name.rsplit("-a", 1)
    return permit_attempt_id(cwd.parent.parent, node_id, int(attempt))


def _id(run_dir: Path, attempt: int = 1) -> str:
    return permit_attempt_id(run_dir, "a", attempt)


def _cache() -> HealthCache:
    return HealthCache(default_cache_path())


def _make(
    repo: Path,
    executor: Any,
    routes: list[str],
    authority: DispatchAuthority | None,
    *,
    run_name: str = "run1",
    classifier: Any = None,
) -> tuple[DagRuntime, Events]:
    events = Events()
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / run_name,
        graph=WorkGraph("g", (node("a"),)),
        selector=Selector(routes),
        executor=executor,
        classifier=classifier or Classifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=RuntimePolicy(require_dispatch_permit=True),
        now=lambda: NOW,
        dispatch_authority=authority,
    )
    return runtime, events


class PermitSpy(Executor):
    """Records, at send time, the durable state of the attempt it is sent under."""

    def __init__(self, behaviour: dict[tuple[str, str], str] | None = None) -> None:
        super().__init__(behaviour or {}, delay=0)
        self.states_at_send: list[str | None] = []
        self.attempt_ids: list[str] = []

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        # cwd = <run_dir>/worktrees/<node>-a<n>
        attempt_id = _attempt_id(cwd)
        self.attempt_ids.append(attempt_id)
        self.states_at_send.append(_cache().dispatch_attempt_state(attempt_id))
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


async def test_dag_mints_permit_before_executor_and_namespaces_attempt_ids(repo: Path) -> None:
    authority = _authority({"cc/s": _passport("cc/s")})
    ex = PermitSpy()
    rt, ev = _make(repo, ex, ["cc/s"], authority)
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cc/s")]
    # Committed durably as MAYBE_SENT before the executor ran.
    assert ex.attempt_ids == [_id(repo.parent / "run1")]
    assert ex.states_at_send == [PERMIT_STATE_MAYBE_SENT]
    permit = _cache().permit_for_attempt(_id(repo.parent / "run1"))
    assert permit is not None and permit.route_id == "cc/s"
    assert permit.scope == _scope("cc/s")
    minted = [e for e in ev.of("controller", "a") if e.get("phase") == "dispatch_permit"]
    assert [(e["attempt_id"], e["permit_id"]) for e in minted] == [
        (_id(repo.parent / "run1"), permit.permit_id)
    ]

    # A second run with the same node id is not denied as a duplicate attempt.
    (repo / "a.txt").unlink(missing_ok=True)
    ex2 = PermitSpy()
    rt2, _ = _make(repo, ex2, ["cc/s"], authority, run_name="run2")
    result2 = await rt2.run()
    assert result2.outcome is RunOutcome.COMPLETE, result2.reason
    assert ex2.attempt_ids == [_id(repo.parent / "run2")]
    assert ex2.states_at_send == [PERMIT_STATE_MAYBE_SENT]
    second = _cache().permit_for_attempt(_id(repo.parent / "run2"))
    assert second is not None and second.permit_id != permit.permit_id


async def test_dag_without_fresh_passport_sends_nothing(repo: Path) -> None:
    stale = _passport("cc/s", qualified=NOW - timedelta(hours=1))
    ex = PermitSpy()
    rt, ev = _make(repo, ex, ["cc/s", "cx/g"], _authority({"cc/s": stale}))
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.calls == []
    assert result.nodes["a"].state is NodeState.BLOCKED
    reasons = [e.get("reason", "") for e in ev.of("eligibility", "a")]
    assert reasons.count("pre-dispatch recheck: no_fresh_passport") == 2
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is None


async def test_dag_revocation_between_bind_and_commit_sends_nothing(repo: Path) -> None:
    authority = _authority({"cc/s": _passport("cc/s")})
    ex = PermitSpy()
    rt, ev = _make(repo, ex, ["cc/s"], authority)
    real_bind = rt._bind_proof

    def bind_then_revoke(work_node: Any, route_id: str) -> Any:
        bound = real_bind(work_node, route_id)
        _cache().record_revocation(
            _scope(route_id),
            route_id=route_id,
            event_id="denied-between-bind-and-commit",
            source_revision=None,
            observed_at=NOW,
        )
        return bound

    rt._bind_proof = bind_then_revoke  # type: ignore[method-assign]
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.calls == []
    assert ev.of("failure", "a")[0]["category"] == "permit_denied"
    assert _cache().permit_for_attempt(_id(repo.parent / "run1")) is None


async def test_dag_passport_older_than_revocation_stays_denied(repo: Path) -> None:
    _cache().record_revocation(
        _scope("cc/s"),
        route_id="cc/s",
        event_id="earlier-denial",
        source_revision=None,
        observed_at=NOW,
    )
    old = _passport("cc/s", qualified=NOW - timedelta(seconds=30))
    ex = PermitSpy()
    rt, ev = _make(repo, ex, ["cc/s", "cx/g"], _authority({"cc/s": old, "cx/g": _passport("cx/g")}))
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cx/g")]
    assert "pre-dispatch recheck: no_fresh_passport" in [
        e.get("reason") for e in ev.of("eligibility", "a")
    ]


async def test_flag_without_authority_still_denies_every_send(repo: Path) -> None:
    ex = PermitSpy()
    rt, ev = _make(repo, ex, ["cc/s"], None)
    result = await rt.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert ex.calls == []
    assert ev.of("failure", "a")[0]["category"] == "dispatch_denied"


# ---------------------------------------------------------------- production wiring


def _orchestrate_args(tmp_path: Path, *extra: str) -> Any:
    import argparse

    from verdict.orchestration import cli

    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(["orchestrate", "ship it", "--repo", str(tmp_path), "--json", *extra])


def test_orchestrate_cli_always_runs_with_dispatch_authority(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from verdict.orchestration import cli, run

    seen: dict[str, Any] = {}
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "fixture-only")
    monkeypatch.setattr(cli, "build_selector", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_executor", lambda args: object())
    monkeypatch.setattr(cli, "_planner_executor", lambda args: object())

    async def fake_run(goal: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        receipt = tmp_path / "receipt.json"
        receipt.write_text("{}")
        return run.GoldenRunResult(tmp_path, "BLOCKED", "fixture", receipt)

    monkeypatch.setattr(run, "run_golden_path", fake_run)
    cli._orchestrate(_orchestrate_args(tmp_path, "--gateway", "http://gw.test:20128/"))
    authority = seen["dispatch_authority"]
    assert isinstance(authority, DispatchAuthority)
    assert authority.gateway_origin == "http://gw.test:20128"
    assert seen["policy"].require_dispatch_permit is True
    # Production proofs come from the stored passports (read-only); none here.
    assert authority.confirm(["cc/s"], NOW)["admitted"] == []


async def test_run_golden_path_forwards_dispatch_authority_to_the_runtime(repo: Path) -> None:
    from verdict.orchestration.run import run_golden_path

    ex = PermitSpy()
    result = await run_golden_path(
        "write a",
        repo=repo,
        runs_root=repo.parent / "runs",
        selector=Selector(["cc/s"]),
        executor=ex,
        classifier=Classifier(),
        reviewer=Reviewer(),
        graph=WorkGraph("g", (node("a"),)),
        run_id="golden1",
        policy=RuntimePolicy(require_dispatch_permit=True),
        dispatch_authority=_authority({"cc/s": _passport("cc/s", qualified=datetime.now(UTC))}),
    )
    # The hydrated prompt does not drive the spy's file write, so verification
    # fails and the same route is retried: each send needs its own fresh permit.
    golden = repo.parent / "runs" / "golden1"
    assert ex.attempt_ids == [_id(golden, 1), _id(golden, 2)], result.reason
    assert ex.states_at_send == [PERMIT_STATE_MAYBE_SENT] * 2


# ---------------------------------------------------------------- after the commit


class FailingSpy(PermitSpy):
    """``auth`` answers 401, ``mismatch`` reports a model mismatch, ``revoke`` is
    revoked by another writer while the send is in flight."""

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        kind = self.behaviour.get(("a", route_id), "ok")
        if kind in {"auth", "mismatch", "revoke"}:
            self.calls.append(("a", route_id))
            self.attempt_ids.append(_attempt_id(cwd))
            self.states_at_send.append(_cache().dispatch_attempt_state(self.attempt_ids[-1]))
            if kind == "auth":
                return WorkerTerminal(ok=False, model=route_id, status_code=401, error="bad key")
            if kind == "mismatch":
                return WorkerTerminal(ok=False, model=route_id, error="model_mismatch")
            _cache().record_revocation(
                _scope(route_id),
                route_id=route_id,
                event_id="revoked-while-in-flight",
                source_revision=None,
                observed_at=NOW,
            )
            (cwd / "a.txt").write_text("a by revoked route\n")
            return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


def _two_route_run(repo: Path, first: str) -> tuple[FailingSpy, Any, Events]:
    from verdict.orchestration.recovery import FailureIntelligence

    authority = _authority({"cc/s": _passport("cc/s"), "cx/g": _passport("cx/g")})
    ex = FailingSpy({("a", "cc/s"): first})
    rt, ev = _make(repo, ex, ["cc/s", "cx/g"], authority, classifier=FailureIntelligence())
    return ex, rt, ev


async def test_dag_failure_after_permit_revokes_on_auth_and_retries_with_new_permit(
    repo: Path,
) -> None:
    ex, rt, ev = _two_route_run(repo, "auth")
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cc/s"), ("a", "cx/g")]
    assert ex.states_at_send == [PERMIT_STATE_MAYBE_SENT] * 2
    cache = _cache()
    # The failed send stays MAYBE_SENT (it may have reached the provider).
    assert cache.dispatch_attempt_state(_id(repo.parent / "run1")) == PERMIT_STATE_MAYBE_SENT
    first, second = (
        cache.permit_for_attempt(_id(repo.parent / "run1")),
        cache.permit_for_attempt(_id(repo.parent / "run1", 2)),
    )
    assert first is not None and second is not None
    assert (first.route_id, second.route_id) == ("cc/s", "cx/g")
    assert cache.revocation_epoch(_scope("cc/s")) == 1
    assert cache.revocation_epoch(_scope("cx/g")) == 0
    # The consumed attempt id can never mint a second permit.
    assert (
        cache.commit_dispatch(
            _scope("cc/s"),
            route_id="cc/s",
            attempt_id=_id(repo.parent / "run1"),
            authorization_digest="x",
            expected_epoch=1,
            proof_deadline=NOW + timedelta(seconds=60),
            now=NOW,
        )
        is None
    )
    failure = ev.of("failure", "a")[0]
    assert (failure["category"], failure["permit_id"]) == ("authentication", first.permit_id)


async def test_dag_model_mismatch_after_permit_revokes_the_scope(repo: Path) -> None:
    ex, rt, _ = _two_route_run(repo, "mismatch")
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cc/s"), ("a", "cx/g")]
    assert _cache().revocation_epoch(_scope("cc/s")) == 1


async def test_dag_verification_failure_does_not_revoke(repo: Path) -> None:
    ex, rt, ev = _two_route_run(repo, "badcode")
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert "verification_failed" in [e["category"] for e in ev.of("failure", "a")]
    # Same-route rehydrate retry and the reroute each needed their own permit.
    assert ex.attempt_ids == [
        _id(repo.parent / "run1", 1),
        _id(repo.parent / "run1", 2),
        _id(repo.parent / "run1", 3),
    ]
    assert ex.states_at_send == [PERMIT_STATE_MAYBE_SENT] * 3
    assert _cache().revocation_epoch(_scope("cc/s")) == 0


async def test_dag_revocation_after_commit_discards_the_result(repo: Path) -> None:
    ex, rt, ev = _two_route_run(repo, "revoke")
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.calls == [("a", "cc/s"), ("a", "cx/g")]
    assert ev.of("failure", "a")[0]["category"] == "permit_revoked_after_commit"
    assert result.nodes["a"].route_id == "cx/g"


# ---------------------------------------------------------------- review fixes


async def test_same_run_id_under_two_roots_both_get_permits(repo: Path) -> None:
    authority = _authority({"cc/s": _passport("cc/s")})
    seen: list[str] = []
    for root in ("root-x", "root-y"):
        (repo / "a.txt").unlink(missing_ok=True)
        ex = PermitSpy()
        rt, _ = _make(repo, ex, ["cc/s"], authority, run_name=f"{root}/20261019T000000Z")
        result = await rt.run()
        assert result.outcome is RunOutcome.COMPLETE, result.reason
        assert ex.states_at_send == [PERMIT_STATE_MAYBE_SENT]
        seen += ex.attempt_ids
    assert len(set(seen)) == 2


def test_permit_attempt_id_is_stable_for_the_same_run_dir(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    same = permit_attempt_id(tmp_path / "runs" / ".." / "runs" / "r1", "a", 1)
    assert permit_attempt_id(run_dir, "a", 1) == same
    assert permit_attempt_id(run_dir, "a", 2) != same
    assert permit_attempt_id(tmp_path / "other" / "r1", "a", 1) != same


async def test_permit_cache_calls_do_not_block_the_event_loop(repo: Path, monkeypatch: Any) -> None:
    import asyncio
    import time

    from verdict.orchestration import runtime as runtime_module

    class SlowCache(HealthCache):
        def commit_dispatch(self, *args: Any, **kwargs: Any) -> Any:
            time.sleep(0.3)
            return super().commit_dispatch(*args, **kwargs)

        def revocation_epoch(self, *args: Any, **kwargs: Any) -> int:
            time.sleep(0.3)
            return super().revocation_epoch(*args, **kwargs)

        def record_revocation(self, *args: Any, **kwargs: Any) -> int:
            time.sleep(0.3)
            return super().record_revocation(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "HealthCache", SlowCache)
    ex, rt, _ = _two_route_run(repo, "auth")
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    result = await rt.run()
    stop.set()
    await tick
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert ex.states_at_send == [PERMIT_STATE_MAYBE_SENT] * 2
    assert _cache().revocation_epoch(_scope("cc/s")) == 1
    assert max(gaps) < 0.25, f"event loop stalled {max(gaps):.2f}s on a permit cache call"
