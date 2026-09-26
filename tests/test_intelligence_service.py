import asyncio
import subprocess

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
