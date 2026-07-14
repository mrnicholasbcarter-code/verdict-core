import pytest

from llm_gate.intelligence import IntelligenceService


def test_production_readiness_fails_without_backend() -> None:
    # Cold-start and adapter-failure tests prove deterministic safety behavior
    svc = IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={},
        profile="production",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        ruflo_command="nonexistent_ruflo",
    )
    report = svc.readiness()
    assert report.status == "not_ready"
    assert report.profile == "production"
    assert report.degraded_mode is True
    assert report.managed_backend_status == "unavailable"


def test_development_profile_readiness_succeeds_degraded() -> None:
    svc = IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        ruflo_command="nonexistent_ruflo",
    )
    report = svc.readiness()
    assert report.status == "ready"
    assert report.profile == "development"
    assert report.degraded_mode is True
    assert report.managed_backend_status == "unavailable"


def test_redaction_before_ruflo_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def mock_run(args: list[str], **kwargs: dict[str, object]) -> None:
        calls.append(args)
        # Raise OSError to simulate unavailable adapter after checking arguments
        raise OSError("Simulated adapter failure")

    import subprocess

    monkeypatch.setattr(subprocess, "run", mock_run)

    svc = IntelligenceService(
        primary_model="anthropic/claude-3-opus",
        providers={},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
    )
    decision = svc.route("Here is a task with a private_key=sk-1234567890", criticality="medium")

    # Must have fallen back efficiently
    assert decision.model == "anthropic/claude-3-opus"

    # Check that redaction occurred before passing to Ruflo (assuming the mock gets called)
    assert len(calls) > 0
    cmd_args = " ".join(calls[0])
    assert "sk-1234567890" not in cmd_args
