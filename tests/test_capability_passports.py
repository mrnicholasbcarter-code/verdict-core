from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityPassportError,
    CapabilityStatus,
    RouteIdentity,
)

NOW = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
DIGEST = "sha256:" + ("a" * 64)


def evidence(
    status: CapabilityStatus,
    *,
    expires_at: datetime | None = None,
    source: str = "verdict:probe/chat-v1",
) -> CapabilityEvidence:
    return CapabilityEvidence(
        status=status,
        source=source,
        observed_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=10),
        confidence=1,
        evidence_digest=DIGEST,
    )


def passport(
    *,
    claimed: dict[str, CapabilityEvidence] | None = None,
    observed: dict[str, CapabilityEvidence] | None = None,
    expires_at: datetime | None = None,
) -> CapabilityPassport:
    return CapabilityPassport(
        route_identity=RouteIdentity(
            gateway="omniroute",
            provider="openrouter",
            connection="team-free",
            endpoint="https://openrouter.example/v1/chat/completions",
            protocol="openai.chat.completions",
            model_id="nvidia/nemotron-3-super-120b-a12b:free",
            model_revision="2026-07-01",
        ),
        qualified_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=10),
        claimed=claimed or {},
        observed=observed or {},
    )


def test_claim_never_satisfies_a_hard_requirement() -> None:
    item = passport(claimed={"tools": evidence(CapabilityStatus.SUPPORTED)})

    decision = item.resolve("tools", at=NOW + timedelta(seconds=1))

    assert decision.status is CapabilityStatus.UNKNOWN
    assert decision.reason == "observation missing"
    assert decision.claimed is not None
    assert item.satisfies({"tools"}, at=NOW + timedelta(seconds=1)) is False


def test_fresh_observation_is_authoritative_over_conflicting_claim() -> None:
    item = passport(
        claimed={"tools": evidence(CapabilityStatus.SUPPORTED, source="models.dev")},
        observed={"tools": evidence(CapabilityStatus.UNSUPPORTED)},
    )

    decision = item.resolve("tools", at=NOW + timedelta(seconds=1))

    assert decision.status is CapabilityStatus.UNSUPPORTED
    assert decision.admitted is False
    assert decision.claimed is not None
    assert decision.observed is not None


def test_expired_observation_and_passport_fail_closed() -> None:
    observation = evidence(CapabilityStatus.SUPPORTED, expires_at=NOW + timedelta(seconds=5))
    item = passport(observed={"streaming": observation})

    assert (
        item.resolve("streaming", at=NOW + timedelta(seconds=6)).status is CapabilityStatus.UNKNOWN
    )
    assert item.resolve("streaming", at=NOW + timedelta(minutes=11)).reason == "passport expired"


def test_all_required_capabilities_must_be_freshly_observed() -> None:
    item = passport(
        observed={
            "tools": evidence(CapabilityStatus.SUPPORTED),
            "streaming": evidence(CapabilityStatus.UNKNOWN),
        }
    )

    assert item.satisfies({"tools"}, at=NOW + timedelta(seconds=1)) is True
    assert item.satisfies({"tools", "streaming"}, at=NOW + timedelta(seconds=1)) is False


def test_route_key_does_not_collapse_distinct_connections_or_protocols() -> None:
    first = passport()
    second_payload = first.to_dict()
    second_payload["route_identity"]["connection"] = "paid-account"
    second = CapabilityPassport.from_dict(second_payload)
    third_payload = first.to_dict()
    third_payload["route_identity"]["protocol"] = "openai.responses"
    third = CapabilityPassport.from_dict(third_payload)

    assert len({first.route_identity.key, second.route_identity.key, third.route_identity.key}) == 3


def test_serialization_is_canonical_and_digest_detects_changes() -> None:
    first = passport(
        claimed={"tools": evidence(CapabilityStatus.SUPPORTED)},
        observed={"tools": evidence(CapabilityStatus.SUPPORTED)},
    )
    round_trip = CapabilityPassport.from_dict(first.to_dict())
    changed_payload = first.to_dict()
    changed_payload["limitations"] = ["tools probe did not cover parallel calls"]
    changed = CapabilityPassport.from_dict(changed_payload)

    assert round_trip == first
    assert round_trip.digest == first.digest
    assert changed.digest != first.digest


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update({"unexpected": True}), "unknown field"),
        (
            lambda value: value["observed"].update(
                {"Tools": evidence(CapabilityStatus.SUPPORTED).to_dict()}
            ),
            "capability names",
        ),
        (
            lambda value: value["observed"].update(
                {
                    "tools": {
                        **evidence(CapabilityStatus.SUPPORTED).to_dict(),
                        "evidence_digest": "not-a-digest",
                    }
                }
            ),
            "sha256",
        ),
    ],
)
def test_parser_rejects_non_contract_input(mutate, message: str) -> None:
    payload = passport().to_dict()
    mutate(payload)

    with pytest.raises(CapabilityPassportError, match=message):
        CapabilityPassport.from_dict(payload)


def test_json_schema_accepts_canonical_passport_and_rejects_default_true_shape() -> None:
    schema_path = Path(__file__).parents[1] / "verdict" / "schemas" / "capability-passport.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    valid = passport(
        claimed={"tools": evidence(CapabilityStatus.SUPPORTED)},
        observed={"tools": evidence(CapabilityStatus.SUPPORTED)},
    ).to_dict()
    invalid = passport().to_dict()
    invalid["claimed"]["tools"] = True

    assert list(validator.iter_errors(valid)) == []
    assert list(validator.iter_errors(invalid))
