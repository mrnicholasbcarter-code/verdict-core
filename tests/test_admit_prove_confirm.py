"""Fixture tests: free∩active ∩ fresh passport ∩ budgeted confirm admit gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from verdict.admit_prove_confirm import (
    REASON_CONFIRM_FAILED,
    REASON_CONFIRM_UNAVAILABLE,
    REASON_NO_PASSPORT,
    REASON_PASSPORT_STALE,
    gate_admit_prove_confirm,
    passport_is_fresh,
)
from verdict.free_tier_admit import admit_free_tier_active, snapshot_from_payloads
from verdict.model_passports import ModelPassport
from verdict.prove_at_rest import (
    STATUS_HEALTHY,
    ProofResult,
    ProveAtRestCycle,
    ProveAtRestStore,
    load_healthy_passports,
)

NOW = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)


def _snapshot():
    return snapshot_from_payloads(
        catalog={
            "data": [
                {"id": "openrouter/nvidia/nemotron-3-nano-30b-a3b:free", "owned_by": "openrouter"},
                {"id": "opencode/hy3-free", "owned_by": "opencode"},
                {"id": "anthropic/claude-3-opus-20240229", "owned_by": "claude"},
            ]
        },
        free_tier={
            "perModel": [
                {
                    "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                    "provider": "openrouter",
                    "freeType": "recurring-daily",
                },
                {"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"},
            ]
        },
        providers={
            "connections": [
                {"provider": "openrouter", "isActive": True, "testStatus": "active"},
                {"provider": "opencode", "isActive": True, "testStatus": "active"},
            ]
        },
    )


def _passport(
    identity_id: str, *, expires_at: datetime | None = None, qualified_at: datetime | None = None
) -> ModelPassport:
    provider = identity_id.split("/", 1)[0]
    qualified = qualified_at or (NOW - timedelta(minutes=1))
    expires = expires_at or (NOW + timedelta(minutes=10))
    return ModelPassport(
        provider=provider,
        model_id=identity_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=expires,
    )


def _ok_transport(fail: set[str] | None = None):
    failed = fail or set()

    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        if model_id in failed:
            return {"status_code": 401, "body": {"error": {"message": "expired oauth"}}}
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def test_passport_is_fresh_respects_expiry() -> None:
    fresh = _passport("openrouter/nvidia/nemotron-3-nano-30b-a3b:free")
    stale = _passport(
        "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
        qualified_at=NOW - timedelta(minutes=30),
        expires_at=NOW - timedelta(minutes=1),
    )
    assert passport_is_fresh(fresh, now=NOW) is True
    assert passport_is_fresh(stale, now=NOW) is False


def test_gate_reuses_fresh_passport_without_confirm_probes() -> None:
    """BOD-350: one eligible fresh stored passport is sufficient; no confirm probes."""
    base = admit_free_tier_active(_snapshot())
    assert "openrouter/nvidia/nemotron-3-nano-30b-a3b:free" in base.admitted

    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    probed: list[str] = []

    def spy(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        probed.append(model_id)
        return _ok_transport()(model_id, payload, timeout)

    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity)},
        confirm_transport=spy,
        now=NOW,
        live=False,
        consented=False,
    )
    assert gated.chosen == identity
    assert gated.empty_intersection is False
    assert any(row.fresh for row in gated.passport)
    assert probed == []
    assert gated.confirm == ()
    payload = gated.to_dict()
    assert payload.get("passport")
    assert not payload.get("confirm")
    assert any(row["identity_id"] == identity and row["fresh"] for row in payload["passport"])


def test_gate_named_drop_no_passport_fail_closed() -> None:
    base = admit_free_tier_active(_snapshot())
    gated = gate_admit_prove_confirm(base, passports={}, confirm_transport=_ok_transport(), now=NOW)
    assert gated.chosen is None
    assert gated.empty_intersection is True
    reasons = {item.reason for item in gated.exclusions}
    assert REASON_NO_PASSPORT in reasons
    assert all(not row.fresh for row in gated.passport)


def test_live_consent_refreshes_expired_passport_inside_confirm_budget() -> None:
    """Operational TTL expiry must not empty the pool when a bounded confirm works."""
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    other = "opencode/hy3-free"
    expired = dict(qualified_at=NOW - timedelta(minutes=30), expires_at=NOW - timedelta(minutes=5))
    probed: list[str] = []

    def spy(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        probed.append(model_id)
        return _ok_transport()(model_id, payload, timeout)

    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity, **expired), other: _passport(other, **expired)},
        confirm_transport=spy,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=1,
    )
    assert probed == [identity]
    assert gated.chosen == identity
    refreshed = next(row for row in gated.passport if row.identity_id == identity)
    assert refreshed.fresh is True
    named = {item.model_id: item.reason for item in gated.exclusions}
    assert named[other] == "confirm_budget_exhausted"

    denied = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity, **expired)},
        confirm_transport=spy,
        now=NOW,
        live=False,
        consented=False,
    )
    assert denied.chosen is None
    assert REASON_PASSPORT_STALE in {item.reason for item in denied.exclusions}


def test_confirm_refresh_persists_when_store_path_is_configured(tmp_path: Path) -> None:
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    path = tmp_path / "state.json"
    expired = dict(qualified_at=NOW - timedelta(minutes=30), expires_at=NOW - timedelta(minutes=5))
    ProveAtRestStore(path=path).write(
        ProveAtRestCycle(
            cycle_id="cycle-1",
            started_at=NOW - timedelta(minutes=30),
            finished_at=NOW - timedelta(minutes=29),
            results=(
                ProofResult(
                    identity_id=identity,
                    provider="openrouter",
                    status="healthy",
                    proved_at=NOW - timedelta(minutes=30),
                    passport=_passport(identity, **expired),
                ),
            ),
        )
    )

    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity, **expired)},
        passport_store_path=path,
        confirm_transport=_ok_transport(),
        now=NOW,
        live=True,
        consented=True,
    )

    assert gated.chosen == identity
    stored = load_healthy_passports(path)[identity]
    assert stored.expires_at > NOW


def test_gate_named_drop_passport_stale() -> None:
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    gated = gate_admit_prove_confirm(
        base,
        passports={
            identity: _passport(
                identity,
                qualified_at=NOW - timedelta(minutes=30),
                expires_at=NOW - timedelta(minutes=5),
            ),
            "opencode/hy3-free": _passport(
                "opencode/hy3-free",
                qualified_at=NOW - timedelta(minutes=30),
                expires_at=NOW - timedelta(minutes=5),
            ),
        },
        confirm_transport=_ok_transport(),
        now=NOW,
    )
    assert gated.chosen is None
    reasons = {item.reason for item in gated.exclusions}
    assert REASON_PASSPORT_STALE in reasons


def test_gate_named_drop_confirm_failed() -> None:
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    other = "opencode/hy3-free"
    expired = dict(qualified_at=NOW - timedelta(minutes=30), expires_at=NOW - timedelta(minutes=5))
    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity, **expired), other: _passport(other, **expired)},
        confirm_transport=_ok_transport(fail={identity, other}),
        now=NOW,
        live=True,
        consented=True,
    )
    assert gated.chosen is None
    reasons = {item.reason for item in gated.exclusions}
    assert REASON_CONFIRM_FAILED in reasons
    assert all(not row.confirmed for row in gated.confirm)


def test_gate_confirm_unavailable_fail_closed() -> None:
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    other = "opencode/hy3-free"
    expired = dict(qualified_at=NOW - timedelta(minutes=30), expires_at=NOW - timedelta(minutes=5))
    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity, **expired), other: _passport(other, **expired)},
        confirm_transport=None,
        now=NOW,
        live=True,
        consented=True,
    )
    assert gated.chosen is None
    reasons = {item.reason for item in gated.exclusions}
    assert REASON_CONFIRM_UNAVAILABLE in reasons


def test_gate_loads_passports_from_prove_at_rest_store(tmp_path: Path) -> None:
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    passport = _passport(identity)
    cycle = ProveAtRestCycle(
        cycle_id="cycle-1",
        started_at=NOW,
        finished_at=NOW,
        results=(
            ProofResult(
                identity_id=identity,
                provider="openrouter",
                status=STATUS_HEALTHY,
                proved_at=NOW,
                passport=passport,
            ),
        ),
        admitted=(identity,),
        active_providers=("openrouter",),
        free_tier_providers=("openrouter",),
    )
    store_path = tmp_path / "state.json"
    ProveAtRestStore(path=store_path).write(cycle)
    assert identity in load_healthy_passports(store_path)

    base = admit_free_tier_active(_snapshot())
    # Snapshot also admits opencode/hy3-free — no passport → dropped.
    gated = gate_admit_prove_confirm(
        base, passport_store_path=store_path, confirm_transport=_ok_transport(), now=NOW
    )
    assert gated.chosen == identity
    assert "opencode/hy3-free" not in gated.admitted
    drop_reasons = {
        item.reason for item in gated.exclusions if item.model_id == "opencode/hy3-free"
    }
    assert REASON_NO_PASSPORT in drop_reasons


# --- authoritative free-first confirm shortlist --------------------


def test_free_identity_without_free_suffix_is_confirmed_before_paid() -> None:
    """A free model named without ``:free``/``-free`` must not lose the shortlist to paid."""
    from dataclasses import replace

    from verdict.free_tier_admit import expand_admit_for_worthiness

    free_plain = "opencode/hy3"  # free per OmniRoute free-tier summary, no suffix in the ID
    paid = "anthropic/claude-3-opus-20240229"
    snapshot = snapshot_from_payloads(
        catalog={
            "data": [
                {"id": free_plain, "owned_by": "opencode"},
                {"id": paid, "owned_by": "anthropic", "pricing": {"input": 10.0, "output": 20.0}},
            ]
        },
        free_tier={"perModel": [{"modelId": "hy3", "provider": "opencode", "freeType": "keyless"}]},
        providers={
            "connections": [
                {"provider": "opencode", "isActive": True, "testStatus": "active"},
                {"provider": "anthropic", "isActive": True, "testStatus": "active"},
            ]
        },
    )
    base = admit_free_tier_active(snapshot)
    assert base.free_admitted == (free_plain,)
    ordinary = expand_admit_for_worthiness(
        base, snapshot, task_class="ordinary", class_reasons=("test",)
    )
    assert set(ordinary.admitted) == {free_plain, paid}

    probed: list[str] = []

    def spy(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        probed.append(model_id)
        return _ok_transport()(model_id, payload, timeout)

    gated = gate_admit_prove_confirm(
        ordinary,
        passports={
            identity: _passport(
                identity,
                qualified_at=NOW - timedelta(minutes=30),
                expires_at=NOW - timedelta(minutes=1),
            )
            for identity in (free_plain, paid)
        },
        confirm_transport=spy,
        now=NOW,
        max_confirm_candidates=1,
        live=True,
        consented=True,
    )
    assert probed == [free_plain], "free candidate must be confirmed before any paid fallback"
    assert gated.chosen == free_plain
    named = {item.model_id: item.reason for item in gated.exclusions}
    assert named[paid] == "confirm_budget_exhausted"

    # Name heuristics alone would have put the alphabetically-earlier paid model first.
    heuristic_only = replace(ordinary, free_admitted=())
    probed.clear()
    gate_admit_prove_confirm(
        heuristic_only,
        passports={
            identity: _passport(
                identity,
                qualified_at=NOW - timedelta(minutes=30),
                expires_at=NOW - timedelta(minutes=1),
            )
            for identity in (free_plain, paid)
        },
        confirm_transport=spy,
        now=NOW,
        max_confirm_candidates=1,
        live=True,
        consented=True,
    )
    assert probed == [paid], "sanity: the fix is the authoritative free_admitted set"


# --- no-passport candidates reach live confirm probe (BOD-349) --------


def test_no_passport_candidate_admitted_via_live_confirm() -> None:
    """A candidate with NO existing passport must reach the budgeted confirm probe.

    When live=True, consented=True, and confirm_transport succeeds, a candidate
    with zero prior passport must survive gate_admit_prove_confirm and appear in
    the returned receipt.admitted — not be dropped as no_passport.
    """
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert identity in base.admitted

    probed: list[str] = []

    def spy(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        probed.append(model_id)
        return _ok_transport()(model_id, payload, timeout)

    # passports={} -> identity has NO passport at all
    gated = gate_admit_prove_confirm(
        base, passports={}, confirm_transport=spy, now=NOW, live=True, consented=True
    )
    assert identity in probed, "no-passport candidate must be probed by budgeted confirm"
    assert identity in gated.admitted, "successful confirm must admit the no-passport candidate"
    assert gated.chosen is not None
    assert gated.empty_intersection is False
    # The confirm evidence must show confirmed
    confirmed_ids = {row.identity_id for row in gated.confirm if row.confirmed}
    assert identity in confirmed_ids


def test_no_passport_candidate_excluded_when_confirm_fails() -> None:
    """Fail-closed: a no-passport candidate whose confirm probe fails is excluded."""
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"

    gated = gate_admit_prove_confirm(
        base,
        passports={},
        confirm_transport=_ok_transport(fail={identity, "opencode/hy3-free"}),
        now=NOW,
        live=True,
        consented=True,
    )
    assert identity not in gated.admitted
    assert gated.chosen is None
    assert gated.empty_intersection is True
    reasons = {item.reason for item in gated.exclusions}
    assert REASON_CONFIRM_FAILED in reasons


def test_failed_confirm_shortlist_reaches_next_candidate_with_same_token_budget():
    from dataclasses import replace

    from verdict.probes import ProbeBudget

    base = admit_free_tier_active(_snapshot())
    ids = ("kiro/a-mini", "kiro/b-mini", "kiro/c-mini", "mistral/codestral-latest")
    base = replace(base, admitted=ids, free_admitted=ids)
    calls = []

    def transport(model, payload, timeout):
        calls.append(model)
        assert payload["max_tokens"] == 1
        if len(calls) < 3:
            return {"status_code": [429, 499][len(calls) - 1], "body": {"error": "busy"}}
        if len(calls) == 3:
            return {"status_code": 200, "body": {"choices": []}}
        return _ok_transport()(model, payload, timeout)

    result = gate_admit_prove_confirm(
        base,
        passports={},
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=4,
        confirm_budget=ProbeBudget(provider="admit_confirm", max_requests=4, max_tokens=384),
    )
    assert calls == list(ids)
    assert result.chosen == ids[-1]
    assert {d.model_id for d in result.exclusions if d.reason == REASON_CONFIRM_FAILED} == set(
        ids[:3]
    )


def test_confirm_replacements_obey_explicit_request_budget():
    from verdict.probes import ProbeBudget

    base = admit_free_tier_active(_snapshot())
    calls = []

    def transport(model, payload, timeout):
        calls.append(model)
        return {"status_code": 429, "body": {"error": "busy"}}

    result = gate_admit_prove_confirm(
        base,
        passports={},
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        confirm_budget=ProbeBudget(provider="admit_confirm", max_requests=1, max_tokens=128),
    )
    assert len(calls) == 1
    assert result.chosen is None
    assert any(d.reason == "confirm_budget_exhausted" for d in result.exclusions)


def test_confirm_gate_does_not_probe_fresh_passports() -> None:
    from dataclasses import replace

    base = admit_free_tier_active(_snapshot())
    ids = tuple(sorted(base.admitted))
    calls = []

    def transport(model, payload, timeout):
        calls.append(model)
        return _ok_transport()(model, payload, timeout)

    result = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity) for identity in ids},
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=2,
    )
    assert calls == []
    assert result.chosen in ids
    assert result.admitted == ids
    assert result.exclusions == base.exclusions
    assert result.confirm == ()

    # A fresh timestamp does not override revoked authorization.
    unauthorized = replace(_passport(ids[0]), auth_state="unauthorized")
    denied = gate_admit_prove_confirm(
        base,
        passports={ids[0]: unauthorized, ids[1]: _passport(ids[1])},
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=1,
    )
    assert denied.admitted == (ids[1],)
    assert calls == []


def test_confirm_gate_bounds_refresh_probes_when_passports_are_stale(monkeypatch) -> None:
    from dataclasses import replace

    ids = tuple(f"opencode/model-{i}" for i in range(6))
    base = replace(admit_free_tier_active(_snapshot()), admitted=ids, free_admitted=ids)
    expired = dict(qualified_at=NOW - timedelta(minutes=30), expires_at=NOW - timedelta(minutes=5))
    calls = []
    persisted = []

    def transport(model, payload, timeout):
        calls.append(model)
        return _ok_transport(fail={ids[0]})(model, payload, timeout)

    def persist(observations, **kwargs):
        persisted.extend(observations.values())

    monkeypatch.setattr("verdict.admit_prove_confirm._persist_confirm_observations", persist)
    result = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity, **expired) for identity in ids},
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=2,
    )
    assert calls == list(ids[:2])
    assert result.admitted == (ids[1],)
    assert any(
        d.model_id == ids[0] and d.reason == REASON_CONFIRM_FAILED for d in result.exclusions
    )
    assert any(
        d.model_id == ids[-1] and d.reason == REASON_PASSPORT_STALE for d in result.exclusions
    )
    assert {o.model_id for o in persisted} == set(calls)
    assert all(o.status != "skipped" for o in persisted)


def test_expired_health_cache_bounds_sparse_passport_refresh(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from verdict.orchestration.health_cache import HealthCache, ProbeResult

    ids = tuple(f"opencode/model-{i}" for i in range(6))
    base = replace(admit_free_tier_active(_snapshot()), admitted=ids, free_admitted=ids)
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    cache.record(
        ids[0],
        ProbeResult(category="ok", chat_ok=True, tool_ok=True, capacity_evidence="free"),
        NOW,
    )
    cache.save()
    monkeypatch.setattr("verdict.admit_prove_confirm.load_healthy_passports", lambda path: {})
    monkeypatch.setattr(
        "verdict.admit_prove_confirm._persist_confirm_observations", lambda *args, **kwargs: None
    )
    calls = []

    def transport(model, payload, timeout):
        calls.append(model)
        return _ok_transport(fail={ids[0]})(model, payload, timeout)

    fresh = gate_admit_prove_confirm(
        base,
        health_cache_path=path,
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=2,
    )
    assert fresh.admitted == (ids[0],)
    assert calls == []
    expired = gate_admit_prove_confirm(
        base,
        health_cache_path=path,
        confirm_transport=transport,
        now=NOW + timedelta(hours=1),
        live=True,
        consented=True,
        max_confirm_candidates=2,
    )
    assert calls == list(ids[:2])
    assert expired.admitted == (ids[1],)


def test_one_fresh_passport_skips_three_stale_candidates() -> None:
    from dataclasses import replace

    ids = tuple(f"opencode/model-{i}" for i in range(4))
    base = replace(admit_free_tier_active(_snapshot()), admitted=ids, free_admitted=ids)
    stale = dict(qualified_at=NOW - timedelta(minutes=30), expires_at=NOW - timedelta(minutes=1))
    passports = {identity: _passport(identity, **stale) for identity in ids}
    passports[ids[-1]] = _passport(ids[-1])
    calls = []

    def transport(model, payload, timeout):
        calls.append(model)
        return _ok_transport()(model, payload, timeout)

    result = gate_admit_prove_confirm(
        base,
        passports=passports,
        confirm_transport=transport,
        now=NOW,
        live=True,
        consented=True,
        max_confirm_candidates=4,
    )
    assert calls == []
    assert result.admitted == (ids[-1],)
    assert result.chosen == ids[-1]
    assert result.confirm == ()
