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


def test_gate_requires_fresh_passport_and_confirm() -> None:
    base = admit_free_tier_active(_snapshot())
    assert "openrouter/nvidia/nemotron-3-nano-30b-a3b:free" in base.admitted

    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity)},
        confirm_transport=_ok_transport(),
        now=NOW,
        live=False,
        consented=False,
    )
    assert gated.chosen == identity
    assert gated.empty_intersection is False
    assert any(row.fresh for row in gated.passport)
    assert any(row.confirmed for row in gated.confirm)
    payload = gated.to_dict()
    assert payload.get("passport")
    assert payload.get("confirm")
    assert any(row["identity_id"] == identity and row["fresh"] for row in payload["passport"])
    assert any(row["identity_id"] == identity and row["confirmed"] for row in payload["confirm"])


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
    gated = gate_admit_prove_confirm(
        base,
        passports={identity: _passport(identity), other: _passport(other)},
        confirm_transport=_ok_transport(fail={identity, other}),
        now=NOW,
    )
    assert gated.chosen is None
    reasons = {item.reason for item in gated.exclusions}
    assert REASON_CONFIRM_FAILED in reasons
    assert all(not row.confirmed for row in gated.confirm)


def test_gate_confirm_unavailable_fail_closed() -> None:
    base = admit_free_tier_active(_snapshot())
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    gated = gate_admit_prove_confirm(
        base,
        passports={
            identity: _passport(identity),
            "opencode/hy3-free": _passport("opencode/hy3-free"),
        },
        confirm_transport=None,
        now=NOW,
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


# --- BOD-112: authoritative free-first confirm shortlist --------------------


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
                {"id": paid, "owned_by": "anthropic"},
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
        passports={free_plain: _passport(free_plain), paid: _passport(paid)},
        confirm_transport=spy,
        now=NOW,
        max_confirm_candidates=1,
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
        passports={free_plain: _passport(free_plain), paid: _passport(paid)},
        confirm_transport=spy,
        now=NOW,
        max_confirm_candidates=1,
    )
    assert probed == [paid], "sanity: the fix is the authoritative free_admitted set"
