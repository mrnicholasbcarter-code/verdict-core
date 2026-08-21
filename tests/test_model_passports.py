"""Model passport contract and qualification-cascade tests (MODEL-001, #256)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.availability import AvailabilityState
from verdict.model_passports import (
    CAPACITY_CONFIDENCE_THRESHOLD,
    LARGE_TASK_TOKENS,
    MODEL_PASSPORT_SCHEMA_VERSION,
    PASSPORT_TTL_SECONDS,
    ModelPassport,
    ModelPassportError,
    estimate_capacity_confidence,
    run_qualification,
)
from verdict.models import ModelInfo

NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
SCHEMA = json.loads(
    Path(__file__).parents[1].joinpath("verdict/schemas/model-passport.v1.json").read_text()
)


def _passport(**overrides: object) -> ModelPassport:
    base = dict(
        provider="p",
        model_id="p/model",
        auth_state="authorized",
        latency_p95=120.0,
        context_window=128_000,
        tool_support=True,
        token_cost_per_1k=2.5,
        last_verified_timestamp=NOW,
        availability_state="eligible",
        qualified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=10),
    )
    return ModelPassport(**{**base, **overrides})


def _model(**overrides: object) -> ModelInfo:
    base = dict(
        id="p/model",
        provider="p",
        capability_tier=1,
        context_window=128_000,
        cost_per_1k=2.5,
        capabilities=frozenset(["tools"]),
    )
    return ModelInfo(**{**base, **overrides})


def _ok_transport(calls: list | None = None):
    def transport(model_id, payload, timeout_seconds):
        if calls is not None:
            calls.append((model_id, payload, timeout_seconds))
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            },
        }

    return transport


class TestModelPassportContract:
    def test_strict_round_trip(self) -> None:
        item = _passport()
        assert ModelPassport.from_dict(item.to_dict()) == item
        assert item.digest == item.digest  # deterministic

    def test_schema_valid(self) -> None:
        assert list(Draft202012Validator(SCHEMA).iter_errors(_passport().to_dict())) == []

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ModelPassportError, match="unknown field"):
            ModelPassport.from_dict({**_passport().to_dict(), "extra": 1})

    def test_missing_field_rejected(self) -> None:
        data = _passport().to_dict()
        del data["provider"]
        with pytest.raises(ModelPassportError, match="missing field"):
            ModelPassport.from_dict(data)

    def test_expires_must_follow_qualified_at(self) -> None:
        with pytest.raises(ModelPassportError, match="expires_at must be after"):
            _passport(expires_at=NOW - timedelta(seconds=5))

    def test_schema_version_must_be_one(self) -> None:
        with pytest.raises(ModelPassportError, match="schema_version"):
            _passport(schema_version="2")

    def test_quarantine_requires_timestamps(self) -> None:
        with pytest.raises(ModelPassportError, match="quarantine timestamps"):
            _passport(availability_state="quarantined")

    def test_non_quarantine_rejects_timestamps(self) -> None:
        with pytest.raises(ModelPassportError, match="cannot carry quarantine"):
            _passport(quarantine_until=NOW + timedelta(seconds=5), quarantined_at=NOW)

    def test_key(self) -> None:
        assert _passport().key == "p/p/model"


class TestCapacityConfidence:
    def test_known_window_fits(self) -> None:
        model = _model()
        score = estimate_capacity_confidence(model, estimated_tokens=1_000, headroom_pct=100.0)
        assert score == 1.0

    def test_unknown_window_neutral(self) -> None:
        model = _model(context_window=-1)
        assert estimate_capacity_confidence(model, estimated_tokens=1_000) == 0.5

    def test_large_task_with_low_headroom_rejected(self) -> None:
        model = _model()
        score = estimate_capacity_confidence(
            model, estimated_tokens=60_000, headroom_pct=10.0, quota_remaining_pct=10.0
        )
        assert score <= CAPACITY_CONFIDENCE_THRESHOLD

    def test_large_task_with_high_headroom_admitted(self) -> None:
        model = _model()
        score = estimate_capacity_confidence(
            model, estimated_tokens=60_000, headroom_pct=90.0, quota_remaining_pct=90.0
        )
        assert score > CAPACITY_CONFIDENCE_THRESHOLD

    def test_estimator_pure_and_bounded(self) -> None:
        model = _model()
        a = estimate_capacity_confidence(model, estimated_tokens=60_000, headroom_pct=50.0)
        b = estimate_capacity_confidence(model, estimated_tokens=60_000, headroom_pct=50.0)
        assert a == b
        assert 0.0 <= a <= 1.0


class TestQualification:
    def test_tier1_ping_only_fast_path(self) -> None:
        calls: list = []
        passport = run_qualification(
            provider="p",
            model_id="p/model",
            transport=_ok_transport(calls),
            model=_model(),
            now=NOW,
        )
        # Only the one-token ping transport call.
        assert len(calls) == 1
        assert calls[0][1]["max_tokens"] == 1
        assert calls[0][1]["tools"] == []
        assert passport.availability_state == "eligible"
        assert passport.auth_state == "authorized"
        assert passport.tool_support is True
        # The probe observes its own wall-clock; assert it is present and aware.
        assert passport.last_verified_timestamp is not None
        assert passport.last_verified_timestamp.tzinfo is not None

    def test_tier2_runs_on_tool_demand(self) -> None:
        calls: list = []
        run_qualification(
            provider="p",
            model_id="p/model",
            transport=_ok_transport(calls),
            model=_model(),
            require_tools=True,
            now=NOW,
        )
        # Tier-1 ping + Tier-2 deep probe.
        assert len(calls) == 2

    def test_tier2_runs_on_large_task(self) -> None:
        calls: list = []
        run_qualification(
            provider="p",
            model_id="p/model",
            transport=_ok_transport(calls),
            model=_model(),
            estimated_tokens=LARGE_TASK_TOKENS + 1,
            now=NOW,
        )
        assert len(calls) == 2

    def test_requires_tools_degrades_when_unsupported(self) -> None:
        model = _model(capabilities=frozenset())
        passport = run_qualification(
            provider="p",
            model_id="p/model",
            transport=_ok_transport(),
            model=model,
            require_tools=True,
            now=NOW,
        )
        assert passport.availability_state == "degraded"
        assert passport.availability_reason == "tool_support_unavailable"

    def test_single_failure_degrades_not_quarantines(self) -> None:
        # Quarantine requires consecutive failures (ProbePolicy.failure_threshold
        # defaults to 3); a single 429 degrades without quarantining.
        def failing_transport(model_id, payload, timeout_seconds):
            return {"status_code": 429, "body": {"error": {"message": "rate limited"}}}

        passport = run_qualification(
            provider="p", model_id="p/model", transport=failing_transport, model=_model(), now=NOW
        )
        assert passport.availability_state in {"degraded", "denied"}

    def test_quarantined_passport_requires_timestamps(self) -> None:
        # A quarantined passport must carry quarantine timestamps (contract).
        _passport(
            availability_state="quarantined",
            quarantined_at=NOW,
            quarantine_until=NOW + timedelta(seconds=300),
        )

    def test_capacity_gate_degrades_large_unsafe_task(self) -> None:
        model = _model(context_window=8_000)
        passport = run_qualification(
            provider="p",
            model_id="p/model",
            transport=_ok_transport(),
            model=model,
            estimated_tokens=LARGE_TASK_TOKENS + 1,
            capacity_headroom_pct=5.0,
            capacity_quota_pct=5.0,
            now=NOW,
        )
        assert passport.availability_state == "degraded"
        assert passport.availability_reason == "capacity_confidence_insufficient"


class TestAvailabilityStateParity:
    def test_quarantined_state_exists(self) -> None:
        assert AvailabilityState.QUARANTINED.value == "quarantined"

    def test_quarantined_maps_to_unavailable_externally(self) -> None:
        from verdict.availability import _map_to_accepted_state

        assert (
            _map_to_accepted_state(AvailabilityState.QUARANTINED) is AvailabilityState.UNAVAILABLE
        )

    def test_passport_ttl_constant(self) -> None:
        assert PASSPORT_TTL_SECONDS == 300
        assert MODEL_PASSPORT_SCHEMA_VERSION == "1"


class TestDefaultExpiryDerivation:
    """Regression: expires_at defaulted to a bare _now(), colliding with qualified_at.

    Both fields used `default_factory=lambda: _now()`. Two separate clock reads land in
    the same microsecond on a warm/fast path, making `expires_at == qualified_at` and
    tripping the `expires_at must be after qualified_at` guard. Rare when cold, but
    ~50% under load -- which is exactly when CI runs.
    """

    def test_default_construction_never_collides_under_load(self) -> None:
        for _ in range(20_000):
            ModelPassport(
                provider="p", model_id="p/model", auth_state="authorized", context_window=80
            )

    def test_default_expiry_is_ttl_after_truncated_qualified_at(self) -> None:
        passport = ModelPassport(
            provider="p", model_id="p/model", auth_state="authorized", context_window=80
        )
        assert passport.expires_at is not None
        expected = passport.qualified_at.replace(second=0, microsecond=0) + timedelta(
            seconds=PASSPORT_TTL_SECONDS
        )
        assert passport.expires_at == expected
        assert passport.expires_at > passport.qualified_at

    def test_explicit_expires_at_is_still_honoured(self) -> None:
        expires = NOW + timedelta(seconds=PASSPORT_TTL_SECONDS)
        passport = ModelPassport(
            provider="p",
            model_id="p/model",
            auth_state="authorized",
            context_window=80,
            qualified_at=NOW,
            expires_at=expires,
        )
        assert passport.expires_at == expires

    def test_explicit_invalid_expires_at_still_raises(self) -> None:
        with pytest.raises(ModelPassportError, match="expires_at must be after qualified_at"):
            ModelPassport(
                provider="p",
                model_id="p/model",
                auth_state="authorized",
                context_window=80,
                qualified_at=NOW,
                expires_at=NOW,
            )
