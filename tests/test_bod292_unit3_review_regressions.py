"""Regression tests for the BOD-292 unit-3 independent review (cx/gpt-6-sol).

Each test here pins a BLOCKING review finding. Every one FAILS on bec2e26
(the reviewed commit) and PASSES after the fix. All I/O is injected: no real
gateway or model is ever contacted.

Findings covered:
  1. confirmed METERED/UNKNOWN manual plans must EXECUTE (not reused_fresh);
  2. VERDICT_AUTO_REFRESH=0 must block the explicit picker/selection triggers;
  3. execute must re-check gateway_origin + evidence generation (refuse
     missing/mismatch) before any call;
  4. the health-cache writer (daemon included) must never destroy a newer
     concurrent write;
  5. per-model 403 / per-model 429 scope only that route, not the provider;
  6. a joined consumer's cancel must stop the shared owner and show live
     progress, and a still-running marker must not be reported complete;
  + per-provider token reservations must be a persisted spend boundary
    (controller-upgraded BLOCKING), and complete must not be read as tested.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any
import time
from datetime import datetime, timezone
from pathlib import Path

from verdict.actions.model_refresh import action_models_refresh_execute, action_models_refresh_plan
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.orchestration.verified_refresh import (
    CAPACITY_FREE,
    CAPACITY_METERED,
    CAPACITY_UNKNOWN,
    OUTCOME_AUTO_DISABLED,
    OUTCOME_CANCELLED,
    ProbeExchange,
    RefreshConfig,
    RefreshCoordinator,
    RefreshSnapshot,
    RowInput,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _coord(tmp_path: Path, transport, *, monotonic=None, sleep=None):
    return RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=monotonic or (lambda: 0.0),
        sleep=sleep or (lambda _s: None),
        lock_path=tmp_path / "refresh.lock",
        marker_path=tmp_path / "refresh.json",
    )


def _ok(route_id: str, phase: str) -> ProbeExchange:
    suffix = route_id.split("/", 1)[1] if "/" in route_id else route_id
    return ProbeExchange(
        http_status=200,
        ok=True,
        chat_exact=(phase == "chat"),
        tool_called=(phase == "tool"),
        latency_ms=1.0,
        reported_model=suffix,
    )


class _Transport:
    def __init__(self, script=None):
        self.script = script or {}
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, route_id: str, phase: str, timeout: float) -> ProbeExchange:
        with self.lock:
            self.calls.append((route_id, phase))
        return self.script.get((route_id, phase)) or _ok(route_id, phase)


# ---------------------------------------------------------------------------
# Finding 1: confirmed metered/unknown plans MUST execute (not reused_fresh).
# ---------------------------------------------------------------------------


def test_finding1_confirmed_metered_plan_executes(tmp_path: Path) -> None:
    """A confirmed METERED plan runs a chat-only liveness probe (1 call).

    On bec2e26 the coordinator's unconditional prepaid-only candidate filter
    dropped the metered id, so execute returned ``reused_fresh`` with probed=0,
    requests_made=0 and an empty call list -- the explicit consent was ignored.
    """
    row = {
        "route_id": "paid/a",
        "provider": "paid",
        "status": "STALE",
        "capacity_class": CAPACITY_METERED,
        "refreshable": True,
    }
    plan = action_models_refresh_plan(
        snapshot_rows=[row],
        needed_ids=["paid/a"],
        include_metered=True,
        now=NOW,
        gateway_origin="http://g",
        evidence_generation="gen1",
    ).data
    assert plan["estimated_requests"] == 1  # one liveness request, not two
    transport = _Transport()
    snap = RefreshSnapshot(
        rows=(RowInput("paid/a", "paid", "STALE", CAPACITY_METERED, True),),
        generation="gen1",
        gateway_origin="http://g",
    )
    r = action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=snap,
        now=NOW,
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )
    assert r.ok
    assert r.data["outcome"] == "completed"
    assert r.data["probed"] == 1
    assert r.data["verified"] == 1
    assert r.data["requests_made"] == 1  # chat-only liveness: never 0, never 2
    assert transport.calls == [("paid/a", "chat")]
    # Recorded as a healthy LIVENESS entry: chat-only, not a coding worker.
    entry = HealthCache(tmp_path / "health-cache.json").entry("paid/a")
    assert entry is not None and entry.healthy and entry.tool_ok is False


def test_finding1_confirmed_unknown_plan_executes(tmp_path: Path) -> None:
    """UNKNOWN capacity also executes under explicit consent (liveness)."""
    row = {
        "route_id": "x/u",
        "provider": "x",
        "status": "UNVERIFIED",
        "capacity_class": CAPACITY_UNKNOWN,
        "refreshable": True,
    }
    plan = action_models_refresh_plan(
        snapshot_rows=[row],
        needed_ids=["x/u"],
        include_metered=True,
        now=NOW,
        gateway_origin="http://g",
        evidence_generation="gen1",
    ).data
    transport = _Transport()
    snap = RefreshSnapshot(
        rows=(RowInput("x/u", "x", "UNVERIFIED", CAPACITY_UNKNOWN, True),),
        generation="gen1",
        gateway_origin="http://g",
    )
    r = action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=snap,
        now=NOW,
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )
    assert r.ok and r.data["probed"] == 1 and r.data["requests_made"] == 1
    assert transport.calls == [("x/u", "chat")]


# ---------------------------------------------------------------------------
# Finding 2: VERDICT_AUTO_REFRESH=0 disables ALL automatic triggers, including
# the explicit picker/selection ones (they are automatic, not consented plans).
# ---------------------------------------------------------------------------


def test_finding2_disabled_auto_refresh_blocks_explicit_picker(tmp_path: Path) -> None:
    """auto_refresh=False + explicit picker must NOT dispatch any call.

    On bec2e26 the disabled gate was ``not auto_refresh and not explicit``, so
    an explicit picker/selection trigger bypassed it and probed anyway.
    """
    transport = _Transport()
    coord = _coord(tmp_path, transport)
    snap = RefreshSnapshot(
        rows=(RowInput("free/a", "free", "STALE", CAPACITY_FREE, True),), generation="gen1"
    )
    out = coord.refresh_for_consumer(
        snap,
        consumer="picker",
        needed_ids=["free/a"],
        config=RefreshConfig(auto_refresh=False),
        explicit=True,
    )
    assert out.outcome == OUTCOME_AUTO_DISABLED
    assert out.requests_made == 0
    assert transport.calls == []


def test_finding2_disabled_auto_refresh_allows_authorized_manual(tmp_path: Path) -> None:
    """A consented manual plan still runs while auto-refresh is disabled.

    VERDICT_AUTO_REFRESH governs automatic triggers only; a separately typed
    ``authorized`` plan is not an automatic trigger and must execute.
    """
    transport = _Transport()
    coord = _coord(tmp_path, transport)
    snap = RefreshSnapshot(
        rows=(RowInput("free/a", "free", "STALE", CAPACITY_FREE, True),), generation="gen1"
    )
    out = coord.refresh_for_consumer(
        snap,
        consumer="manual",
        needed_ids=["free/a"],
        config=RefreshConfig(auto_refresh=False),
        explicit=True,
        authorized=True,
    )
    assert out.outcome == "completed"
    assert out.verified == 1
    assert transport.calls == [("free/a", "chat"), ("free/a", "tool")]


# ---------------------------------------------------------------------------
# Finding 3: execute re-checks gateway_origin + generation before any call.
# ---------------------------------------------------------------------------


def _plan(now=NOW, gateway="http://planned", gen="gen1"):
    row = {
        "route_id": "free/a",
        "provider": "free",
        "status": "STALE",
        "capacity_class": CAPACITY_FREE,
        "refreshable": True,
    }
    return action_models_refresh_plan(
        snapshot_rows=[row],
        needed_ids=["free/a"],
        now=now,
        gateway_origin=gateway,
        evidence_generation=gen,
    ).data


def test_finding3_execute_refuses_when_current_generation_missing(tmp_path: Path) -> None:
    """A gen1-bound plan against a snapshot with NO generation is refused.

    On bec2e26 the check was ``if gen and snapshot.generation``: an empty
    current generation skipped the guard and the plan ran two calls.
    """
    transport = _Transport()
    snap = RefreshSnapshot(
        rows=(RowInput("free/a", "free", "STALE", CAPACITY_FREE, True),),
        generation="",
        gateway_origin="http://planned",
    )
    r = action_models_refresh_execute(
        confirmed=True,
        plan=_plan(),
        snapshot=snap,
        now=NOW,
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )
    assert not r.ok
    assert r.data["reason"] == "evidence_changed"
    assert r.data["calls"] == 0
    assert transport.calls == []


def test_finding3_execute_refuses_on_gateway_mismatch(tmp_path: Path) -> None:
    """A plan digest-bound to one endpoint refuses a different current endpoint.

    On bec2e26 execute never compared gateway_origin despite the digest binding
    (design s6:141-143), so a plan ran against a swapped endpoint.
    """
    transport = _Transport()
    snap = RefreshSnapshot(
        rows=(RowInput("free/a", "free", "STALE", CAPACITY_FREE, True),),
        generation="gen1",
        gateway_origin="http://OTHER",
    )
    r = action_models_refresh_execute(
        confirmed=True,
        plan=_plan(gateway="http://planned"),
        snapshot=snap,
        now=NOW,
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )
    assert not r.ok
    assert r.data["reason"] == "gateway_changed"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Finding 4: the health-cache writer (daemon included) never destroys a newer
# concurrent write. save() is a serialized read-merge-write.
# ---------------------------------------------------------------------------


def test_finding4_stale_save_does_not_clobber_newer_merge(tmp_path: Path) -> None:
    """A stale object's save() must not wipe a route another writer just added.

    Repro from the review: a stale HealthCache object; a coordinator
    merge_and_save adds cc/a; the stale object's save() then ran and -- on
    bec2e26 -- removed cc/a because save() wrote only its own old snapshot.
    """
    path = tmp_path / "health-cache.json"
    stale = HealthCache(path)  # loaded while the file is empty
    coordinator = HealthCache(path)
    coordinator.merge_and_save(
        lambda c: c.record("cc/a", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    )
    assert "cc/a" in HealthCache(path).routes()
    # The stale object records its own route and saves (the daemon's path).
    stale.record("kr/b", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    stale.save()
    final = set(HealthCache(path).routes())
    assert "cc/a" in final  # the newer merge survived
    assert "kr/b" in final  # the stale writer's own route also landed


def test_finding4_daemon_cycle_save_preserves_coordinator_write(tmp_path: Path) -> None:
    """A full daemon run_once cycle must not clobber a coordinator's cc/a write."""
    from verdict.prove_at_rest import AdmittedRoute, Prober

    path = tmp_path / "health-cache.json"
    # Coordinator writes cc/a via the serialized merge path.
    HealthCache(path).merge_and_save(
        lambda c: c.record("cc/a", ProbeResult(category="ok", chat_ok=True, tool_ok=True), NOW)
    )

    # A daemon prober object that was constructed BEFORE that write (so its
    # in-memory view lacks cc/a) runs a cycle and saves repeatedly.
    def _t(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        return _ok(route_id, phase)

    prober = Prober(
        cache=HealthCache(path),  # fresh load includes cc/a; simulate staleness below
        routes_loader=lambda: [AdmittedRoute("free/m", "free", "free")],
        transport=_t,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        epsilon=0,
    )
    # Force staleness: drop cc/a from the prober's in-memory view, as if it was
    # loaded before the coordinator's write.
    prober.cache._routes.pop("cc/a", None)
    prober.run_once()
    final = set(HealthCache(path).routes())
    assert "cc/a" in final  # coordinator's write survived the daemon's save()
    assert "free/m" in final  # the daemon's own probe landed


# ---------------------------------------------------------------------------
# Finding 5: per-model 403 / 429 scope only that route (siblings stay eligible),
# while an account/provider-wide failure still stops the whole provider.
# ---------------------------------------------------------------------------


def test_finding5_model_scoped_403_leaves_sibling_eligible(tmp_path: Path) -> None:
    """A 403 that names the model stops only that route; the sibling verifies.

    On bec2e26 every 403 stopped and persisted a provider cooldown, so the
    sibling route wrongly became UNAVAILABLE and ``model_scoped`` was
    unreachable (``403 and not provider_stop`` is always False for a 403).
    """
    script = {
        ("cc/a", "chat"): ProbeExchange(
            http_status=403,
            ok=False,
            error_category="permission",
            detail="403 Forbidden for model cc/a",
        )
    }
    transport = _Transport(script)
    coord = _coord(tmp_path, transport)
    rows = (
        RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True, rank_hint=1),
        RowInput("cc/b", "cc", "STALE", CAPACITY_FREE, True, rank_hint=2),
    )
    out = coord.refresh_for_consumer(
        RefreshSnapshot(rows, generation="gen1"),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    assert out.route_outcomes["cc/a"].verified is False
    # The sibling is probed and verified, NOT stopped by a provider cooldown.
    assert out.route_outcomes["cc/b"].verified is True
    assert out.route_outcomes["cc/b"].refresh_reason is None
    assert ("cc/b", "chat") in transport.calls
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.cooldown_for("provider:cc", NOW) is None  # provider NOT stopped
    assert cache.cooldown_for("route:cc/a", NOW) is not None  # route-only block


def test_finding5_account_wide_403_still_stops_provider(tmp_path: Path) -> None:
    """A 403 with no model marker stays provider-scoped (conservative default)."""
    script = {
        ("cc/a", "chat"): ProbeExchange(
            http_status=403,
            ok=False,
            error_category="permission",
            detail="Your API key is not permitted on this account",
        )
    }
    transport = _Transport(script)
    coord = _coord(tmp_path, transport)
    rows = (
        RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True, rank_hint=1),
        RowInput("cc/b", "cc", "STALE", CAPACITY_FREE, True, rank_hint=2),
    )
    out = coord.refresh_for_consumer(
        RefreshSnapshot(rows, generation="gen1"),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    assert out.route_outcomes["cc/b"].refresh_reason == "provider_scope_stopped"
    assert ("cc/b", "chat") not in transport.calls
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.cooldown_for("provider:cc", NOW) is not None


def test_finding5_per_model_429_scopes_route_only(tmp_path: Path) -> None:
    """A per-model 429 quota message zeroes only that route, not the provider."""
    script = {
        ("cc/a", "chat"): ProbeExchange(
            http_status=429,
            ok=False,
            error_category="rate_limited",
            retry_after_seconds=120.0,
            detail="usage limit reached for this model cc/a",
        )
    }
    transport = _Transport(script)
    coord = _coord(tmp_path, transport)
    rows = (
        RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True, rank_hint=1),
        RowInput("cc/b", "cc", "STALE", CAPACITY_FREE, True, rank_hint=2),
    )
    out = coord.refresh_for_consumer(
        RefreshSnapshot(rows, generation="gen1"),
        consumer="verified_view",
        needed_ids=["cc/a", "cc/b"],
        config=RefreshConfig(),
    )
    assert out.route_outcomes["cc/b"].verified is True  # sibling not stopped
    cache = HealthCache(tmp_path / "health-cache.json")
    assert cache.cooldown_for("provider:cc", NOW) is None
    assert cache.cooldown_for("route:cc/a", NOW) is not None


# ---------------------------------------------------------------------------
# Finding 6: a joined consumer's cancel stops the shared owner and shows live
# progress; a still-running marker is never reported complete.
# ---------------------------------------------------------------------------


def test_finding6_joined_cancel_stops_shared_owner(tmp_path: Path) -> None:
    """A joiner's cancel raises the shared flag; the owner stops dispatch too.

    On bec2e26 the joining wait only broke its own loop on local cancel; it
    never signalled the owner, so the owner's cancel stayed False and it kept
    dispatching every route.
    """
    lock_path = tmp_path / "refresh.lock"
    marker_path = tmp_path / "refresh.json"
    rows = tuple(
        RowInput(f"cc/{i}", "cc", "STALE", CAPACITY_FREE, True, rank_hint=i) for i in range(6)
    )
    owner_calls: list[tuple[str, str]] = []
    call_lock = threading.Lock()

    def owner_transport(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        with call_lock:
            owner_calls.append((route_id, phase))
        time.sleep(0.05)  # keep the job in flight so the joiner can cancel it
        return _ok(route_id, phase)

    owner = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=owner_transport,
        clock=lambda: NOW,
        monotonic=time.monotonic,
        sleep=time.sleep,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    result: dict[str, object] = {}

    def run_owner() -> None:
        result["owner"] = owner.refresh_for_consumer(
            RefreshSnapshot(rows, generation="gen1"),
            consumer="verified_view",
            needed_ids=[r.route_id for r in rows],
            config=RefreshConfig(wall_seconds=30.0),
        )

    thread = threading.Thread(target=run_owner)
    thread.start()
    time.sleep(0.08)  # let the owner acquire the lock and start probing

    def forbid(route_id: str, phase: str, timeout: float) -> ProbeExchange:
        raise AssertionError("a joined consumer must never probe itself")

    joiner = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json"),
        transport=forbid,
        clock=lambda: NOW,
        monotonic=time.monotonic,
        sleep=time.sleep,
        lock_path=lock_path,
        marker_path=marker_path,
    )
    progress: list[int] = []
    out = joiner.refresh_for_consumer(
        RefreshSnapshot(rows, generation="gen1"),
        consumer="picker",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(wall_seconds=30.0),
        explicit=True,
        on_progress=lambda e: progress.append(e.probed),
        cancel=lambda: True,  # cancel immediately on join
    )
    thread.join(timeout=30)

    owner_outcome = result["owner"]
    assert out.outcome == OUTCOME_CANCELLED
    assert out.complete is False
    # The shared owner was cancelled and stopped before dispatching all routes.
    assert owner_outcome.outcome == OUTCOME_CANCELLED  # type: ignore[union-attr]
    assert owner_outcome.probed < 6  # type: ignore[union-attr]
    # The joined consumer saw at least one live progress tick (shared progress).
    assert len(progress) >= 1


def test_finding6_still_running_marker_not_reported_complete(tmp_path: Path) -> None:
    """A deadline that fires while the owner is still running is NOT complete.

    On bec2e26 a joined wait that expired against a still-running marker that
    already *covered* the needed id reported JOINED with ``complete=True`` (the
    marker's covered rows all had ``refresh_reason is None``), so a consumer
    would treat the row as a finished, trustworthy result even though the owner
    had NOT finished the job. The fix reports ``complete=False`` whenever the
    wait ends while the owner is still running.
    """
    lock_path = tmp_path / "refresh.lock"
    marker_path = tmp_path / "refresh.json"
    # A RUNNING marker that already covers cc/a as probed+verified. The job is
    # NOT finished (``running: true``); only one of its routes has a result so
    # far. Hold the lock live so the owner looks alive the whole wait.
    marker_path.write_text(
        '{"job_id": "j1", "running": true, "ids": ["cc/a"], "sequence": 3, '
        '"outcomes": {"cc/a": {"provider": "cc", "probed": true, "verified": true, '
        '"category": "ok", "http_status": 200, "refresh_reason": null}}}\n',
        encoding="utf-8",
    )
    import fcntl

    # An ADVANCING fake monotonic: each injected sleep moves it forward so the
    # bounded wait terminates on its deadline, never busy-spinning.
    clock_state = {"t": 0.0}

    def fake_monotonic() -> float:
        return clock_state["t"]

    def fake_sleep(seconds: float) -> None:
        clock_state["t"] += max(seconds, 0.01)

    held = lock_path.open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
    try:
        joiner = RefreshCoordinator(
            cache=HealthCache(tmp_path / "health-cache.json"),
            transport=_Transport(),
            clock=lambda: NOW,
            monotonic=fake_monotonic,
            sleep=fake_sleep,
            lock_path=lock_path,
            marker_path=marker_path,
        )
        out = joiner.refresh_for_consumer(
            RefreshSnapshot(
                (RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True),), generation="gen1"
            ),
            consumer="picker",
            needed_ids=["cc/a"],
            config=RefreshConfig(wall_seconds=0.2),
            explicit=True,
        )
    finally:
        fcntl.flock(held.fileno(), fcntl.LOCK_UN)
        held.close()
    # The owner never marked the job finished, so this join is NOT complete even
    # though the marker already carried a verified row for cc/a. A consumer must
    # not treat the still-in-flight result as a trustworthy finished proof.
    assert out.complete is False
    assert out.cap_reason == "lock_timeout"


# ---------------------------------------------------------------------------
# Controller-upgraded BLOCKING: per-provider token reservations are a persisted
# spend boundary, enforced across sequential same-provider routes.
# ---------------------------------------------------------------------------


def test_token_reservations_persist_and_cap_same_provider_spend(tmp_path: Path) -> None:
    """8 same-provider routes with a bucket of 10 dispatch at most 10 requests.

    On bec2e26 reservations were in-memory ``consume`` calls that the next
    ``merge_and_save`` disk reload dropped, so every route saw a full bucket and
    16 calls went out while the persisted bucket tracked only the last write.
    """
    transport = _Transport()
    coord = RefreshCoordinator(
        cache=HealthCache(tmp_path / "health-cache.json", bucket_capacity=10),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "r.lock",
        marker_path=tmp_path / "r.json",
    )
    rows = tuple(RowInput(f"cc/{i}", "cc", "STALE", CAPACITY_FREE, True) for i in range(8))
    out = coord.refresh_for_consumer(
        RefreshSnapshot(rows, generation="gen1"),
        consumer="verified_view",
        needed_ids=[r.route_id for r in rows],
        config=RefreshConfig(),
    )
    # Two requests per full probe, bucket 10 => at most 5 probes / 10 calls.
    assert len(transport.calls) <= 10
    assert out.requests_made <= 10
    assert out.probed <= 5
    # The remaining routes are capped by the bucket, not silently over-spent.
    bucket_rows = [o for o in out.route_outcomes.values() if o.refresh_reason == "bucket"]
    assert bucket_rows
    persisted = HealthCache(tmp_path / "health-cache.json", bucket_capacity=10)
    assert persisted.bucket_remaining("cc", NOW) == 0  # the spend was persisted


def test_consented_unit1_confirmation_rows_and_auto_gate(tmp_path: Path) -> None:
    """Real unit-1 metered/unknown rows need consent despite refreshable=False."""
    rows = [
        {
            "route_id": rid,
            "provider": rid.split("/")[0],
            "status": status,
            "capacity_class": capacity,
            "refreshable": False,
            "refresh_reason": "requires_confirmation",
        }
        for rid, status, capacity in (
            ("paid/a", "STALE", CAPACITY_METERED),
            ("other/b", "UNVERIFIED", CAPACITY_UNKNOWN),
        )
    ]
    snap = RefreshSnapshot(
        rows=tuple(RowInput(**r) for r in rows), generation="gen1", gateway_origin="http://g"
    )
    plan = action_models_refresh_plan(
        snapshot_rows=rows,
        needed_ids=[r["route_id"] for r in rows],
        include_metered=True,
        now=NOW,
        gateway_origin="http://g",
        evidence_generation="gen1",
    ).data
    auto_transport = _Transport()
    auto = _coord(tmp_path, auto_transport).refresh_for_consumer(
        snap, consumer="picker", needed_ids=[r["route_id"] for r in rows], config=RefreshConfig()
    )
    assert auto.requests_made == 0 and auto_transport.calls == []
    transport = _Transport()
    result = action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=snap,
        now=NOW,
        cache=HealthCache(tmp_path / "consent-cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "consent.lock",
        marker_path=tmp_path / "consent.json",
    )
    assert result.ok and result.data["probed"] == 2
    assert result.data["requests_made"] == 2
    assert transport.calls == [("paid/a", "chat"), ("other/b", "chat")]
    outcomes = result.data["route_outcomes"]
    assert set(outcomes) == {"paid/a", "other/b"}
    for rid in outcomes:
        assert outcomes[rid] == {
            "status_after": "VERIFIED",
            "probed": True,
            "category": "ok",
            "http_status": 200,
            "refresh_reason": None,
            "requests_made": 1,
        }


def test_consented_blocked_row_never_probed(tmp_path: Path) -> None:
    row = RowInput("paid/a", "paid", "STALE", CAPACITY_METERED, False, "blocked")
    transport = _Transport()
    out = _coord(tmp_path, transport).refresh_for_consumer(
        RefreshSnapshot((row,), generation="gen1"),
        consumer="manual",
        needed_ids=[row.route_id],
        config=RefreshConfig(),
        authorized=True,
    )
    assert out.requests_made == 0 and transport.calls == []


def test_changed_confirmation_plan_is_refused_without_probe(tmp_path: Path) -> None:
    row = {
        "route_id": "paid/a",
        "provider": "paid",
        "status": "STALE",
        "capacity_class": CAPACITY_METERED,
        "refreshable": False,
        "refresh_reason": "requires_confirmation",
    }
    plan = action_models_refresh_plan(
        snapshot_rows=[row],
        needed_ids=["paid/a"],
        include_metered=True,
        now=NOW,
        gateway_origin="http://g",
        evidence_generation="gen1",
    ).data
    plan["routes"][0]["route_id"] = "paid/changed"
    transport = _Transport()
    result = action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=RefreshSnapshot((RowInput(**row),), "gen1", "http://g"),
        now=NOW,
        transport=transport,
    )
    assert not result.ok and result.data["reason"] == "digest_mismatch"
    assert transport.calls == []


def test_probe_model_list_rejects_option_like_ids() -> None:
    import pytest

    from verdict.tui_verified_controls import parse_probe_model_list

    for value in ("--probe", "cc/a, -force"):
        with pytest.raises(ValueError):
            parse_probe_model_list(value)


def test_execute_route_outcomes_report_blocked_and_missing_without_verified(tmp_path: Path) -> None:
    rows = [
        {
            "route_id": rid,
            "provider": "cc",
            "status": "STALE",
            "capacity_class": CAPACITY_FREE,
            "refreshable": True,
        }
        for rid in ("cc/blocked", "cc/missing")
    ]
    plan = action_models_refresh_plan(
        snapshot_rows=rows,
        needed_ids=[r["route_id"] for r in rows],
        now=NOW,
        gateway_origin="http://g",
        evidence_generation="gen1",
    ).data
    blocked = RowInput("cc/blocked", "cc", "STALE", CAPACITY_FREE, False, "blocked")
    transport = _Transport()
    result = action_models_refresh_execute(
        confirmed=True,
        plan=plan,
        snapshot=RefreshSnapshot((blocked,), "gen1", "http://g"),
        now=NOW,
        cache=HealthCache(tmp_path / "cache.json"),
        transport=transport,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
        lock_path=tmp_path / "lock",
        marker_path=tmp_path / "marker",
    )
    assert result.ok and transport.calls == []
    outcomes = result.data["route_outcomes"]
    assert set(outcomes) == {"cc/blocked", "cc/missing"}
    assert all(not o["probed"] and o["status_after"] != "VERIFIED" for o in outcomes.values())
    assert all(o["requests_made"] == 0 for o in outcomes.values())
    assert outcomes["cc/blocked"]["refresh_reason"] == "blocked"
    assert outcomes["cc/missing"]["refresh_reason"] == "not_tested"


def test_review3_empty_bindings_refused(tmp_path: Path) -> None:
    for generation, gateway, reason in (
        ("", "http://planned", "evidence_changed"),
        ("gen1", "", "gateway_changed"),
    ):
        transport = _Transport()
        snapshot = RefreshSnapshot(
            rows=(RowInput("free/a", "free", "STALE", CAPACITY_FREE, True),),
            generation=generation,
            gateway_origin=gateway,
        )
        result = action_models_refresh_execute(
            confirmed=True,
            plan=_plan(gen=generation, gateway=gateway),
            snapshot=snapshot,
            now=NOW,
            transport=transport,
            cache=HealthCache(tmp_path / "cache.json"),
        )
        assert not result.ok
        assert result.data["reason"] == reason
        assert transport.calls == []


def test_review3_scope_requires_exact_route_name() -> None:
    from verdict.prove_at_rest import FullProbeOutcome

    cases = (
        (403, "permission", "Your account does not have access to models on this provider.", False),
        (429, "rate_limited", "Your account has reached per model usage limit for all models on this provider.", False),
        (403, "permission", "No access to model alpha", True),
        (429, "rate_limited", "Per model usage limit for free/alpha", True),
        (403, "permission", "No access to model alphabet", False),
        (403, "permission", "No access to model beta", False),
        (403, "permission", "No access to this model", False),
        (429, "rate_limited", "Per model alpha limit for this provider", False),
        (403, "permission", "Organization blocked model alpha", False),
        (429, "rate_limited", "Plan limit for alpha", False),
        (429, "rate_limited", "All models including alpha exceeded quota", False),
        (500, "upstream", "Model alpha unavailable", False),
        (403, "permission", "", False),
    )
    for status, category, detail, expected in cases:
        outcome = FullProbeOutcome("free/alpha", None, 1, False, False, "", category,
                                   http_status=status, detail=detail)
        assert outcome.model_scoped is expected, detail


def test_review3_frozen_join_has_poll_cap(tmp_path: Path) -> None:
    import fcntl

    coord = _coord(tmp_path, _Transport())
    with (tmp_path / "refresh.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = coord.refresh_for_consumer(
            RefreshSnapshot((RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True),)),
            consumer="picker", needed_ids=["cc/a"], config=RefreshConfig(wall_seconds=0.01),
        )
    assert result.outcome == "lock_timeout"
    assert not result.complete


def test_review3_cache_locks_have_deadline_and_poll_cap(tmp_path: Path) -> None:
    import fcntl
    import pytest
    from verdict.orchestration.health_cache import HealthCacheLockTimeout, LOCK_POLL_CAP

    cache = HealthCache(tmp_path / "cache.json")
    calls: list[float] = []
    with (tmp_path / "cache.json.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        for save in (False, True):
            calls.clear()
            with pytest.raises(HealthCacheLockTimeout, match="lock_timeout"):
                if save:
                    cache.save(deadline=1, monotonic=lambda: 0.0, sleep=calls.append)
                else:
                    cache.merge_and_save(lambda _: None, deadline=1,
                                         monotonic=lambda: 0.0, sleep=calls.append)
            assert len(calls) < LOCK_POLL_CAP
        with pytest.raises(HealthCacheLockTimeout):
            cache.save(deadline=0, monotonic=lambda: 0.0, sleep=calls.append)


def test_review3_cache_lock_timeout_stops_dispatch(tmp_path: Path) -> None:
    import fcntl

    transport = _Transport()
    coord = _coord(tmp_path, transport)
    with (tmp_path / "health-cache.json.lock").open("a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = coord.refresh_for_consumer(
            RefreshSnapshot((RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True),)),
            consumer="picker", needed_ids=["cc/a"], config=RefreshConfig(),
        )
    assert result.outcome == "lock_timeout"
    assert not result.complete
    assert transport.calls == []


def test_review3_cancel_is_job_keyed_before_publication(tmp_path: Path) -> None:
    from unittest.mock import patch
    from verdict.orchestration import verified_refresh as module

    transport = _Transport()
    coord = _coord(tmp_path, transport)
    original = module._Marker.write

    def publish(marker: module._Marker, payload: Mapping[str, Any]) -> None:
        original(marker, payload)
        if payload.get("running"):
            coord._request_shared_cancel(str(payload["job_id"]))

    coord._request_shared_cancel("old-job")
    with patch.object(module._Marker, "write", publish):
        result = coord.refresh_for_consumer(
            RefreshSnapshot((RowInput("cc/a", "cc", "STALE", CAPACITY_FREE, True),)),
            consumer="picker", needed_ids=["cc/a"], config=RefreshConfig(), job_id="new-job",
        )
    assert result.outcome == "cancelled"
    assert transport.calls == []
    assert coord._shared_cancel_requested("old-job")
    assert not coord._shared_cancel_requested("new-job")


def test_review3_equal_time_keeps_disk_negative(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    stale = HealthCache(path)
    stale.record("cc/a", ProbeResult("ok", True, True), NOW)
    stale.save()
    fresh = HealthCache(path)
    fresh.merge_and_save(lambda cache: cache.record("cc/a", ProbeResult("permission", False, False), NOW))
    stale.save()
    entry = HealthCache(path).entry("cc/a")
    assert entry is not None and entry.category == "permission"


def test_review3_bucket_merge_preserves_newer_reservations(tmp_path: Path) -> None:
    from datetime import timedelta

    path = tmp_path / "cache.json"
    stale = HealthCache(path, bucket_capacity=10)
    assert stale.consume("cc", NOW - timedelta(seconds=120), amount=10)
    stale.save()
    fresh = HealthCache(path, bucket_capacity=10)
    reservation = fresh.reserve_bucket("cc", NOW, amount=2)
    assert reservation is not None
    stale.save()
    disk = HealthCache(path, bucket_capacity=10).bucket_for("cc")
    assert set(reservation.token_ids) <= set(disk.token_ids)
    assert disk.remaining(NOW) == 8


def test_review3_bucket_release_is_owned_and_idempotent(tmp_path: Path) -> None:
    from datetime import timedelta

    path = tmp_path / "cache.json"
    a = HealthCache(path, bucket_capacity=10)
    b = HealthCache(path, bucket_capacity=10)
    handle_a = a.reserve_bucket("cc", NOW, amount=2)
    handle_b = b.reserve_bucket("cc", NOW + timedelta(seconds=30))
    assert handle_a is not None and handle_b is not None
    a.release_bucket(handle_a, 1)
    disk = HealthCache(path).bucket_for("cc")
    assert set(handle_b.token_ids) <= set(disk.token_ids)
    assert len(disk.timestamps) == 2
    a.release_bucket(handle_a, 1)
    a.save()  # stale reservation must not resurrect a released token
    disk = HealthCache(path).bucket_for("cc")
    assert len(disk.timestamps) == 2
    assert disk.remaining(NOW + timedelta(seconds=61)) == 9


def test_review3_revision_rejects_stale_future_positive(tmp_path: Path) -> None:
    from datetime import timedelta

    path = tmp_path / "cache.json"
    owner = HealthCache(path)
    owner.record("cc/a", ProbeResult("ok", True, True), NOW)
    owner.save()
    stale = HealthCache(path)
    owner.merge_and_save(lambda cache: cache.record("cc/a", ProbeResult("permission", False, False), NOW))
    stale.record("cc/a", ProbeResult("ok", True, True), NOW + timedelta(seconds=1))
    stale.save()
    entry = HealthCache(path).entry("cc/a")
    assert entry is not None and entry.category == "permission"
    assert entry.write_revision == 2
