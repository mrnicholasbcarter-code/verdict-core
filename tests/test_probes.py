import time
import urllib.error
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from verdict.availability import (
    AvailabilityState,
    CandidateRequirements,
    normalize_observation,
    select_capable_candidates,
)
from verdict.models import ModelInfo
from verdict.probes import (
    PROBE_PROMPT,
    ProbeObservation,
    ProbePolicy,
    ProbeRunner,
    openai_probe_transport,
)

MODEL = ModelInfo(id="runtime/model", provider="runtime", model="model", capability_tier=2)


def ok_transport(calls):
    def transport(model_id, payload, timeout):
        calls.append((model_id, payload, timeout))
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        }

    return transport


def assert_probe_excluded(result: ProbeObservation, expected: AvailabilityState) -> None:
    state = normalize_observation(MODEL, result.as_runtime_observation(), now=result.observed_at)

    assert state.state is expected
    assert select_capable_candidates([state], CandidateRequirements(allow_degraded=True)) == []


def assert_serialized_probe_state(result: ProbeObservation, expected: AvailabilityState) -> None:
    serialized = asdict(result.as_runtime_observation())
    state = normalize_observation(MODEL, serialized, now=result.observed_at)  # type: ignore[arg-type]

    assert state.state is expected


def test_one_token_payload_and_usage_marks_ready():
    calls = []
    result = ProbeRunner(ProbePolicy(max_models_per_run=1)).run(
        ["runtime/model"], ok_transport(calls)
    )
    assert result[0].availability_state == "ready"
    assert result[0].usage_available is True
    assert result[0].completion_tokens == 1
    assert calls[0][1]["messages"][0]["content"] == PROBE_PROMPT
    assert calls[0][1]["max_tokens"] == 1
    assert calls[0][1]["tools"] == []
    state = normalize_observation(
        MODEL, result[0].as_runtime_observation(), now=result[0].observed_at
    )
    assert state.state is AvailabilityState.ELIGIBLE


def test_bound_is_enforced_and_ids_remain_opaque():
    calls = []
    result = ProbeRunner(ProbePolicy(max_models_per_run=2)).run(
        ["catalog-id-1", "opaque/value.2", "third-runtime-id"], ok_transport(calls)
    )
    assert [item.model_id for item in result] == ["catalog-id-1", "opaque/value.2"]
    assert len(calls) == 2


def test_zero_usage_is_not_ready():
    def zero_usage(model_id, payload, timeout):
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            },
        }

    result = ProbeRunner().run(["runtime/model"], zero_usage)[0]

    assert result.availability_state == "degraded"
    assert result.usage_available is False
    assert_serialized_probe_state(result, AvailabilityState.DEGRADED)
    state = normalize_observation(MODEL, result.as_runtime_observation(), now=result.observed_at)
    assert select_capable_candidates([state], CandidateRequirements(allow_degraded=True)) == [state]


def test_empty_completion_is_not_ready():
    def empty_completion(model_id, payload, timeout):
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": ""}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        }

    result = ProbeRunner().run(["runtime/model"], empty_completion)[0]

    assert result.availability_state == "degraded"
    assert result.status == "completion_unavailable"


def test_quota_exhaustion_is_classified_separately():
    def quota_exhausted(model_id, payload, timeout):
        return {"status_code": 402, "body": {}}

    result = ProbeRunner().run(["runtime/model"], quota_exhausted)[0]

    assert result.availability_state == "degraded"
    assert result.error_class == "quota_exhausted"


def test_non_assistant_message_is_not_ready():
    def wrong_role(model_id, payload, timeout):
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "user", "content": "OK"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        }

    result = ProbeRunner().run(["runtime/model"], wrong_role)[0]

    assert result.availability_state == "degraded"
    assert result.status == "completion_unavailable"


def test_malformed_http_status_is_not_ready():
    def malformed_status(model_id, payload, timeout):
        return {
            "status_code": "200",
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        }

    result = ProbeRunner().run(["runtime/model"], malformed_status)[0]

    assert result.availability_state == "degraded"
    assert result.error_class == "malformed_response"


def test_openai_transport_preserves_http_error_classification():
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", hdrs=None, fp=None)

    transport = openai_probe_transport("http://127.0.0.1:20128/v1", opener=opener)

    with pytest.raises(urllib.error.HTTPError):
        transport("runtime/model", ProbePolicy().payload("runtime/model"), 0.1)


def test_runner_records_http_error_status_from_openai_transport():
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", hdrs=None, fp=None)

    transport = openai_probe_transport("http://127.0.0.1:20128/v1", opener=opener)
    result = ProbeRunner().run(["runtime/model"], transport)[0]

    assert result.availability_state == "degraded"
    assert result.error_class == "rate_limited"
    assert result.http_status == 429
    assert_probe_excluded(result, AvailabilityState.RATE_LIMITED)
    assert_serialized_probe_state(result, AvailabilityState.RATE_LIMITED)


def test_runner_round_trip_preserves_upstream_unavailability() -> None:
    def unavailable(model_id, payload, timeout):
        return {"status_code": 503, "body": {}}

    result = ProbeRunner().run(["runtime/model"], unavailable)[0]

    assert result.error_class == "upstream_error"
    assert result.http_status == 503
    assert_probe_excluded(result, AvailabilityState.UNAVAILABLE)
    assert_serialized_probe_state(result, AvailabilityState.UNAVAILABLE)


def test_cooldown_skips_without_transport():
    calls = []
    runner = ProbeRunner(ProbePolicy(cooldown_seconds=60))
    runner.run(["runtime/model"], ok_transport(calls))
    second = runner.run(["runtime/model"], ok_transport(calls))
    assert second[0].status == "skipped"
    assert second[0].error == "cooldown"
    assert len(calls) == 1
    assert_probe_excluded(second[0], AvailabilityState.RATE_LIMITED)


def test_repeated_failures_quarantine_and_redact():
    def failing(model_id, payload, timeout):
        raise RuntimeError("Bearer secret-value https://example.test/path?token=secret")

    runner = ProbeRunner(
        ProbePolicy(cooldown_seconds=0, failure_threshold=2, quarantine_seconds=60)
    )
    first = runner.run(["runtime/model"], failing)[0]
    second = runner.run(["runtime/model"], failing)[0]
    third = runner.run(["runtime/model"], failing)[0]
    assert first.availability_state == "degraded"
    assert second.availability_state == "denied"
    assert third.status == "skipped"
    assert "secret-value" not in (second.error or "")
    assert "token=secret" not in (second.error or "")
    assert "REDACTED" in (second.error or "")
    assert_probe_excluded(third, AvailabilityState.DENIED)


def test_timeout_and_usage_unavailable_are_not_ready():
    def slow(model_id, payload, timeout):
        time.sleep(0.2)
        return {"status_code": 200, "body": {}}

    runner = ProbeRunner(ProbePolicy(timeout_seconds=0.03, cooldown_seconds=0))
    result = runner.run(["runtime/model"], slow)[0]
    assert result.status == "timeout"
    assert result.availability_state == "degraded"
    assert_probe_excluded(result, AvailabilityState.TIMEOUT)


def test_timeout_that_triggers_quarantine_round_trips_as_denied() -> None:
    def slow(model_id, payload, timeout):
        time.sleep(0.2)
        return {"status_code": 200, "body": {}}

    result = ProbeRunner(
        ProbePolicy(
            timeout_seconds=0.03, cooldown_seconds=0, failure_threshold=1, quarantine_seconds=60
        )
    ).run(["runtime/model"], slow)[0]

    assert result.status == "timeout"
    assert result.availability_state == "denied"
    assert_probe_excluded(result, AvailabilityState.DENIED)
    assert_serialized_probe_state(result, AvailabilityState.DENIED)


def test_runtime_observation_mapping_is_structured():
    calls = []
    result = ProbeRunner().run(["runtime/model"], ok_transport(calls))[0]
    observation = result.as_runtime_observation()
    assert observation.source == "verdict:probe"
    assert observation.raw["usage_available"] is True
    assert observation.observed_at.tzinfo == timezone.utc


@pytest.mark.parametrize(
    ("availability_state", "status", "error_class", "expected"),
    [
        ("ready", "ready", None, AvailabilityState.ELIGIBLE),
        ("degraded", "usage_unavailable", None, AvailabilityState.DEGRADED),
        ("degraded", "failed", "unauthorized", AvailabilityState.UNAUTHORIZED),
        ("degraded", "failed", "quota_exhausted", AvailabilityState.QUOTA_EXHAUSTED),
        ("degraded", "failed", "rate_limited", AvailabilityState.RATE_LIMITED),
        ("degraded", "timeout", "timeout", AvailabilityState.TIMEOUT),
        ("degraded", "failed", "malformed_response", AvailabilityState.MALFORMED),
    ],
)
def test_probe_round_trip_preserves_truthful_availability_state(
    availability_state, status, error_class, expected
) -> None:
    observed_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    probe = ProbeObservation(
        model_id="runtime/model",
        availability_state=availability_state,
        status=status,
        observed_at=observed_at,
        error_class=error_class,
        error="safe operational detail" if status != "ready" else None,
        usage_available=status in {"ready", "completion_unavailable"},
        http_status=(
            200 if status in {"ready", "usage_unavailable", "completion_unavailable"} else None
        ),
    )

    state = normalize_observation(
        ModelInfo(id="runtime/model", provider="runtime", model="model", capability_tier=2),
        probe.as_runtime_observation(),
        now=observed_at,
    )

    assert state.state is expected


def test_injected_observation_time_controls_next_probe_time():
    observed_at = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    result = ProbeRunner(ProbePolicy(cooldown_seconds=30)).run(
        ["runtime/model"], ok_transport([]), now=observed_at
    )[0]

    assert result.next_probe_at is not None
    assert (result.next_probe_at - observed_at).total_seconds() == 30
