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
