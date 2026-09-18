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
