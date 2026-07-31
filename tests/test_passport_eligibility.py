from datetime import datetime, timedelta, timezone

import pytest

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
)
from verdict.passport_eligibility import evaluate_passports

NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
DIGEST = "sha256:" + ("a" * 64)


def make_passport(
    *, status: CapabilityStatus = CapabilityStatus.SUPPORTED, expires_at: datetime | None = None
) -> CapabilityPassport:
    return CapabilityPassport(
        route_identity=RouteIdentity(
            gateway="gateway",
            provider="provider",
            connection="connection",
            endpoint="https://gateway.example/v1/chat/completions",
            protocol="openai.chat.completions",
            model_id="provider/model",
        ),
        qualified_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=10),
        observed={
            "tools": CapabilityEvidence(
                status=status,
                source="fixture",
                observed_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                confidence=1,
                evidence_digest=DIGEST,
                authority=EvidenceAuthority.OBSERVED,
                method="fixture",
                adapter_version="test-1",
                scope="test",
            )
        },
    )


def test_fresh_exact_route_and_observation_are_admitted() -> None:
    passport = make_passport()

    result = evaluate_passports({passport.route_identity.key: passport}, {"tools"}, at=NOW)

    assert result.admitted == (passport.route_identity.key,)
    assert result.records[0].reason == "all required capabilities admitted"


@pytest.mark.parametrize(
    ("required", "passport_kwargs", "reason"),
    [
        ({"vision"}, {}, "required capability vision: observation missing"),
        (
            {"tools"},
            {"expires_at": NOW + timedelta(seconds=1)},
            "required capability tools: passport expired",
        ),
        (
            {"tools"},
            {"status": CapabilityStatus.UNSUPPORTED},
            "required capability tools: fresh observation is unsupported",
        ),
    ],
)
def test_missing_expired_or_unsupported_requirements_fail_closed(
    required: set[str], passport_kwargs: dict[str, object], reason: str
) -> None:
    passport = make_passport(**passport_kwargs)

    evaluation_time = NOW + timedelta(seconds=2) if "expired" in reason else NOW
    result = evaluate_passports(
        {passport.route_identity.key: passport}, required, at=evaluation_time
    )

    assert result.admitted == ()
    assert result.records[0].reason == reason


def test_route_key_mismatch_fails_closed_and_empty_requirements_are_safe() -> None:
    passport = make_passport()
    route_key = passport.route_identity.key

    mismatch = evaluate_passports({"sha256:" + ("b" * 64): passport}, set(), at=NOW)
    empty = evaluate_passports({route_key: passport}, set(), at=NOW)

    assert mismatch.to_dict()["records"][0]["reason"] == "route identity mismatch"
    assert empty.admitted == (route_key,)
    assert empty.to_dict()["required"] == []


def test_explanation_order_and_json_shape_are_deterministic() -> None:
    passport = make_passport()
    route_key = passport.route_identity.key
    other = RouteIdentity(
        gateway="gateway",
        provider="provider",
        connection="other",
        endpoint="https://gateway.example/v1/chat/completions",
        protocol="openai.chat.completions",
        model_id="provider/model",
    )
    other_passport = CapabilityPassport(
        route_identity=other, qualified_at=NOW, expires_at=NOW + timedelta(minutes=10)
    )

    result = evaluate_passports(
        {other.key: other_passport, route_key: passport}, {"tools", "vision"}, at=NOW
    )

    assert [record.route_key for record in result.records] == sorted([other.key, route_key])
    assert result.to_dict()["required"] == ["tools", "vision"]
    assert set(result.to_dict()) == {"required", "admitted", "records"}
