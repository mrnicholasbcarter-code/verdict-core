"""Select-time proof production preserves the permit's ten-minute boundary."""

from datetime import timedelta

from tests.test_identify_rank import NOW, _conn, _ladder_row
from verdict.admit_prove_confirm import passport_confirmation
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import (
    PERMIT_PROOF_MAX_VALIDITY_SECONDS,
    HealthCache,
    ProbeResult,
)
from verdict.probes import ProbeObservation
from verdict.prove_at_rest import ProveAtRestStore
from verdict.relay import admission_proof, dispatch_scope
from verdict.subagent_selection import HealthResult


def test_select_confirm_refreshes_stale_proof_and_permit_remains_single_use(tmp_path, monkeypatch):
    store = tmp_path / "passports.json"
    monkeypatch.setenv("VERDICT_PROVE_AT_REST_STATE", str(store))
    # The actual default path is patched, not the operator's state.
    import verdict.prove_at_rest_legacy as legacy

    monkeypatch.setattr(legacy, "default_state_path", lambda: store)
    import verdict.prove_at_rest as prover

    monkeypatch.setattr(prover, "default_state_path", lambda: store)
    old = NOW - timedelta(seconds=PERMIT_PROOF_MAX_VALIDITY_SECONDS + 1)
    ProveAtRestStore(store).record_confirm(
        "cc/model",
        ProbeObservation(
            model_id="cc/model", availability_state="ready", status="ok", observed_at=old
        ),
        now=old,
    )
    cache = HealthCache(tmp_path / "health.json")
    cache.record("cc/model", ProbeResult(category="ok", chat_ok=True, tool_ok=True), old)
    cache.save()
    calls = []
    ladder = EligibilityLadder(
        [_ladder_row("cc/model", pricing={"input": 1})],
        [_conn("cc", auth="oauth")],
        lambda route: calls.append(route) or HealthResult(True, "ok"),
        tmp_path / "state.json",
        health_cache=cache,
        metadata=None,
    )
    selected, _ = ladder.select(TaskRequirements(), now=NOW)
    assert selected is not None
    assert calls == ["cc/model"]
    confirmation = passport_confirmation(["cc/model"], now=NOW, passport_store_path=store)
    scope = dispatch_scope(
        gateway_origin="http://fixture.test",
        provider="cc",
        model="cc/model",
        route_id="cc/model",
        profile_digest="low-risk",
    )
    proof = admission_proof(
        confirmation,
        route_id="cc/model",
        scope=scope,
        authorization_digest=confirmation["receipt_id"],
        provider="cc",
        model="cc/model",
        now=NOW,
    )
    assert proof is not None and proof.qualified_at == NOW
    kwargs = dict(
        route_id="cc/model",
        attempt_id="one",
        authorization_digest=proof.authorization_digest,
        expected_epoch=proof.epoch,
        qualified_at=proof.qualified_at,
        proof_deadline=NOW + timedelta(seconds=600),
        now=NOW,
    )
    assert cache.commit_dispatch(scope, **kwargs) is not None
    assert cache.commit_dispatch(scope, **kwargs) is None
    cache.record_revocation(
        scope, route_id="cc/model", event_id="revoke", source_revision=None, observed_at=NOW
    )
    assert cache.commit_dispatch(scope, **{**kwargs, "attempt_id": "two"}) is None


def test_cold_store_records_real_confirm(tmp_path):
    store = ProveAtRestStore(tmp_path / "passports.json")
    store.record_confirm(
        "cc/model",
        ProbeObservation(
            model_id="cc/model", availability_state="ready", status="ok", observed_at=NOW
        ),
        now=NOW,
    )
    assert passport_confirmation(["cc/model"], now=NOW, passport_store_path=store.path)[
        "admitted"
    ] == ["cc/model"]


def test_negative_during_confirm_is_not_reauthorized(tmp_path, monkeypatch):
    import verdict.prove_at_rest as prover

    store = tmp_path / "passports.json"
    monkeypatch.setattr(prover, "default_state_path", lambda: store)
    from verdict.orchestration.health_cache import default_cache_path

    cache = HealthCache(default_cache_path())

    def probe(route):
        other = HealthCache(default_cache_path())
        other.record(
            route, ProbeResult(category="authentication", chat_ok=False, tool_ok=False), NOW
        )
        other.save()
        return HealthResult(True, "ok")

    ladder = EligibilityLadder(
        [_ladder_row("cc/model", pricing={"input": 1})],
        [_conn("cc", auth="oauth")],
        probe,
        tmp_path / "state.json",
        health_cache=cache,
        metadata=None,
    )
    selected, _ = ladder.select(TaskRequirements(), now=NOW)
    assert selected is None
    assert passport_confirmation(["cc/model"], now=NOW, passport_store_path=store)["admitted"] == []


def test_fresh_confirm_cannot_lift_active_scoped_negative(tmp_path):
    from verdict.orchestration.health_cache import default_cache_path

    cache = HealthCache(default_cache_path())
    scope = dispatch_scope(
        gateway_origin="http://fixture.test",
        provider="cc",
        model="cc/model",
        route_id="cc/model",
        profile_digest="low-risk",
    )
    cache.record_revocation(
        scope,
        route_id="cc/model",
        event_id="active-negative",
        source_revision=None,
        observed_at=NOW - timedelta(seconds=1),
    )
    store = ProveAtRestStore(tmp_path / "passports.json")
    store.record_confirm(
        "cc/model",
        ProbeObservation(
            model_id="cc/model", availability_state="ready", status="ok", observed_at=NOW
        ),
        now=NOW,
    )
    confirmation = passport_confirmation(["cc/model"], now=NOW, passport_store_path=store.path)
    assert (
        admission_proof(
            confirmation,
            route_id="cc/model",
            scope=scope,
            authorization_digest=confirmation["receipt_id"],
            provider="cc",
            model="cc/model",
            now=NOW,
        )
        is None
    )


def test_late_positive_store_write_cannot_replace_concurrent_negative(tmp_path):
    path = tmp_path / "passports.json"
    first, second = ProveAtRestStore(path), ProveAtRestStore(path)
    second.record_confirm(
        "cc/model",
        ProbeObservation(
            model_id="cc/model",
            availability_state="denied",
            status="failed",
            error="auth",
            observed_at=NOW,
        ),
        now=NOW,
    )
    first.record_confirm(
        "cc/model",
        ProbeObservation(
            model_id="cc/model", availability_state="ready", status="ok", observed_at=NOW
        ),
        now=NOW,
    )
    assert passport_confirmation(["cc/model"], now=NOW, passport_store_path=path)["admitted"] == []


def _observation_scope(route="cc/model"):
    return dispatch_scope(
        gateway_origin="http://fixture.test",
        provider="cc",
        model=route,
        route_id=route,
        profile_digest="low-risk",
    )


def _proof_for(store, route, at):
    confirmation = passport_confirmation([route], now=at, passport_store_path=store)
    return admission_proof(
        confirmation,
        route_id=route,
        scope=_observation_scope(route),
        authorization_digest=confirmation["receipt_id"],
        provider="cc",
        model=route,
        now=at,
    )


def _commit_observed(cache, proof, at, attempt):
    return cache.commit_dispatch(
        proof.scope,
        route_id=proof.route_id,
        attempt_id=attempt,
        authorization_digest=proof.authorization_digest,
        expected_epoch=proof.epoch,
        qualified_at=proof.qualified_at,
        proof_deadline=proof.qualified_at + timedelta(seconds=600),
        now=at,
    )


def test_dispatch_failure_blocks_later_confirm_and_cached_permit(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path

    store = tmp_path / "passports.json"
    before = NOW - timedelta(seconds=1)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=before, passport_store_path=store
    )
    proof = _proof_for(store, "cc/model", before)
    assert proof is not None
    record_dispatch_observation(
        "cc/model", healthy=False, observed_at=NOW, passport_store_path=store
    )
    after = NOW + timedelta(seconds=2)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=after, passport_store_path=store
    )
    assert _proof_for(store, "cc/model", after) is None
    assert _commit_observed(HealthCache(default_cache_path()), proof, after, "old") is None


def test_dispatch_negative_expiry_requires_new_proof_even_after_health_recovery(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path

    store = tmp_path / "passports.json"
    before = NOW - timedelta(seconds=1)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=before, passport_store_path=store
    )
    old_proof = _proof_for(store, "cc/model", before)
    assert old_proof is not None
    record_dispatch_observation(
        "cc/model", healthy=False, observed_at=NOW, passport_store_path=store
    )
    cache = HealthCache(default_cache_path())
    negative = cache.entry("cc/model")
    assert negative is not None and not negative.healthy
    after = negative.until + timedelta(seconds=1)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=after, passport_store_path=store
    )
    proof = _proof_for(store, "cc/model", after)
    assert proof is not None
    assert _commit_observed(cache, old_proof, after, "old-before-recovery") is None
    cache.merge_and_save(
        lambda locked: locked.record(
            "cc/model", ProbeResult(category="ok", chat_ok=True, tool_ok=True), after
        )
    )
    assert _commit_observed(cache, old_proof, after, "old-after-recovery") is None
    assert _commit_observed(cache, proof, after, "fresh") is not None


def test_dispatch_failure_exact_tie_and_sibling_route(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path

    store = tmp_path / "passports.json"
    for route in ("cc/model", "cc/sibling"):
        record_dispatch_observation(route, healthy=True, observed_at=NOW, passport_store_path=store)
    old_proof = _proof_for(store, "cc/model", NOW)
    assert old_proof is not None
    record_dispatch_observation(
        "cc/model", healthy=False, observed_at=NOW, passport_store_path=store
    )
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=NOW, passport_store_path=store
    )
    assert _proof_for(store, "cc/model", NOW) is None
    cache = HealthCache(default_cache_path())
    assert _commit_observed(cache, old_proof, NOW, "tie") is None
    sibling = _proof_for(store, "cc/sibling", NOW)
    assert sibling is not None
    assert _commit_observed(cache, sibling, NOW, "sibling") is not None


def test_concurrent_positive_does_not_revoke_captured_proof(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path

    store = tmp_path / "passports.json"
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=NOW, passport_store_path=store
    )
    proof = _proof_for(store, "cc/model", NOW)
    assert proof is not None
    later = NOW + timedelta(seconds=2)
    cache = HealthCache(default_cache_path())
    cache.merge_and_save(
        lambda locked: locked.record(
            "cc/model", ProbeResult(category="ok", chat_ok=True, tool_ok=True), later
        )
    )
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=later, passport_store_path=store
    )
    assert _commit_observed(cache, proof, later, "in-flight") is not None


def test_dispatch_negative_duplicate_does_not_extend_backoff(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path

    cache = HealthCache(default_cache_path())
    cache.merge_and_save(
        lambda locked: locked.record(
            "cc/model", ProbeResult(category="authentication", chat_ok=False, tool_ok=False), NOW
        )
    )
    original = cache.entry("cc/model")
    record_dispatch_observation(
        "cc/model", healthy=False, observed_at=NOW, passport_store_path=tmp_path / "passports.json"
    )
    cache.reload()
    assert cache.entry("cc/model") == original


def test_legacy_negative_rejects_old_proof_after_expiry_and_liveness_recovery(tmp_path):
    import json

    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path

    store = tmp_path / "passports.json"
    before = NOW - timedelta(seconds=1)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=before, passport_store_path=store
    )
    proof = _proof_for(store, "cc/model", before)
    assert proof is not None
    cache = HealthCache(default_cache_path())
    cache.merge_and_save(
        lambda locked: locked.record(
            "cc/model", ProbeResult(category="upstream", chat_ok=False, tool_ok=False), NOW
        )
    )
    payload = json.loads(cache.path.read_text())
    payload["routes"]["cc/model"].pop("last_failure_at", None)
    cache.path.write_text(json.dumps(payload))
    cache = HealthCache(cache.path)
    negative = cache.entry("cc/model")
    assert negative is not None and negative.last_failure_at is None
    after = negative.until + timedelta(seconds=1)
    assert _commit_observed(cache, proof, after, "legacy-old") is None
    cache.merge_and_save(
        lambda locked: locked.record_liveness(
            "cc/model", latency_ms=None, pool=None, capacity_evidence=None, now=after
        )
    )
    assert _commit_observed(cache, proof, after, "legacy-recovered-old") is None


def test_serve_failure_retained_across_positive_recovery(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path
    from verdict.relay import relay_failure_feedback

    store = tmp_path / "passports.json"
    before = NOW - timedelta(seconds=1)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=before, passport_store_path=store
    )
    proof = _proof_for(store, "cc/model", before)
    assert proof is not None
    cache = HealthCache(default_cache_path())
    result = relay_failure_feedback(cache, route_id="cc/model", status_code=503, now=NOW)
    assert result.recorded and result.entry is not None
    assert result.entry.last_failure_at == NOW
    after = result.entry.until + timedelta(seconds=1)
    cache.merge_and_save(
        lambda locked: locked.record(
            "cc/model", ProbeResult(category="ok", chat_ok=True, tool_ok=True), after
        )
    )
    assert _commit_observed(cache, proof, after, "serve-old") is None


def test_failure_write_wins_while_commit_waits_for_lock(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import verdict.orchestration.health_cache as health_module
    from verdict.admit_prove_confirm import record_dispatch_observation

    store = tmp_path / "passports.json"
    before = NOW - timedelta(seconds=1)
    record_dispatch_observation(
        "cc/model", healthy=True, observed_at=before, passport_store_path=store
    )
    proof = _proof_for(store, "cc/model", before)
    assert proof is not None
    cache = HealthCache(health_module.default_cache_path())
    held, release, waiting = threading.Event(), threading.Event(), threading.Event()
    original_acquire = health_module._acquire_cache_lock

    def acquire(*args, **kwargs):
        if held.is_set():
            waiting.set()
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(health_module, "_acquire_cache_lock", acquire)

    def write(locked):
        locked.record(
            "cc/model", ProbeResult(category="upstream", chat_ok=False, tool_ok=False), NOW
        )
        held.set()
        assert release.wait(1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(cache.merge_and_save, write)
        assert held.wait(1)
        commit = pool.submit(_commit_observed, HealthCache(cache.path), proof, NOW, "racing")
        try:
            assert waiting.wait(1)
        finally:
            release.set()
        writer.result(timeout=2)
        assert commit.result(timeout=2) is None


def test_budgeted_confirm_after_dispatch_failure_stays_denied(tmp_path):
    from dataclasses import replace

    from tests.test_admit_prove_confirm import _ok_transport, _snapshot
    from verdict.admit_prove_confirm import gate_admit_prove_confirm, record_dispatch_observation
    from verdict.free_tier_admit import admit_free_tier_active
    from verdict.orchestration.health_cache import default_cache_path

    route = "opencode/hy3-free"
    store = tmp_path / "passports.json"
    receipt = replace(admit_free_tier_active(_snapshot()), admitted=(route,), chosen=route)
    record_dispatch_observation(route, healthy=False, observed_at=NOW, passport_store_path=store)
    later = NOW + timedelta(seconds=2)
    confirmed = gate_admit_prove_confirm(
        receipt,
        passport_store_path=store,
        health_cache_path=default_cache_path(),
        confirm_transport=_ok_transport(),
        now=later,
        live=True,
        consented=True,
    )
    assert confirmed.admitted == ()
    assert _proof_for(store, route, later) is None
    cache = HealthCache(default_cache_path())
    negative = cache.entry(route)
    assert negative is not None
    after = negative.until + timedelta(seconds=1)
    assert _proof_for(store, route, after) is None


def test_dispatch_failure_uses_existing_category_ttl_and_retry_after(tmp_path):
    from verdict.admit_prove_confirm import record_dispatch_observation
    from verdict.orchestration.health_cache import default_cache_path, negative_seconds

    store = tmp_path / "passports.json"
    record_dispatch_observation(
        "cc/model",
        healthy=False,
        observed_at=NOW,
        failure_category="authentication",
        passport_store_path=store,
    )
    record_dispatch_observation(
        "cc/sibling",
        healthy=False,
        observed_at=NOW,
        failure_category="rate_limited",
        retry_after_seconds=90,
        passport_store_path=store,
    )
    cache = HealthCache(default_cache_path())
    entry = cache.entry("cc/model")
    assert entry is not None
    assert entry.category == "authentication"
    assert entry.until == NOW + timedelta(seconds=negative_seconds("authentication"))
    sibling = cache.entry("cc/sibling")
    assert sibling is not None and sibling.until == NOW + timedelta(seconds=90)
