"""Tests for runtime-negotiated tool and peer passports."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from verdict.capability_passports import CapabilityEvidence, CapabilityStatus, EvidenceAuthority
from verdict.runtime_passports import (
    RuntimeCapabilityPassport,
    RuntimePassportError,
    RuntimeSubjectIdentity,
    RuntimeSubjectKind,
)

NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def subject(**overrides: object) -> RuntimeSubjectIdentity:
    values: dict[str, object] = {
        "kind": RuntimeSubjectKind.MCP_SERVER,
        "subject_id": "docs-server",
        "provider": "fixture",
        "protocol": "mcp",
        "protocol_version": "2025-06-18",
        "transport": "stdio",
        "auth_mode": "local-process",
        "endpoint_digest": DIGEST,
    }
    values.update(overrides)
    return RuntimeSubjectIdentity(**values)


def evidence(
    status: CapabilityStatus,
    *,
    authority: EvidenceAuthority = EvidenceAuthority.OBSERVED,
    expires_at: datetime | None = None,
) -> CapabilityEvidence:
    return CapabilityEvidence(
        status=status,
        source="fixture",
        observed_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=10),
        confidence=1.0,
        evidence_digest=DIGEST,
        authority=authority,
        method="fixture-handshake",
        adapter_version="fixture-1",
        scope="test",
    )


def passport(**overrides: object) -> RuntimeCapabilityPassport:
    values: dict[str, object] = {
        "subject": subject(),
        "qualified_at": NOW,
        "expires_at": NOW + timedelta(minutes=10),
    }
    values.update(overrides)
    return RuntimeCapabilityPassport(**values)


def test_identity_is_secret_free_and_distinguishes_runtime_subjects() -> None:
    first = subject()
    second = subject(kind=RuntimeSubjectKind.ACP_AGENT)
    third = subject(transport="https")

    assert len({first.key, second.key, third.key}) == 3
    assert "token" not in json.dumps(first.to_dict()).lower()
    assert "https://" not in json.dumps(first.to_dict()).lower()


def test_declaration_or_observation_alone_never_admits_policy() -> None:
    declared = passport(declared={"tools.list": evidence(CapabilityStatus.SUPPORTED)})
    observed = passport(observed={"tools.list": evidence(CapabilityStatus.SUPPORTED)})

    assert declared.resolve("tools.list", at=NOW).reason == "negotiation missing"
    assert observed.satisfies({"tools.list"}, at=NOW) is False


def test_fresh_direct_negotiation_is_required_for_admission() -> None:
    item = passport(
        declared={
            "tools.list": evidence(CapabilityStatus.SUPPORTED, authority=EvidenceAuthority.CLAIMED)
        },
        observed={"tools.list": evidence(CapabilityStatus.SUPPORTED)},
        negotiated={"tools.list": evidence(CapabilityStatus.SUPPORTED)},
    )

    decision = item.resolve("tools.list", at=NOW + timedelta(seconds=1))

    assert decision.admitted is True
    assert decision.status is CapabilityStatus.SUPPORTED
    assert item.satisfies({"tools.list"}, at=NOW + timedelta(seconds=1)) is True


def test_unsupported_observation_dominates_optimistic_negotiation() -> None:
    item = passport(
        observed={"tools.call": evidence(CapabilityStatus.UNSUPPORTED)},
        negotiated={"tools.call": evidence(CapabilityStatus.SUPPORTED)},
    )

    decision = item.resolve("tools.call", at=NOW + timedelta(seconds=1))

    assert decision.status is CapabilityStatus.UNSUPPORTED
    assert decision.admitted is False


def test_claimed_negotiation_and_expired_negotiation_fail_closed() -> None:
    claimed = passport(
        negotiated={
            "resources.read": evidence(
                CapabilityStatus.SUPPORTED, authority=EvidenceAuthority.CLAIMED
            )
        }
    )
    expired = passport(
        negotiated={
            "resources.read": evidence(
                CapabilityStatus.SUPPORTED, expires_at=NOW + timedelta(seconds=1)
            )
        }
    )

    assert "not direct" in claimed.resolve("resources.read", at=NOW).reason
    assert expired.resolve("resources.read", at=NOW + timedelta(seconds=2)).reason == (
        "negotiation expired"
    )


def test_inferred_negotiation_fails_closed() -> None:
    item = passport(
        negotiated={
            "tools.list": evidence(CapabilityStatus.SUPPORTED, authority=EvidenceAuthority.INFERRED)
        }
    )

    decision = item.resolve("tools.list", at=NOW)

    assert decision.admitted is False
    assert "not direct" in decision.reason


def test_expired_passport_dominates_fresh_negotiation() -> None:
    item = passport(
        expires_at=NOW + timedelta(seconds=1),
        negotiated={"tools.list": evidence(CapabilityStatus.SUPPORTED)},
    )

    decision = item.resolve("tools.list", at=NOW + timedelta(seconds=2))

    assert decision.status is CapabilityStatus.UNKNOWN
    assert decision.reason == "passport expired"


def test_round_trip_schema_and_digest_are_deterministic() -> None:
    item = passport(negotiated={"prompts.get": evidence(CapabilityStatus.SUPPORTED)})
    round_trip = RuntimeCapabilityPassport.from_dict(item.to_dict())
    schema_path = Path(__file__).parents[1] / "verdict" / "schemas" / "runtime-passport.v1.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert round_trip == item
    assert round_trip.digest == item.digest
    assert list(Draft202012Validator(schema).iter_errors(item.to_dict())) == []


def test_strict_parser_rejects_unknown_fields_and_raw_endpoint_identity() -> None:
    payload = passport().to_dict()
    payload["unexpected"] = True
    with pytest.raises(RuntimePassportError, match="unknown field"):
        RuntimeCapabilityPassport.from_dict(payload)

    with pytest.raises(RuntimePassportError, match="endpoint_digest"):
        RuntimeSubjectIdentity.from_dict({**subject().to_dict(), "endpoint_digest": "https://x"})
