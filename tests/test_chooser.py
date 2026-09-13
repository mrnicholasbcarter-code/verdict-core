"""BOD-95 proof cases for the evidence-based model chooser."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from verdict.autodev_routing import CandidateEvidence
from verdict.availability import AvailabilityState
from verdict.chooser import ChooserError, choose_route
from verdict.gateway_adapters import AdapterRouteIdentity

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _route(
    model_id: str,
    *,
    gateway: str = "gw-a",
    provider: str = "provider-a",
    route_id: str | None = None,
) -> AdapterRouteIdentity:
    return AdapterRouteIdentity(
        gateway_id=gateway,
        route_id=route_id or f"route-{model_id}",
        provider=provider,
        model_id=model_id,
        protocol="openai.chat",
    )


def _candidate(
    model_id: str,
    *,
    resource_class: str,
    availability: AvailabilityState = AvailabilityState.ELIGIBLE,
    capabilities: dict[str, str] | None = None,
    gateway: str = "gw-a",
    provider: str = "provider-a",
    route_id: str | None = None,
    alias: str | None = None,
    quota: float | None = None,
    headroom: float | None = None,
    freshness: float | None = None,
) -> CandidateEvidence:
    caps = {"resource_class": resource_class, "tools": "observed", "code": "observed"}
    if capabilities:
        caps.update(capabilities)
    return CandidateEvidence(
        requested_alias=alias or model_id,
        route=_route(model_id, gateway=gateway, provider=provider, route_id=route_id),
        availability=availability,
        capabilities=caps,
        observed_at=NOW,
        ttl_seconds=60,
        source="fixture",
        freshness_seconds=freshness,
        quota_remaining_pct=quota,
        headroom_pct=headroom,
    )


def test_ordinary_implementation_prefers_free_over_premium() -> None:
    receipt = choose_route(
        (
            _candidate("premium/model", resource_class="subscription_premium", headroom=90.0),
            _candidate("free/model", resource_class="free", headroom=50.0),
        ),
        task_class="implementation",
    )
    assert receipt.selected is not None
    assert receipt.selected["model"] == "free/model"
    assert receipt.selected["resource_pool"] == "free"
    assert "free" in receipt.selected_because


def test_free_capability_mismatch_falls_through_to_subscription_worker() -> None:
    receipt = choose_route(
        (
            _candidate(
                "free/model",
                resource_class="free",
                capabilities={"tools": "unknown", "code": "unknown"},
            ),
            _candidate("worker/model", resource_class="subscription_worker"),
            _candidate("premium/model", resource_class="subscription_premium"),
        ),
        task_class="implementation",
        requires=("tools", "code"),
    )
    assert receipt.selected is not None
    assert receipt.selected["model"] == "worker/model"
    assert receipt.selected["resource_pool"] == "subscription_worker"
    assert any(item["reason"].startswith("capability_mismatch") for item in receipt.exclusions)


def test_quota_exhausted_free_is_excluded_before_ranking() -> None:
    receipt = choose_route(
        (
            _candidate(
                "free/model", resource_class="free", availability=AvailabilityState.QUOTA_EXHAUSTED
            ),
            _candidate("worker/model", resource_class="subscription_worker"),
        ),
        task_class="implementation",
    )
    assert receipt.selected is not None
    assert receipt.selected["model"] == "worker/model"
    assert any("quota_exhausted" in item["reason"] for item in receipt.exclusions)


def test_protected_architecture_selects_premium() -> None:
    receipt = choose_route(
        (
            _candidate("free/model", resource_class="free", headroom=99.0),
            _candidate("premium/model", resource_class="subscription_premium", headroom=10.0),
        ),
        task_class="architecture",
    )
    assert receipt.selected is not None
    assert receipt.selected["model"] == "premium/model"
    assert receipt.protected is True
    assert "premium" in receipt.selected_because


def test_explicit_eligible_model_wins() -> None:
    receipt = choose_route(
        (
            _candidate("free/model", resource_class="free", headroom=99.0),
            _candidate("worker/model", resource_class="subscription_worker"),
        ),
        task_class="implementation",
        explicit_model="worker/model",
    )
    assert receipt.selected is not None
    assert receipt.selected["model"] == "worker/model"
    assert "explicit" in receipt.selected_because


def test_explicit_ineligible_model_fails_closed() -> None:
    with pytest.raises(ChooserError, match="ineligible") as exc_info:
        choose_route(
            (
                _candidate(
                    "worker/model",
                    resource_class="subscription_worker",
                    availability=AvailabilityState.QUOTA_EXHAUSTED,
                ),
                _candidate("free/model", resource_class="free"),
            ),
            task_class="implementation",
            explicit_model="worker/model",
        )
    assert exc_info.value.reason == "explicit_model_ineligible"


def test_all_unavailable_returns_no_eligible_target() -> None:
    with pytest.raises(ChooserError) as exc_info:
        choose_route(
            (
                _candidate(
                    "free/model", resource_class="free", availability=AvailabilityState.UNAVAILABLE
                ),
                _candidate(
                    "premium/model",
                    resource_class="subscription_premium",
                    availability=AvailabilityState.DENIED,
                ),
            ),
            task_class="implementation",
        )
    assert exc_info.value.reason == "no_eligible_target"


def test_unknown_quota_remains_unknown_in_receipt() -> None:
    receipt = choose_route(
        (_candidate("free/model", resource_class="free", quota=None, headroom=None),),
        task_class="implementation",
    )
    assert "quota" in receipt.unknown_evidence_fields
    assert "headroom" in receipt.unknown_evidence_fields
    assert receipt.selected is not None


def test_two_gateways_same_model_id_remain_distinct() -> None:
    receipt = choose_route(
        (
            _candidate(
                "shared/model",
                resource_class="free",
                gateway="gw-a",
                provider="provider-a",
                route_id="route-a",
                alias="gw-a/shared/model",
                headroom=10.0,
            ),
            _candidate(
                "shared/model",
                resource_class="subscription_premium",
                gateway="gw-b",
                provider="provider-b",
                route_id="route-b",
                alias="gw-b/shared/model",
                headroom=90.0,
            ),
        ),
        task_class="implementation",
    )
    assert receipt.selected is not None
    assert receipt.selected["model"] == "shared/model"
    selected_key = (receipt.selected["gateway"], receipt.selected["route_id"])
    fallback_keys = {(item["gateway"], item["route_id"]) for item in receipt.ranked_fallbacks}
    # Native EligibilityGate still keys admission by model_id, so both candidates
    # are admitted together. The receipt must still keep gateway identities distinct.
    assert selected_key in {("gw-a", "route-a"), ("gw-b", "route-b")}
    assert fallback_keys | {selected_key} >= {("gw-a", "route-a"), ("gw-b", "route-b")}
    assert (
        receipt.selected["gateway"] != next(iter(fallback_keys), receipt.selected["gateway"])
        or len(fallback_keys) == 1
    )


def test_repeated_run_is_deterministic() -> None:
    candidates = (
        _candidate("premium/model", resource_class="subscription_premium", headroom=80.0),
        _candidate("free/model", resource_class="free", headroom=40.0),
        _candidate("worker/model", resource_class="subscription_worker", headroom=70.0),
    )
    first = choose_route(candidates, task_class="implementation")
    second = choose_route(candidates, task_class="implementation")
    assert first.to_dict() == second.to_dict()
    assert first.selected == second.selected
