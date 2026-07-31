from datetime import datetime, timedelta, timezone

from verdict.capability_passports import (
    CapabilityEvidence,
    CapabilityPassport,
    CapabilityStatus,
    EvidenceAuthority,
    RouteIdentity,
)
from verdict.qualification_report import build_qualification_report

NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def _passport() -> CapabilityPassport:
    evidence = CapabilityEvidence(
        status=CapabilityStatus.SUPPORTED,
        source="fixture:probe",
        observed_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        confidence=1,
        evidence_digest=DIGEST,
        authority=EvidenceAuthority.OBSERVED,
    )
    return CapabilityPassport(
        route_identity=RouteIdentity(
            "gateway",
            "provider",
            "account",
            "https://user:secret@example.test/v1?token=secret",
            "openai.chat.completions",
            "provider/model",
        ),
        qualified_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        claimed={"tools": evidence},
        observed={"tools": evidence},
    )


def test_report_is_deterministic_sorted_and_secret_safe() -> None:
    report = build_qualification_report(_passport(), required_capabilities=["tools"], at=NOW)
    again = build_qualification_report(_passport(), required_capabilities=["tools"], at=NOW)

    assert report.passed
    assert report.digest == again.digest
    assert report.to_dict() == again.to_dict()
    rendered = str(report.to_dict())
    assert "secret" not in rendered
    assert report.to_dict()["route_identity"]["endpoint"] == "https://example.test/v1"


def test_report_preserves_claim_vs_observation_and_fails_closed() -> None:
    passport = _passport()
    payload = passport.to_dict()
    payload["observed"] = {}
    report = build_qualification_report(
        CapabilityPassport.from_dict(payload), required_capabilities=("tools",), at=NOW
    )

    assert report.passed is False
    assert report.decisions[0].status is CapabilityStatus.UNKNOWN
    assert report.decisions[0].reason == "observation missing"
    assert report.claimed["tools"].authority == "observed"
    assert "tools" not in report.observed


def test_empty_requirements_are_explicitly_non_admission_inventory() -> None:
    report = build_qualification_report(_passport(), at=NOW)

    assert report.passed
    assert "no hard requirements supplied" in report.limitations
