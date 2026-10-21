"""BOD-433/S03 phase 1: dispatch permits — races, dedupe, scope independence.

No network, no provider calls. Every case drives the cache file directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.health_cache import (
    CATEGORY_OK,
    PERMIT_PROOF_MAX_VALIDITY_SECONDS,
    PERMIT_STATE_MAYBE_SENT,
    DispatchPermit,
    DispatchScope,
    HealthCache,
    HealthCacheError,
    ProbeResult,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

SCOPE = DispatchScope(
    gateway_origin="https://gateway.example",
    config_generation="config-1",
    profile_digest="profile-1",
    binding_id="binding-1",
)
ROUTE = "omni/route-a"


def _at(seconds: float) -> datetime:
    return NOW + timedelta(seconds=seconds)


def _commit(
    cache: HealthCache, attempt_id: str, *, expected_epoch: int = 0, now: datetime | None = None
) -> DispatchPermit | None:
    return cache.commit_dispatch(
        SCOPE,
        route_id=ROUTE,
        attempt_id=attempt_id,
        authorization_digest="auth-digest",
        expected_epoch=expected_epoch,
        qualified_at=NOW,
        proof_deadline=_at(60) if now is None else now + timedelta(seconds=60),
        now=NOW if now is None else now,
    )


def test_duplicate_attempt_id_never_obtains_a_second_permit(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    first = _commit(cache, "attempt-dup")
    assert first is not None
    assert _commit(cache, "attempt-dup") is None
    # Even after a revocation raises the epoch, the consumed attempt stays dead.
    cache.record_revocation(
        SCOPE, route_id=ROUTE, event_id="neg-1", source_revision=None, observed_at=NOW
    )
    assert _commit(cache, "attempt-dup", expected_epoch=1) is None
    # A distinct attempt under the new epoch is fine: retries need new permits.
    assert _commit(cache, "attempt-dup-2", expected_epoch=1) is not None


def test_late_fenced_writer_cannot_mint_a_second_permit(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    fenced = HealthCache(path)  # loads the epoch-0 view
    live = HealthCache(path)
    live.record_revocation(
        SCOPE, route_id=ROUTE, event_id="neg-1", source_revision="snap-1", observed_at=NOW
    )
    permit = _commit(live, "attempt-1", expected_epoch=1)
    assert permit is not None
    # The stale writer re-reads disk under the commit lock: both its superseded
    # epoch and the consumed attempt deny it a permit.
    assert _commit(fenced, "attempt-1", expected_epoch=0) is None
    assert _commit(fenced, "attempt-1", expected_epoch=1) is None
    # A plain save() from the stale writer must not clobber the permit or epoch.
    fenced.record("other/route", ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    fenced.save()
    reopened = HealthCache(path)
    assert reopened.permit_for_attempt("attempt-1") == permit
    assert reopened.dispatch_attempt_state("attempt-1") == PERMIT_STATE_MAYBE_SENT
    assert reopened.revocation_epoch(SCOPE) == 1


def test_revocation_event_replay_is_idempotent_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    first = cache.record_revocation(
        SCOPE, route_id=ROUTE, event_id="neg-1", source_revision="snap-1", observed_at=NOW
    )
    replay = cache.record_revocation(
        SCOPE,
        route_id=ROUTE,
        event_id="neg-1",
        source_revision="snap-1",
        observed_at=_at(3600),  # delayed replay, later timestamp
    )
    assert first == replay == 1
    reopened = HealthCache(path)
    delayed = reopened.record_revocation(
        SCOPE, route_id=ROUTE, event_id="neg-1", source_revision=None, observed_at=_at(7200)
    )
    assert delayed == 1
    second = reopened.record_revocation(
        SCOPE, route_id=ROUTE, event_id="neg-2", source_revision=None, observed_at=_at(7200)
    )
    assert second == 2


def test_future_dated_negative_stays_in_force(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    # A skewed/future-dated negative is still a negative: it never authorizes.
    epoch = cache.record_revocation(
        SCOPE, route_id=ROUTE, event_id="neg-future", source_revision=None, observed_at=_at(600)
    )
    assert epoch == 1
    assert _commit(cache, "attempt-1", expected_epoch=0) is None


def test_proof_expiry_window_never_authorizes_stale_or_future_proofs(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    # Expired (deadline reached; equality expires per the basic-proof age).
    assert (
        cache.commit_dispatch(
            SCOPE,
            route_id=ROUTE,
            attempt_id="expired",
            authorization_digest="auth-digest",
            expected_epoch=0,
            qualified_at=NOW,
            proof_deadline=NOW,
            now=NOW,
        )
        is None
    )
    # Future-stamped: beyond the maximum proof validity the implied observation
    # time lies after ``now``, so the positive is fabricated.
    assert (
        cache.commit_dispatch(
            SCOPE,
            route_id=ROUTE,
            attempt_id="future",
            authorization_digest="auth-digest",
            expected_epoch=0,
            qualified_at=NOW,
            proof_deadline=_at(PERMIT_PROOF_MAX_VALIDITY_SECONDS + 1),
            now=NOW,
        )
        is None
    )
    # Within the window the boundary itself is allowed.
    assert (
        cache.commit_dispatch(
            SCOPE,
            route_id=ROUTE,
            attempt_id="edge",
            authorization_digest="auth-digest",
            expected_epoch=0,
            qualified_at=NOW,
            proof_deadline=_at(PERMIT_PROOF_MAX_VALIDITY_SECONDS),
            now=NOW,
        )
        is not None
    )
    assert PERMIT_PROOF_MAX_VALIDITY_SECONDS == 600


def test_permit_rejects_proof_older_than_eleven_minutes(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    assert (
        cache.commit_dispatch(
            SCOPE,
            route_id=ROUTE,
            attempt_id="aged-proof",
            authorization_digest="auth-digest",
            expected_epoch=0,
            qualified_at=NOW,
            proof_deadline=NOW + timedelta(seconds=60),
            now=NOW - timedelta(minutes=11),
        )
        is None
    )


def test_scope_and_permit_inputs_are_validated(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "health.json")
    with pytest.raises(HealthCacheError):
        DispatchScope(gateway_origin="", config_generation="c", profile_digest="p", binding_id="b")
    with pytest.raises(HealthCacheError):
        cache.record_revocation(
            SCOPE, route_id=ROUTE, event_id=" ", source_revision=None, observed_at=NOW
        )
    naive = datetime(2026, 10, 18, 12, 0)
    with pytest.raises(HealthCacheError):
        cache.commit_dispatch(
            SCOPE,
            route_id=ROUTE,
            attempt_id="a1",
            authorization_digest="d",
            expected_epoch=0,
            qualified_at=NOW,
            proof_deadline=naive,
            now=NOW,
        )
    with pytest.raises(HealthCacheError):
        cache.commit_dispatch(
            SCOPE,
            route_id=ROUTE,
            attempt_id="a1",
            authorization_digest="d",
            expected_epoch=-1,
            qualified_at=NOW,
            proof_deadline=_at(60),
            now=NOW,
        )


def test_dispatch_envelope_is_additive_for_permit_free_caches(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    cache.record(ROUTE, ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True), NOW)
    cache.save()
    assert "dispatch" not in json.loads(path.read_text(encoding="utf-8"))
    _commit(cache, "attempt-1")
    assert "dispatch" in json.loads(path.read_text(encoding="utf-8"))


def test_latest_revocation_at_is_read_only_scoped_maximum(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    cache = HealthCache(path)
    assert cache.latest_revocation_at(SCOPE) is None
    assert not path.exists()
    cache.record_revocation(
        SCOPE, route_id=ROUTE, event_id="newer", source_revision=None, observed_at=_at(30)
    )
    cache.record_revocation(
        SCOPE, route_id=ROUTE, event_id="older", source_revision=None, observed_at=NOW
    )
    other_scope = DispatchScope("https://other.example", "c", "p", "b")
    cache.record_revocation(
        other_scope, route_id=ROUTE, event_id="other", source_revision=None, observed_at=_at(60)
    )
    before = path.read_bytes()
    reopened = HealthCache(path)
    assert reopened.latest_revocation_at(SCOPE) == _at(30)
    assert reopened.latest_revocation_at(other_scope) == _at(60)
    assert reopened.revocation_epoch(SCOPE) == 2
    assert path.read_bytes() == before
