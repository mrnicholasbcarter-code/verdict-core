"""Tests for the pure offline environment discovery contracts."""

from __future__ import annotations

import json

import pytest

from verdict.environment_discovery import (
    DiscoveryState,
    EnvironmentObservation,
    RecommendationStatus,
    build_discovery_report,
)


def observation(
    evidence_id: str,
    component: str,
    state: DiscoveryState,
    *,
    capabilities: frozenset[str] = frozenset(),
    confidence: float = 0.9,
    freshness: str = "fresh",
) -> EnvironmentObservation:
    return EnvironmentObservation(
        evidence_id=evidence_id,
        component=component,
        state=state,
        source="fixture",
        version="1",
        capabilities=capabilities,
        confidence=confidence,
        freshness=freshness,
    )


def test_report_is_deterministic_and_recommends_profiles_from_fresh_evidence() -> None:
    evidence = (
        observation(
            "working-local",
            "ollama",
            DiscoveryState.OBSERVED_WORKING,
            capabilities=frozenset({"local", "coding"}),
        ),
        observation("gateway-auth", "team", DiscoveryState.AUTHENTICATED),
        observation("gateway-protocol", "team", DiscoveryState.PROTOCOL_COMPATIBLE),
    )

    first = build_discovery_report(tuple(reversed(evidence))).to_dict()
    second = build_discovery_report(evidence).to_dict()

    assert first == second
    assert [item["evidence_id"] for item in first["observations"]] == [
        "gateway-auth",
        "gateway-protocol",
        "working-local",
    ]
    profiles = {item["profile"]: item for item in first["recommendations"]}
    assert profiles["local/private"]["status"] == RecommendationStatus.RECOMMENDED
    assert profiles["coding-agent"]["status"] == RecommendationStatus.RECOMMENDED
    assert profiles["team-gateway"]["status"] == RecommendationStatus.RECOMMENDED
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_conflicting_component_evidence_fails_closed_to_unknown() -> None:
    report = build_discovery_report(
        (
            observation("a", "router", DiscoveryState.OBSERVED_WORKING),
            observation("b", "router", DiscoveryState.DEGRADED),
        )
    ).to_dict()

    local = next(item for item in report["recommendations"] if item["profile"] == "local/private")
    assert local["status"] == "unknown"
    assert "conflicting" in local["reason"].lower()
    assert local["questions"] == ["Resolve conflicting evidence for: router."]


def test_stale_evidence_does_not_authorize_recommendation() -> None:
    report = build_discovery_report(
        (
            observation(
                "stale-local",
                "ollama",
                DiscoveryState.OBSERVED_WORKING,
                capabilities=frozenset({"local"}),
                freshness="stale",
            ),
        )
    ).to_dict()

    local = next(item for item in report["recommendations"] if item["profile"] == "local/private")
    assert local["status"] == "unknown"
    assert local["evidence_ids"] == []


@pytest.mark.parametrize("value", ["sk-live-secret", "/home/alice/.config/tool", "api_key=secret"])
def test_secret_material_and_private_paths_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        EnvironmentObservation(
            evidence_id=value,
            component="fixture",
            state=DiscoveryState.CONFIGURED,
            source="fixture",
            confidence=0.5,
        )


def test_duplicate_evidence_ids_are_rejected() -> None:
    item = observation("duplicate", "one", DiscoveryState.INSTALLED)
    with pytest.raises(ValueError, match="unique"):
        build_discovery_report((item, item))
