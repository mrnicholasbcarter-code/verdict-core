import asyncio
import subprocess
from datetime import datetime, timezone

import pytest

from verdict.intelligence import IntelligenceService


def _service(*, profile: str = "development") -> IntelligenceService:
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={},
        profile=profile,
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
    )


def test_production_readiness_succeeds_without_ruflo() -> None:
    report = _service(profile="production").readiness()
    assert report.status == "ready"
    assert report.production_ready is True
    assert report.profile == "production"
    assert report.degraded_mode is False
    assert report.managed_backend_status == "not_used"
    assert report.reason == "ready"
    assert report.adapter_versions == {}


def test_development_profile_readiness_succeeds_without_ruflo() -> None:
    report = _service(profile="development").readiness()
    assert report.status == "ready"
    assert report.production_ready is True
    assert report.profile == "development"
    assert report.degraded_mode is False
    assert report.managed_backend_status == "not_used"


def test_route_does_not_invoke_ruflo(monkeypatch) -> None:
    calls: list[list[str]] = []

    def mock_run(args: list[str], **kwargs: object) -> None:
        calls.append(list(args))
        raise OSError("Ruflo must not be invoked on the cheap path")

    monkeypatch.setattr(subprocess, "run", mock_run)

    decision = asyncio.run(
        _service().route("Here is a task with a private_key=sk-1234567890", criticality="medium")
    )

    assert decision.model == "anthropic/claude-3-opus"
    assert decision.degraded_mode is False
    assert decision.managed_backend_status == "not_used"
    assert all("ruflo" not in " ".join(str(part) for part in args) for args in calls)


def test_default_profile_is_development_and_fail_closed_does_not_depend_on_it(monkeypatch) -> None:
    """S2-F F3: DEFAULT_PROFILE stays "development"; no fail-closed guarantee hangs on it.

    - The API serve path forces execution-path authority in every profile.
    - Unverified candidates stay excluded in development unless the operator opts in.
    """
    from verdict.eligibility import EligibilityGate, allow_unverified_dev_from_env
    from verdict.intelligence import DEFAULT_PROFILE
    from verdict.models import ModelInfo
    from verdict.serve_path import CONTEXT_REQUIRE_AUTHORITY, serve_path_authority_required

    monkeypatch.delenv("VERDICT_REQUIRE_EXECUTION_PATH", raising=False)
    monkeypatch.delenv("VERDICT_ALLOW_UNVERIFIED_DEV", raising=False)
    assert DEFAULT_PROFILE == "development"
    assert serve_path_authority_required(profile=DEFAULT_PROFILE) is False
    assert serve_path_authority_required(profile="production") is True
    # api._route_with_intelligence sets this key for every API request.
    assert (
        serve_path_authority_required(
            profile=DEFAULT_PROFILE, context={CONTEXT_REQUIRE_AUTHORITY: True}
        )
        is True
    )

    gate = EligibilityGate(
        lambda _mid: None,
        protected_fail_closed=True,
        allow_unverified_in_dev=allow_unverified_dev_from_env(),
    )
    result = gate.evaluate(
        [ModelInfo(id="d/4", provider="d", capability_tier=2)],
        dev_mode=(DEFAULT_PROFILE == "development"),
    )
    assert result.admitted == []


# --- Execution-path route clock (route-execution-path-aware-clock) ---------------
# These drive the real IntelligenceService down the serve branch: no fixture
# admit snapshot, a configured OmniRoute endpoint, build_selector, the real
# eligibility ladder and the real health cache. Only external reads are faked
# (tests/helpers/live_execution_path.py).


def _route_live(svc: IntelligenceService):
    import verdict.api as api
    from tests.helpers.live_execution_path import public_request_body

    body = public_request_body()
    context = api._authority_context(dict(body), task=body["task"])
    return asyncio.run(svc.route(body["task"], criticality="low", context=context))


def test_live_execution_path_route_without_admit_now_reaches_a_decision(
    monkeypatch, tmp_path, no_gateway_network
) -> None:
    """Catches the live 500: admit_now=None reached HealthCache.lookup as ``now``.

    On the unfixed service this raises HealthCacheError("now must be a
    timezone-aware datetime") from selector._assess_all.
    """
    from tests.helpers.live_execution_path import ROUTE, install_live_fakes, live_service

    install_live_fakes(monkeypatch, tmp_path)
    decision = _route_live(live_service(admit_now=None))

    assert decision.decision == "selected"
    assert f"{decision.provider}/{decision.model}" == ROUTE


def test_live_execution_path_route_reads_one_instant_for_every_health_check(
    monkeypatch, tmp_path, no_gateway_network
) -> None:
    """Catches a second clock: every health-cache time check in one request sees one instant.

    Spies on the ladder's HealthCache.lookup (selector._assess_all) and on the
    candidate-pool billing check in _route_capacity. A call site that reads
    datetime.now() for itself shows up as a second, later instant.
    """
    import verdict.intelligence as intelligence
    from tests.helpers.live_execution_path import install_live_fakes, live_service
    from verdict.orchestration.health_cache import HealthCache

    install_live_fakes(monkeypatch, tmp_path)
    seen: dict[str, list[datetime]] = {"lookup": [], "billing": []}
    real_lookup = HealthCache.lookup
    real_billing = intelligence.billing_capacity_override

    def lookup(self, route, now):
        seen["lookup"].append(now)
        return real_lookup(self, route, now)

    def billing(cache, route_id, now):
        seen["billing"].append(now)
        return real_billing(cache, route_id, now)

    monkeypatch.setattr(HealthCache, "lookup", lookup)
    monkeypatch.setattr(intelligence, "billing_capacity_override", billing)

    assert _route_live(live_service(admit_now=None)).decision == "selected"

    assert seen["lookup"], "the ladder never read the health cache"
    assert seen["billing"], "_route_capacity never ran"
    instants = {*seen["lookup"], *seen["billing"]}
    assert len(instants) == 1, f"one request read {len(instants)} clocks: {sorted(instants)}"
    (instant,) = instants
    assert instant.tzinfo is not None


def test_live_execution_path_route_uses_injected_admit_now(
    monkeypatch, tmp_path, no_gateway_network
) -> None:
    """Catches a call site that ignores the injected clock on the serve branch."""
    from datetime import timedelta

    from tests.helpers.live_execution_path import install_live_fakes, live_service
    from verdict.orchestration.health_cache import HealthCache

    install_live_fakes(monkeypatch, tmp_path)
    injected = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=5)
    seen: list[datetime] = []
    real_lookup = HealthCache.lookup

    def lookup(self, route, now):
        seen.append(now)
        return real_lookup(self, route, now)

    monkeypatch.setattr(HealthCache, "lookup", lookup)

    assert _route_live(live_service(admit_now=injected)).decision == "selected"
    assert seen and set(seen) == {injected}


@pytest.mark.parametrize("assign", [False, True])
def test_naive_admit_now_is_rejected(assign: bool) -> None:
    """Catches a naive injected clock that would fail later, inside a request, as a 500."""
    naive = datetime(2026, 11, 17, 12, 0)
    if assign:
        svc = _service()
        with pytest.raises(ValueError, match="timezone-aware"):
            svc.admit_now = naive
        assert svc.admit_now is None
        return
    with pytest.raises(ValueError, match="timezone-aware"):
        IntelligenceService(
            primary_model="anthropic/claude-3-opus",
            providers={},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            admit_now=naive,
        )
