"""Tests for deterministic runtime compatibility reporting."""

from datetime import datetime, timedelta, timezone

import pytest

from verdict.capability_passports import CapabilityEvidence, CapabilityStatus, EvidenceAuthority
from verdict.runtime_compatibility import (
    RuntimeCompatibilityStatus,
    build_runtime_compatibility_report,
)
from verdict.runtime_passports import (
    RuntimeCapabilityPassport,
    RuntimeSubjectIdentity,
    RuntimeSubjectKind,
)

NOW = datetime(2026, 7, 31, 20, tzinfo=timezone.utc)
DIGEST = "sha256:" + "a" * 64


def passport(
    *,
    declared: bool = False,
    observed: CapabilityStatus | None = None,
    negotiated: CapabilityStatus | None = None,
    expires_at: datetime | None = None,
    limitations: tuple[str, ...] = (),
) -> RuntimeCapabilityPassport:
    def evidence(status: CapabilityStatus, authority: EvidenceAuthority) -> CapabilityEvidence:
        observed_at = (
            expires_at - timedelta(minutes=1)
            if expires_at is not None and expires_at <= NOW
            else NOW
        )
        return CapabilityEvidence(
            status=status,
            source="fixture",
            observed_at=observed_at,
            expires_at=expires_at or NOW + timedelta(minutes=10),
            confidence=1.0,
            evidence_digest=DIGEST,
            authority=authority,
            method="fixture",
            adapter_version="test-1",
            scope="test",
        )

    return RuntimeCapabilityPassport(
        subject=RuntimeSubjectIdentity(
            kind=RuntimeSubjectKind.MCP_SERVER,
            subject_id="docs-server",
            provider="fixture/provider?token=secret",
            protocol="mcp/v1",
            protocol_version="2025-06-18",
            transport="https://endpoint.test",
            auth_mode="bearer-secret",
            endpoint_digest=DIGEST,
            scope="account/test",
        ),
        qualified_at=NOW,
        expires_at=NOW + timedelta(minutes=20),
        declared={"tools.list": evidence(CapabilityStatus.SUPPORTED, EvidenceAuthority.CLAIMED)}
        if declared
        else {},
        observed={"tools.list": evidence(observed, EvidenceAuthority.OBSERVED)}
        if observed is not None
        else {},
        negotiated={"tools.list": evidence(negotiated, EvidenceAuthority.OBSERVED)}
        if negotiated is not None
        else {},
        limitations=limitations,
    )


def test_fresh_negotiated_support_is_compatible() -> None:
    report = build_runtime_compatibility_report(
        {
            "ignored-index": passport(
                observed=CapabilityStatus.SUPPORTED, negotiated=CapabilityStatus.SUPPORTED
            )
        },
        {"tools.list"},
        at=NOW,
    )

    assert report.entries[0].status is RuntimeCompatibilityStatus.COMPATIBLE
    assert report.entries[0].assessments[0].negotiated == "supported"


@pytest.mark.parametrize(
    ("item", "status"),
    [
        (passport(observed=CapabilityStatus.SUPPORTED), RuntimeCompatibilityStatus.UNKNOWN),
        (
            passport(observed=CapabilityStatus.UNSUPPORTED, negotiated=CapabilityStatus.SUPPORTED),
            RuntimeCompatibilityStatus.INCOMPATIBLE,
        ),
        (passport(declared=True), RuntimeCompatibilityStatus.UNKNOWN),
        (
            passport(
                observed=CapabilityStatus.SUPPORTED,
                negotiated=CapabilityStatus.SUPPORTED,
                expires_at=NOW - timedelta(seconds=1),
            ),
            RuntimeCompatibilityStatus.UNKNOWN,
        ),
    ],
)
def test_missing_expired_or_unsupported_evidence_is_fail_closed(item, status) -> None:
    report = build_runtime_compatibility_report({"subject": item}, {"tools.list"}, at=NOW)
    assert report.entries[0].status is status


def test_limitations_are_visible_as_degraded_and_output_is_secret_safe() -> None:
    report = build_runtime_compatibility_report(
        {
            "subject": passport(
                observed=CapabilityStatus.SUPPORTED,
                negotiated=CapabilityStatus.SUPPORTED,
                limitations=("requires prompt=secret",),
            )
        },
        {"tools.list"},
        at=NOW,
    )
    rendered = str(report.to_dict())
    assert report.entries[0].status is RuntimeCompatibilityStatus.DEGRADED
    assert "token=secret" not in rendered
    assert "https://endpoint.test" not in rendered
    assert report.entries[0].remediation


def test_order_and_digest_are_deterministic() -> None:
    first = passport(observed=CapabilityStatus.SUPPORTED, negotiated=CapabilityStatus.SUPPORTED)
    second = passport(observed=CapabilityStatus.SUPPORTED, negotiated=CapabilityStatus.SUPPORTED)
    report_a = build_runtime_compatibility_report({"b": second, "a": first}, ["tools.list"], at=NOW)
    report_b = build_runtime_compatibility_report({"a": first, "b": second}, ["tools.list"], at=NOW)
    assert report_a.to_dict() == report_b.to_dict()
    assert report_a.digest.startswith("sha256:")


def test_invalid_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        build_runtime_compatibility_report({}, "tools.list", at=NOW)
