"""Fixture-only tests for the free∩active prove-at-rest daemon."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from verdict.free_tier_admit import (
    REASON_INACTIVE_UNCONNECTED,
    REASON_NOT_FREE_TIER,
    REASON_OPAQUE_ROUTE_DISALLOWED,
    snapshot_from_payloads,
)
from verdict.prove_at_rest import (
    STATUS_FAILED,
    STATUS_HEALTHY,
    STATUS_SKIPPED,
    ProveAtRestDaemon,
    ProveAtRestStore,
    load_healthy_passports,
)

NOW = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)


def _snapshot(
    *,
    catalog: list[dict[str, object]],
    free_tier: list[dict[str, object]],
    providers: list[dict[str, object]],
):
    return snapshot_from_payloads(
        catalog={"data": catalog},
        free_tier={"perModel": free_tier},
        providers={"connections": providers, "total": len(providers)},
    )


def _catalog_row(identity_id: str, owned_by: str | None = None) -> dict[str, object]:
    return {
        "id": identity_id,
        "owned_by": owned_by or identity_id.split("/", 1)[0],
        "object": "model",
    }


def _fixture_snapshot():
    return _snapshot(
        catalog=[
            _catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free"),
            _catalog_row("opencode/hy3-free"),
            _catalog_row("openrouter/auto"),
            _catalog_row("anthropic/claude-3-opus-20240229", "claude"),
            _catalog_row("mistral/mistral-large-latest"),
        ],
        free_tier=[
            {
                "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                "provider": "openrouter",
                "freeType": "recurring-daily",
            },
            {"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"},
            {"modelId": "auto", "provider": "openrouter", "freeType": "recurring-uncapped"},
            {"modelId": "ghost-free", "provider": "mistral", "freeType": "keyless"},
        ],
        providers=[
            {"provider": "openrouter", "isActive": True, "testStatus": "active"},
            {"provider": "opencode", "isActive": True, "testStatus": "active"},
            {"provider": "claude", "isActive": True, "testStatus": "active"},
            {"provider": "mistral", "isActive": False, "testStatus": "error"},
        ],
    )


def _ok_transport(calls: list[str]):
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        calls.append(model_id)
        if model_id.endswith("hy3-free"):
            return {"status_code": 500, "body": {"error": {"message": "upstream failed"}}}
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def test_run_once_proves_only_free_intersect_active(tmp_path: Path) -> None:
    calls: list[str] = []
    store = ProveAtRestStore(path=tmp_path / "state.json")
    daemon = ProveAtRestDaemon(
        store=store,
        snapshot_loader=_fixture_snapshot,
        transport=_ok_transport(calls),
        live=False,
        consented=False,
        clock=lambda: NOW,
        sleep=lambda _s: None,
    )
    cycle = daemon.run_once()

    assert "openrouter/nvidia/nemotron-3-nano-30b-a3b:free" in cycle.admitted
    assert "opencode/hy3-free" in cycle.admitted
    assert "anthropic/claude-3-opus-20240229" not in cycle.admitted
    assert "mistral/mistral-large-latest" not in cycle.admitted

    # Paid/frontier and inactive providers are never probed.
    assert "anthropic/claude-3-opus-20240229" not in calls
    assert "mistral/mistral-large-latest" not in calls
    assert "openrouter/auto" not in calls
    assert set(calls) == {"openrouter/nvidia/nemotron-3-nano-30b-a3b:free", "opencode/hy3-free"}

    by_id = {item.identity_id: item for item in cycle.results}
    healthy = by_id["openrouter/nvidia/nemotron-3-nano-30b-a3b:free"]
    assert healthy.status == STATUS_HEALTHY
    assert healthy.passport is not None
    assert healthy.passport.availability_state == "eligible"
    assert healthy.passport.auth_state == "authorized"

    failed = by_id["opencode/hy3-free"]
    assert failed.status == STATUS_FAILED
    assert failed.reason is not None

    skipped_reasons = {item.reason for item in cycle.results if item.status == STATUS_SKIPPED}
    assert REASON_OPAQUE_ROUTE_DISALLOWED in skipped_reasons
    assert REASON_INACTIVE_UNCONNECTED in skipped_reasons

    # Persisted and reloadable for later admit.
    reloaded = store.read()
    assert reloaded is not None
    assert reloaded.cycle_id == cycle.cycle_id
    assert reloaded.healthy_identities() == ("openrouter/nvidia/nemotron-3-nano-30b-a3b:free",)
    passports = load_healthy_passports(tmp_path / "state.json")
    assert "openrouter/nvidia/nemotron-3-nano-30b-a3b:free" in passports
    assert "opencode/hy3-free" not in passports


def test_record_confirm_refreshes_one_passport_without_replacing_cycle(tmp_path: Path) -> None:
    from datetime import timedelta

    from verdict.probes import ProbeObservation

    store = ProveAtRestStore(path=tmp_path / "state.json")
    daemon = ProveAtRestDaemon(
        store=store,
        snapshot_loader=_fixture_snapshot,
        transport=_ok_transport([]),
        live=False,
        consented=False,
        clock=lambda: NOW,
        sleep=lambda _s: None,
    )
    cycle = daemon.run_once()
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    later = NOW + timedelta(minutes=30)
    store.record_confirm(
        identity,
        ProbeObservation(
            model_id=identity,
            availability_state="ready",
            status="ok",
            observed_at=later,
            latency_ms=12.5,
            http_status=200,
        ),
        now=later,
    )

    reloaded = store.read()
    assert reloaded is not None
    assert reloaded.cycle_id == cycle.cycle_id
    by_id = {item.identity_id: item for item in reloaded.results}
    assert by_id[identity].passport is not None
    assert by_id[identity].passport.qualified_at == later
    assert by_id["opencode/hy3-free"].status == STATUS_FAILED
    assert load_healthy_passports(tmp_path / "state.json")[identity].expires_at > later


def test_paid_catalog_identity_never_probed_even_if_active(tmp_path: Path) -> None:
    calls: list[str] = []
    snapshot = _snapshot(
        catalog=[
            _catalog_row("anthropic/claude-3-opus-20240229", "claude"),
            _catalog_row("opencode/hy3-free"),
        ],
        free_tier=[{"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"}],
        providers=[
            {"provider": "claude", "isActive": True, "testStatus": "active"},
            {"provider": "opencode", "isActive": True, "testStatus": "active"},
        ],
    )
    daemon = ProveAtRestDaemon(
        store=ProveAtRestStore(path=tmp_path / "state.json"),
        snapshot_loader=lambda: snapshot,
        transport=_ok_transport(calls),
        clock=lambda: NOW,
        sleep=lambda _s: None,
    )
    cycle = daemon.run_once()
    assert calls == ["opencode/hy3-free"]
    assert all(item.identity_id != "anthropic/claude-3-opus-20240229" for item in cycle.results)
    assert cycle.summary[STATUS_HEALTHY] + cycle.summary[STATUS_FAILED] == 1


def test_daemon_loop_stops_and_rewrites_state(tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []
    snapshot = _snapshot(
        catalog=[_catalog_row("opencode/hy3-free")],
        free_tier=[
            {"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"},
            {"modelId": "discontinued-free", "provider": "opencode", "freeType": "discontinued"},
        ],
        providers=[{"provider": "opencode", "isActive": True, "testStatus": "active"}],
    )

    # Force hy3 to succeed for this test.
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        calls.append(model_id)
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    daemon = ProveAtRestDaemon(
        store=ProveAtRestStore(path=tmp_path / "state.json"),
        snapshot_loader=lambda: snapshot,
        transport=transport,
        interval_seconds=0.5,
        clock=lambda: NOW,
        sleep=lambda seconds: sleeps.append(seconds) or daemon.stop(),
    )
    last = daemon.run_forever()
    assert last is not None
    assert last.summary[STATUS_HEALTHY] == 1
    assert any(item.reason == REASON_NOT_FREE_TIER for item in last.results)
    assert sleeps  # slept once then stop()
    assert (tmp_path / "state.json").exists()


def test_daemon_loop_survives_transient_cycle_error_and_retries(tmp_path: Path) -> None:
    attempts = 0
    errors: list[Exception] = []
    snapshot = _snapshot(
        catalog=[_catalog_row("opencode/hy3-free")],
        free_tier=[{"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"}],
        providers=[{"provider": "opencode", "isActive": True, "testStatus": "active"}],
    )

    def load_snapshot():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient inventory timeout")
        return snapshot

    sleeps = 0

    def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            daemon.stop()

    daemon = ProveAtRestDaemon(
        store=ProveAtRestStore(path=tmp_path / "state.json"),
        snapshot_loader=load_snapshot,
        transport=_ok_transport([]),
        interval_seconds=0.25,
        clock=lambda: NOW,
        sleep=sleep,
        on_cycle_error=errors.append,
    )

    last = daemon.run_forever()

    assert attempts == 2
    assert len(errors) == 1
    assert str(errors[0]) == "transient inventory timeout"
    assert last is not None
    assert last.summary[STATUS_FAILED] == 1
    assert daemon.status() == last


def test_status_empty_store(tmp_path: Path) -> None:
    daemon = ProveAtRestDaemon(
        store=ProveAtRestStore(path=tmp_path / "missing.json"),
        snapshot_loader=_fixture_snapshot,
        transport=_ok_transport([]),
    )
    assert daemon.status() is None
    assert load_healthy_passports(tmp_path / "missing.json") == {}


def test_skipped_requires_named_reason() -> None:
    from verdict.prove_at_rest import ProofResult, ProveAtRestError

    with pytest.raises(ProveAtRestError, match="named reason"):
        ProofResult(identity_id="x/y", provider="x", status=STATUS_SKIPPED, reason=None)
